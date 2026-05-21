"""Google Sheets APIクライアント。読み書きとステータス管理。"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from googleapiclient.discovery import build

from src.config import (
    GOOGLE_CREDENTIALS_JSON,
    GOOGLE_SCOPES,
    OUTPUT_SHEET_NAME,
    SPREADSHEET_ID,
)

logger = logging.getLogger("jissen_comment")


@dataclass
class ClinicRecord:
    """スプレッドシートの1行分のデータ。"""
    row_number: int  # 1-indexed (ヘッダー除く)
    clinic_name: str  # A列: 医院名
    person_name: str  # B列: 氏名
    email: str  # C列: メールアドレス
    status: str  # D列: ステータス


def _get_credentials() -> Any:
    """環境に応じた認証情報を取得する。"""
    try:
        import google.colab.auth  # type: ignore
        google.colab.auth.authenticate_user()
        from google.auth import default
        creds, _ = default(scopes=GOOGLE_SCOPES)
        return creds
    except ImportError:
        pass

    if GOOGLE_CREDENTIALS_JSON:
        from google.oauth2 import service_account
        info = json.loads(GOOGLE_CREDENTIALS_JSON)
        return service_account.Credentials.from_service_account_info(
            info, scopes=GOOGLE_SCOPES
        )

    # ADC（Workload Identity 連携 / gcloud auth）
    try:
        from google.auth import default
        creds, _ = default(scopes=GOOGLE_SCOPES)
        return creds
    except Exception:
        pass

    raise RuntimeError("Google認証情報が見つかりません")


def get_sheets_service() -> Any:
    """Sheets APIサービスを構築する。"""
    creds = _get_credentials()
    return build("sheets", "v4", credentials=creds)


def read_records(
    spreadsheet_id: str | None = None,
    sheet_name: str = "Sheet1",
) -> list[ClinicRecord]:
    """スプレッドシートから全レコードを読み取る。

    Args:
        spreadsheet_id: スプレッドシートID
        sheet_name: シート名

    Returns:
        ClinicRecordのリスト
    """
    spreadsheet_id = spreadsheet_id or SPREADSHEET_ID
    if not spreadsheet_id:
        raise ValueError("SPREADSHEET_IDが設定されていません")

    service = get_sheets_service()
    range_name = f"{sheet_name}!A:D"

    result = service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=range_name,
    ).execute()

    rows = result.get("values", [])
    if not rows:
        logger.warning("スプレッドシートにデータがありません")
        return []

    # ヘッダー行をスキップ
    records = []
    for i, row in enumerate(rows[1:], start=2):
        # 列が足りない場合は空文字で補完
        while len(row) < 4:
            row.append("")

        clinic_name, person_name, email, status = row[0], row[1], row[2], row[3]

        if not clinic_name or not person_name:
            continue

        email_clean = email.strip()
        if email_clean and not _validate_email(email_clean):
            logger.warning(f"Sheets: 行{i} メールアドレスの形式が不正のためスキップ")
            email_clean = ""

        records.append(ClinicRecord(
            row_number=i,
            clinic_name=clinic_name.strip(),
            person_name=person_name.strip(),
            email=email_clean,
            status=status.strip(),
        ))

    logger.info(f"Sheets: {len(records)}件のレコードを取得")
    return records


def get_unprocessed_records(
    spreadsheet_id: str | None = None,
    sheet_name: str = "Sheet1",
) -> list[ClinicRecord]:
    """ステータスが「未処理」または空のレコードのみ取得する。"""
    records = read_records(spreadsheet_id, sheet_name)
    unprocessed = [r for r in records if r.status in ("", "未処理")]
    logger.info(f"Sheets: 未処理レコード {len(unprocessed)}件 / 全{len(records)}件")
    return unprocessed


def update_status(
    row_number: int,
    status: str,
    spreadsheet_id: str | None = None,
    sheet_name: str = "Sheet1",
) -> None:
    """指定行のステータス（D列）を更新する。

    Args:
        row_number: 行番号（1-indexed、ヘッダー含む）
        status: 新しいステータス文字列
        spreadsheet_id: スプレッドシートID
        sheet_name: シート名
    """
    spreadsheet_id = spreadsheet_id or SPREADSHEET_ID
    if not spreadsheet_id:
        raise ValueError("SPREADSHEET_IDが設定されていません")

    service = get_sheets_service()
    range_name = f"{sheet_name}!D{row_number}"

    service.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=range_name,
        valueInputOption="RAW",
        body={"values": [[status]]},
    ).execute()

    logger.info(f"Sheets: 行{row_number}のステータスを「{status}」に更新")


def _validate_email(email: str) -> bool:
    """簡易メールアドレスバリデーション。"""
    import re
    pattern = r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"
    return bool(re.match(pattern, email))


_OUTPUT_HEADER = ["管理番号", "医院名", "個人名", "実践事例名", "Drive URL", "処理日時"]


def _ensure_output_sheet(
    service: Any,
    spreadsheet_id: str,
    sheet_name: str,
) -> None:
    """出力一覧シートが無ければ作成し、ヘッダー行を書き込む。"""
    meta = service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    existing_titles = [s["properties"]["title"] for s in meta.get("sheets", [])]

    if sheet_name not in existing_titles:
        service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={
                "requests": [
                    {"addSheet": {"properties": {"title": sheet_name}}}
                ]
            },
        ).execute()
        logger.info(f"Sheets: 出力一覧シートを新規作成 ({sheet_name})")

    header_range = f"{sheet_name}!A1:F1"
    current = service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=header_range,
    ).execute()
    if not current.get("values"):
        service.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=header_range,
            valueInputOption="RAW",
            body={"values": [_OUTPUT_HEADER]},
        ).execute()
        logger.info(f"Sheets: 出力一覧シートのヘッダーを書き込み ({sheet_name})")


def append_output_record(
    management_number: str,
    clinic_name: str,
    person_name: str,
    sample_name: str,
    drive_url: str,
    spreadsheet_id: str | None = None,
    sheet_name: str | None = None,
    processed_at: str | None = None,
) -> None:
    """出力一覧シートに1行追加する（管理番号 / 医院名 / 個人名 / 実践事例名 / Drive URL / 処理日時）。

    Args:
        management_number: 管理番号（PDF ファイル名先頭から抽出済み。
            抽出できなかった場合は空文字列）
        clinic_name: 医院名
        person_name: 個人名
        sample_name: 実践事例名（元PDFファイル名）
        drive_url: アップロードしたPDFのDrive閲覧URL
        spreadsheet_id: スプレッドシートID（省略時は設定値）
        sheet_name: シート名（省略時は設定値 ``OUTPUT_SHEET_NAME``）
        processed_at: 処理日時文字列（省略時は現在時刻 ISO形式）
    """
    spreadsheet_id = spreadsheet_id or SPREADSHEET_ID
    if not spreadsheet_id:
        raise ValueError("SPREADSHEET_IDが設定されていません")
    sheet_name = sheet_name or OUTPUT_SHEET_NAME
    processed_at = processed_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    service = get_sheets_service()
    _ensure_output_sheet(service, spreadsheet_id, sheet_name)

    service.spreadsheets().values().append(
        spreadsheetId=spreadsheet_id,
        range=f"{sheet_name}!A:F",
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={
            "values": [
                [
                    management_number,
                    clinic_name,
                    person_name,
                    sample_name,
                    drive_url,
                    processed_at,
                ]
            ]
        },
    ).execute()

    logger.info(
        f"Sheets: 出力一覧に追加 ({management_number} / {clinic_name} / {person_name} / {sample_name})"
    )
