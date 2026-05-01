"""scripts.triage_pdfs のテスト。1000+ PDF処理前の分類ロジック。"""

import io
import unittest

from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

from scripts.triage_pdfs import (
    HEALTHY,
    SCANNED,
    CORRUPTED,
    OVERSIZED,
    DUPLICATE,
    classify,
    decision,
    summarize,
    triage_iter,
)
from src.config import FONT_REGULAR
from src.utils import ensure_fonts


def _healthy_pdf(text: str = "テスト歯科医院 白川蓮 報告書") -> bytes:
    ensure_fonts()
    if "NotoSansJP" not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont("NotoSansJP", str(FONT_REGULAR)))
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.setFont("NotoSansJP", 12)
    # 50字以上の日本語を書き込んで healthy 判定にする
    c.drawString(72, 720, text)
    c.drawString(72, 700, "実践事例: 新患獲得施策、自費率向上、スタッフ定着の3軸で改善")
    c.drawString(72, 680, "結果: 自費率15%→25%、新患月20名→45名、離職率20%→2%")
    c.showPage()
    c.save()
    return buf.getvalue()


def _scanned_pdf() -> bytes:
    """テキスト皆無のPDF（スキャン画像のみ想定）。"""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.setFillColorRGB(0.5, 0.5, 0.5)
    c.rect(100, 100, 200, 200, fill=1)
    c.showPage()
    c.save()
    return buf.getvalue()


class TestTriageClassification(unittest.TestCase):

    def setUp(self):
        self.seen: dict[str, str] = {}

    def test_healthy_pdf_with_text(self):
        data = _healthy_pdf()
        entry = classify("ok.pdf", "id1", data, self.seen)
        self.assertEqual(entry.category, HEALTHY)
        self.assertGreaterEqual(entry.page_count, 1)
        self.assertEqual(entry.recommended_action, "proceed")

    def test_scanned_pdf_classified_as_scanned(self):
        data = _scanned_pdf()
        entry = classify("scan.pdf", "id2", data, self.seen)
        self.assertEqual(entry.category, SCANNED)
        self.assertIn("OCR", entry.recommended_action)

    def test_corrupted_bytes(self):
        data = b"this is definitely not a PDF file"
        entry = classify("bad.pdf", "id3", data, self.seen)
        self.assertEqual(entry.category, CORRUPTED)

    def test_oversized_pdf(self):
        # 11MBのダミーバイトで oversized 分岐をトリガーする
        data = b"%PDF-1.4\n" + (b"x" * (11 * 1024 * 1024))
        entry = classify("big.pdf", "id4", data, self.seen)
        self.assertEqual(entry.category, OVERSIZED)

    def test_duplicate_detection_by_sha256(self):
        data = _healthy_pdf()
        first = classify("first.pdf", "id5", data, self.seen)
        second = classify("dup.pdf", "id6", data, self.seen)
        self.assertEqual(first.category, HEALTHY)
        self.assertEqual(second.category, DUPLICATE)
        self.assertIn("first.pdf", second.recommended_action)


class TestTriageAggregation(unittest.TestCase):

    def test_summarize_counts_each_category(self):
        items = [
            ("id1", "a.pdf", _healthy_pdf()),
            ("id2", "b.pdf", _healthy_pdf("別の医院 別の人 別の内容です。日本語50文字以上書き込みます")),
            ("id3", "c.pdf", _scanned_pdf()),
            ("id4", "d.pdf", b"not a pdf"),
        ]
        entries = triage_iter(items)
        counts = summarize(entries)
        self.assertEqual(counts[HEALTHY], 2)
        self.assertEqual(counts[SCANNED], 1)
        self.assertEqual(counts[CORRUPTED], 1)

    def test_decision_proceed_when_all_healthy(self):
        items = [("id", "a.pdf", _healthy_pdf())]
        entries = triage_iter(items)
        verdict, _ = decision(entries)
        self.assertEqual(verdict, "PROCEED")

    def test_decision_halt_above_threshold(self):
        # 1 healthy + 1 corrupted = 50% unhealthy → HALT
        items = [
            ("id1", "a.pdf", _healthy_pdf()),
            ("id2", "b.pdf", b"corrupted"),
        ]
        entries = triage_iter(items)
        verdict, reason = decision(entries, halt_threshold=0.01)
        self.assertEqual(verdict, "HALT")
        self.assertIn("threshold", reason)

    def test_decision_halt_on_empty(self):
        verdict, reason = decision([])
        self.assertEqual(verdict, "HALT")
        self.assertIn("no PDFs", reason)


if __name__ == "__main__":
    unittest.main()
