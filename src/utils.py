"""ユーティリティモジュール。ログ設定とフォント自動ダウンロード。"""

import logging
import sys
from pathlib import Path

from src.config import LOG_LEVEL, LOG_FILE, LOGS_DIR, ASSETS_DIR, FONT_REGULAR, FONT_BOLD


def setup_logging() -> logging.Logger:
    """アプリケーション全体のロガーを設定する。"""
    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("jissen_comment")
    logger.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))

    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # コンソール出力
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    # ファイル出力
    file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    return logger


def mask_email(email: str) -> str:
    """メールアドレスをマスクする（ログ出力用）。"""
    if "@" not in email:
        return "***"
    local, domain = email.rsplit("@", 1)
    if len(local) <= 2:
        masked_local = "*" * len(local)
    else:
        masked_local = local[0] + "*" * (len(local) - 2) + local[-1]
    return f"{masked_local}@{domain}"


def ensure_fonts() -> None:
    """NotoSansJPフォントが存在しない場合、Google Fontsからダウンロードする。"""
    import requests

    ASSETS_DIR.mkdir(parents=True, exist_ok=True)

    fonts = {
        FONT_REGULAR: "NotoSansJP%5Bwght%5D.ttf",
        FONT_BOLD: "NotoSansJP%5Bwght%5D.ttf",
    }

    # Google Fonts API からダウンロード
    base_url = "https://fonts.google.com/download?family=Noto+Sans+JP"

    missing = [f for f in [FONT_REGULAR, FONT_BOLD] if not f.exists()]
    if not missing:
        return

    logger = logging.getLogger("jissen_comment")
    logger.info("NotoSansJPフォントをダウンロード中...")

    # GitHub の google/fonts リポジトリから直接ダウンロード
    # Variable fontを取得して、Regular/Bold両方に使用
    variable_font_url = (
        "https://github.com/google/fonts/raw/main/ofl/notosansjp/"
        "NotoSansJP%5Bwght%5D.ttf"
    )

    try:
        response = requests.get(variable_font_url, timeout=60)
        response.raise_for_status()
        font_data = response.content

        # Variable fontは1ファイルで全ウェイトを含むため、
        # Regular/Bold両方に同じファイルを使用
        for font_path in [FONT_REGULAR, FONT_BOLD]:
            if not font_path.exists():
                font_path.write_bytes(font_data)
                logger.info(f"フォント保存: {font_path.name}")

        logger.info("フォントのダウンロード完了")
    except Exception as e:
        logger.error(f"フォントのダウンロードに失敗: {e}")
        raise RuntimeError(
            f"NotoSansJPフォントのダウンロードに失敗しました。"
            f"手動で {ASSETS_DIR} にフォントファイルを配置してください。"
        ) from e
