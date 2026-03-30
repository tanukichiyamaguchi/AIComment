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


    def test_very_long_comment_overflow(self):
        """非常に長いコメント（1000文字超）でもエラーなくPDFが生成されること。"""
        comment = "これは非常に長いコメントのテストです。歯科医院の取り組みについて詳しく評価します。" * 50  # 約2000文字
        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir) / "test_overflow.pdf"
            result = create_comment_page(
                comment=comment,
                clinic_name="オーバーフローテスト歯科",
                person_name="テスト太郎",
                output_path=output,
            )
            self.assertTrue(result.exists())
            self.assertGreater(result.stat().st_size, 1000)
            # PDFヘッダーの確認
            with open(output, "rb") as f:
                header = f.read(5)
            self.assertEqual(header, b"%PDF-")

    def test_special_characters_in_comment(self):
        """特殊文字を含むコメントでもPDFが正常に生成されること。"""
        comment = (
            "素晴らしい取り組みです！★☆♪\n"
            "自費率15%→25%への改善、見事です。\n"
            "「予約」を「お約束」と呼ぶ工夫（キャンセル率-40%）も効果的。\n"
            "引き続き頑張ってください♪＊＆＄＃＠"
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir) / "test_special.pdf"
            result = create_comment_page(
                comment=comment,
                clinic_name="特殊文字テスト歯科＆クリニック",
                person_name="山田＠太郎",
                output_path=output,
            )
            self.assertTrue(result.exists())
            self.assertGreater(result.stat().st_size, 1000)

    def test_empty_comment(self):
        """空のコメントでもエラーなくPDFが生成されること。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir) / "test_empty.pdf"
            result = create_comment_page(
                comment="",
                clinic_name="空コメント歯科",
                person_name="テスト",
                output_path=output,
            )
            self.assertTrue(result.exists())

    def test_newlines_in_comment(self):
        """改行を多数含むコメントでもPDFが正常に生成されること。"""
        comment = "一行目\n\n二行目\n\n\n三行目\n四行目\n五行目"
        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir) / "test_newlines.pdf"
            result = create_comment_page(
                comment=comment,
                clinic_name="改行テスト歯科",
                person_name="改行太郎",
                output_path=output,
            )
            self.assertTrue(result.exists())


if __name__ == "__main__":
    unittest.main()
