"""ユーティリティモジュール。ログ設定・フォント自動ダウンロード・ファイル名整形。"""

from __future__ import annotations

import logging
import re
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


# Forbidden chars: path separators, Windows-illegal chars, and non-whitespace
# control characters. Whitespace controls (\t \n \r) are intentionally excluded
# so that the subsequent whitespace-collapse step turns them into single spaces.
_FORBIDDEN_FILENAME_CHARS = re.compile(
    r'[\\/:*?"<>|\x00-\x08\x0b\x0c\x0e-\x1f]'
)


def sanitize_filename(
    name: str,
    fallback: str = "untitled",
    max_length: int = 100,
) -> str:
    """ファイル名として安全な文字列に整形する。

    - パス区切り（``/`` ``\\``）と Windows / Drive で問題を起こす特殊文字を除去
    - タブ・改行などの空白制御文字は空白に変換し、連続する空白を1つにまとめる
    - 先頭・末尾の空白とドットを除去
    - 空になったら ``fallback`` を返す
    - ``max_length`` で切り詰め

    Args:
        name: 元の文字列
        fallback: 整形後に空になった場合の代替文字列
        max_length: 上限文字数

    Returns:
        ファイル名として使える文字列
    """
    if not isinstance(name, str):
        name = str(name)
    cleaned = _FORBIDDEN_FILENAME_CHARS.sub("", name)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    if not cleaned:
        return fallback
    return cleaned[:max_length]


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
