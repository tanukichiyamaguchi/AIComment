"""ユーティリティモジュール。ログ設定・フォント自動ダウンロード・ファイル名整形。"""

from __future__ import annotations

import logging
import re
import sys
import unicodedata

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


# 実践事例 PDF のファイル名先頭に埋め込まれた管理番号のパターン。
# ``NNN-NN-N``（数字3 - 数字2 - 数字1、ハイフン込みで計 8 文字）。
_MANAGEMENT_NUMBER_PATTERN = re.compile(r"^\d{3}-\d{2}-\d")


def extract_management_number(filename: str) -> str:
    """PDFファイル名の先頭から管理番号を抽出する。

    実践事例 PDF はファイル名の先頭に ``NNN-NN-N`` 形式（数字3-数字2-数字1、
    計8文字）の管理番号が埋め込まれている。例: ``001-01-0実践事例.pdf`` → ``001-01-0``。

    Args:
        filename: PDF のファイル名

    Returns:
        抽出した8文字の管理番号。先頭がパターンに合致しない場合は空文字列。
        （呼び出し側で空文字列を検知して warning ログを出すこと）
    """
    if not isinstance(filename, str):
        filename = str(filename)
    match = _MANAGEMENT_NUMBER_PATTERN.match(filename)
    return match.group(0) if match else ""


def normalize_name_for_match(name: str) -> str:
    """医院名・氏名のマッチング用に正規化する。

    AIが抽出する医院名・氏名は、同じ医院/人物でも軽微な表記揺れ
    （半角/全角、空白の有無）が発生する。Driveフォルダの重複作成を防ぐため、
    ルックアップ時のみこの正規化済み形で比較する。

    変換内容:
        - NFKC 正規化（全角英数字・記号を半角に統一）
        - 全種類の空白文字（半角・全角・タブ等）をすべて除去

    変換しないこと（保守的判定のため）:
        - 大文字小文字（"WKWK" と "wkwk" は別物として扱う）
        - 句読点・記号（"森本歯科" と "森本歯科クリニック" は別物）

    マッチング比較**専用**で、表示・保存には使わない（元の表記を保持する）。
    """
    if not isinstance(name, str):
        name = str(name)
    nfkc = unicodedata.normalize("NFKC", name)
    return re.sub(r"\s+", "", nfkc)


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
