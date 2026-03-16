"""PDF結合モジュール。元PDFの末尾にコメントページを1ページ追加する。"""

import io
import logging
from pathlib import Path

from pypdf import PdfReader, PdfWriter

logger = logging.getLogger("jissen_comment")


def merge_pdfs(
    original_pdf_data: bytes,
    comment_page_path: str | Path,
    output_path: str | Path,
) -> Path:
    """元PDFの末尾にコメントページPDFを1ページ追加する。

    元PDFのページは一切変更しない。

    Args:
        original_pdf_data: 元PDFのバイナリデータ
        comment_page_path: コメントページPDFのパス
        output_path: 結合後PDFの出力先パス

    Returns:
        結合後PDFのパス
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    writer = PdfWriter()

    # 元PDFの全ページを追加
    original_reader = PdfReader(io.BytesIO(original_pdf_data))
    original_pages = len(original_reader.pages)
    for page in original_reader.pages:
        writer.add_page(page)

    # コメントページを追加
    comment_reader = PdfReader(str(comment_page_path))
    writer.add_page(comment_reader.pages[0])

    # 書き出し
    with open(output_path, "wb") as f:
        writer.write(f)

    total_pages = original_pages + 1
    logger.info(
        f"PDF結合完了: {original_pages}ページ + 1ページ = {total_pages}ページ → {output_path}"
    )
    return output_path


def make_output_filename(clinic_name: str, person_name: str) -> str:
    """出力PDFのファイル名を生成する。"""
    return f"{clinic_name}_{person_name}_じっせん君コメント.pdf"
