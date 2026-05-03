"""utils モジュールのテスト。"""

import unittest

from src.utils import mask_email, sanitize_filename


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


if __name__ == "__main__":
    unittest.main()
