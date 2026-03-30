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
CLAUDE_MODEL = "claude-sonnet-4-5-20241022"
CLAUDE_MAX_TOKENS = 1024
CLAUDE_TEMPERATURE = 0.9

# ── Google設定 ──
DRIVE_FOLDER_ID = _get_secret("DRIVE_FOLDER_ID")
SPREADSHEET_ID = _get_secret("SPREADSHEET_ID")
GMAIL_TOKEN_JSON = _get_secret("GMAIL_TOKEN_JSON")
GOOGLE_CREDENTIALS_JSON = _get_secret("GOOGLE_CREDENTIALS_JSON")

GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/gmail.compose",
]

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
