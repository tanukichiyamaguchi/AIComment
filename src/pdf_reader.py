"""PDFテキスト抽出モジュール。pdfplumberで全ページからテキストを抽出する。"""

from __future__ import annotations

import io
import logging
from pathlib import Path

import pdfplumber

logger = logging.getLogger("jissen_comment")


def extract_text_from_file(file_path: str | Path) -> str:
    """PDFファイルパスからテキストを抽出する。

    Args:
        file_path: PDFファイルのパス

    Returns:
        抽出したテキスト。ファイルが存在しない場合や読み取り不可の場合は空文字列。
    """
    path = Path(file_path)
    if not path.exists():
        logger.warning(f"PDFファイルが存在しません: {file_path}")
        return ""
    if not path.is_file():
        logger.warning(f"PDFパスがファイルではありません: {file_path}")
        return ""

    try:
        pdf_data = path.read_bytes()
    except OSError as e:
        logger.warning(f"PDFファイル読み取り失敗: {file_path} - {e}")
        return ""

    return extract_text(pdf_data)


def extract_text(pdf_data: bytes) -> str:
    """PDFバイナリデータから全ページのテキストを抽出する。

    Args:
        pdf_data: PDFファイルのバイナリデータ

    Returns:
        全ページのテキストを結合した文字列。
        破損PDFや抽出不可の場合は空文字列を返す。
    """
    if not pdf_data:
        logger.warning("PDF入力データが空です")
        return ""

    pages_text: list[str] = []

    try:
        with pdfplumber.open(io.BytesIO(pdf_data)) as pdf:
            total_pages = len(pdf.pages)
            logger.info(f"PDF読み取り開始: {total_pages}ページ")

            for i, page in enumerate(pdf.pages):
                text = page.extract_text()
                if text:
                    pages_text.append(text)
                else:
                    logger.warning(f"ページ{i + 1}: テキスト抽出不可")
    except Exception as e:
        logger.warning(f"PDF読み取りエラー（破損または不正なPDF）: {e}")
        return ""

    if not pages_text:
        logger.info("PDFから抽出可能なテキストがありませんでした")
        return ""

    full_text = "\n\n".join(pages_text)
    logger.info(
        f"テキスト抽出完了: {len(pages_text)}/{total_pages}ページ, "
        f"{len(full_text)}文字"
    )
    return full_text
