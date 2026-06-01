"""config モジュールのテスト（_get_bool と ENABLE_GMAIL_DRAFTS トグル）。"""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from src import config

_KEY = "X_FLAG_TEST_ONLY"


class TestGetBool(unittest.TestCase):
    """``config._get_bool``：GitHub Actions の boolean 入力（文字列）を緩く解釈。"""

    def setUp(self):
        os.environ.pop(_KEY, None)

    def tearDown(self):
        os.environ.pop(_KEY, None)

    def test_unset_returns_default(self):
        self.assertTrue(config._get_bool(_KEY, default=True))
        self.assertFalse(config._get_bool(_KEY, default=False))

    def test_truthy_values(self):
        for v in ("true", "True", "1", "yes", "on", "TRUE", "anything"):
            with patch.dict(os.environ, {_KEY: v}):
                self.assertTrue(
                    config._get_bool(_KEY, default=False), f"{v!r} は真であるべき"
                )

    def test_falsy_values(self):
        for v in ("false", "False", "0", "no", "off", "  OFF  ", "FALSE"):
            with patch.dict(os.environ, {_KEY: v}):
                self.assertFalse(
                    config._get_bool(_KEY, default=True), f"{v!r} は偽であるべき"
                )

    def test_empty_string_uses_default(self):
        with patch.dict(os.environ, {_KEY: ""}):
            self.assertTrue(config._get_bool(_KEY, default=True))
            self.assertFalse(config._get_bool(_KEY, default=False))


class TestEnableGmailDraftsDefault(unittest.TestCase):
    """ENABLE_GMAIL_DRAFTS は未設定なら True（後方互換）。"""

    def test_default_true_when_unset(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ENABLE_GMAIL_DRAFTS", None)
            self.assertTrue(config._get_bool("ENABLE_GMAIL_DRAFTS", default=True))

    def test_false_when_env_false(self):
        with patch.dict(os.environ, {"ENABLE_GMAIL_DRAFTS": "false"}):
            self.assertFalse(config._get_bool("ENABLE_GMAIL_DRAFTS", default=True))


if __name__ == "__main__":
    unittest.main()
