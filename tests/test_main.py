"""main.py のエントリポイントテスト：``--profile`` 引数のパースと
プロファイル経由での各 client 呼び出しを検証する。
``--target-folder`` 引数の自動検出モードもここでテストする。
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

from src import main as main_module
from src.discover import DiscoveredContext
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
        """``--profile`` ``--target-folder`` 両方省略時、``run()`` には
        ``profile_name=None`` / ``target_folder=None`` で渡り、``run()`` 内で
        ``jissen_default`` にフォールバックする（後方互換）。
        """
        with patch.object(sys, "argv", ["main.py"]), \
             patch.object(main_module, "run") as mock_run:
            main_module.main()
        kwargs = mock_run.call_args.kwargs
        self.assertIsNone(kwargs["profile_name"])
        self.assertIsNone(kwargs["target_folder"])
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
    @patch("src.discover.load_profile")
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
    @patch("src.discover.load_profile")
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
    @patch("src.discover.load_profile")
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


class TestArgparseTargetFolder(unittest.TestCase):
    """``--target-folder`` 引数のパースと ``--profile`` との排他制御。"""

    def test_target_folder_passed_to_run(self):
        """``--target-folder 2024_Q1`` が ``run()`` に伝搬する。"""
        with patch.object(
            sys, "argv", ["main.py", "--target-folder", "2024_Q1_実践事例"],
        ), patch.object(main_module, "run") as mock_run:
            main_module.main()
        kwargs = mock_run.call_args.kwargs
        self.assertEqual(kwargs["target_folder"], "2024_Q1_実践事例")
        self.assertIsNone(kwargs["profile_name"])

    def test_target_folder_list_marker_passed(self):
        """``--target-folder __list__`` は run() にそのまま渡る（run 内で分岐）。"""
        with patch.object(
            sys, "argv", ["main.py", "--target-folder", "__list__"],
        ), patch.object(main_module, "run") as mock_run:
            main_module.main()
        kwargs = mock_run.call_args.kwargs
        self.assertEqual(kwargs["target_folder"], "__list__")

    def test_profile_and_target_folder_are_mutually_exclusive(self):
        """``--profile`` と ``--target-folder`` の同時指定は argparse がエラー終了。"""
        with patch.object(
            sys, "argv",
            ["main.py", "--profile", "jissen_2024_q1",
             "--target-folder", "anything"],
        ):
            with self.assertRaises(SystemExit):
                main_module.main()

    def test_test_count_passes_alongside_target_folder(self):
        with patch.object(
            sys, "argv",
            ["main.py", "--target-folder", "x", "--test-count", "7"],
        ), patch.object(main_module, "run") as mock_run:
            main_module.main()
        kwargs = mock_run.call_args.kwargs
        self.assertEqual(kwargs["target_folder"], "x")
        self.assertEqual(kwargs["test_count"], 7)


class TestRunListMode(unittest.TestCase):
    """``--target-folder __list__`` で候補列挙して即終了する。"""

    @patch("src.main.drive_client")
    @patch("src.main.sheets_client")
    @patch("src.discover.list_target_folder_names")
    @patch("src.main.ensure_fonts")
    def test_list_mode_returns_before_processing(
        self,
        mock_ensure_fonts,
        mock_list_names,
        mock_sheets_client,
        mock_drive_client,
    ):
        """``__list__`` モードは ``list_target_folder_names`` を呼んだ後 return。

        - ``ensure_fonts`` は呼ばれない（フォント DL は不要）
        - ``drive_client.list_pdfs`` は呼ばれない
        - ``sheets_client.get_max_management_number`` も呼ばれない
        """
        mock_list_names.return_value = ["a", "b"]

        with patch("src.config.DRIVE_INPUT_ROOT", "fake_root_id"):
            main_module.run(target_folder="__list__")

        mock_list_names.assert_called_once_with("fake_root_id")
        mock_ensure_fonts.assert_not_called()
        mock_drive_client.list_pdfs.assert_not_called()
        mock_sheets_client.get_max_management_number.assert_not_called()

    @patch("src.main.drive_client")
    @patch("src.main.sheets_client")
    @patch("src.discover.list_target_folder_names")
    @patch("src.main.ensure_fonts")
    def test_list_mode_returns_when_input_root_not_set(
        self,
        mock_ensure_fonts,
        mock_list_names,
        mock_sheets_client,
        mock_drive_client,
    ):
        """DRIVE_INPUT_ROOT が空なら early return（クラッシュしない）。"""
        with patch("src.config.DRIVE_INPUT_ROOT", ""):
            main_module.run(target_folder="__list__")

        mock_list_names.assert_not_called()
        mock_drive_client.list_pdfs.assert_not_called()


class TestRunUsesTargetFolder(unittest.TestCase):
    """``--target-folder`` 指定時、``resolve_context`` 経由で設定が解決される。"""

    @patch("src.main.sheets_client")
    @patch("src.main.drive_client")
    @patch("src.discover.resolve_context")
    @patch("src.main.ensure_fonts")
    def test_target_folder_resolves_context_and_uses_it(
        self,
        mock_ensure_fonts,
        mock_resolve_context,
        mock_drive_client,
        mock_sheets_client,
    ):
        """``target_folder`` 指定時、``discover.resolve_context`` が呼ばれ、
        その戻り値の各 ID / シート名 / prefix が ``drive_client`` / ``sheets_client``
        に正しく伝搬する。
        """
        mock_resolve_context.return_value = DiscoveredContext(
            target_folder_name="2024_Q1_実践事例",
            input_folder_id="auto_input_id",
            output_folder_id="auto_output_id",
            output_sheet_name="2024_Q1_実践事例",
            management_number_prefix="2024_Q1_実践事例-",
        )
        mock_drive_client.list_pdfs.return_value = []
        mock_sheets_client.get_max_management_number.return_value = 0

        with patch("src.config.DRIVE_INPUT_ROOT", "input_root"), \
             patch("src.config.DRIVE_OUTPUT_ROOT", "output_root"), \
             patch("src.config.SPREADSHEET_ID", "sheet_xxx"):
            main_module.run(test_count=0, target_folder="2024_Q1_実践事例")

        # resolve_context が target_folder と 3 つの ROOT/ID で呼ばれた
        resolve_kwargs = mock_resolve_context.call_args.kwargs
        self.assertEqual(resolve_kwargs["target_folder"], "2024_Q1_実践事例")
        self.assertEqual(resolve_kwargs["input_root_id"], "input_root")
        self.assertEqual(resolve_kwargs["output_root_id"], "output_root")
        self.assertEqual(resolve_kwargs["spreadsheet_id"], "sheet_xxx")

        # drive_client.list_pdfs が context の input_folder_id を受け取る
        mock_drive_client.list_pdfs.assert_called_once_with(
            folder_id="auto_input_id",
        )

        # sheets_client.get_max_management_number が context のシート / prefix を受ける
        sheets_kwargs = mock_sheets_client.get_max_management_number.call_args.kwargs
        self.assertEqual(sheets_kwargs["sheet_name"], "2024_Q1_実践事例")
        self.assertEqual(sheets_kwargs["prefix"], "2024_Q1_実践事例-")

    @patch("src.main.sheets_client")
    @patch("src.main.drive_client")
    @patch("src.discover.load_profile")
    @patch("src.discover.resolve_context")
    @patch("src.main.ensure_fonts")
    def test_target_folder_overrides_profile(
        self,
        mock_ensure_fonts,
        mock_resolve_context,
        mock_load_profile,
        mock_drive_client,
        mock_sheets_client,
    ):
        """``target_folder`` 指定時は ``load_profile`` は呼ばれない（profile より優先）。"""
        mock_resolve_context.return_value = DiscoveredContext(
            target_folder_name="x",
            input_folder_id="x_in",
            output_folder_id="x_out",
            output_sheet_name="x",
            management_number_prefix="x-",
        )
        mock_drive_client.list_pdfs.return_value = []
        mock_sheets_client.get_max_management_number.return_value = 0

        with patch("src.config.DRIVE_INPUT_ROOT", "ir"), \
             patch("src.config.DRIVE_OUTPUT_ROOT", "or"), \
             patch("src.config.SPREADSHEET_ID", "sid"):
            main_module.run(
                test_count=0,
                profile_name=None,
                target_folder="x",
            )

        mock_load_profile.assert_not_called()
        mock_resolve_context.assert_called_once()

    @patch("src.main.sheets_client")
    @patch("src.main.drive_client")
    @patch("src.discover.load_profile")
    @patch("src.discover.resolve_context")
    @patch("src.main.ensure_fonts")
    def test_no_target_folder_no_profile_uses_jissen_default(
        self,
        mock_ensure_fonts,
        mock_resolve_context,
        mock_load_profile,
        mock_drive_client,
        mock_sheets_client,
    ):
        """両引数省略時は ``load_profile("jissen_default")`` が呼ばれる（既存挙動）。"""
        mock_load_profile.return_value = _make_profile()
        mock_drive_client.list_pdfs.return_value = []
        mock_sheets_client.get_max_management_number.return_value = 0

        main_module.run(test_count=0)

        mock_load_profile.assert_called_once_with("jissen_default")
        mock_resolve_context.assert_not_called()


if __name__ == "__main__":
    unittest.main()
