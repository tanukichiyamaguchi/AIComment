"""utils モジュールのテスト。"""

import unittest

from src.utils import mask_email, normalize_name_for_match, sanitize_filename


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


if __name__ == "__main__":
    unittest.main()
