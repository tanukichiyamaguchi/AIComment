"""Google Sheets APIクライアント。読み書きとステータス管理。"""

from __future__ import annotations

import collections
import json
import logging
import re
import threading
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from googleapiclient.discovery import build

from src.config import (
    GOOGLE_API_NUM_RETRIES,
    GOOGLE_CREDENTIALS_JSON,
    GOOGLE_SCOPES,
    MASTER_SHEET_NAME,
    OUTPUT_SHEET_NAME,
    SPREADSHEET_ID,
)

logger = logging.getLogger("jissen_comment")


# Sheets API write throttle（P-023）。
# Google Sheets API は **1 ユーザーあたり 60 write requests/min** が
# ハード上限。1000 PDF 規模の連続実行で append_output_record / append_clinic_*
# が短時間に集中して 429 quota_exceeded を踏むと、batch_main の Step4 が
# 途中失敗 → 半端に処理済み行が残るリスクがある。安全マージンを取って
# 50 writes / 60 sec に抑える（理論最大の 83%、本番運用で観測した quota も
# このあたりが安定する）。
_SHEETS_MAX_WRITES_PER_60S = 50
_SHEETS_WRITE_TIMES: collections.deque[float] = collections.deque()
_SHEETS_WRITE_LOCK = threading.Lock()


def _throttle_sheets_write() -> None:
    """Sheets write 直前に呼び、過去 60 秒の write 数が閾値超なら sleep する。

    Quota 超過 (429) を能動的に防ぐ。GOOGLE_API_NUM_RETRIES のリトライは
    一過性エラー対策であり、quota の自衛策ではないため別途必要（P-017 補強）。
    """
    with _SHEETS_WRITE_LOCK:
        now = time.monotonic()
        # 60 秒経過した古い記録を捨てる
        while _SHEETS_WRITE_TIMES and now - _SHEETS_WRITE_TIMES[0] >= 60:
            _SHEETS_WRITE_TIMES.popleft()
        if len(_SHEETS_WRITE_TIMES) >= _SHEETS_MAX_WRITES_PER_60S:
            sleep_for = 60.0 - (now - _SHEETS_WRITE_TIMES[0]) + 0.5
            if sleep_for > 0:
                logger.info(
                    f"Sheets quota throttle: {sleep_for:.1f}秒 sleep "
                    f"(直近60秒 {len(_SHEETS_WRITE_TIMES)} writes)"
                )
                time.sleep(sleep_for)
                # sleep 後は最古を 1 つ捨てる
                if _SHEETS_WRITE_TIMES:
                    _SHEETS_WRITE_TIMES.popleft()
        _SHEETS_WRITE_TIMES.append(time.monotonic())


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
    ).execute(num_retries=GOOGLE_API_NUM_RETRIES)

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
    ).execute(num_retries=GOOGLE_API_NUM_RETRIES)

    logger.info(f"Sheets: 行{row_number}のステータスを「{status}」に更新")


def _validate_email(email: str) -> bool:
    """簡易メールアドレスバリデーション。"""
    pattern = r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"
    return bool(re.match(pattern, email))


_OUTPUT_HEADER = ["管理番号", "医院名", "個人名", "実践事例名", "Drive URL", "処理日時"]

# 医院フォルダURLシートのヘッダー（3列）。出力一覧シート（6列）とは別タブで、
# 医院ごとに 1 行、医院フォルダの Drive URL を記録する。
_CLINIC_SHEET_HEADER = ["医院番号", "医院名", "医院フォルダURL"]


def _ensure_sheet_with_header(
    service: Any,
    spreadsheet_id: str,
    sheet_name: str,
    header: list[str],
) -> bool:
    """指定シートが無ければ作成し、ヘッダー行が空なら書き込む（汎用ヘルパー）。

    出力一覧シート（6列）と医院フォルダURLシート（3列）と参加者マスター
    シート（5列）の 3 種類で使う。ヘッダーの列数は ``header`` の長さから動的に
    決める（A1 から N 列ぶん）。

    Args:
        service: Sheets API サービス
        spreadsheet_id: スプレッドシートID
        sheet_name: シート名（タブ名）
        header: ヘッダー行（列数はこのリスト長から決まる）

    Returns:
        本呼び出しでタブを **新規作成** したなら True、既存タブを再利用したなら
        False。呼び出し側が「初回作成かどうか」で警告レベルを切り替えるために使う
        （参加者マスターのサイレント空読み事故対策、P-022）。
    """
    meta = service.spreadsheets().get(
        spreadsheetId=spreadsheet_id
    ).execute(num_retries=GOOGLE_API_NUM_RETRIES)
    existing_titles = [s["properties"]["title"] for s in meta.get("sheets", [])]

    was_created = sheet_name not in existing_titles
    if was_created:
        service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={
                "requests": [
                    {"addSheet": {"properties": {"title": sheet_name}}}
                ]
            },
        ).execute(num_retries=GOOGLE_API_NUM_RETRIES)
        logger.info(f"Sheets: シートを新規作成 ({sheet_name})")

    # ヘッダー range は列数に合わせて A1:<最終列>1 を組み立てる。
    last_col = chr(ord("A") + len(header) - 1)
    header_range = f"{sheet_name}!A1:{last_col}1"
    current = service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=header_range,
    ).execute(num_retries=GOOGLE_API_NUM_RETRIES)
    if not current.get("values"):
        service.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=header_range,
            valueInputOption="RAW",
            body={"values": [header]},
        ).execute(num_retries=GOOGLE_API_NUM_RETRIES)
        logger.info(f"Sheets: シートのヘッダーを書き込み ({sheet_name})")

    return was_created


def _ensure_output_sheet(
    service: Any,
    spreadsheet_id: str,
    sheet_name: str,
) -> None:
    """出力一覧シート（6列）が無ければ作成し、ヘッダー行を書き込む。"""
    _ensure_sheet_with_header(
        service, spreadsheet_id, sheet_name, _OUTPUT_HEADER
    )


def _ensure_clinic_sheet(
    service: Any,
    spreadsheet_id: str,
    sheet_name: str,
) -> None:
    """医院フォルダURLシート（3列）が無ければ作成し、ヘッダー行を書き込む。"""
    _ensure_sheet_with_header(
        service, spreadsheet_id, sheet_name, _CLINIC_SHEET_HEADER
    )


def get_processed_management_numbers(
    spreadsheet_id: str | None = None,
    sheet_name: str | None = None,
) -> set[str]:
    """出力一覧シートの A列（管理番号）から、処理済み管理番号の集合を返す。

    重複検知（増分処理）に使う。入力フォルダに PDF を継続的に追加して
    ワークフローを再実行するとき、既に出力済みの管理番号を事前に把握し、
    download / Claude API 呼び出しの前にスキップ判定するためのキー集合。

    Args:
        spreadsheet_id: スプレッドシートID（省略時は設定値 ``SPREADSHEET_ID``）
        sheet_name: シート名（省略時は設定値 ``OUTPUT_SHEET_NAME``）

    Returns:
        処理済み管理番号の集合。空セル・ヘッダー行は除外する。
        シートが未作成（``_ensure_output_sheet`` 前）の場合や
        ``values`` キーが無い場合は空集合を返す。
    """
    spreadsheet_id = spreadsheet_id or SPREADSHEET_ID
    if not spreadsheet_id:
        raise ValueError("SPREADSHEET_IDが設定されていません")
    sheet_name = sheet_name or OUTPUT_SHEET_NAME

    service = get_sheets_service()

    # 出力一覧シートがまだ作成されていない（初回実行）場合、A2:A の取得は
    # 400 エラーになる。シート一覧を先に確認し、未作成なら空集合で返す。
    meta = service.spreadsheets().get(
        spreadsheetId=spreadsheet_id
    ).execute(num_retries=GOOGLE_API_NUM_RETRIES)
    existing_titles = [s["properties"]["title"] for s in meta.get("sheets", [])]
    if sheet_name not in existing_titles:
        logger.info(
            f"Sheets: 出力一覧シート未作成のため処理済み管理番号は0件 ({sheet_name})"
        )
        return set()

    result = service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=f"{sheet_name}!A2:A",
    ).execute(num_retries=GOOGLE_API_NUM_RETRIES)

    rows = result.get("values", [])
    processed = {
        row[0].strip()
        for row in rows
        if row and row[0] and row[0].strip()
    }
    logger.info(
        f"Sheets: 処理済み管理番号 {len(processed)}件を取得 ({sheet_name})"
    )
    return processed


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

    _throttle_sheets_write()
    service.spreadsheets().values().append(
        spreadsheetId=spreadsheet_id,
        range=f"{sheet_name}!A:F",
        # RAW: 各値を入力された文字列のまま格納する。USER_ENTERED だと
        # 処理日時 "2026-05-21 14:28:42" が日付シリアル値（46163.23... など）に
        # 変換され、列の書式次第で生の数値が表示されてしまう。
        valueInputOption="RAW",
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
    ).execute(num_retries=GOOGLE_API_NUM_RETRIES)

    logger.info(
        f"Sheets: 出力一覧に追加 ({management_number} / {clinic_name} / {person_name} / {sample_name})"
    )


def append_completion_marker(
    spreadsheet_id: str | None = None,
    sheet_name: str | None = None,
    completed_at: str | None = None,
    summary: str = "",
) -> None:
    """出力一覧シートの最終行に「完了」マーカー行を 1 行追加する。

    全 PDF 処理が終わったことを運用者がシート上で一目で把握できるようにする
    （ログを開かなくても完了が分かる）。出力一覧シートの末尾に
    A 列に ``"完了"``、B 列に処理日時、C 列に件数サマリーを書く。

    Args:
        spreadsheet_id: スプレッドシートID（省略時は設定値）
        sheet_name: シート名（省略時は ``OUTPUT_SHEET_NAME``）
        completed_at: 完了日時文字列（省略時は現在時刻）
        summary: 件数サマリー（例 ``"成功 42件 / エラー 0件"``）。空でもよい。
    """
    spreadsheet_id = spreadsheet_id or SPREADSHEET_ID
    if not spreadsheet_id:
        raise ValueError("SPREADSHEET_IDが設定されていません")
    sheet_name = sheet_name or OUTPUT_SHEET_NAME
    completed_at = completed_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    service = get_sheets_service()
    _ensure_output_sheet(service, spreadsheet_id, sheet_name)

    _throttle_sheets_write()
    service.spreadsheets().values().append(
        spreadsheetId=spreadsheet_id,
        range=f"{sheet_name}!A:F",
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": [["完了", completed_at, summary, "", "", ""]]},
    ).execute(num_retries=GOOGLE_API_NUM_RETRIES)
    logger.info(
        f"Sheets: 完了マーカーを追記 ({sheet_name}, {completed_at}, {summary})"
    )


def get_recorded_clinic_numbers(
    spreadsheet_id: str | None = None,
    sheet_name: str | None = None,
) -> set[str]:
    """医院フォルダURLシートの A列（医院番号）から記録済み医院番号の集合を返す。

    重複記録防止に使う。医院フォルダURLシートに既に行がある医院番号は、
    同じ実行内・後続実行で再度追記しないための事前スナップショット。

    Args:
        spreadsheet_id: スプレッドシートID（省略時は設定値 ``SPREADSHEET_ID``）
        sheet_name: 医院シート名（``<出力シート名>_医院``）。
            省略時は設定値 ``OUTPUT_SHEET_NAME``。

    Returns:
        記録済み医院番号の集合。空セル・ヘッダー行は除外する。
        シートが未作成（``_ensure_clinic_sheet`` 前）の場合や
        ``values`` キーが無い場合は空集合を返す。
    """
    spreadsheet_id = spreadsheet_id or SPREADSHEET_ID
    if not spreadsheet_id:
        raise ValueError("SPREADSHEET_IDが設定されていません")
    sheet_name = sheet_name or OUTPUT_SHEET_NAME

    service = get_sheets_service()

    # 医院シートがまだ作成されていない（初回実行）場合、A2:A の取得は
    # 400 エラーになる。シート一覧を先に確認し、未作成なら空集合で返す。
    meta = service.spreadsheets().get(
        spreadsheetId=spreadsheet_id
    ).execute(num_retries=GOOGLE_API_NUM_RETRIES)
    existing_titles = [s["properties"]["title"] for s in meta.get("sheets", [])]
    if sheet_name not in existing_titles:
        logger.info(
            f"Sheets: 医院フォルダURLシート未作成のため記録済み医院番号は0件 "
            f"({sheet_name})"
        )
        return set()

    result = service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=f"{sheet_name}!A2:A",
    ).execute(num_retries=GOOGLE_API_NUM_RETRIES)

    rows = result.get("values", [])
    recorded = {
        row[0].strip()
        for row in rows
        if row and row[0] and row[0].strip()
    }
    logger.info(
        f"Sheets: 記録済み医院番号 {len(recorded)}件を取得 ({sheet_name})"
    )
    return recorded


def append_clinic_folder_record(
    clinic_number: str,
    clinic_name: str,
    clinic_folder_url: str,
    spreadsheet_id: str | None = None,
    sheet_name: str | None = None,
) -> None:
    """医院フォルダURLシートに1行追加する（医院番号 / 医院名 / 医院フォルダURL）。

    シートが無ければヘッダー付きで自動作成する。重複記録の防止は呼び出し側の
    責務（``get_recorded_clinic_numbers`` で事前スナップショットを取る）。

    Args:
        clinic_number: 医院番号（管理番号の先頭セグメント、3〜5桁）
        clinic_name: 医院名（参加者マスターから引いた標準表記。空なら AI 抽出値）
        clinic_folder_url: 医院フォルダの Drive 閲覧 URL
        spreadsheet_id: スプレッドシートID（省略時は設定値）
        sheet_name: 医院シート名（``<出力シート名>_医院``）。
            省略時は設定値 ``OUTPUT_SHEET_NAME``。
    """
    spreadsheet_id = spreadsheet_id or SPREADSHEET_ID
    if not spreadsheet_id:
        raise ValueError("SPREADSHEET_IDが設定されていません")
    sheet_name = sheet_name or OUTPUT_SHEET_NAME

    service = get_sheets_service()
    _ensure_clinic_sheet(service, spreadsheet_id, sheet_name)

    _throttle_sheets_write()
    service.spreadsheets().values().append(
        spreadsheetId=spreadsheet_id,
        range=f"{sheet_name}!A:C",
        # RAW: 各値を入力された文字列のまま格納する（医院番号の先頭ゼロや
        # URL が数式・数値に解釈されないようにする）。
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={
            "values": [
                [clinic_number, clinic_name, clinic_folder_url]
            ]
        },
    ).execute(num_retries=GOOGLE_API_NUM_RETRIES)

    logger.info(
        f"Sheets: 医院フォルダURLシートに追加 "
        f"({clinic_number} / {clinic_name} / {clinic_folder_url})"
    )


# ── 参加者マスターシート（医院名標準化 + Gmail 下書き用ルックアップ表） ──


@dataclass
class MasterRecord:
    """参加者マスターシートの 1 行。

    1 行 per 個人。同じ医院（例 ``101``）には複数参加者がいる場合
    ``101-01``、``101-02`` … と並ぶ。医院名は重複記入されている前提。

    Attributes:
        management_number: A 列、管理番号（``101-01`` のような ``xxx-yy`` 形式）。
            先頭セグメント（``-`` の前）が医院コードで、PDF ファイル名から
            抽出した医院コードと突合する。
        clinic_name: B 列、医院名（標準表記。「開業準備中」等の固定文字列も入る）
        participant_name: C 列、参加者名（個人特定の突合キー）
        venue: D 列、申し込み会場（参照用）
        email: E 列、メールアドレス
    """
    management_number: str
    clinic_name: str
    participant_name: str
    venue: str
    email: str

    @property
    def clinic_number(self) -> str:
        """管理番号 ``101-01`` から医院コード ``101`` を派生させる。

        ``-`` を含まない管理番号は、そのまま医院コードとみなす（``101`` のみ
        が入っているケースをサポート）。空文字なら空文字を返す。
        """
        if not self.management_number:
            return ""
        return self.management_number.split("-", 1)[0]


_MASTER_SHEET_HEADER = [
    "管理番号", "医院名", "参加者名", "申し込み会場", "メールアドレス"
]


# 個人名の表記揺れ吸収用文字テーブル。NFKC で吸収しきれない、
# ユーザー入力でよく混ざるパターンを最低限カバーする。
# - 全角/半角スペース、タブ、ノーブレークスペースは全削除
# - カタカナはひらがなに統一（半角カナは NFKC で全角に変換済み）
_WHITESPACE_PATTERN = re.compile(r"[\s　 ]+")


def _katakana_to_hiragana(text: str) -> str:
    """全角カタカナを全角ひらがなに変換する。"""
    result = []
    for ch in text:
        code = ord(ch)
        if 0x30A1 <= code <= 0x30F6:  # ァ..ヶ
            result.append(chr(code - 0x60))
        else:
            result.append(ch)
    return "".join(result)


def _normalize_person_name(name: str) -> str:
    """個人名を突合用に強めに正規化する。

    手順:
        1. NFKC 正規化（全角/半角・互換文字を統一。半角カナ→全角カナも含む）
        2. 全種類の空白を削除（半角/全角/タブ/ノーブレーク）
        3. カタカナ → ひらがな統一（``ヤマダ`` と ``やまだ`` を一致扱い）
        4. 小文字化（ローマ字名の大文字小文字差を吸収）

    旧字体/新字体（``髙`` ↔ ``高`` 等）は変換しないため、マスター側で
    新字体に統一しておくのが前提。ファジーマッチで 1 文字差まで救う。

    Args:
        name: 比較対象の個人名

    Returns:
        正規化後の文字列。空入力なら空文字。
    """
    if not name:
        return ""
    normalized = unicodedata.normalize("NFKC", name)
    normalized = _WHITESPACE_PATTERN.sub("", normalized)
    normalized = _katakana_to_hiragana(normalized)
    return normalized.lower()


def _levenshtein_distance(a: str, a_other: str) -> int:
    """2 文字列のレーベンシュタイン距離（編集距離）を返す。

    Wagner-Fischer の動的計画法。文字列長が短い前提（個人名は通常 10 文字
    以下）なので素朴な O(len(a) * len(b)) 実装で十分。

    Args:
        a: 比較元
        a_other: 比較先

    Returns:
        編集距離（挿入/削除/置換 = それぞれコスト 1）。
    """
    if a == a_other:
        return 0
    if not a:
        return len(a_other)
    if not a_other:
        return len(a)

    previous = list(range(len(a_other) + 1))
    for i, ch_a in enumerate(a, start=1):
        current = [i] + [0] * len(a_other)
        for j, ch_b in enumerate(a_other, start=1):
            cost = 0 if ch_a == ch_b else 1
            current[j] = min(
                current[j - 1] + 1,       # 挿入
                previous[j] + 1,          # 削除
                previous[j - 1] + cost,   # 置換
            )
        previous = current
    return previous[-1]


def _ensure_master_sheet(
    service: Any,
    spreadsheet_id: str,
    sheet_name: str,
) -> bool:
    """参加者マスターシート（5列）が無ければ作成し、ヘッダー行を書き込む。

    本タブを「**今回の呼び出しで**」新規作成した場合は、運用者が見落とさない
    よう WARNING を出す。マスタータブが空のまま実行されると Gmail 下書きの TO が
    全件空になる事故（過去発生）の再発防止（P-022）。

    Returns:
        本呼び出しで新規作成したなら True。既存タブを再利用したなら False。
    """
    was_created = _ensure_sheet_with_header(
        service, spreadsheet_id, sheet_name, _MASTER_SHEET_HEADER
    )
    if was_created:
        logger.warning(
            f"参加者マスタータブ '{sheet_name}' をスプレッドシート "
            f"{spreadsheet_id} に新規作成しました（ヘッダーのみの空タブ）。"
            f"このまま PDF 処理を実行すると、参加者の lookup が全件失敗し、"
            f"Gmail 下書きの TO が全件空になります。"
            f"スプレッドシートのタブにデータを投入してから再実行してください。"
        )
    return was_created


def read_master_records(
    spreadsheet_id: str | None = None,
    sheet_name: str | None = None,
) -> list[MasterRecord]:
    """参加者マスターシートを全件読み取る。

    シートが存在しなければ自動作成（ヘッダーのみ書き込む）して空リストを返す。
    形式不正のメール (``_validate_email`` で False) は警告して空扱い、行自体は
    残す（医院名 lookup には使えるため）。医院管理番号も医院名も両方空の行は
    空行として読み飛ばす。

    Args:
        spreadsheet_id: スプレッドシート ID。省略時は ``config.SPREADSHEET_ID``。
        sheet_name: タブ名。省略時は ``MASTER_SHEET_NAME``。

    Returns:
        全行を ``MasterRecord`` のリストで返す。空行はスキップ。
    """
    spreadsheet_id = spreadsheet_id or SPREADSHEET_ID
    if not spreadsheet_id:
        raise ValueError("SPREADSHEET_IDが設定されていません")
    sheet_name = sheet_name or MASTER_SHEET_NAME

    service = get_sheets_service()
    was_created = _ensure_master_sheet(service, spreadsheet_id, sheet_name)

    result = service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=f"{sheet_name}!A:E",
    ).execute(num_retries=GOOGLE_API_NUM_RETRIES)

    rows = result.get("values", [])
    if not rows:
        # ヘッダーすら無い状態（API は完全空のシートでは values キーを返さない）。
        # 直前に作成したなら _ensure_master_sheet が WARNING 済みなので INFO で OK。
        logger.info(
            f"Sheets: 参加者マスターシートが空 (ヘッダーのみ): {sheet_name}"
        )
        return []

    records: list[MasterRecord] = []
    # ヘッダー行はスキップ（A1 はヘッダー）
    for i, row in enumerate(rows[1:], start=2):
        # 列が足りない場合は空文字で補完
        while len(row) < 5:
            row.append("")

        management_number = (row[0] or "").strip()
        clinic_name = (row[1] or "").strip()
        participant_name = (row[2] or "").strip()
        venue = (row[3] or "").strip()
        email = (row[4] or "").strip()

        # 管理番号も医院名も両方空の行は無視（空行 / コメント行扱い）
        if not management_number and not clinic_name:
            continue

        if email and not _validate_email(email):
            logger.warning(
                f"Sheets: 参加者マスター 行{i} メールアドレスの形式が不正のため空扱い"
            )
            email = ""

        records.append(MasterRecord(
            management_number=management_number,
            clinic_name=clinic_name,
            participant_name=participant_name,
            venue=venue,
            email=email,
        ))

    if not records:
        # ヘッダーは存在するが行が無いケース。Gmail 下書きの TO が全件空に
        # なるため、サイレントに INFO で流すのではなく WARNING を出す（P-022）。
        logger.warning(
            f"Sheets: 参加者マスター '{sheet_name}' は 0 件です（ヘッダーのみ）。"
            f"このまま PDF 処理を実行すると Gmail 下書きの TO が全件空になります。"
        )
    else:
        logger.info(
            f"Sheets: 参加者マスター {len(records)}件を取得 ({sheet_name})"
        )
    return records


def _normalize_clinic_number(clinic_number: str) -> str:
    """医院番号を比較用に正規化（先頭ゼロ除去）。

    PDF ファイル名と参加者マスターで医院番号のゼロパディング桁数が異なる
    ケース（``"001"`` と ``"00001"`` を同一医院として扱う）を吸収するため、
    先頭ゼロを除去してから比較する。空文字は空文字を返す。``"000"`` のような
    全部ゼロのケースは ``"0"`` を返す（lstrip 後が空になるのを避ける）。
    """
    if not clinic_number:
        return ""
    stripped = clinic_number.lstrip("0")
    return stripped or "0"


# ファジー一致を許可する個人名の最小文字数（正規化後）。1-2 文字の CJK 名は
# Levenshtein 距離 1 で他人を誤って巻き込むリスクが高い（例: ``木`` ↔ ``林``
# は距離 1 で同一クリニックの別人を誤マッチする）ため、短すぎる名前では
# ファジー一致を無効化し、完全一致のみ採用する（P-022）。
_FUZZY_MIN_LENGTH = 3


def lookup_clinic_name(
    records: list[MasterRecord],
    clinic_number: str,
) -> str:
    """医院コードから標準医院名を引く。

    A 列の管理番号 (``101-01`` 等) の先頭セグメント (``101``) が
    ``clinic_number`` と一致する最初の行の ``clinic_name`` を返す。複数行
    ある場合は最初に見つかった値を使う（同一医院は同じ医院名で登録されて
    いる前提）。一致行がない、または医院名が空文字なら空文字を返す。

    Args:
        records: ``read_master_records`` の戻り値
        clinic_number: 医院コード（``101`` のような数字部分）

    Returns:
        標準医院名。未登録なら空文字。呼び出し側は空文字なら AI 抽出値で代用。
    """
    if not clinic_number:
        return ""

    norm_target = _normalize_clinic_number(clinic_number)
    for r in records:
        if (
            _normalize_clinic_number(r.clinic_number) == norm_target
            and r.clinic_name
        ):
            return r.clinic_name
    return ""


def lookup_email_by_clinic_and_person(
    records: list[MasterRecord],
    clinic_number: str,
    person_name: str,
) -> str:
    """医院コード + 個人名から参加者のメールアドレスを引く。

    突合手順:
        1. 管理番号 (A 列) の先頭セグメントが ``clinic_number`` と一致する
           行に絞り込む（``101-01`` の管理番号は医院コード ``101`` として扱う）
        2. 個人名 (C 列) を ``_normalize_person_name`` で正規化して
           比較（NFKC + 全空白除去 + ひらがな統一 + 小文字化）
        3. 完全一致行が 1 件 → そのメールを返す
        4. 完全一致行が 2 件以上 → 警告ログ + 先頭採用
        5. 完全一致なし → Levenshtein 距離 ≤ 1 でファジー一致を試行
        6. ファジー一致 1 件 → そのメールを返す（fuzzy match ログを残す）
        7. ファジー一致 2 件以上 → 警告ログ + 先頭採用
        8. ヒットなし → 空文字を返す（呼び出し側は宛先空で下書きを作る）

    Args:
        records: ``read_master_records`` の戻り値
        clinic_number: 医院コード（``001`` のような数字部分）
        person_name: PDF 本文から AI 抽出した個人名

    Returns:
        メールアドレス。未登録、または空文字なら空文字を返す。
    """
    if not clinic_number or not person_name:
        return ""

    norm_clinic_target = _normalize_clinic_number(clinic_number)
    candidates = [
        r for r in records
        if _normalize_clinic_number(r.clinic_number) == norm_clinic_target
    ]
    if not candidates:
        logger.warning(
            f"参加者マスター: 医院管理番号 {clinic_number} に該当行なし "
            f"(PDF 個人名={person_name!r})"
        )
        return ""

    normalized_target = _normalize_person_name(person_name)

    # 完全一致（正規化後）
    exact = [
        r for r in candidates
        if _normalize_person_name(r.participant_name) == normalized_target
    ]
    if len(exact) == 1:
        return exact[0].email
    if len(exact) > 1:
        logger.warning(
            f"参加者マスター: 医院 {clinic_number} に同姓同名複数ヒット "
            f"({person_name!r} で {len(exact)}件) → 先頭採用"
        )
        return exact[0].email

    # ファジー一致を試す前に、個人名が短すぎる（1-2 文字 CJK 等）場合は
    # 距離 1 で他人を誤マッチするリスクが高いため、ファジー一致をスキップ
    # して完全一致のみ採用する（P-022）。
    if len(normalized_target) < _FUZZY_MIN_LENGTH:
        logger.warning(
            f"参加者マスター: 個人名 {person_name!r} が短すぎてファジー一致を"
            f"スキップ (医院 {clinic_number} 内に完全一致なし、誤マッチ防止のため"
            f"距離 1 候補は採用しない)"
        )
        return ""

    # ファジー一致（Levenshtein 距離 ≤ 1）
    fuzzy = [
        r for r in candidates
        if _levenshtein_distance(
            _normalize_person_name(r.participant_name), normalized_target
        ) <= 1
    ]
    if len(fuzzy) == 1:
        logger.info(
            f"参加者マスター: ファジー一致採用 "
            f"(PDF={person_name!r} ↔ マスター={fuzzy[0].participant_name!r})"
        )
        return fuzzy[0].email
    if len(fuzzy) > 1:
        logger.warning(
            f"参加者マスター: 医院 {clinic_number} にファジー候補複数 "
            f"({person_name!r} で {len(fuzzy)}件) → 先頭採用 "
            f"({fuzzy[0].participant_name!r})"
        )
        return fuzzy[0].email

    logger.warning(
        f"参加者マスター: 医院 {clinic_number} 内に "
        f"個人名 {person_name!r} に一致する参加者なし"
    )
    return ""
