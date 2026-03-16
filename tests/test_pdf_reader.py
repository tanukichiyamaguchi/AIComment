"""pdf_reader モジュールのテスト。"""

import io
import unittest

from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

from src.pdf_reader import extract_text
from src.utils import ensure_fonts
from src.config import FONT_REGULAR


def _create_test_pdf(pages: list[str]) -> bytes:
    """テスト用PDFをメモリ上で作成する（日本語対応）。"""
    ensure_fonts()
    if "NotoSansJP" not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont("NotoSansJP", str(FONT_REGULAR)))

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    for text in pages:
        c.setFont("NotoSansJP", 12)
        c.drawString(72, 700, text)
        c.showPage()
    c.save()
    return buf.getvalue()


class TestPdfReader(unittest.TestCase):

    def test_single_page(self):
        pdf_data = _create_test_pdf(["テスト歯科医院 白川蓮"])
        text = extract_text(pdf_data)
        self.assertIn("テスト歯科医院", text)
        self.assertIn("白川蓮", text)

    def test_multi_page(self):
        pages = [f"ページ{i+1}の内容" for i in range(5)]
        pdf_data = _create_test_pdf(pages)
        text = extract_text(pdf_data)
        for i in range(5):
            self.assertIn(f"ページ{i+1}の内容", text)

    def test_empty_pdf_raises(self):
        # 空のPDF（テキストなし）
        buf = io.BytesIO()
        c = canvas.Canvas(buf, pagesize=A4)
        c.showPage()
        c.save()
        with self.assertRaises(ValueError):
            extract_text(buf.getvalue())


if __name__ == "__main__":
    unittest.main()
