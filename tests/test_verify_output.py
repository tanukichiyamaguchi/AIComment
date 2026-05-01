"""scripts.verify_output のテスト。サンプル検証ロジック。"""

import io
import tempfile
import unittest
from pathlib import Path

from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from pypdf import PdfReader, PdfWriter

from scripts.verify_output import (
    MIN_LAST_PAGE_CHARS,
    TEXT_OVERLAP_THRESHOLD,
    _overlap_ratio,
    select_sample,
    verify_one,
)
from src.config import FONT_REGULAR
from src.utils import ensure_fonts


def _make_pdf(pages_text: list[str]) -> bytes:
    ensure_fonts()
    if "NotoSansJP" not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont("NotoSansJP", str(FONT_REGULAR)))
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    for text in pages_text:
        c.setFont("NotoSansJP", 12)
        # write text in multiple lines to ensure enough chars on page
        y = 720
        for chunk in [text[i:i+30] for i in range(0, len(text), 30)]:
            c.drawString(72, y, chunk)
            y -= 20
        c.showPage()
    c.save()
    return buf.getvalue()


def _pad_to_min_size(data: bytes, target_bytes: int) -> bytes:
    """PDFをサイズ閾値超過のためにパディングする（PDFコメントとして安全に追加）。"""
    if len(data) >= target_bytes:
        return data
    padding = b"\n%" + b"x" * (target_bytes - len(data) - 2)
    return data + padding


class TestOverlapRatio(unittest.TestCase):

    def test_identical_texts_overlap_one(self):
        self.assertEqual(_overlap_ratio("素晴らしい取り組み", "素晴らしい取り組み"), 1.0)

    def test_partial_overlap(self):
        # expected has 10 unique chars, actual contains 5 of them
        ratio = _overlap_ratio("素晴らしい取り組みです", "素晴らしい")
        self.assertGreater(ratio, 0.4)
        self.assertLess(ratio, 0.7)

    def test_no_japanese_in_expected_returns_one(self):
        self.assertEqual(_overlap_ratio("ASCII only", "anything"), 1.0)


class TestSelectSample(unittest.TestCase):

    def test_sample_respects_minimum(self):
        paths = [Path(f"f{i}.pdf") for i in range(20)]
        # rate=0.05 of 20 = 1, but MIN_SAMPLE=30 floors → returns all 20
        sample = select_sample(paths, sample_rate=0.05)
        self.assertEqual(len(sample), 20)

    def test_sample_uses_rate_when_above_minimum(self):
        paths = [Path(f"f{i}.pdf") for i in range(1000)]
        sample = select_sample(paths, sample_rate=0.05)
        self.assertEqual(len(sample), 50)

    def test_sample_is_deterministic_with_seed(self):
        paths = [Path(f"f{i}.pdf") for i in range(1000)]
        a = select_sample(paths, sample_rate=0.05, seed=42)
        b = select_sample(paths, sample_rate=0.05, seed=42)
        self.assertEqual(a, b)

    def test_empty_input_returns_empty(self):
        self.assertEqual(select_sample([], sample_rate=0.5), [])


class TestVerifyOne(unittest.TestCase):

    def test_healthy_pdf_passes(self):
        comment = "素晴らしい取り組みでした。" * 10
        pdf_bytes = _make_pdf([
            "ページ1の本文" * 10,
            comment,
        ])
        pdf_bytes = _pad_to_min_size(pdf_bytes, 100 * 1024)
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "ok.pdf"
            p.write_bytes(pdf_bytes)
            entry = verify_one(p, expected_comment=comment, expected_original_pages=1)
        self.assertTrue(entry.passed, msg=f"failures: {entry.failures}")
        self.assertEqual(entry.page_count, 2)
        self.assertGreaterEqual(entry.last_page_jp_chars, MIN_LAST_PAGE_CHARS)
        self.assertGreaterEqual(entry.overlap_ratio, TEXT_OVERLAP_THRESHOLD)

    def test_missing_file_fails(self):
        entry = verify_one(Path("/nonexistent/none.pdf"), None, None)
        self.assertFalse(entry.passed)
        self.assertIn("file_missing", entry.failures)

    def test_undersized_file_fails(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "tiny.pdf"
            # Make a real but tiny PDF — actual PDF, just under 100KB
            tiny = _make_pdf(["small"])
            p.write_bytes(tiny)
            entry = verify_one(p, expected_comment=None, expected_original_pages=None)
        self.assertFalse(entry.passed)
        self.assertTrue(any(f.startswith("undersized") for f in entry.failures))

    def test_unopenable_file_fails(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "bad.pdf"
            p.write_bytes(b"x" * (200 * 1024))  # large enough to pass size check
            entry = verify_one(p, expected_comment=None, expected_original_pages=None)
        self.assertFalse(entry.passed)
        self.assertTrue(any(f.startswith("unopenable") for f in entry.failures))

    def test_wrong_page_count_fails(self):
        pdf_bytes = _pad_to_min_size(_make_pdf(["only one page" * 10]), 100 * 1024)
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "onepage.pdf"
            p.write_bytes(pdf_bytes)
            entry = verify_one(p, expected_comment=None, expected_original_pages=5)
        self.assertFalse(entry.passed)
        self.assertTrue(any(f.startswith("page_count") for f in entry.failures))


if __name__ == "__main__":
    unittest.main()
