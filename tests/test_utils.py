"""utils モジュールのテスト。"""

import unittest

from src.utils import (
    extract_clinic_number,
    extract_management_number,
    is_attachment_filename,
    mask_email,
    normalize_name_for_match,
    sanitize_filename,
)


class TestMaskEmail(unittest.TestCase):

    def test_masks_local_part(self):
        self.assertEqual(mask_email("yamada@example.com"), "y****a@example.com")

    def test_short_local_part_fully_masked(self):
        self.assertEqual(mask_email("ab@example.com"), "**@example.com")

    def test_invalid_returns_placeholder(self):
        self.assertEqual(mask_email("not-an-email"), "***")


class TestSanitizeFilename(unittest.TestCase):

    def test_keeps_safe_japanese_text(self):
        self.assertEqual(
            sanitize_filename("AI活用インプラント新患獲得"),
            "AI活用インプラント新患獲得",
        )

    def test_strips_path_separators(self):
        self.assertEqual(sanitize_filename("a/b\\c"), "abc")

    def test_strips_windows_forbidden_chars(self):
        self.assertEqual(sanitize_filename('a:b*c?d"e<f>g|h'), "abcdefgh")

    def test_collapses_whitespace(self):
        self.assertEqual(sanitize_filename("a    b\t\nc"), "a b c")

    def test_strips_leading_trailing_spaces_and_dots(self):
        self.assertEqual(sanitize_filename("  ..hello..  "), "hello")

    def test_returns_fallback_when_empty_after_sanitize(self):
        self.assertEqual(sanitize_filename("////"), "untitled")
        self.assertEqual(sanitize_filename("...", fallback="X"), "X")
        self.assertEqual(sanitize_filename("   "), "untitled")

    def test_truncates_to_max_length(self):
        result = sanitize_filename("あ" * 200, max_length=10)
        self.assertEqual(len(result), 10)
        self.assertEqual(result, "あ" * 10)

    def test_non_string_is_coerced(self):
        self.assertEqual(sanitize_filename(123), "123")  # type: ignore[arg-type]


class TestNormalizeNameForMatch(unittest.TestCase):
    """医院名・氏名のマッチング用正規化のテスト。"""

    def test_removes_halfwidth_space(self):
        self.assertEqual(
            normalize_name_for_match("医療法人 かがやき歯科クリニック"),
            "医療法人かがやき歯科クリニック",
        )

    def test_removes_fullwidth_space(self):
        self.assertEqual(
            normalize_name_for_match("医療法人　かがやき歯科クリニック"),
            "医療法人かがやき歯科クリニック",
        )

    def test_idempotent_on_clean_name(self):
        self.assertEqual(
            normalize_name_for_match("医療法人かがやき歯科クリニック"),
            "医療法人かがやき歯科クリニック",
        )

    def test_clinic_name_with_space_matches_without(self):
        a = normalize_name_for_match("医療法人社団wkwk 森本歯科クリニック")
        b = normalize_name_for_match("医療法人社団wkwk森本歯科クリニック")
        self.assertEqual(a, b)

    def test_person_name_with_space_matches_without(self):
        self.assertEqual(
            normalize_name_for_match("白川 蓮"),
            normalize_name_for_match("白川蓮"),
        )

    def test_fullwidth_alphanum_to_halfwidth(self):
        # NFKC で全角英数字 → 半角に正規化される
        self.assertEqual(
            normalize_name_for_match("ＡＢＣ１２３"),
            "ABC123",
        )

    def test_fullwidth_and_halfwidth_alpha_match(self):
        self.assertEqual(
            normalize_name_for_match("ｗｋｗｋ歯科"),
            normalize_name_for_match("wkwk歯科"),
        )

    def test_collapses_multiple_whitespaces(self):
        self.assertEqual(
            normalize_name_for_match("医療法人  社団   wkwk\t森本"),
            "医療法人社団wkwk森本",
        )

    def test_genuinely_different_names_stay_different(self):
        # 語が違うものは別物として扱う（保守的）
        self.assertNotEqual(
            normalize_name_for_match("森本歯科"),
            normalize_name_for_match("森本歯科クリニック"),
        )

    def test_case_sensitive(self):
        # 大文字小文字は維持する（"WKWK" と "wkwk" は別物）
        self.assertNotEqual(
            normalize_name_for_match("WKWK歯科"),
            normalize_name_for_match("wkwk歯科"),
        )

    def test_non_string_is_coerced(self):
        self.assertEqual(normalize_name_for_match(123), "123")  # type: ignore[arg-type]


class TestExtractManagementNumber(unittest.TestCase):
    """PDFファイル名先頭からの管理番号抽出のテスト。

    実践事例 PDF はファイル名先頭に ``NNN-NN-N``（先頭セグメント 3〜5桁 -
    数字2 - 数字1）形式の管理番号が埋め込まれている。先頭セグメント
    （= 医院番号）が 3〜5 桁可変なので、管理番号全体は 7〜9 文字になる。
    """

    def test_extracts_from_title_directly_after_code(self):
        """``001-01-0実践事例.pdf`` → ``001-01-0``（コード直後にタイトル）。"""
        self.assertEqual(
            extract_management_number("001-01-0実践事例タイトル.pdf"),
            "001-01-0",
        )

    def test_extracts_when_separated_by_underscore(self):
        """``001-01-0_実践事例.pdf`` → ``001-01-0``（区切り文字がアンダースコア）。"""
        self.assertEqual(
            extract_management_number("001-01-0_実践事例.pdf"),
            "001-01-0",
        )

    def test_extracts_all_nines(self):
        """``999-99-9.pdf`` → ``999-99-9``（全桁が 9）。"""
        self.assertEqual(extract_management_number("999-99-9.pdf"), "999-99-9")

    def test_extracts_exactly_eight_chars_without_extension(self):
        """先頭8文字ちょうど・拡張子なし ``001-01-0`` → ``001-01-0``。"""
        self.assertEqual(extract_management_number("001-01-0"), "001-01-0")

    def test_returns_empty_when_starts_with_non_digit(self):
        """先頭が数字でない ``実践事例.pdf`` → 空文字列。"""
        self.assertEqual(extract_management_number("実践事例.pdf"), "")

    def test_returns_empty_when_first_group_too_short(self):
        """先頭グループの桁数不足 ``01-01-0.pdf`` → 空文字列。"""
        self.assertEqual(extract_management_number("01-01-0.pdf"), "")

    def test_extracts_four_digit_first_group(self):
        """先頭グループが 4 桁 ``0001-01-0.pdf`` → ``0001-01-0``。

        医院番号（先頭セグメント）は 3〜5 桁可変なので 4 桁も有効。
        """
        self.assertEqual(
            extract_management_number("0001-01-0.pdf"), "0001-01-0"
        )

    def test_extracts_five_digit_first_group(self):
        """先頭グループが 5 桁 ``00001-01-0.pdf`` → ``00001-01-0``（5桁も有効）。"""
        self.assertEqual(
            extract_management_number("00001-01-0.pdf"), "00001-01-0"
        )

    def test_returns_empty_when_first_group_six_digits(self):
        """先頭グループが 6 桁 ``000001-01-0.pdf`` → 空文字列（6桁は無効）。

        ``^\\d{3,5}-\\d{2}-\\d`` は先頭 5 桁 ``00000`` までしかマッチせず、
        その直後にハイフンが必要なところで ``1`` が来るためマッチしない。
        """
        self.assertEqual(extract_management_number("000001-01-0.pdf"), "")

    def test_returns_empty_for_empty_string(self):
        """空文字列 ``""`` → 空文字列。"""
        self.assertEqual(extract_management_number(""), "")

    def test_returns_empty_for_fullwidth_hyphen(self):
        """ハイフンが全角 ``００１ー０１ー０`` → 空文字列（半角数字/ハイフンのみ対象）。"""
        self.assertEqual(extract_management_number("００１ー０１ー０.pdf"), "")

    def test_non_string_input_is_coerced(self):
        """非文字列入力（int 等）は str に変換される。

        int ``1234567`` は ``"1234567"`` となり、``123`` の後にハイフンが
        ないためマッチせず空文字列を返す。
        """
        self.assertEqual(extract_management_number(1234567), "")  # type: ignore[arg-type]

    def test_returns_empty_when_second_group_wrong_length(self):
        """中央グループが 1 桁 ``001-1-0.pdf`` → 空文字列。"""
        self.assertEqual(extract_management_number("001-1-0.pdf"), "")

    def test_ignores_trailing_extra_digits(self):
        """8 文字に合致すれば後続が数字でも先頭8文字のみ返す。

        ``001-01-09extra.pdf`` の先頭は ``001-01-0`` がパターンに合致し、
        9 文字目以降は ``group(0)`` に含まれない。
        """
        self.assertEqual(
            extract_management_number("001-01-09extra.pdf"),
            "001-01-0",
        )

    def test_returns_empty_when_only_partial_pattern(self):
        """ハイフンが欠落 ``00101-0.pdf`` → 空文字列。"""
        self.assertEqual(extract_management_number("00101-0.pdf"), "")

    def test_extracts_with_leading_zeros(self):
        """先頭ゼロを含む ``000-00-0実践.pdf`` → ``000-00-0``。"""
        self.assertEqual(extract_management_number("000-00-0実践.pdf"), "000-00-0")


class TestExtractClinicNumber(unittest.TestCase):
    """PDFファイル名先頭の管理番号からの医院番号抽出のテスト。

    医院番号 = 管理番号 ``NNN-NN-N`` の先頭セグメント（最初のハイフンより前、
    3〜5 桁）。例: ``001-01-0実践事例.pdf`` → ``001``。
    """

    def test_extracts_three_digit_clinic_number(self):
        """3桁医院番号 ``001-01-0実践事例.pdf`` → ``001``。"""
        self.assertEqual(
            extract_clinic_number("001-01-0実践事例タイトル.pdf"), "001"
        )

    def test_extracts_four_digit_clinic_number(self):
        """4桁医院番号 ``0012-34-5.pdf`` → ``0012``。"""
        self.assertEqual(extract_clinic_number("0012-34-5.pdf"), "0012")

    def test_extracts_five_digit_clinic_number(self):
        """5桁医院番号 ``00123-45-6.pdf`` → ``00123``。"""
        self.assertEqual(extract_clinic_number("00123-45-6.pdf"), "00123")

    def test_extracts_when_separated_by_underscore(self):
        """区切りがアンダースコア ``012-03-4_別の事例.pdf`` → ``012``。"""
        self.assertEqual(extract_clinic_number("012-03-4_別の事例.pdf"), "012")

    def test_returns_empty_when_no_management_number(self):
        """先頭が管理番号でない ``実践事例.pdf`` → 空文字列。"""
        self.assertEqual(extract_clinic_number("実践事例.pdf"), "")

    def test_returns_empty_when_first_group_too_short(self):
        """先頭グループが 2 桁 ``01-01-0.pdf`` → 空文字列。"""
        self.assertEqual(extract_clinic_number("01-01-0.pdf"), "")

    def test_returns_empty_when_first_group_six_digits(self):
        """先頭グループが 6 桁 ``000001-01-0.pdf`` → 空文字列（6桁は無効）。"""
        self.assertEqual(extract_clinic_number("000001-01-0.pdf"), "")

    def test_returns_empty_for_empty_string(self):
        """空文字列 → 空文字列。"""
        self.assertEqual(extract_clinic_number(""), "")

    def test_returns_empty_for_fullwidth_hyphen(self):
        """ハイフンが全角 ``００１ー０１ー０.pdf`` → 空文字列（半角のみ対象）。"""
        self.assertEqual(extract_clinic_number("００１ー０１ー０.pdf"), "")

    def test_non_string_input_is_coerced(self):
        """非文字列入力（int 等）は str に変換される。

        int ``1234567`` は ``"1234567"`` となり、``123`` の後にハイフンが
        ないためマッチせず空文字列を返す。
        """
        self.assertEqual(extract_clinic_number(1234567), "")  # type: ignore[arg-type]

    def test_clinic_number_matches_management_number_prefix(self):
        """医院番号は管理番号の先頭ハイフンより前の部分と一致する。"""
        filename = "00123-45-6実践事例.pdf"
        mgmt = extract_management_number(filename)
        clinic = extract_clinic_number(filename)
        self.assertEqual(mgmt, "00123-45-6")
        self.assertEqual(clinic, "00123")
        self.assertEqual(mgmt.split("-")[0], clinic)


class TestIsAttachmentFilename(unittest.TestCase):
    """添付資料ファイル名判定のテスト。

    ファイル名に「【添付資料】」（全角の隅付き括弧込み）を含む PDF は、
    実践事例の補足資料。AI 処理せずメインと同じ出力フォルダにコピーする対象。
    """

    def test_detects_marker_at_start(self):
        """先頭にマーカーがあるファイル名 → True。"""
        self.assertTrue(
            is_attachment_filename("【添付資料】001-01-0補足.pdf")
        )

    def test_detects_marker_in_middle(self):
        """ファイル名の途中にマーカーがあっても → True。"""
        self.assertTrue(
            is_attachment_filename("001-01-0【添付資料】補足データ.pdf")
        )

    def test_detects_marker_without_extension(self):
        """拡張子なしでもマーカーを含めば → True。"""
        self.assertTrue(is_attachment_filename("【添付資料】補足"))

    def test_returns_false_for_main_practice_case(self):
        """マーカーを含まないメイン実践事例 PDF → False。"""
        self.assertFalse(
            is_attachment_filename("001-01-0実践事例タイトル.pdf")
        )

    def test_returns_false_for_empty_string(self):
        """空文字列 → False。"""
        self.assertFalse(is_attachment_filename(""))

    def test_partial_bracket_does_not_match(self):
        """隅付き括弧が欠けた「添付資料」だけでは → False（全角【】が必須）。"""
        self.assertFalse(is_attachment_filename("001-01-0添付資料.pdf"))

    def test_halfwidth_bracket_does_not_match(self):
        """半角の括弧 [添付資料] では → False（全角の【】のみ対象）。"""
        self.assertFalse(is_attachment_filename("001-01-0[添付資料].pdf"))

    def test_non_string_input_is_coerced(self):
        """非文字列入力（int 等）は str に変換される。

        int ``12345`` は ``"12345"`` となり、マーカーを含まないため False。
        """
        self.assertFalse(is_attachment_filename(12345))  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
