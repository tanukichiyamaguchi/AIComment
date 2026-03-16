"""PDFテキスト抽出モジュール。pdfplumberで全ページからテキストを抽出する。"""

import io
import logging

import pdfplumber

logger = logging.getLogger("jissen_comment")


def extract_text(pdf_data: bytes) -> str:
    """PDFバイナリデータから全ページのテキストを抽出する。

    Args:
        pdf_data: PDFファイルのバイナリデータ

    Returns:
        全ページのテキストを結合した文字列

    Raises:
        ValueError: テキストが抽出できなかった場合
    """
    pages_text: list[str] = []

    with pdfplumber.open(io.BytesIO(pdf_data)) as pdf:
        total_pages = len(pdf.pages)
        logger.info(f"PDF読み取り開始: {total_pages}ページ")

        for i, page in enumerate(pdf.pages):
            text = page.extract_text()
            if text:
                pages_text.append(text)
            else:
                logger.warning(f"ページ{i + 1}: テキスト抽出不可")

    if not pages_text:
        raise ValueError("PDFからテキストを抽出できませんでした")

    full_text = "\n\n".join(pages_text)
    logger.info(
        f"テキスト抽出完了: {len(pages_text)}/{total_pages}ページ, "
        f"{len(full_text)}文字"
    )
    return full_text
