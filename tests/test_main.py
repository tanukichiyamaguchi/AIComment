"""main.py のエントリポイントテスト：``--profile`` 引数のパースと
プロファイル経由での各 client 呼び出しを検証する。
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

from src import main as main_module
from src.profile import ProfileConfig


def _make_profile(**overrides) -> ProfileConfig:
    """テスト用 ProfileConfig ファクトリ。"""
    defaults = dict(
        name="jissen_default",
        display_name="default",
        document_type="jissen_practice_case",
        period="default",
        input_folder_id="input_folder_xxx",
        output_folder_id="output_folder_yyy",
        output_sheet_name="出力一覧",
        management_number_prefix="",
        prompt_template="jissen_practice_case",
    )
    defaults.update(overrides)
    return ProfileConfig(**defaults)


class TestArgparseProfile(unittest.TestCase):

    def test_default_profile_is_jissen_default(self):
        """``--profile`` 省略時は ``jissen_default`` がデフォルト。"""
        with patch.object(sys, "argv", ["main.py"]), \
             patch.object(main_module, "run") as mock_run:
            main_module.main()
        kwargs = mock_run.call_args.kwargs
        self.assertEqual(kwargs["profile_name"], "jissen_default")
        self.assertEqual(kwargs["test_count"], 0)

    def test_explicit_profile_passed_to_run(self):
        """``--profile jissen_2024_q3`` が ``run()`` に伝搬する。"""
        with patch.object(
            sys, "argv", ["main.py", "--profile", "jissen_2024_q3"],
        ), patch.object(main_module, "run") as mock_run:
            main_module.main()
        kwargs = mock_run.call_args.kwargs
        self.assertEqual(kwargs["profile_name"], "jissen_2024_q3")

    def test_test_count_passed_to_run(self):
        with patch.object(
            sys, "argv", ["main.py", "--test-count", "3"],
        ), patch.object(main_module, "run") as mock_run:
            main_module.main()
        kwargs = mock_run.call_args.kwargs
        self.assertEqual(kwargs["test_count"], 3)


class TestRunUsesProfile(unittest.TestCase):
    """``run()`` がプロファイルから入力フォルダ・出力フォルダ・シート・prefix を受け取る。"""

    @patch("src.main.sheets_client")
    @patch("src.main.drive_client")
    @patch("src.main.load_profile")
    @patch("src.main.ensure_fonts")
    def test_passes_profile_input_folder_to_drive_client(
        self,
        mock_ensure_fonts,
        mock_load_profile,
        mock_drive_client,
        mock_sheets_client,
    ):
        mock_load_profile.return_value = _make_profile(
            input_folder_id="profile_input_id",
        )
        mock_drive_client.list_pdfs.return_value = []
        mock_sheets_client.get_max_management_number.return_value = 0

        main_module.run(test_count=0, profile_name="jissen_default")

        mock_drive_client.list_pdfs.assert_called_once_with(
            folder_id="profile_input_id",
        )

    @patch("src.main.sheets_client")
    @patch("src.main.drive_client")
    @patch("src.main.load_profile")
    @patch("src.main.ensure_fonts")
    def test_passes_profile_sheet_and_prefix_to_get_max_mgmt_number(
        self,
        mock_ensure_fonts,
        mock_load_profile,
        mock_drive_client,
        mock_sheets_client,
    ):
        mock_load_profile.return_value = _make_profile(
            output_sheet_name="実践事例_2024Q1_出力一覧",
            management_number_prefix="J24Q1-",
        )
        mock_drive_client.list_pdfs.return_value = []
        mock_sheets_client.get_max_management_number.return_value = 0

        main_module.run(test_count=0, profile_name="jissen_2024_q1")

        call_kwargs = mock_sheets_client.get_max_management_number.call_args.kwargs
        self.assertEqual(call_kwargs["sheet_name"], "実践事例_2024Q1_出力一覧")
        self.assertEqual(call_kwargs["prefix"], "J24Q1-")

    @patch("src.main.sheets_client")
    @patch("src.main.drive_client")
    @patch("src.main.load_profile")
    @patch("src.main.ensure_fonts")
    def test_default_profile_passes_empty_prefix_as_none(
        self,
        mock_ensure_fonts,
        mock_load_profile,
        mock_drive_client,
        mock_sheets_client,
    ):
        """既存挙動互換：jissen_default の prefix='' は None として渡される（純粋数値で集計）。"""
        mock_load_profile.return_value = _make_profile(
            management_number_prefix="",
        )
        mock_drive_client.list_pdfs.return_value = []
        mock_sheets_client.get_max_management_number.return_value = 0

        main_module.run(test_count=0, profile_name="jissen_default")

        call_kwargs = mock_sheets_client.get_max_management_number.call_args.kwargs
        # 既存テスト互換性のため prefix=None で渡す
        self.assertIsNone(call_kwargs["prefix"])


if __name__ == "__main__":
    unittest.main()
