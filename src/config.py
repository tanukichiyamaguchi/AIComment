"""設定値管理モジュール。環境変数またはColab Secretsから設定を読み込む。"""

from __future__ import annotations

import os
from pathlib import Path


def _get_secret(key: str, default: str = "") -> str:
    """環境変数またはColab Secretsから値を取得する。"""
    value = os.environ.get(key)
    if value:
        return value
    try:
        from google.colab import userdata  # type: ignore
        return userdata.get(key)
    except Exception:
        return default


def _get_bool(key: str, default: bool) -> bool:
    """環境変数を真偽値として取得する。

    GitHub Actions の ``workflow_dispatch`` boolean 入力は文字列 ``"true"`` /
    ``"false"`` として渡ってくるため、文字列を緩く解釈する。未設定（空文字）
    のときは ``default`` を返す。``false/0/no/off`` を偽、それ以外の非空値を真。
    """
    raw = _get_secret(key, "").strip().lower()
    if not raw:
        return default
    return raw not in ("false", "0", "no", "off")


# ── パス設定 ──
PROJECT_ROOT = Path(__file__).resolve().parent.parent
ASSETS_DIR = PROJECT_ROOT / "assets"
LOGS_DIR = PROJECT_ROOT / "logs"

# ── API設定 ──
ANTHROPIC_API_KEY = _get_secret("ANTHROPIC_API_KEY")
CLAUDE_MODEL = "claude-sonnet-4-6"
CLAUDE_MAX_TOKENS = 1024
CLAUDE_TEMPERATURE = 0.9

# ── Google設定 ──
DRIVE_FOLDER_ID = _get_secret("DRIVE_FOLDER_ID")
DRIVE_OUTPUT_FOLDER_ID = _get_secret("DRIVE_OUTPUT_FOLDER_ID")
SPREADSHEET_ID = _get_secret("SPREADSHEET_ID")
GMAIL_TOKEN_JSON = _get_secret("GMAIL_TOKEN_JSON")
GOOGLE_CREDENTIALS_JSON = _get_secret("GOOGLE_CREDENTIALS_JSON")

# ── フォルダ自動検出モード用設定 ──
# Convention over Configuration の方針で、Secret/YAML 追加なしで新セミナーに
# 対応するための入出力 ROOT フォルダ ID。``--target-folder`` 指定時のみ参照する。
# 既存の ``--profile`` モードでは未使用（後方互換）。
DRIVE_INPUT_ROOT = _get_secret("DRIVE_INPUT_ROOT")
DRIVE_OUTPUT_ROOT = _get_secret("DRIVE_OUTPUT_ROOT")

# OAuthユーザートークン（Drive/Sheets/Gmail共通）。
# サービスアカウントは My Drive 配下にファイルをアップロードできない（storageQuotaExceeded）
# ため、Drive書き込み時はユーザー認可トークンを優先する。
# 旧 GMAIL_TOKEN_JSON も後方互換でフォールバックとして使用する。
GOOGLE_OAUTH_TOKEN_JSON = _get_secret("GOOGLE_OAUTH_TOKEN_JSON") or GMAIL_TOKEN_JSON

GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/gmail.compose",
]

# Google API の一過性エラー（503/429/5xx）に対する自動リトライ回数。
# googleapiclient の ``HttpRequest.execute(num_retries=N)`` /
# ``MediaIoBaseDownload.next_chunk(num_retries=N)`` に渡すと、5xx
# （500/502/503/504）と 429/rate-limit エラーを指数バックオフ + ジッターで
# 自動再試行する。指数バックオフは googleapiclient が担うため、こちらは
# 回数のみ指定する。401/403/404 のような恒久エラーはリトライ対象外。
GOOGLE_API_NUM_RETRIES = 5

# ── 出力一覧シート設定 ──
OUTPUT_SHEET_NAME = "出力一覧"

# ── 参加者マスターシート設定 ──
# 医院名標準化（フォルダ命名）と Gmail 下書きの TO を引くためのルックアップ表。
# シートが無ければ初回実行時に自動作成し、ヘッダー（5 列）だけ書き込んだ空
# シートになる。シート名はプロファイルでオーバーライド可能
# （``ProfileConfig.master_sheet_name``）。
# 5 列構造: A=管理番号 / B=医院名 / C=参加者名 / D=申し込み会場 / E=メールアドレス
# 1 行 per 個人。同じ医院番号 (例 101) の複数行 (101-01, 101-02) は同じ医院名を
# 重複記入する想定。
MASTER_SHEET_NAME = "参加者マスター"

# ── フォント設定 ──
FONT_REGULAR = ASSETS_DIR / "NotoSansJP-Regular.ttf"
FONT_BOLD = ASSETS_DIR / "NotoSansJP-Bold.ttf"
FONT_DOWNLOAD_BASE_URL = (
    "https://github.com/google/fonts/raw/main/ofl/notosansjp/"
)

# ── アセット ──
JISSEN_KUN_IMAGE = ASSETS_DIR / "jissen_kun.png"

# ── PDF設定 ──
PDF_PAGE_SIZE = "A4"

# ── Gmail 下書き生成のオン/オフ ──
# Gmail 下書き（医院宛メールの下書き）を作成するかどうか。
# デフォルトは True（従来挙動を維持）。本番で不要になった場合は環境変数
# ``ENABLE_GMAIL_DRAFTS=false`` で OFF にできる（GitHub Actions の
# workflow_dispatch 入力 create_gmail_drafts から渡す想定）。
# OFF のときは PDF 生成・Drive アップロード・出力一覧シート追記までは従来通り
# 行い、最後の下書き作成ステップだけをスキップする。
ENABLE_GMAIL_DRAFTS = _get_bool("ENABLE_GMAIL_DRAFTS", default=True)

# ── ログ設定 ──
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
LOG_FILE = LOGS_DIR / "jissen_comment.log"


# ── プロファイル経由で設定を取得するヘルパー ──
# 既存定数（``DRIVE_FOLDER_ID`` など）は維持しつつ、プロファイル制度を
# 使うコードからは ``get_profile_config(name)`` 一本で必要な値を取れる。
def get_profile_config(profile_name: str = "jissen_default"):
    """プロファイル名から ``ProfileConfig`` を取得する薄いラッパー。

    ``src.profile.load_profile`` への循環インポートを避けるため、関数内で
    遅延 import している。既存コードを ``profile.load_profile`` 直呼び出し
    から段階的に移行したい場合のエイリアスとして使う。
    """
    from src.profile import load_profile
    return load_profile(profile_name)
