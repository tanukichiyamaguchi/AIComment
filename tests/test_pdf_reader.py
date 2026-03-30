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

    def test_empty_pdf_returns_empty(self):
        # 空のPDF（テキストなし）→ 空文字列を返す
        buf = io.BytesIO()
        c = canvas.Canvas(buf, pagesize=A4)
        c.showPage()
        c.save()
        result = extract_text(buf.getvalue())
        self.assertEqual(result, "")

    def test_empty_pdf_no_pages(self):
        """ページはあるがテキストなしのPDFで空文字列が返ること。"""
        buf = io.BytesIO()
        c = canvas.Canvas(buf, pagesize=A4)
        # 複数の空ページ
        c.showPage()
        c.showPage()
        c.showPage()
        c.save()
        result = extract_text(buf.getvalue())
        self.assertEqual(result, "")

    def test_pdf_with_only_images_no_text(self):
        """テキストなし（画像のみ想定）のPDFで空文字列が返ること。"""
        buf = io.BytesIO()
        c = canvas.Canvas(buf, pagesize=A4)
        # テキストを描画せず、図形のみ描画（画像の代替）
        c.setFillColorRGB(0.5, 0.5, 0.5)
        c.rect(100, 100, 200, 200, fill=1)
        c.circle(300, 500, 50, fill=1)
        c.showPage()
        c.save()
        result = extract_text(buf.getvalue())
        self.assertEqual(result, "")

    def test_large_text_extraction(self):
        """大量テキストのPDFから正常に抽出できること。"""
        ensure_fonts()
        if "NotoSansJP" not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont("NotoSansJP", str(FONT_REGULAR)))

        # 50ページ分のPDFを作成
        pages = [f"ページ{i+1}の内容です。テスト歯科医院の報告書。" for i in range(50)]
        pdf_data = _create_test_pdf(pages)
        text = extract_text(pdf_data)
        # 全ページのテキストが含まれていること
        for i in range(50):
            self.assertIn(f"ページ{i+1}の内容です", text)
        # テキスト量が十分であること
        self.assertGreater(len(text), 500)

    def test_invalid_pdf_bytes(self):
        """不正なバイナリデータで空文字列が返ること。"""
        result = extract_text(b"this is not a pdf")
        self.assertEqual(result, "")

    def test_empty_bytes_input(self):
        """空バイト入力で空文字列が返ること。"""
        result = extract_text(b"")
        self.assertEqual(result, "")


if __name__ == "__main__":
    unittest.main()
