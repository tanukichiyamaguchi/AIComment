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

# ── メールアドレス一覧シート設定 ──
# Gmail 下書き作成時の TO/CC を引くためのルックアップ表。シートが無ければ
# 初回実行時に自動作成し、ヘッダー（5 列）だけ書き込んだ空シートになる。
# シート名はプロファイルでオーバーライド可能（``ProfileConfig.email_sheet_name``）。
EMAIL_SHEET_NAME = "メールアドレス一覧"

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
