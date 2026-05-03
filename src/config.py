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

# ── 出力一覧シート設定 ──
OUTPUT_SHEET_NAME = "出力一覧"

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

# ── ログ設定 ──
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
LOG_FILE = LOGS_DIR / "jissen_comment.log"
