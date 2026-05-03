"""pdf_merger モジュールのテスト。"""

import io
import tempfile
import unittest
from pathlib import Path

from pypdf import PdfReader
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

from src.pdf_merger import merge_pdfs, make_output_filename
from src.pdf_creator import create_comment_page
from src.utils import ensure_fonts
from src.config import FONT_REGULAR


def _create_test_pdf(pages: list[str]) -> bytes:
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


class TestPdfMerger(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        ensure_fonts()

    def test_merge_adds_one_page(self):
        original = _create_test_pdf(["ページ1", "ページ2", "ページ3"])
        original_page_count = len(PdfReader(io.BytesIO(original)).pages)

        with tempfile.TemporaryDirectory() as tmpdir:
            comment_path = Path(tmpdir) / "comment.pdf"
            create_comment_page(
                comment="テストコメント" * 15,
                clinic_name="テスト医院",
                person_name="テスト太郎",
                output_path=comment_path,
            )

            output_path = Path(tmpdir) / "merged.pdf"
            merge_pdfs(original, comment_path, output_path)

            merged = PdfReader(str(output_path))
            self.assertEqual(len(merged.pages), original_page_count + 1)

    def test_original_pages_unchanged(self):
        pages_text = ["元ページ1の内容", "元ページ2の内容"]
        original = _create_test_pdf(pages_text)

        with tempfile.TemporaryDirectory() as tmpdir:
            comment_path = Path(tmpdir) / "comment.pdf"
            create_comment_page(
                comment="コメント" * 20,
                clinic_name="医院",
                person_name="氏名",
                output_path=comment_path,
            )

            output_path = Path(tmpdir) / "merged.pdf"
            merge_pdfs(original, comment_path, output_path)

            merged = PdfReader(str(output_path))
            # 元のページ数 + 1
            self.assertEqual(len(merged.pages), 3)

    def test_make_output_filename(self):
        name = make_output_filename("三浦歯科医院", "白川蓮", "AI活用インプラント新患獲得")
        self.assertEqual(name, "三浦歯科医院＿白川蓮＿AI活用インプラント新患獲得.pdf")


class TestPdfMergerSingle(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        ensure_fonts()

    def test_single_page_pdf(self):
        original = _create_test_pdf(["1ページだけのPDF"])

        with tempfile.TemporaryDirectory() as tmpdir:
            comment_path = Path(tmpdir) / "comment.pdf"
            create_comment_page(
                comment="コメント" * 20,
                clinic_name="医院",
                person_name="氏名",
                output_path=comment_path,
            )

            output_path = Path(tmpdir) / "merged.pdf"
            merge_pdfs(original, comment_path, output_path)

            merged = PdfReader(str(output_path))
            self.assertEqual(len(merged.pages), 2)


class TestPdfMergerEdgeCases(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        ensure_fonts()

    def test_merge_with_empty_original_pdf(self):
        """テキストなし（空ページのみ）のPDFにコメントページを追加できること。"""
        buf = io.BytesIO()
        c = canvas.Canvas(buf, pagesize=A4)
        c.showPage()  # 空ページ1つ
        c.save()
        empty_pdf = buf.getvalue()

        original_page_count = len(PdfReader(io.BytesIO(empty_pdf)).pages)
        self.assertEqual(original_page_count, 1)

        with tempfile.TemporaryDirectory() as tmpdir:
            comment_path = Path(tmpdir) / "comment.pdf"
            create_comment_page(
                comment="空PDFへのコメント" * 15,
                clinic_name="空ページ医院",
                person_name="テスト太郎",
                output_path=comment_path,
            )

            output_path = Path(tmpdir) / "merged.pdf"
            merge_pdfs(empty_pdf, comment_path, output_path)

            merged = PdfReader(str(output_path))
            self.assertEqual(len(merged.pages), original_page_count + 1)

    def test_merge_preserves_single_page_content(self):
        """1ページPDFの結合後も元ページの内容が保持されること。"""
        original = _create_test_pdf(["単一ページの内容テスト"])

        with tempfile.TemporaryDirectory() as tmpdir:
            comment_path = Path(tmpdir) / "comment.pdf"
            create_comment_page(
                comment="単一ページPDFへのコメント" * 15,
                clinic_name="単一ページ医院",
                person_name="テスト花子",
                output_path=comment_path,
            )

            output_path = Path(tmpdir) / "merged.pdf"
            merge_pdfs(original, comment_path, output_path)

            merged = PdfReader(str(output_path))
            self.assertEqual(len(merged.pages), 2)
            # 元ページのテキストが保持されていること
            first_page_text = merged.pages[0].extract_text()
            self.assertIn("単一ページの内容テスト", first_page_text)

    def test_merge_many_pages(self):
        """多ページ（20ページ）PDFへの結合が正常に動作すること。"""
        pages = [f"テストページ{i+1}" for i in range(20)]
        original = _create_test_pdf(pages)

        with tempfile.TemporaryDirectory() as tmpdir:
            comment_path = Path(tmpdir) / "comment.pdf"
            create_comment_page(
                comment="多ページPDFへのコメント" * 15,
                clinic_name="多ページ医院",
                person_name="テスト次郎",
                output_path=comment_path,
            )

            output_path = Path(tmpdir) / "merged.pdf"
            merge_pdfs(original, comment_path, output_path)

            merged = PdfReader(str(output_path))
            self.assertEqual(len(merged.pages), 21)

    def test_make_output_filename_special_chars(self):
        """特殊文字を含む名前でもファイル名が生成されること。"""
        name = make_output_filename("テスト＆歯科", "山田（太郎）", "夏祭り/イベント")
        self.assertEqual(name, "テスト＆歯科＿山田（太郎）＿夏祭りイベント.pdf")


if __name__ == "__main__":
    unittest.main()
