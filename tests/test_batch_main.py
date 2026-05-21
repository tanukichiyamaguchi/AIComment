"""batch_main.py のエントリポイントテスト：``--profile`` のパースと、
プロファイルが各ステップ（特に Step1/Step4）に渡されることを検証。
``--target-folder`` 引数の自動検出モードもここでテストする。
"""

from __future__ import annotations

import sys
import unittest
from unittest.mock import patch

from src import batch_main
from src.discover import DiscoveredContext
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
        prompt_template="jissen_practice_case",
    )
    defaults.update(overrides)
    return ProfileConfig(**defaults)


def _make_batch_items(filenames: list[str]) -> list[dict]:
    """step4_generate_pdfs 用の items リストを作る。"""
    return [
        {
            "custom_id": f"item_{i:04d}",
            "pdf_data_id": f"id_{i:04d}",
            "pdf_file_name": name,
        }
        for i, name in enumerate(filenames, start=1)
    ]


def _make_batch_results(count: int) -> dict[str, dict[str, str]]:
    """step4_generate_pdfs 用の results 辞書を作る。"""
    return {
        f"item_{i:04d}": {
            "clinic_name": "山田歯科",
            "person_name": "田中太郎",
            "sample_title": "事例タイトル",
            "comment": "コメント本文",
        }
        for i in range(1, count + 1)
    }


def _install_step4_mocks(mock_drive, mock_merger) -> None:
    """step4_generate_pdfs が PDF 生成ループを回すための標準モック。"""
    mock_drive.download_pdf.return_value = b"%PDF-1.4 fake"
    mock_drive.upload_pdf_to_clinic_person.return_value = {
        "webViewLink": "https://drive.google.com/fake",
    }
    mock_merger.make_output_filename.return_value = "out.pdf"


class TestArgparseProfile(unittest.TestCase):

    def test_default_profile_is_jissen_default(self):
        """``--profile`` ``--target-folder`` 両方省略時、``run()`` には
        ``profile_name=None`` / ``target_folder=None`` で渡り、``run()`` 内で
        ``jissen_default`` にフォールバックする（後方互換）。
        """
        with patch.object(sys, "argv", ["batch_main.py"]), \
             patch.object(batch_main, "run") as mock_run:
            batch_main.main()
        kwargs = mock_run.call_args.kwargs
        self.assertIsNone(kwargs["profile_name"])
        self.assertIsNone(kwargs["target_folder"])

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
        ), patch("src.main.run") as mock_normal_run:
            batch_main.main()
        kwargs = mock_normal_run.call_args.kwargs
        self.assertEqual(kwargs["profile_name"], "jissen_2024_q1")


class TestStep1UsesProfile(unittest.TestCase):
    """Step1 がプロファイルの入力フォルダで Drive を見にいく。"""

    @patch("src.batch_main.sheets_client")
    @patch("src.batch_main.drive_client")
    def test_step1_passes_profile_input_folder_to_list_pdfs(
        self, mock_drive, mock_sheets,
    ):
        mock_drive.list_pdfs.return_value = []
        mock_sheets.get_processed_management_numbers.return_value = set()
        profile = _make_profile(input_folder_id="profile_input_id")

        batch_main.step1_prepare(profile, test_count=0)

        mock_drive.list_pdfs.assert_called_once_with(
            folder_id="profile_input_id",
        )


def _install_step1_mocks(mock_drive, mock_reader) -> None:
    """step1_prepare の download → extract_text を成功させる標準モック。"""
    mock_drive.download_pdf.return_value = b"%PDF-1.4 fake"
    mock_reader.extract_text.return_value = "PDFテキスト"


class TestStep1IncrementalDedup(unittest.TestCase):
    """``step1_prepare`` の増分処理（重複検知）。

    スキップ対象（処理済み / 管理番号なし）は ``items`` に含めず、Batch API
    に投げない（コスト削減）。重複判定は download / Claude 投入の前に行う。
    """

    @patch("src.batch_main.pdf_reader")
    @patch("src.batch_main.sheets_client")
    @patch("src.batch_main.drive_client")
    def test_processed_pdf_excluded_from_items(
        self, mock_drive, mock_sheets, mock_reader,
    ):
        """処理済み管理番号の PDF は items に含まれず download もされない。"""
        mock_drive.list_pdfs.return_value = [
            {"id": "id_1", "name": "001-01-0既存.pdf"},
            {"id": "id_2", "name": "001-01-1新規.pdf"},
        ]
        mock_sheets.get_processed_management_numbers.return_value = {"001-01-0"}
        _install_step1_mocks(mock_drive, mock_reader)
        profile = _make_profile()

        items = batch_main.step1_prepare(profile, test_count=0)

        # 新規 1 件だけが items に入る
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["pdf_file_name"], "001-01-1新規.pdf")
        # 処理済みは download されない（Batch API 投入前にスキップ）
        mock_drive.download_pdf.assert_called_once_with("id_2")
        # 重複判定は出力シート単位
        mock_sheets.get_processed_management_numbers.assert_called_once_with(
            sheet_name="出力一覧",
        )

    @patch("src.batch_main.pdf_reader")
    @patch("src.batch_main.sheets_client")
    @patch("src.batch_main.drive_client")
    def test_no_management_number_pdf_excluded_from_items(
        self, mock_drive, mock_sheets, mock_reader,
    ):
        """管理番号なし PDF は items に含まれず warning が出る。"""
        mock_drive.list_pdfs.return_value = [
            {"id": "id_1", "name": "管理番号なし.pdf"},
            {"id": "id_2", "name": "001-01-1正常.pdf"},
        ]
        mock_sheets.get_processed_management_numbers.return_value = set()
        _install_step1_mocks(mock_drive, mock_reader)
        profile = _make_profile()

        with self.assertLogs("jissen_comment", level="WARNING") as log_ctx:
            items = batch_main.step1_prepare(profile, test_count=0)

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["pdf_file_name"], "001-01-1正常.pdf")
        self.assertIn("管理番号なし.pdf", "\n".join(log_ctx.output))

    @patch("src.batch_main.pdf_reader")
    @patch("src.batch_main.sheets_client")
    @patch("src.batch_main.drive_client")
    def test_all_processed_pdfs_excluded_unconditionally(
        self, mock_drive, mock_sheets, mock_reader,
    ):
        """全 PDF が処理済みなら無条件で items から除外され download もされない。

        重複スキップに bypass はない。再処理は出力シートの行を手動削除して行う。
        """
        mock_drive.list_pdfs.return_value = [
            {"id": "id_1", "name": "001-01-0既存.pdf"},
            {"id": "id_2", "name": "001-01-1既存.pdf"},
        ]
        mock_sheets.get_processed_management_numbers.return_value = {
            "001-01-0", "001-01-1",
        }
        _install_step1_mocks(mock_drive, mock_reader)
        profile = _make_profile()

        items = batch_main.step1_prepare(profile, test_count=0)

        # 処理済みは無条件除外 → items は空、download も呼ばれない
        self.assertEqual(len(items), 0)
        mock_drive.download_pdf.assert_not_called()

    @patch("src.batch_main.pdf_reader")
    @patch("src.batch_main.sheets_client")
    @patch("src.batch_main.drive_client")
    def test_test_count_applies_to_new_targets_only(
        self, mock_drive, mock_sheets, mock_reader,
    ):
        """``test_count`` は重複・管理番号なしを除外した新規 PDF に適用される。"""
        mock_drive.list_pdfs.return_value = [
            {"id": "id_0", "name": "001-01-0既存.pdf"},
            {"id": "id_x", "name": "管理番号なし.pdf"},
            {"id": "id_1", "name": "001-01-1新規.pdf"},
            {"id": "id_2", "name": "001-01-2新規.pdf"},
            {"id": "id_3", "name": "001-01-3新規.pdf"},
        ]
        mock_sheets.get_processed_management_numbers.return_value = {"001-01-0"}
        _install_step1_mocks(mock_drive, mock_reader)
        profile = _make_profile()

        with self.assertLogs("jissen_comment", level="WARNING"):
            items = batch_main.step1_prepare(profile, test_count=2)

        # 新規候補 3 件の先頭 2 件のみ
        self.assertEqual(len(items), 2)
        names = [it["pdf_file_name"] for it in items]
        self.assertEqual(names, ["001-01-1新規.pdf", "001-01-2新規.pdf"])

    @patch("src.batch_main.pdf_reader")
    @patch("src.batch_main.sheets_client")
    @patch("src.batch_main.drive_client")
    def test_dedup_uses_run_config_sheet_name(
        self, mock_drive, mock_sheets, mock_reader,
    ):
        """``RunConfig`` 経由でも ``output_sheet_name`` で重複判定する。"""
        mock_drive.list_pdfs.return_value = []
        mock_sheets.get_processed_management_numbers.return_value = set()
        _install_step1_mocks(mock_drive, mock_reader)
        cfg = batch_main.RunConfig(
            display_name="自動検出: X",
            input_folder_id="auto_in",
            output_folder_id="auto_out",
            output_sheet_name="X_sheet",
        )

        batch_main.step1_prepare(cfg, test_count=0)

        mock_sheets.get_processed_management_numbers.assert_called_once_with(
            sheet_name="X_sheet",
        )


class TestStep4UsesProfile(unittest.TestCase):
    """Step4 がプロファイルの出力シート名を sheets_client に渡し、
    管理番号は PDF ファイル名先頭から抽出する。"""

    @patch("src.batch_main.pdf_merger")
    @patch("src.batch_main.pdf_creator")
    @patch("src.batch_main.drive_client")
    @patch("src.batch_main.sheets_client")
    @patch("src.batch_main.ensure_fonts")
    def test_step4_passes_sheet_name_to_append_record(
        self, mock_fonts, mock_sheets, mock_drive, mock_creator, mock_merger,
    ):
        profile = _make_profile(output_sheet_name="実践事例_2024Q3_出力一覧")
        _install_step4_mocks(mock_drive, mock_merger)

        batch_main.step4_generate_pdfs(
            profile,
            results=_make_batch_results(1),
            items=_make_batch_items(["050-06-7実践事例.pdf"]),
        )

        kwargs = mock_sheets.append_output_record.call_args.kwargs
        self.assertEqual(kwargs["sheet_name"], "実践事例_2024Q3_出力一覧")

    @patch("src.batch_main.pdf_merger")
    @patch("src.batch_main.pdf_creator")
    @patch("src.batch_main.drive_client")
    @patch("src.batch_main.sheets_client")
    @patch("src.batch_main.ensure_fonts")
    def test_step4_management_number_extracted_from_filename(
        self, mock_fonts, mock_sheets, mock_drive, mock_creator, mock_merger,
    ):
        """管理番号は ``pdf_file_name`` 先頭（NNN-NN-N）から抽出され、自動採番しない。"""
        profile = _make_profile()
        _install_step4_mocks(mock_drive, mock_merger)

        batch_main.step4_generate_pdfs(
            profile,
            results=_make_batch_results(2),
            items=_make_batch_items(
                ["111-22-3事例A.pdf", "111-22-4_事例B.pdf"]
            ),
        )

        mgmt_nums = [
            c.kwargs["management_number"]
            for c in mock_sheets.append_output_record.call_args_list
        ]
        self.assertEqual(mgmt_nums, ["111-22-3", "111-22-4"])

    @patch("src.batch_main.pdf_merger")
    @patch("src.batch_main.pdf_creator")
    @patch("src.batch_main.drive_client")
    @patch("src.batch_main.sheets_client")
    @patch("src.batch_main.ensure_fonts")
    def test_step4_unextractable_filename_yields_empty_with_warning(
        self, mock_fonts, mock_sheets, mock_drive, mock_creator, mock_merger,
    ):
        """先頭が NNN-NN-N でないファイルは管理番号が空文字列になり warning が出る。"""
        profile = _make_profile()
        _install_step4_mocks(mock_drive, mock_merger)

        with self.assertLogs("jissen_comment", level="WARNING") as log_ctx:
            batch_main.step4_generate_pdfs(
                profile,
                results=_make_batch_results(1),
                items=_make_batch_items(["管理番号なし.pdf"]),
            )

        kwargs = mock_sheets.append_output_record.call_args.kwargs
        self.assertEqual(kwargs["management_number"], "")
        self.assertIn("管理番号なし.pdf", "\n".join(log_ctx.output))


class TestArgparseTargetFolder(unittest.TestCase):
    """``--target-folder`` 引数のパースと ``--profile`` との排他制御。"""

    def test_target_folder_passed_to_run(self):
        with patch.object(
            sys, "argv", ["batch_main.py", "--target-folder", "2024_Q1_実践事例"],
        ), patch.object(batch_main, "run") as mock_run:
            batch_main.main()
        kwargs = mock_run.call_args.kwargs
        self.assertEqual(kwargs["target_folder"], "2024_Q1_実践事例")
        self.assertIsNone(kwargs["profile_name"])

    def test_target_folder_list_marker_passed(self):
        with patch.object(
            sys, "argv", ["batch_main.py", "--target-folder", "__list__"],
        ), patch.object(batch_main, "run") as mock_run:
            batch_main.main()
        kwargs = mock_run.call_args.kwargs
        self.assertEqual(kwargs["target_folder"], "__list__")

    def test_profile_and_target_folder_are_mutually_exclusive(self):
        with patch.object(
            sys, "argv",
            ["batch_main.py", "--profile", "jissen_2024_q1",
             "--target-folder", "anything"],
        ):
            with self.assertRaises(SystemExit):
                batch_main.main()

    def test_no_batch_passes_target_folder_to_normal_run(self):
        """``--no-batch --target-folder X`` で main.run に target_folder が伝搬する。"""
        with patch.object(
            sys, "argv",
            ["batch_main.py", "--no-batch", "--target-folder", "X"],
        ), patch("src.main.run") as mock_normal_run:
            batch_main.main()
        kwargs = mock_normal_run.call_args.kwargs
        self.assertEqual(kwargs["target_folder"], "X")
        self.assertIsNone(kwargs["profile_name"])


class TestRunListMode(unittest.TestCase):
    """``--target-folder __list__`` で候補列挙して即終了する。"""

    @patch("src.discover.list_target_folder_names")
    def test_list_mode_returns_before_processing(self, mock_list_names):
        mock_list_names.return_value = ["a", "b"]

        with patch("src.config.DRIVE_INPUT_ROOT", "fake_root_id"):
            batch_main.run(target_folder="__list__")

        mock_list_names.assert_called_once_with("fake_root_id")

    @patch("src.discover.list_target_folder_names")
    def test_list_mode_returns_when_input_root_not_set(self, mock_list_names):
        with patch("src.config.DRIVE_INPUT_ROOT", ""):
            batch_main.run(target_folder="__list__")
        mock_list_names.assert_not_called()


class TestRunUsesTargetFolder(unittest.TestCase):
    """``--target-folder`` 指定時、``resolve_context`` 経由で設定が解決される。"""

    @patch("src.batch_main.sheets_client")
    @patch("src.batch_main.drive_client")
    @patch("src.discover.resolve_context")
    @patch("src.batch_main.ensure_fonts")
    def test_target_folder_resolves_context_and_step1_uses_it(
        self,
        mock_ensure_fonts,
        mock_resolve_context,
        mock_drive,
        mock_sheets,
    ):
        """``target_folder`` 指定時、Step1 の list_pdfs に自動検出した input ID が渡る。"""
        mock_resolve_context.return_value = DiscoveredContext(
            target_folder_name="X",
            input_folder_id="auto_input",
            output_folder_id="auto_output",
            output_sheet_name="X",
        )
        mock_drive.list_pdfs.return_value = []

        with patch("src.config.DRIVE_INPUT_ROOT", "in_root"), \
             patch("src.config.DRIVE_OUTPUT_ROOT", "out_root"), \
             patch("src.config.SPREADSHEET_ID", "ssid"):
            # step="prepare" で Step1 だけ走らせる
            batch_main.run(
                batch_mode=True,
                test_count=0,
                step="prepare",
                target_folder="X",
            )

        mock_drive.list_pdfs.assert_called_once_with(folder_id="auto_input")

    @patch("src.batch_main.sheets_client")
    @patch("src.batch_main.drive_client")
    @patch("src.discover.load_profile")
    @patch("src.discover.resolve_context")
    @patch("src.batch_main.ensure_fonts")
    def test_target_folder_overrides_profile(
        self,
        mock_ensure_fonts,
        mock_resolve_context,
        mock_load_profile,
        mock_drive,
        mock_sheets,
    ):
        mock_resolve_context.return_value = DiscoveredContext(
            target_folder_name="X",
            input_folder_id="x_in",
            output_folder_id="x_out",
            output_sheet_name="X",
        )
        mock_drive.list_pdfs.return_value = []

        with patch("src.config.DRIVE_INPUT_ROOT", "ir"), \
             patch("src.config.DRIVE_OUTPUT_ROOT", "or"), \
             patch("src.config.SPREADSHEET_ID", "ssid"):
            batch_main.run(
                batch_mode=True,
                test_count=0,
                step="prepare",
                target_folder="X",
            )

        mock_load_profile.assert_not_called()
        mock_resolve_context.assert_called_once()


class TestStep4WithRunConfig(unittest.TestCase):
    """``step4_generate_pdfs`` が ``RunConfig`` ベースの設定を受け付ける。"""

    @patch("src.batch_main.pdf_merger")
    @patch("src.batch_main.pdf_creator")
    @patch("src.batch_main.drive_client")
    @patch("src.batch_main.sheets_client")
    @patch("src.batch_main.ensure_fonts")
    def test_step4_accepts_run_config(
        self, mock_fonts, mock_sheets, mock_drive, mock_creator, mock_merger,
    ):
        """``RunConfig`` が ``ProfileConfig`` と同じインターフェースで使える。"""
        _install_step4_mocks(mock_drive, mock_merger)
        cfg = batch_main.RunConfig(
            display_name="自動検出: X",
            input_folder_id="auto_in",
            output_folder_id="auto_out",
            output_sheet_name="X_sheet",
        )

        batch_main.step4_generate_pdfs(
            cfg,
            results=_make_batch_results(1),
            items=_make_batch_items(["200-30-4実践事例.pdf"]),
        )

        kwargs = mock_sheets.append_output_record.call_args.kwargs
        self.assertEqual(kwargs["sheet_name"], "X_sheet")
        self.assertEqual(kwargs["management_number"], "200-30-4")


if __name__ == "__main__":
    unittest.main()
