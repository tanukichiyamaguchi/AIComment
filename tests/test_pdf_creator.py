"""pdf_creator モジュールのテスト。"""

import tempfile
import unittest
from pathlib import Path

import pdfplumber

from src.pdf_creator import _wrap_text, create_comment_page
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

    def test_clinic_name_and_person_name_not_rendered_on_page(self):
        """生成PDFに医院名・氏名・敬称が描画されていないことの回帰防止。

        個人情報をコメントページ本体に残さない方針。clinic_name / person_name
        は引数として受け取るが（ファイル名・Drive フォルダ階層では使う）、
        ページ上には出力しない。
        """
        comment = (
            "実践内容は非常に明快で、目標と行動と成果が一直線でした。"
            "教育の仕組み化も具体的で、再現性が高い良い取り組みだと感じます。"
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir) / "test_no_names.pdf"
            create_comment_page(
                comment=comment,
                clinic_name="三浦歯科医院",
                person_name="白川 蓮",
                output_path=output,
            )
            with pdfplumber.open(output) as pdf:
                page_text = "".join(p.extract_text() or "" for p in pdf.pages)
        # 描画されているべきもの: タイトル + 本文
        self.assertIn("じっせん君", page_text)
        # 描画されてはいけないもの: 医院名 / 氏名 / 「○○ 様」
        self.assertNotIn("三浦歯科医院", page_text)
        self.assertNotIn("白川 蓮", page_text)
        self.assertNotIn("白川", page_text)
        self.assertNotIn("様", page_text)


class TestWrapTextRespectsParagraphsAndKinsoku(unittest.TestCase):
    """``_wrap_text`` は ``\\n`` を段落区切りとして尊重し、
    行頭禁則（「、」「。」等が行頭に来ない）を適用する。
    """

    @classmethod
    def setUpClass(cls):
        # _wrap_text は pdfmetrics でフォントを参照するため、create_comment_page と
        # 同様にフォント登録を済ませる。
        from src.pdf_creator import _register_fonts
        _register_fonts()

    def test_explicit_newline_creates_paragraph_break(self):
        """``\\n`` が独立した行（段落区切り）として尊重される。"""
        text = "前段の文章です。\n次の段落です。"
        # 各段落は十分短いので幅オーバー折り返しは発生しない
        lines = _wrap_text(text, "NotoSansJP", 11, max_width=1000.0)
        self.assertEqual(lines, ["前段の文章です。", "次の段落です。"])

    def test_multiple_newlines_yield_blank_lines(self):
        """``\\n\\n`` のような連続改行は空行として残す（段落間の余白）。"""
        text = "段落A。\n\n段落B。"
        lines = _wrap_text(text, "NotoSansJP", 11, max_width=1000.0)
        self.assertEqual(lines, ["段落A。", "", "段落B。"])

    def test_period_does_not_appear_at_line_start(self):
        """折り返しで「。」が次行頭に来そうなとき、前行末尾にぶら下げる。"""
        # ちょうど 1 文字あふれて「。」が次行先頭になるよう max_width を調整する。
        # NotoSansJP 11pt で「abc」を測ってから「。」分の余裕を確保しないギリギリ幅で発火させる。
        from reportlab.pdfbase import pdfmetrics
        base = "あいうえおかきくけこ"
        base_w = pdfmetrics.stringWidth(base, "NotoSansJP", 11)
        # base + 「。」 を入れると max_width を超えるが、base 単独では収まる幅にする。
        max_width = base_w + pdfmetrics.stringWidth("。", "NotoSansJP", 11) - 1.0
        lines = _wrap_text(base + "。続き", "NotoSansJP", 11, max_width)
        # 「。」が単独で行頭に来ていないこと（ぶら下げで前行末尾に残っている）
        for line in lines:
            self.assertFalse(line.startswith("。"), f"行頭に句点が来ている: {lines}")

    def test_comma_does_not_appear_at_line_start(self):
        """折り返しで「、」が行頭に来ないこと。"""
        from reportlab.pdfbase import pdfmetrics
        base = "あいうえおかきくけこ"
        base_w = pdfmetrics.stringWidth(base, "NotoSansJP", 11)
        max_width = base_w + pdfmetrics.stringWidth("、", "NotoSansJP", 11) - 1.0
        lines = _wrap_text(base + "、続き", "NotoSansJP", 11, max_width)
        for line in lines:
            self.assertFalse(line.startswith("、"), f"行頭に読点が来ている: {lines}")

    def test_closing_paren_does_not_appear_at_line_start(self):
        """閉じ括弧「）」「」」「』」なども行頭に来ない。"""
        from reportlab.pdfbase import pdfmetrics
        base = "あいうえおかきくけこ"
        base_w = pdfmetrics.stringWidth(base, "NotoSansJP", 11)
        max_width = base_w + pdfmetrics.stringWidth("）", "NotoSansJP", 11) - 1.0
        lines = _wrap_text(base + "）の続き", "NotoSansJP", 11, max_width)
        for line in lines:
            self.assertFalse(line.startswith("）"), f"行頭に閉じ括弧が来ている: {lines}")

    def test_normal_character_at_line_start_is_fine(self):
        """禁則対象外の通常文字は普通に行頭に置かれる（過剰なぶら下げは起きない）。"""
        from reportlab.pdfbase import pdfmetrics
        base = "あいうえおかきくけこ"
        base_w = pdfmetrics.stringWidth(base, "NotoSansJP", 11)
        max_width = base_w + pdfmetrics.stringWidth("さ", "NotoSansJP", 11) - 1.0
        lines = _wrap_text(base + "さしすせそ", "NotoSansJP", 11, max_width)
        # 「さ」が次行先頭に来ているのが正常
        self.assertEqual(lines[0], base)
        self.assertTrue(lines[1].startswith("さ"))


class TestSystemPromptHasNewlineGuidance(unittest.TestCase):
    """生成プロンプトに「文脈の切れ目で改行を入れる」指示が含まれていること。

    PDF 描画側は ``\\n`` を段落区切りとして尊重するだけなので、実際に段落分け
    するのは Claude の生成段階。プロンプトから指示が消えると改行されない
    1 段落のべた書きに退行する。
    """

    def test_prompt_instructs_paragraph_break_on_newline(self):
        from src.comment_generator import SYSTEM_PROMPT
        self.assertIn("改行ルール", SYSTEM_PROMPT)
        # \n 挿入の明示
        self.assertIn("\\n", SYSTEM_PROMPT)
        # 「文脈」「段落」のいずれかが含まれる
        self.assertTrue(
            "文脈" in SYSTEM_PROMPT or "段落" in SYSTEM_PROMPT,
            "段落分けの指示が欠落している",
        )


if __name__ == "__main__":
    unittest.main()
