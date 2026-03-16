"""pdf_creator モジュールのテスト。"""

import tempfile
import unittest
from pathlib import Path

from src.pdf_creator import create_comment_page
from src.utils import ensure_fonts


class TestPdfCreator(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        """フォントを事前にダウンロードしておく。"""
        ensure_fonts()

    def test_create_comment_page(self):
        comment = "テストコメントです。" * 15  # 約200文字
        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir) / "test_comment.pdf"
            result = create_comment_page(
                comment=comment,
                clinic_name="三浦歯科医院",
                person_name="白川蓮",
                output_path=output,
            )
            self.assertTrue(result.exists())
            # PDFファイルとして最低限のサイズがあること
            self.assertGreater(result.stat().st_size, 1000)

    def test_long_comment(self):
        comment = "長いコメントのテストです。" * 30  # 約450文字
        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir) / "test_long.pdf"
            result = create_comment_page(
                comment=comment,
                clinic_name="テスト歯科クリニック",
                person_name="テスト太郎",
                output_path=output,
            )
            self.assertTrue(result.exists())

    def test_output_is_valid_pdf(self):
        comment = "PDFバリデーションテスト。" * 15
        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir) / "test_valid.pdf"
            create_comment_page(
                comment=comment,
                clinic_name="バリデーション歯科",
                person_name="確認太郎",
                output_path=output,
            )
            # PDFヘッダーの確認
            with open(output, "rb") as f:
                header = f.read(5)
            self.assertEqual(header, b"%PDF-")


if __name__ == "__main__":
    unittest.main()
