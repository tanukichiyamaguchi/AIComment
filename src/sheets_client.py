"""Google Sheets APIクライアント。読み書きとステータス管理。"""

import json
import logging
from dataclasses import dataclass

from googleapiclient.discovery import build

from src.config import SPREADSHEET_ID, GOOGLE_CREDENTIALS_JSON, GOOGLE_SCOPES

logger = logging.getLogger("jissen_comment")


@dataclass
class ClinicRecord:
    """スプレッドシートの1行分のデータ。"""
    row_number: int  # 1-indexed (ヘッダー除く)
    clinic_name: str  # A列: 医院名
    person_name: str  # B列: 氏名
    email: str  # C列: メールアドレス
    status: str  # D列: ステータス


def _get_credentials():
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

    raise RuntimeError("Google認証情報が見つかりません")


def get_sheets_service():
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

        records.append(ClinicRecord(
            row_number=i,
            clinic_name=clinic_name.strip(),
            person_name=person_name.strip(),
            email=email.strip(),
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
