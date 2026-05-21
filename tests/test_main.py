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


def _install_run_mocks(
    mock_drive, mock_gen, mock_reader, mock_creator, mock_merger,
    *, pdf_files: list[dict],
) -> None:
    """``main.run()`` の処理ループを 1 周以上回すための標準モック。"""
    mock_drive.list_pdfs.return_value = pdf_files
    mock_drive.download_pdf.return_value = b"%PDF-1.4 fake"
    mock_drive.upload_pdf_to_clinic_person.return_value = {
        "webViewLink": "https://drive.google.com/fake",
    }
    mock_reader.extract_text.return_value = "PDFテキスト"
    mock_gen.generate_comment_with_metadata.return_value = {
        "clinic_name": "山田歯科",
        "person_name": "田中太郎",
        "sample_title": "事例タイトル",
        "comment": "コメント本文",
    }
    mock_merger.make_output_filename.return_value = "out.pdf"


class TestRunUsesProfile(unittest.TestCase):
    """``run()`` がプロファイルから入力フォルダ・出力フォルダ・シートを受け取り、
    管理番号は PDF ファイル名先頭から抽出することを検証する。"""

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

        main_module.run(test_count=0, profile_name="jissen_default")

        mock_drive_client.list_pdfs.assert_called_once_with(
            folder_id="profile_input_id",
        )

    @patch("src.main.ensure_fonts")
    @patch("src.main.pdf_merger")
    @patch("src.main.pdf_creator")
    @patch("src.main.pdf_reader")
    @patch("src.main.comment_generator")
    @patch("src.main.sheets_client")
    @patch("src.main.drive_client")
    @patch("src.discover.load_profile")
    def test_passes_profile_sheet_name_to_append_record(
        self,
        mock_load_profile,
        mock_drive_client,
        mock_sheets_client,
        mock_gen,
        mock_reader,
        mock_creator,
        mock_merger,
        mock_ensure_fonts,
    ):
        """プロファイルの ``output_sheet_name`` が ``append_output_record`` に伝搬する。"""
        mock_load_profile.return_value = _make_profile(
            output_sheet_name="実践事例_2024Q1_出力一覧",
        )
        _install_run_mocks(
            mock_drive_client, mock_gen, mock_reader, mock_creator, mock_merger,
            pdf_files=[{"id": "id_1", "name": "001-01-0実践事例.pdf"}],
        )

        main_module.run(test_count=0, profile_name="jissen_2024_q1")

        call_kwargs = mock_sheets_client.append_output_record.call_args.kwargs
        self.assertEqual(call_kwargs["sheet_name"], "実践事例_2024Q1_出力一覧")

    @patch("src.main.ensure_fonts")
    @patch("src.main.pdf_merger")
    @patch("src.main.pdf_creator")
    @patch("src.main.pdf_reader")
    @patch("src.main.comment_generator")
    @patch("src.main.sheets_client")
    @patch("src.main.drive_client")
    @patch("src.discover.load_profile")
    def test_management_number_extracted_from_pdf_filename(
        self,
        mock_load_profile,
        mock_drive_client,
        mock_sheets_client,
        mock_gen,
        mock_reader,
        mock_creator,
        mock_merger,
        mock_ensure_fonts,
    ):
        """管理番号は PDF ファイル名先頭（NNN-NN-N）から抽出され、自動採番しない。"""
        mock_load_profile.return_value = _make_profile()
        _install_run_mocks(
            mock_drive_client, mock_gen, mock_reader, mock_creator, mock_merger,
            pdf_files=[
                {"id": "id_1", "name": "012-03-4実践事例タイトル.pdf"},
                {"id": "id_2", "name": "012-03-5_別の事例.pdf"},
            ],
        )

        main_module.run(test_count=0, profile_name="jissen_default")

        mgmt_nums = [
            c.kwargs["management_number"]
            for c in mock_sheets_client.append_output_record.call_args_list
        ]
        self.assertEqual(mgmt_nums, ["012-03-4", "012-03-5"])

    @patch("src.main.ensure_fonts")
    @patch("src.main.pdf_merger")
    @patch("src.main.pdf_creator")
    @patch("src.main.pdf_reader")
    @patch("src.main.comment_generator")
    @patch("src.main.sheets_client")
    @patch("src.main.drive_client")
    @patch("src.discover.load_profile")
    def test_unextractable_filename_yields_empty_with_warning(
        self,
        mock_load_profile,
        mock_drive_client,
        mock_sheets_client,
        mock_gen,
        mock_reader,
        mock_creator,
        mock_merger,
        mock_ensure_fonts,
    ):
        """先頭が NNN-NN-N でないファイルは管理番号が空文字列になり、warning が出る。"""
        mock_load_profile.return_value = _make_profile()
        _install_run_mocks(
            mock_drive_client, mock_gen, mock_reader, mock_creator, mock_merger,
            pdf_files=[{"id": "id_1", "name": "管理番号なし.pdf"}],
        )

        with self.assertLogs("jissen_comment", level="WARNING") as log_ctx:
            main_module.run(test_count=0, profile_name="jissen_default")

        call_kwargs = mock_sheets_client.append_output_record.call_args.kwargs
        self.assertEqual(call_kwargs["management_number"], "")
        # warning にファイル名が含まれる（サイレントにしない）
        joined = "\n".join(log_ctx.output)
        self.assertIn("管理番号なし.pdf", joined)


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
        - ``sheets_client.append_output_record`` も呼ばれない
        """
        mock_list_names.return_value = ["a", "b"]

        with patch("src.config.DRIVE_INPUT_ROOT", "fake_root_id"):
            main_module.run(target_folder="__list__")

        mock_list_names.assert_called_once_with("fake_root_id")
        mock_ensure_fonts.assert_not_called()
        mock_drive_client.list_pdfs.assert_not_called()
        mock_sheets_client.append_output_record.assert_not_called()

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

    @patch("src.main.ensure_fonts")
    @patch("src.main.pdf_merger")
    @patch("src.main.pdf_creator")
    @patch("src.main.pdf_reader")
    @patch("src.main.comment_generator")
    @patch("src.main.sheets_client")
    @patch("src.main.drive_client")
    @patch("src.discover.resolve_context")
    def test_target_folder_resolves_context_and_uses_it(
        self,
        mock_resolve_context,
        mock_drive_client,
        mock_sheets_client,
        mock_gen,
        mock_reader,
        mock_creator,
        mock_merger,
        mock_ensure_fonts,
    ):
        """``target_folder`` 指定時、``discover.resolve_context`` が呼ばれ、
        その戻り値の各 ID / シート名が ``drive_client`` / ``sheets_client``
        に正しく伝搬する。管理番号はファイル名先頭から抽出する。
        """
        mock_resolve_context.return_value = DiscoveredContext(
            target_folder_name="2024_Q1_実践事例",
            input_folder_id="auto_input_id",
            output_folder_id="auto_output_id",
            output_sheet_name="2024_Q1_実践事例",
        )
        _install_run_mocks(
            mock_drive_client, mock_gen, mock_reader, mock_creator, mock_merger,
            pdf_files=[{"id": "id_1", "name": "007-08-9実践事例.pdf"}],
        )

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

        # sheets_client.append_output_record が context のシート名と
        # ファイル名抽出の管理番号を受ける
        sheets_kwargs = mock_sheets_client.append_output_record.call_args.kwargs
        self.assertEqual(sheets_kwargs["sheet_name"], "2024_Q1_実践事例")
        self.assertEqual(sheets_kwargs["management_number"], "007-08-9")

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
        )
        mock_drive_client.list_pdfs.return_value = []

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

        main_module.run(test_count=0)

        mock_load_profile.assert_called_once_with("jissen_default")
        mock_resolve_context.assert_not_called()


if __name__ == "__main__":
    unittest.main()
