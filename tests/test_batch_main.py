"""batch_main.py のエントリポイントテスト：``--profile`` のパースと、
プロファイルが各ステップ（特に Step1/Step4）に渡されることを検証。
"""

from __future__ import annotations

import sys
import unittest
from unittest.mock import patch

from src import batch_main
from src.profile import ProfileConfig


def _make_profile(**overrides) -> ProfileConfig:
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
        with patch.object(sys, "argv", ["batch_main.py"]), \
             patch.object(batch_main, "run") as mock_run:
            batch_main.main()
        kwargs = mock_run.call_args.kwargs
        self.assertEqual(kwargs["profile_name"], "jissen_default")

    def test_explicit_profile_passed_to_run(self):
        with patch.object(
            sys, "argv", ["batch_main.py", "--profile", "jissen_2024_q2"],
        ), patch.object(batch_main, "run") as mock_run:
            batch_main.main()
        kwargs = mock_run.call_args.kwargs
        self.assertEqual(kwargs["profile_name"], "jissen_2024_q2")

    def test_no_batch_falls_back_to_normal_with_profile(self):
        """``--no-batch`` 時は main.run にフォールバック、profile も伝搬する。"""
        with patch.object(
            sys, "argv",
            ["batch_main.py", "--no-batch", "--profile", "jissen_2024_q1"],
        ), patch("src.main.run") as mock_normal_run, \
             patch.object(batch_main, "load_profile"):
            batch_main.main()
        kwargs = mock_normal_run.call_args.kwargs
        self.assertEqual(kwargs["profile_name"], "jissen_2024_q1")


class TestStep1UsesProfile(unittest.TestCase):
    """Step1 がプロファイルの入力フォルダで Drive を見にいく。"""

    @patch("src.batch_main.drive_client")
    def test_step1_passes_profile_input_folder_to_list_pdfs(self, mock_drive):
        mock_drive.list_pdfs.return_value = []
        profile = _make_profile(input_folder_id="profile_input_id")

        batch_main.step1_prepare(profile, test_count=0)

        mock_drive.list_pdfs.assert_called_once_with(
            folder_id="profile_input_id",
        )


class TestStep4UsesProfile(unittest.TestCase):
    """Step4 がプロファイルの出力シート名 / prefix を sheets_client に渡す。"""

    @patch("src.batch_main.sheets_client")
    @patch("src.batch_main.ensure_fonts")
    def test_step4_passes_sheet_and_prefix_for_get_max(
        self, mock_fonts, mock_sheets,
    ):
        mock_sheets.get_max_management_number.return_value = 0
        profile = _make_profile(
            output_sheet_name="実践事例_2024Q3_出力一覧",
            management_number_prefix="J24Q3-",
        )

        # results / items が空でも管理番号採番起点の取得は走る
        batch_main.step4_generate_pdfs(profile, results={}, items=[])

        kwargs = mock_sheets.get_max_management_number.call_args.kwargs
        self.assertEqual(kwargs["sheet_name"], "実践事例_2024Q3_出力一覧")
        self.assertEqual(kwargs["prefix"], "J24Q3-")


if __name__ == "__main__":
    unittest.main()
