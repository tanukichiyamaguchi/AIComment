"""batch_main.py のエントリポイントテスト：``--profile`` のパースと、
プロファイルが各ステップ（特に Step1/Step4）に渡されることを検証。
``--target-folder`` 引数の自動検出モードもここでテストする。
"""

from __future__ import annotations

import json
import sys
import unittest
from unittest.mock import MagicMock, patch

import anthropic

from src import batch_main
from src.comment_generator import PermanentRunFailureError
from src.config import LOGS_DIR
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


def _install_step4_mocks(mock_drive, mock_merger, mock_sheets=None) -> None:
    """step4_generate_pdfs が PDF 生成ループを回すための標準モック。

    ``mock_sheets`` が渡された場合、参加者マスター系のデフォルト戻り値
    （未登録扱いで AI 抽出値にフォールバック / メール未登録）も同時設定する。
    """
    mock_drive.download_pdf.return_value = b"%PDF-1.4 fake"
    mock_drive.upload_pdf_to_clinic_person.return_value = {
        "webViewLink": "https://drive.google.com/fake",
        "clinic_folder_id": "clinic_folder_fake",
    }
    mock_merger.make_output_filename.return_value = "out.pdf"
    if mock_sheets is not None:
        # 既定: マスター未登録 → AI 抽出値で代用 / メール未登録 → 宛先空で下書き
        mock_sheets.read_master_records.return_value = []
        mock_sheets.lookup_clinic_name.return_value = ""
        mock_sheets.lookup_email_by_clinic_and_person.return_value = ""


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
        _install_step4_mocks(mock_drive, mock_merger, mock_sheets)

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
        _install_step4_mocks(mock_drive, mock_merger, mock_sheets)

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
        _install_step4_mocks(mock_drive, mock_merger, mock_sheets)

        with self.assertLogs("jissen_comment", level="WARNING") as log_ctx:
            batch_main.step4_generate_pdfs(
                profile,
                results=_make_batch_results(1),
                items=_make_batch_items(["管理番号なし.pdf"]),
            )

        kwargs = mock_sheets.append_output_record.call_args.kwargs
        self.assertEqual(kwargs["management_number"], "")
        self.assertIn("管理番号なし.pdf", "\n".join(log_ctx.output))


class TestStep4ClinicNumberFolder(unittest.TestCase):
    """``step4_generate_pdfs`` の医院番号付きフォルダ名 + 医院フォルダURLシート。

    医院フォルダ名は ``<医院番号>_<医院名>``。医院フォルダURLシート
    （``<出力シート名>_医院``）に医院を記録し、同一医院は 1 行のみ。
    """

    @patch("src.batch_main.pdf_merger")
    @patch("src.batch_main.pdf_creator")
    @patch("src.batch_main.drive_client")
    @patch("src.batch_main.sheets_client")
    @patch("src.batch_main.ensure_fonts")
    def test_clinic_number_and_name_passed_separately(
        self, mock_fonts, mock_sheets, mock_drive, mock_creator, mock_merger,
    ):
        """``upload_pdf_to_clinic_person`` に医院番号と医院名が別引数で渡る
        （P-019: 医院フォルダの識別は医院番号のみ、医院名は AI 抽出の生の値）。"""
        profile = _make_profile()
        mock_sheets.get_recorded_clinic_numbers.return_value = set()
        _install_step4_mocks(mock_drive, mock_merger, mock_sheets)

        batch_main.step4_generate_pdfs(
            profile,
            results=_make_batch_results(1),
            items=_make_batch_items(["111-22-3実践事例.pdf"]),
        )

        upload_kwargs = mock_drive.upload_pdf_to_clinic_person.call_args.kwargs
        self.assertEqual(upload_kwargs["clinic_number"], "111")
        self.assertEqual(upload_kwargs["clinic_name"], "山田歯科")

    @patch("src.batch_main.pdf_merger")
    @patch("src.batch_main.pdf_creator")
    @patch("src.batch_main.drive_client")
    @patch("src.batch_main.sheets_client")
    @patch("src.batch_main.ensure_fonts")
    def test_clinic_folder_url_recorded_in_clinic_sheet(
        self, mock_fonts, mock_sheets, mock_drive, mock_creator, mock_merger,
    ):
        """医院フォルダURLシートに医院番号 / 医院名 / フォルダURL が記録される。"""
        profile = _make_profile(output_sheet_name="出力一覧")
        mock_sheets.get_recorded_clinic_numbers.return_value = set()
        mock_sheets.read_master_records.return_value = []
        mock_sheets.lookup_clinic_name.return_value = ""
        mock_sheets.lookup_email_by_clinic_and_person.return_value = ""
        mock_drive.download_pdf.return_value = b"%PDF-1.4 fake"
        mock_drive.upload_pdf_to_clinic_person.return_value = {
            "webViewLink": "https://drive.google.com/fake",
            "clinic_folder_id": "clinic_abc",
        }
        mock_merger.make_output_filename.return_value = "out.pdf"

        batch_main.step4_generate_pdfs(
            profile,
            results=_make_batch_results(1),
            items=_make_batch_items(["111-22-3実践事例.pdf"]),
        )

        # 医院シート名は <出力シート名>_医院
        snapshot_call = mock_sheets.get_recorded_clinic_numbers.call_args.kwargs
        self.assertEqual(snapshot_call["sheet_name"], "出力一覧_医院")
        rec_call = mock_sheets.append_clinic_folder_record.call_args.kwargs
        self.assertEqual(rec_call["clinic_number"], "111")
        self.assertEqual(rec_call["clinic_name"], "山田歯科")
        self.assertEqual(
            rec_call["clinic_folder_url"],
            "https://drive.google.com/drive/folders/clinic_abc",
        )
        self.assertEqual(rec_call["sheet_name"], "出力一覧_医院")

    @patch("src.batch_main.pdf_merger")
    @patch("src.batch_main.pdf_creator")
    @patch("src.batch_main.drive_client")
    @patch("src.batch_main.sheets_client")
    @patch("src.batch_main.ensure_fonts")
    def test_same_clinic_recorded_only_once(
        self, mock_fonts, mock_sheets, mock_drive, mock_creator, mock_merger,
    ):
        """同一医院番号の PDF が複数あっても医院シートには 1 行のみ記録される。"""
        profile = _make_profile()
        mock_sheets.get_recorded_clinic_numbers.return_value = set()
        _install_step4_mocks(mock_drive, mock_merger, mock_sheets)

        # 医院番号 200 が 2 件、300 が 1 件
        batch_main.step4_generate_pdfs(
            profile,
            results=_make_batch_results(3),
            items=_make_batch_items(
                ["200-01-0事例A.pdf", "200-01-1事例B.pdf", "300-02-0事例C.pdf"]
            ),
        )

        recorded = [
            c.kwargs["clinic_number"]
            for c in mock_sheets.append_clinic_folder_record.call_args_list
        ]
        self.assertEqual(sorted(recorded), ["200", "300"])

    @patch("src.batch_main.pdf_merger")
    @patch("src.batch_main.pdf_creator")
    @patch("src.batch_main.drive_client")
    @patch("src.batch_main.sheets_client")
    @patch("src.batch_main.ensure_fonts")
    def test_already_recorded_clinic_not_appended_again(
        self, mock_fonts, mock_sheets, mock_drive, mock_creator, mock_merger,
    ):
        """医院シートに既に記録済みの医院番号は再追記されない。"""
        profile = _make_profile()
        # 医院番号 111 は前回実行で記録済み
        mock_sheets.get_recorded_clinic_numbers.return_value = {"111"}
        _install_step4_mocks(mock_drive, mock_merger, mock_sheets)

        batch_main.step4_generate_pdfs(
            profile,
            results=_make_batch_results(1),
            items=_make_batch_items(["111-22-3実践事例.pdf"]),
        )

        mock_sheets.append_output_record.assert_called_once()
        mock_sheets.append_clinic_folder_record.assert_not_called()


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
        _install_step4_mocks(mock_drive, mock_merger, mock_sheets)
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


class TestStep1AttachmentClassification(unittest.TestCase):
    """``step1_prepare`` の添付資料分類。

    ファイル名に「【添付資料】」を含む PDF は Claude API に投げず、
    ``items``（バッチ投入対象）には含めない。添付資料の情報は
    ``batch_attachments.json`` に別途保存する。
    """

    def setUp(self):
        # batch_prep.json / batch_attachments.json の汚染を避けるためバックアップ
        self._prep_file = LOGS_DIR / "batch_prep.json"
        self._att_file = LOGS_DIR / "batch_attachments.json"
        self._prep_backup = (
            self._prep_file.read_text() if self._prep_file.exists() else None
        )
        self._att_backup = (
            self._att_file.read_text() if self._att_file.exists() else None
        )

    def tearDown(self):
        for path, backup in (
            (self._prep_file, self._prep_backup),
            (self._att_file, self._att_backup),
        ):
            if backup is None:
                path.unlink(missing_ok=True)
            else:
                path.write_text(backup)

    @patch("src.batch_main.pdf_reader")
    @patch("src.batch_main.sheets_client")
    @patch("src.batch_main.drive_client")
    def test_attachment_excluded_from_batch_items(
        self, mock_drive, mock_sheets, mock_reader,
    ):
        """添付資料は items に含まれず（Batch API に投げられず）、
        batch_attachments.json に保存される。"""
        mock_drive.list_pdfs.return_value = [
            {"id": "id_main", "name": "001-01-0実践事例.pdf"},
            {"id": "id_att", "name": "001-01-0【添付資料】補足.pdf"},
        ]
        mock_sheets.get_processed_management_numbers.return_value = set()
        _install_step1_mocks(mock_drive, mock_reader)
        profile = _make_profile()

        items = batch_main.step1_prepare(profile, test_count=0)

        # メイン 1 件だけが items に入る（添付資料は除外）
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["pdf_file_name"], "001-01-0実践事例.pdf")
        # 添付資料は download されない（Claude 投入経路に乗らない）
        mock_drive.download_pdf.assert_called_once_with("id_main")
        # batch_attachments.json に添付資料情報が保存される
        records = json.loads(self._att_file.read_text())
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["file_id"], "id_att")
        self.assertEqual(records[0]["file_name"], "001-01-0【添付資料】補足.pdf")
        self.assertEqual(records[0]["management_number"], "001-01-0")

    @patch("src.batch_main.pdf_reader")
    @patch("src.batch_main.sheets_client")
    @patch("src.batch_main.drive_client")
    def test_processed_attachment_not_saved(
        self, mock_drive, mock_sheets, mock_reader,
    ):
        """記録済みマーカーを持つ添付資料は batch_attachments.json に保存されない。

        重複判定は「メインの管理番号が処理済みか」ではなく「その添付資料自体が
        出力一覧シートに ``【添付資料】<元名>`` で記録済みか」で行う（メイン
        処理後に添付だけ後から追加されたケースの恒久ロスト防止）。
        """
        mock_drive.list_pdfs.return_value = [
            {"id": "id_att", "name": "001-01-0【添付資料】補足.pdf"},
        ]
        mock_sheets.get_processed_management_numbers.return_value = {"001-01-0"}
        mock_sheets.get_recorded_attachment_names.return_value = {
            "【添付資料】001-01-0【添付資料】補足.pdf",
        }
        _install_step1_mocks(mock_drive, mock_reader)
        profile = _make_profile()

        batch_main.step1_prepare(profile, test_count=0)

        records = json.loads(self._att_file.read_text())
        self.assertEqual(records, [])

    @patch("src.batch_main.pdf_reader")
    @patch("src.batch_main.sheets_client")
    @patch("src.batch_main.drive_client")
    def test_attachment_of_processed_main_still_saved_when_not_recorded(
        self, mock_drive, mock_sheets, mock_reader,
    ):
        """メインが処理済みでも、添付自体が未記録なら保存対象になる。

        (1) メイン処理後に添付だけ後から Drive に追加、(2) メイン記録後・
        添付コピー前のクラッシュ再実行、のどちらでも添付資料を取りこぼさない。
        """
        mock_drive.list_pdfs.return_value = [
            {"id": "id_att", "name": "001-01-0【添付資料】補足.pdf"},
        ]
        mock_sheets.get_processed_management_numbers.return_value = {"001-01-0"}
        mock_sheets.get_recorded_attachment_names.return_value = set()
        _install_step1_mocks(mock_drive, mock_reader)
        profile = _make_profile()

        batch_main.step1_prepare(profile, test_count=0)

        records = json.loads(self._att_file.read_text())
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["file_id"], "id_att")

    @patch("src.batch_main.pdf_reader")
    @patch("src.batch_main.sheets_client")
    @patch("src.batch_main.drive_client")
    def test_attachment_without_mgmt_number_not_saved(
        self, mock_drive, mock_sheets, mock_reader,
    ):
        """管理番号なしの添付資料は保存されず warning が出る。"""
        mock_drive.list_pdfs.return_value = [
            {"id": "id_att", "name": "【添付資料】管理番号なし.pdf"},
        ]
        mock_sheets.get_processed_management_numbers.return_value = set()
        _install_step1_mocks(mock_drive, mock_reader)
        profile = _make_profile()

        with self.assertLogs("jissen_comment", level="WARNING") as log_ctx:
            batch_main.step1_prepare(profile, test_count=0)

        records = json.loads(self._att_file.read_text())
        self.assertEqual(records, [])
        self.assertIn("【添付資料】管理番号なし.pdf", "\n".join(log_ctx.output))


class TestStep4AttachmentPassthrough(unittest.TestCase):
    """``step4_generate_pdfs`` の添付資料パススルー。

    メイン結果処理ループで case_map を構築し、batch_attachments.json を
    読んで添付資料をメインと同じフォルダへコピーする。
    """

    def setUp(self):
        self._att_file = LOGS_DIR / "batch_attachments.json"
        self._att_backup = (
            self._att_file.read_text() if self._att_file.exists() else None
        )
        LOGS_DIR.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        if self._att_backup is None:
            self._att_file.unlink(missing_ok=True)
        else:
            self._att_file.write_text(self._att_backup)

    @patch("src.batch_main.pdf_merger")
    @patch("src.batch_main.pdf_creator")
    @patch("src.batch_main.drive_client")
    @patch("src.batch_main.sheets_client")
    @patch("src.batch_main.ensure_fonts")
    def test_step4_copies_attachment_to_main_folder(
        self, mock_fonts, mock_sheets, mock_drive, mock_creator, mock_merger,
    ):
        """添付資料は同じ管理番号のメインと同じ医院/個人フォルダにコピーされる。"""
        _install_step4_mocks(mock_drive, mock_merger, mock_sheets)
        self._att_file.write_text(json.dumps([
            {
                "file_id": "id_att",
                "file_name": "111-22-3【添付資料】補足.pdf",
                "management_number": "111-22-3",
            }
        ]))
        profile = _make_profile()

        batch_main.step4_generate_pdfs(
            profile,
            results=_make_batch_results(1),
            items=_make_batch_items(["111-22-3実践事例.pdf"]),
        )

        # upload は 2 回（メイン + 添付資料）。添付資料は同じ医院番号 + 医院名
        # で呼ばれ、find_or_create_clinic_folder がメインと同じ医院フォルダへ
        # 合流させる（P-019）。元ファイル名はそのまま。
        self.assertEqual(mock_drive.upload_pdf_to_clinic_person.call_count, 2)
        att_upload = mock_drive.upload_pdf_to_clinic_person.call_args_list[1]
        self.assertEqual(att_upload.kwargs["clinic_number"], "111")
        self.assertEqual(att_upload.kwargs["clinic_name"], "山田歯科")
        self.assertEqual(att_upload.kwargs["person_name"], "田中太郎")
        self.assertEqual(
            att_upload.kwargs["file_name"], "111-22-3【添付資料】補足.pdf"
        )
        # シートに「【添付資料】<元名>」で記録
        att_row = mock_sheets.append_output_record.call_args_list[1]
        self.assertEqual(
            att_row.kwargs["sample_name"],
            "【添付資料】111-22-3【添付資料】補足.pdf",
        )
        self.assertEqual(att_row.kwargs["management_number"], "111-22-3")
        # コメントページ生成・マージは添付資料には行われない（メイン1件分のみ）
        self.assertEqual(mock_creator.create_comment_page.call_count, 1)
        self.assertEqual(mock_merger.merge_pdfs.call_count, 1)

    @patch("src.batch_main.pdf_merger")
    @patch("src.batch_main.pdf_creator")
    @patch("src.batch_main.drive_client")
    @patch("src.batch_main.sheets_client")
    @patch("src.batch_main.ensure_fonts")
    def test_step4_orphan_attachment_skipped_with_warning(
        self, mock_fonts, mock_sheets, mock_drive, mock_creator, mock_merger,
    ):
        """対応するメインが results に無く、マスターにも未登録の添付資料は
        スキップされ warning が出る。"""
        _install_step4_mocks(mock_drive, mock_merger, mock_sheets)
        mock_sheets.lookup_participant_by_management_number.return_value = None
        self._att_file.write_text(json.dumps([
            {
                "file_id": "id_orphan",
                "file_name": "999-99-9【添付資料】孤児.pdf",
                "management_number": "999-99-9",
            }
        ]))
        profile = _make_profile()

        with self.assertLogs("jissen_comment", level="WARNING") as log_ctx:
            batch_main.step4_generate_pdfs(
                profile,
                results=_make_batch_results(1),
                items=_make_batch_items(["111-22-3実践事例.pdf"]),
            )

        # メイン 1 件のみ upload。孤児添付資料はコピーされない。
        self.assertEqual(mock_drive.upload_pdf_to_clinic_person.call_count, 1)
        self.assertIn("999-99-9【添付資料】孤児.pdf", "\n".join(log_ctx.output))

    @patch("src.batch_main.pdf_merger")
    @patch("src.batch_main.pdf_creator")
    @patch("src.batch_main.drive_client")
    @patch("src.batch_main.sheets_client")
    @patch("src.batch_main.ensure_fonts")
    def test_step4_attachment_uses_same_clinic_number(
        self, mock_fonts, mock_sheets, mock_drive, mock_creator, mock_merger,
    ):
        """添付資料もメインと同じ医院番号 + 医院名で
        ``upload_pdf_to_clinic_person`` が呼ばれる（同じ医院フォルダへ合流、P-019）。"""
        _install_step4_mocks(mock_drive, mock_merger, mock_sheets)
        mock_sheets.get_recorded_clinic_numbers.return_value = set()
        self._att_file.write_text(json.dumps([
            {
                "file_id": "id_att",
                "file_name": "111-22-3【添付資料】補足.pdf",
                "management_number": "111-22-3",
            }
        ]))
        profile = _make_profile()

        batch_main.step4_generate_pdfs(
            profile,
            results=_make_batch_results(1),
            items=_make_batch_items(["111-22-3実践事例.pdf"]),
        )

        # メイン・添付資料ともに医院番号 111 + 医院名 山田歯科で呼ばれる
        for call in mock_drive.upload_pdf_to_clinic_person.call_args_list:
            self.assertEqual(call.kwargs["clinic_number"], "111")
            self.assertEqual(call.kwargs["clinic_name"], "山田歯科")
        # 出力一覧シートの医院名列は AI 抽出値（医院番号なし）
        for call in mock_sheets.append_output_record.call_args_list:
            self.assertEqual(call.kwargs["clinic_name"], "山田歯科")

    @patch("src.batch_main.pdf_merger")
    @patch("src.batch_main.pdf_creator")
    @patch("src.batch_main.drive_client")
    @patch("src.batch_main.sheets_client")
    @patch("src.batch_main.ensure_fonts")
    def test_step4_no_attachments_file_does_nothing(
        self, mock_fonts, mock_sheets, mock_drive, mock_creator, mock_merger,
    ):
        """batch_attachments.json が存在しない場合、添付資料処理は何もしない。"""
        _install_step4_mocks(mock_drive, mock_merger, mock_sheets)
        self._att_file.unlink(missing_ok=True)
        profile = _make_profile()

        batch_main.step4_generate_pdfs(
            profile,
            results=_make_batch_results(1),
            items=_make_batch_items(["111-22-3実践事例.pdf"]),
        )

        # メイン 1 件のみ。添付資料経路は何もしない。
        self.assertEqual(mock_drive.upload_pdf_to_clinic_person.call_count, 1)
        self.assertEqual(mock_sheets.append_output_record.call_count, 1)




class TestStep4MasterSheetIntegration(unittest.TestCase):
    """``step4_generate_pdfs`` の参加者マスター統合（医院名標準化 + Gmail 下書き）。

    Step4 でメインの PDF アップロード成功後 + シート追記後、参加者マスター
    シートから管理番号でメールアドレスを引き Gmail 下書きを作成する。CC は
    使わない。医院名は管理番号 prefix（医院番号）で標準表記を引き、未登録
    なら AI 抽出値で代用 + 警告ログ。``_process_attachments`` でも同じ
    ロジックで添付資料経路の下書き・医院名を扱う。
    """

    def setUp(self):
        # _process_attachments が batch_attachments.json を読むため空ファイル
        # を用意し、副作用を出さないようにする。
        self._att_file = LOGS_DIR / "batch_attachments.json"
        self._att_backup = (
            self._att_file.read_text() if self._att_file.exists() else None
        )
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        self._att_file.write_text("[]")

    def tearDown(self):
        if self._att_backup is None:
            self._att_file.unlink(missing_ok=True)
        else:
            self._att_file.write_text(self._att_backup)

    def _master_record(
        self, management_number, clinic_name, email,
        participant_name="田中太郎", venue="",
    ):
        """テスト用 MasterRecord。

        ``management_number`` は ``xxx-yy`` 形式（例 ``111-22``）で渡す。
        医院コードは property で先頭セグメントから派生する。
        ``participant_name`` のデフォルトは AI 抽出名 ``田中太郎`` と一致。
        """
        from src.sheets_client import MasterRecord
        return MasterRecord(
            management_number=management_number,
            clinic_name=clinic_name,
            participant_name=participant_name,
            venue=venue,
            email=email,
        )

    @patch("src.batch_main.pdf_merger")
    @patch("src.batch_main.pdf_creator")
    @patch("src.batch_main.gmail_client")
    @patch("src.batch_main.drive_client")
    @patch("src.batch_main.sheets_client")
    @patch("src.batch_main.ensure_fonts")
    def test_main_pdf_creates_draft_with_master_email_no_cc(
        self, mock_fonts, mock_sheets, mock_drive, mock_gmail,
        mock_creator, mock_merger,
    ):
        """管理番号 hit → TO=マスターのメール、CC=None で create_draft。"""
        from src import sheets_client as real_sheets_client

        mock_sheets.get_recorded_clinic_numbers.return_value = set()
        mock_sheets.read_master_records.return_value = [
            self._master_record(
                "111-22", "標準医院名", "tanaka@example.com"
            ),
        ]
        mock_sheets.lookup_clinic_name.side_effect = (
            lambda recs, cn: real_sheets_client.lookup_clinic_name(recs, cn)
        )
        mock_sheets.lookup_email_by_clinic_and_person.side_effect = (
            lambda recs, cn, pn: real_sheets_client.lookup_email_by_clinic_and_person(recs, cn, pn)
        )
        _install_step4_mocks(mock_drive, mock_merger)
        profile = _make_profile()

        batch_main.step4_generate_pdfs(
            profile,
            results=_make_batch_results(1),
            items=_make_batch_items(["111-22-3実践事例.pdf"]),
        )

        mock_gmail.create_draft.assert_called_once()
        kwargs = mock_gmail.create_draft.call_args.kwargs
        self.assertEqual(kwargs["to_email"], "tanaka@example.com")
        self.assertIsNone(kwargs["cc_email"])  # CC は使わない
        self.assertEqual(kwargs["person_name"], "田中太郎")

    @patch("src.batch_main.pdf_merger")
    @patch("src.batch_main.pdf_creator")
    @patch("src.batch_main.gmail_client")
    @patch("src.batch_main.drive_client")
    @patch("src.batch_main.sheets_client")
    @patch("src.batch_main.ensure_fonts")
    def test_clinic_name_from_master_replaces_ai_value(
        self, mock_fonts, mock_sheets, mock_drive, mock_gmail,
        mock_creator, mock_merger,
    ):
        """医院名 lookup hit → マスター標準名でフォルダ作成・シート記録。"""
        from src import sheets_client as real_sheets_client

        mock_sheets.get_recorded_clinic_numbers.return_value = set()
        mock_sheets.read_master_records.return_value = [
            self._master_record("111-22", "標準医院名", "t@example.com"),
        ]
        mock_sheets.lookup_clinic_name.side_effect = (
            lambda recs, cn: real_sheets_client.lookup_clinic_name(recs, cn)
        )
        mock_sheets.lookup_email_by_clinic_and_person.side_effect = (
            lambda recs, cn, pn: real_sheets_client.lookup_email_by_clinic_and_person(recs, cn, pn)
        )
        _install_step4_mocks(mock_drive, mock_merger)
        profile = _make_profile()

        batch_main.step4_generate_pdfs(
            profile,
            results=_make_batch_results(1),
            items=_make_batch_items(["111-22-3実践事例.pdf"]),
        )

        # AI 抽出値は「山田歯科」だが、フォルダ作成・出力シート行・医院シート行
        # すべて「標準医院名」（マスターの値）に統一される
        upload_kwargs = mock_drive.upload_pdf_to_clinic_person.call_args.kwargs
        self.assertEqual(upload_kwargs["clinic_name"], "標準医院名")
        append_kwargs = mock_sheets.append_output_record.call_args.kwargs
        self.assertEqual(append_kwargs["clinic_name"], "標準医院名")

    @patch("src.batch_main.pdf_merger")
    @patch("src.batch_main.pdf_creator")
    @patch("src.batch_main.gmail_client")
    @patch("src.batch_main.drive_client")
    @patch("src.batch_main.sheets_client")
    @patch("src.batch_main.ensure_fonts")
    def test_clinic_name_falls_back_to_ai_with_warning(
        self, mock_fonts, mock_sheets, mock_drive, mock_gmail,
        mock_creator, mock_merger,
    ):
        """医院名 lookup ミス → AI 抽出値で代用 + 警告ログ。"""
        from src import sheets_client as real_sheets_client

        mock_sheets.get_recorded_clinic_numbers.return_value = set()
        mock_sheets.read_master_records.return_value = []  # マスター空
        mock_sheets.lookup_clinic_name.side_effect = (
            lambda recs, cn: real_sheets_client.lookup_clinic_name(recs, cn)
        )
        mock_sheets.lookup_email_by_clinic_and_person.side_effect = (
            lambda recs, cn, pn: real_sheets_client.lookup_email_by_clinic_and_person(recs, cn, pn)
        )
        _install_step4_mocks(mock_drive, mock_merger)
        profile = _make_profile()

        with self.assertLogs("jissen_comment", level="WARNING") as log_ctx:
            batch_main.step4_generate_pdfs(
                profile,
                results=_make_batch_results(1),
                items=_make_batch_items(["111-22-3実践事例.pdf"]),
            )

        upload_kwargs = mock_drive.upload_pdf_to_clinic_person.call_args.kwargs
        self.assertEqual(upload_kwargs["clinic_name"], "山田歯科")
        joined = "\n".join(log_ctx.output)
        self.assertIn("参加者マスター未登録", joined)
        self.assertIn("111", joined)
        # PII ログマスク（PR-5）: 医院名は先頭1文字+＊で伏せられる
        self.assertIn("山＊", joined)
        self.assertNotIn("山田歯科", joined)

    @patch("src.batch_main.pdf_merger")
    @patch("src.batch_main.pdf_creator")
    @patch("src.batch_main.gmail_client")
    @patch("src.batch_main.drive_client")
    @patch("src.batch_main.sheets_client")
    @patch("src.batch_main.ensure_fonts")
    def test_unregistered_email_creates_draft_with_empty_to(
        self, mock_fonts, mock_sheets, mock_drive, mock_gmail,
        mock_creator, mock_merger,
    ):
        """メール lookup ミス → 宛先空で create_draft + 警告。PDF 処理は完了。"""
        from src import sheets_client as real_sheets_client

        mock_sheets.get_recorded_clinic_numbers.return_value = set()
        mock_sheets.read_master_records.return_value = []
        mock_sheets.lookup_clinic_name.side_effect = (
            lambda recs, cn: real_sheets_client.lookup_clinic_name(recs, cn)
        )
        mock_sheets.lookup_email_by_clinic_and_person.side_effect = (
            lambda recs, cn, pn: real_sheets_client.lookup_email_by_clinic_and_person(recs, cn, pn)
        )
        _install_step4_mocks(mock_drive, mock_merger)
        profile = _make_profile()

        with self.assertLogs("jissen_comment", level="WARNING") as log_ctx:
            batch_main.step4_generate_pdfs(
                profile,
                results=_make_batch_results(1),
                items=_make_batch_items(["111-22-3実践事例.pdf"]),
            )

        # 下書きは宛先空で 1 回作成される
        mock_gmail.create_draft.assert_called_once()
        kwargs = mock_gmail.create_draft.call_args.kwargs
        self.assertEqual(kwargs["to_email"], "")
        joined = "\n".join(log_ctx.output)
        self.assertIn("メール未ヒット", joined)
        self.assertIn("111", joined)
        # PDF 処理は通常通り完了
        mock_drive.upload_pdf_to_clinic_person.assert_called_once()
        mock_sheets.append_output_record.assert_called_once()

    @patch("src.batch_main.pdf_merger")
    @patch("src.batch_main.pdf_creator")
    @patch("src.batch_main.gmail_client")
    @patch("src.batch_main.drive_client")
    @patch("src.batch_main.sheets_client")
    @patch("src.batch_main.ensure_fonts")
    def test_draft_creation_exception_does_not_stop_processing(
        self, mock_fonts, mock_sheets, mock_drive, mock_gmail,
        mock_creator, mock_merger,
    ):
        """create_draft が例外を投げても次の PDF に進む（fail-soft）。"""
        from src import sheets_client as real_sheets_client

        mock_sheets.get_recorded_clinic_numbers.return_value = set()
        mock_sheets.read_master_records.return_value = [
            self._master_record("111-22", "三浦歯科医院", "t@example.com"),
            self._master_record("222-22", "山本歯科", "next@example.com"),
        ]
        mock_sheets.lookup_clinic_name.side_effect = (
            lambda recs, cn: real_sheets_client.lookup_clinic_name(recs, cn)
        )
        mock_sheets.lookup_email_by_clinic_and_person.side_effect = (
            lambda recs, cn, pn: real_sheets_client.lookup_email_by_clinic_and_person(recs, cn, pn)
        )
        mock_gmail.create_draft.side_effect = [
            RuntimeError("Gmail API down"),
            "draft_ok",
        ]
        _install_step4_mocks(mock_drive, mock_merger)
        profile = _make_profile()

        batch_main.step4_generate_pdfs(
            profile,
            results=_make_batch_results(2),
            items=_make_batch_items(
                ["111-22-3実践事例A.pdf", "222-22-3実践事例B.pdf"]
            ),
        )

        # 両方の PDF が処理される（最初の Gmail 失敗で停止しない）
        self.assertEqual(mock_drive.upload_pdf_to_clinic_person.call_count, 2)
        self.assertEqual(mock_sheets.append_output_record.call_count, 2)
        # create_draft は両方の PDF で呼ばれる
        self.assertEqual(mock_gmail.create_draft.call_count, 2)

    @patch("src.batch_main.pdf_merger")
    @patch("src.batch_main.pdf_creator")
    @patch("src.batch_main.gmail_client")
    @patch("src.batch_main.drive_client")
    @patch("src.batch_main.sheets_client")
    @patch("src.batch_main.ensure_fonts")
    def test_master_records_read_once_per_step4(
        self, mock_fonts, mock_sheets, mock_drive, mock_gmail,
        mock_creator, mock_merger,
    ):
        """Step4 全体で read_master_records は 1 回だけ呼ばれる。"""
        from src import sheets_client as real_sheets_client

        mock_sheets.get_recorded_clinic_numbers.return_value = set()
        mock_sheets.read_master_records.return_value = []
        mock_sheets.lookup_clinic_name.side_effect = (
            lambda recs, cn: real_sheets_client.lookup_clinic_name(recs, cn)
        )
        mock_sheets.lookup_email_by_clinic_and_person.side_effect = (
            lambda recs, cn, pn: real_sheets_client.lookup_email_by_clinic_and_person(recs, cn, pn)
        )
        _install_step4_mocks(mock_drive, mock_merger)
        profile = _make_profile()

        batch_main.step4_generate_pdfs(
            profile,
            results=_make_batch_results(3),
            items=_make_batch_items(
                ["111-22-3.pdf", "112-22-3.pdf", "113-22-3.pdf"]
            ),
        )

        # 3 件処理しても read_master_records は 1 回だけ
        self.assertEqual(mock_sheets.read_master_records.call_count, 1)

    @patch("src.batch_main.pdf_merger")
    @patch("src.batch_main.pdf_creator")
    @patch("src.batch_main.gmail_client")
    @patch("src.batch_main.drive_client")
    @patch("src.batch_main.sheets_client")
    @patch("src.batch_main.ensure_fonts")
    def test_attachment_path_also_creates_gmail_draft(
        self, mock_fonts, mock_sheets, mock_drive, mock_gmail,
        mock_creator, mock_merger,
    ):
        """添付資料経路の PDF も同じメールアドレスのグループにまとめられる。

        メイン + 添付資料が同じ個人 (=同じメールアドレス) → 1 通の下書きに
        両方添付される。
        """
        from src import sheets_client as real_sheets_client

        mock_sheets.get_recorded_clinic_numbers.return_value = set()
        mock_sheets.read_master_records.return_value = [
            self._master_record(
                "111-22", "三浦歯科医院", "tanaka@example.com"
            ),
        ]
        mock_sheets.lookup_clinic_name.side_effect = (
            lambda recs, cn: real_sheets_client.lookup_clinic_name(recs, cn)
        )
        mock_sheets.lookup_email_by_clinic_and_person.side_effect = (
            lambda recs, cn, pn: real_sheets_client.lookup_email_by_clinic_and_person(recs, cn, pn)
        )
        _install_step4_mocks(mock_drive, mock_merger)
        # 添付資料 1 件を batch_attachments.json に入れる
        self._att_file.write_text(json.dumps([
            {
                "file_id": "id_att",
                "file_name": "111-22-3【添付資料】補足.pdf",
                "management_number": "111-22-3",
            }
        ]))
        profile = _make_profile()

        batch_main.step4_generate_pdfs(
            profile,
            results=_make_batch_results(1),
            items=_make_batch_items(["111-22-3実践事例.pdf"]),
        )

        # メイン + 添付資料が同じ tanaka@example.com に集約 → 下書きは 1 通
        mock_gmail.create_draft.assert_called_once()
        kwargs = mock_gmail.create_draft.call_args.kwargs
        self.assertEqual(kwargs["to_email"], "tanaka@example.com")
        self.assertEqual(kwargs["person_name"], "田中太郎")
        self.assertIsNone(kwargs["cc_email"])
        # 添付ファイルは 2 件（メイン PDF + 添付資料 PDF）
        self.assertEqual(len(kwargs["pdf_paths"]), 2)


class TestCreateGroupedDraftsForBatch(unittest.TestCase):
    """F-06 / pdf_paths 型 回帰防止（Batch モード版）。"""

    def _items(self, *triples):
        return [
            {
                "email": email,
                "person_name": person,
                "pdf_path": path,
                "clinic_number": "101",
            }
            for email, person, path in triples
        ]

    @patch("src.batch_main.gmail_client")
    def test_single_person_passes_through(self, mock_gmail):
        items = self._items(("a@example.com", "山田太郎", "/tmp/a.pdf"))
        batch_main._create_grouped_drafts_for_batch(items)
        call = mock_gmail.create_draft.call_args
        self.assertEqual(call.kwargs["person_name"], "山田太郎")
        self.assertEqual(call.kwargs["pdf_paths"], ["/tmp/a.pdf"])

    @patch("src.batch_main.gmail_client")
    def test_multiple_persons_use_hoka_format(self, mock_gmail):
        items = self._items(
            ("g@example.com", "山田太郎", "/tmp/a.pdf"),
            ("g@example.com", "鈴木花子", "/tmp/b.pdf"),
        )
        with self.assertLogs("jissen_comment", level="WARNING"):
            batch_main._create_grouped_drafts_for_batch(items)
        call = mock_gmail.create_draft.call_args
        self.assertIn("ほか1名", call.kwargs["person_name"])

    @patch("src.batch_main.gmail_client")
    def test_empty_email_wraps_pdf_path_in_list(self, mock_gmail):
        items = self._items(("", "山田太郎", "/tmp/a.pdf"))
        batch_main._create_grouped_drafts_for_batch(items)
        call = mock_gmail.create_draft.call_args
        self.assertIsInstance(call.kwargs["pdf_paths"], list)
        self.assertEqual(call.kwargs["pdf_paths"], ["/tmp/a.pdf"])


class TestBatchStateFilesPersistence(unittest.TestCase):
    """CB-1 / CB-2 / P-023 回帰防止: state ファイル永続化と独立 step 実行。"""

    def test_atomic_write_json_replaces_existing(self):
        """atomic write は途中失敗で半端なファイルを残さない。"""
        from pathlib import Path as _P
        import tempfile as _tf
        with _tf.TemporaryDirectory() as tmp:
            target = _P(tmp) / "test.json"
            target.write_text("old")
            batch_main._atomic_write_json(target, {"a": 1, "b": 2})
            import json as _json
            self.assertEqual(_json.loads(target.read_text()), {"a": 1, "b": 2})
            # tmp ファイルは残らない
            self.assertFalse(target.with_suffix(".json.tmp").exists())

    @patch.object(batch_main, "LOGS_DIR")
    def test_load_items_raises_when_prep_missing(self, mock_logs_dir):
        """batch_prep.json が無いときに FileNotFoundError を上げる。"""
        from pathlib import Path as _P
        import tempfile as _tf
        with _tf.TemporaryDirectory() as tmp:
            mock_logs_dir.__truediv__ = lambda self_, x: _P(tmp) / x
            mock_logs_dir.mkdir = MagicMock()
            with self.assertRaises(FileNotFoundError):
                batch_main._load_items_from_disk()

    def test_results_roundtrip(self):
        """results を atomic 保存して読み戻せる。"""
        from pathlib import Path as _P
        import tempfile as _tf
        with _tf.TemporaryDirectory() as tmp:
            with patch.object(batch_main, "LOGS_DIR", _P(tmp)):
                results = {
                    "item_0001": {
                        "clinic_name": "X歯科", "person_name": "山田",
                        "sample_title": "事例1", "comment": "A" * 100,
                    },
                }
                batch_main._save_results_to_disk(results)
                loaded = batch_main._load_results_from_disk()
                self.assertEqual(loaded, results)

    @patch("src.batch_main.comment_generator")
    def test_step2_persists_batch_id_atomically(self, mock_gen):
        """``step2_submit_batch`` が batch_id を atomic write で永続化する。

        Anthropic Batch は 29 日保持されるため、step3 が落ちても
        ``--step results --batch-id <id>`` で再開できる。書き込み中クラッシュで
        ``batch_id.txt`` が空 / 半端な値になると再開不能になるため atomic。
        """
        from pathlib import Path as _P
        import tempfile as _tf
        mock_gen.plan_batch_chunks.side_effect = lambda items: [items]
        mock_gen.submit_batch.return_value = "batch_real_id_1234567890"
        with _tf.TemporaryDirectory() as tmp:
            with patch.object(batch_main, "LOGS_DIR", _P(tmp)):
                batch_ids = batch_main.step2_submit_batch(
                    _make_batch_items(["001-01-1新規.pdf"])
                )
                self.assertEqual(batch_ids, ["batch_real_id_1234567890"])
                # 永続ファイルに正しく書かれている（1 行 1 ID 形式）
                self.assertEqual(
                    (_P(tmp) / "batch_id.txt").read_text().strip(),
                    "batch_real_id_1234567890",
                )
                # tmp ファイルは残らない（atomic rename 完了）
                self.assertFalse((_P(tmp) / "batch_id.txt.tmp").exists())

    @patch("src.batch_main.comment_generator")
    def test_run_resume_from_step_results_loads_batch_id(self, mock_gen):
        """``run(step="results", batch_id=None)`` は disk の batch_id を読み戻す。

        本番事象の再開シナリオの回帰防止。step3 が 503 で死んだ後、
        オペレータが ``--step results`` で別 GHA 実行を起こしたとき、
        ``logs/batch_id.txt`` から自動で batch_id をロードして結果取得に進む。
        """
        from pathlib import Path as _P
        import tempfile as _tf
        ok_status = {
            "id": "batch_persisted", "status": "ended",
            "request_counts": {
                "processing": 0, "succeeded": 1,
                "errored": 0, "canceled": 0, "expired": 0,
            },
        }
        mock_gen.get_batch_status.return_value = ok_status
        mock_gen.get_batch_results.return_value = ({"item_0001": {
            "clinic_name": "X", "person_name": "Y",
            "sample_title": "Z", "comment": "C" * 50,
        }}, [])
        with _tf.TemporaryDirectory() as tmp:
            tmp_path = _P(tmp)
            (tmp_path / "batch_id.txt").write_text("batch_persisted")
            with patch.object(batch_main, "LOGS_DIR", tmp_path), \
                 patch.object(batch_main.discover, "resolve_run_config",
                              return_value=_make_profile()), \
                 patch.object(batch_main, "step4_generate_pdfs") as mock_step4:
                batch_main.run(batch_mode=True, step="results", batch_id=None)
                # batch_id を disk から読んで step3 が呼ばれた
                mock_gen.get_batch_status.assert_called_once_with("batch_persisted")


class TestBatchGmailDraftsToggle(unittest.TestCase):
    """ENABLE_GMAIL_DRAFTS による下書き作成 ON/OFF（Batchモード）。"""

    @staticmethod
    def _item():
        return {
            "email": "a@example.com",
            "person_name": "田中太郎",
            "pdf_path": "/tmp/x.pdf",
        }

    @patch("src.batch_main.gmail_client")
    @patch("src.config.ENABLE_GMAIL_DRAFTS", False)
    def test_drafts_skipped_when_disabled(self, mock_gmail):
        """OFF のとき create_draft は一度も呼ばれない。"""
        batch_main._create_grouped_drafts_for_batch([self._item()])
        mock_gmail.create_draft.assert_not_called()

    @patch("src.batch_main.gmail_client")
    @patch("src.config.ENABLE_GMAIL_DRAFTS", True)
    def test_drafts_created_when_enabled(self, mock_gmail):
        """ON のとき従来どおり create_draft が呼ばれる。"""
        batch_main._create_grouped_drafts_for_batch([self._item()])
        mock_gmail.create_draft.assert_called_once()


class TestStep3PollingResilience(unittest.TestCase):
    """``step3_wait_and_get_results`` のポーリングループ自体の例外耐性。

    本番 GHA ラン（run_id=26811653746, 3h22m）で、``get_batch_status`` 内の
    ``client.messages.batches.retrieve(batch_id)`` が 503 ``overloaded_error``
    を 1 度返しただけで、``step3_wait_and_get_results`` の ``while`` ループを
    抜けて Traceback。3 時間ぶんの待機が水の泡になった。

    多層防御：
      (a) ``get_batch_status`` 内で一過性エラーは指数バックオフリトライ
          （``TestBatchApiRetriesOnTransientErrors`` 参照）。
      (b) **更にその上**、本テストで担保するように、ポーリングループ自体も
          1 回のステータス取得失敗で打ち切らず ``poll_interval`` 待って continue。
          ``PermanentRunFailureError`` は即 raise（fail-fast、PR #46 と同じ方針）。
          ``max_wait`` は維持。
    """

    def _transient_503(self) -> anthropic.InternalServerError:
        return anthropic.InternalServerError(
            message="Error code: 503 - overloaded_error",
            response=MagicMock(status_code=503, headers={}),
            body={
                "type": "overloaded_error",
                "message": "API key validation is temporarily unavailable. Please retry.",
            },
        )

    @patch("src.batch_main._save_results_to_disk")
    @patch("src.batch_main.comment_generator")
    @patch("src.batch_main.time.sleep")
    def test_polling_continues_when_get_status_raises_transient(
        self, mock_sleep, mock_gen, mock_save,
    ):
        """1 回の ``get_batch_status`` が一過性例外を投げてもループ継続。

        get_batch_status のリトライ上限を超えて例外が漏れてきても、ポーリングは
        ``poll_interval`` 待って次イテレーションへ進む。次回 OK なら結果取得まで到達する。
        """
        ok_status = {
            "id": "batch_xx",
            "status": "ended",
            "request_counts": {
                "processing": 0, "succeeded": 5,
                "errored": 0, "canceled": 0, "expired": 0,
            },
        }
        mock_gen.get_batch_status.side_effect = [
            self._transient_503(),  # 1 回目: 503 漏れ（ループは continue）
            ok_status,              # 2 回目: 成功（ended → break）
        ]
        mock_gen.get_batch_results.return_value = ({"item_0001": {
            "clinic_name": "X", "person_name": "Y",
            "sample_title": "Z", "comment": "C",
        }}, [])

        results = batch_main.step3_wait_and_get_results(
            "batch_xx", poll_interval=1, max_wait=10,
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(mock_gen.get_batch_status.call_count, 2)
        mock_gen.get_batch_results.assert_called_once_with("batch_xx")

    @patch("src.batch_main._save_results_to_disk")
    @patch("src.batch_main.comment_generator")
    @patch("src.batch_main.time.sleep")
    def test_polling_halts_immediately_on_permanent_failure(
        self, mock_sleep, mock_gen, mock_save,
    ):
        """``PermanentRunFailureError`` はループ内で握りつぶさず即 raise（fail-fast）。

        恒久エラー（残高不足/認証/権限）はリトライしても必ず失敗するため、
        ループを継続せず即停止して呼び出し側へ伝播する（PR #46 と同じ方針）。
        """
        mock_gen.PermanentRunFailureError = PermanentRunFailureError  # 経由参照対策
        mock_gen.get_batch_status.side_effect = PermanentRunFailureError(
            "Anthropic API の認証エラーのため処理を中止しました。"
        )
        with self.assertRaises(PermanentRunFailureError):
            batch_main.step3_wait_and_get_results(
                "batch_xx", poll_interval=1, max_wait=10,
            )
        # 1 回呼んで即 raise（リトライしない・get_batch_results に到達しない）
        self.assertEqual(mock_gen.get_batch_status.call_count, 1)
        mock_gen.get_batch_results.assert_not_called()

    @patch("src.batch_main._save_results_to_disk")
    @patch("src.batch_main.comment_generator")
    @patch("src.batch_main.time.sleep")
    def test_polling_respects_max_wait_with_continuous_transient(
        self, mock_sleep, mock_gen, mock_save,
    ):
        """一過性エラーが続いても ``max_wait`` を超えたら TimeoutError で停止。

        無限ループにならず、上限時間（既定 86400 秒）を必ず守る。
        ``poll_interval=10, max_wait=30`` で 4 周（0/10/20/30）し、4 周目で
        ``elapsed >= max_wait`` の判定により break。
        """
        mock_gen.get_batch_status.side_effect = self._transient_503()
        with self.assertRaises(TimeoutError):
            batch_main.step3_wait_and_get_results(
                "batch_xx", poll_interval=10, max_wait=30,
            )
        # 一度も成功しないため get_batch_results に到達しない
        mock_gen.get_batch_results.assert_not_called()

    @patch("src.batch_main._save_results_to_disk")
    @patch("src.batch_main.comment_generator")
    @patch("src.batch_main.time.sleep")
    def test_polling_normal_path_unchanged(
        self, mock_sleep, mock_gen, mock_save,
    ):
        """既存挙動の保持：例外が起きない正常系では従来通り 1 回で ended → 結果取得。"""
        ok_status = {
            "id": "batch_xx", "status": "ended",
            "request_counts": {
                "processing": 0, "succeeded": 3,
                "errored": 0, "canceled": 0, "expired": 0,
            },
        }
        mock_gen.get_batch_status.return_value = ok_status
        mock_gen.get_batch_results.return_value = ({"k": {
            "clinic_name": "", "person_name": "",
            "sample_title": "", "comment": "x",
        }}, [])
        results = batch_main.step3_wait_and_get_results(
            "batch_xx", poll_interval=60, max_wait=86400,
        )
        self.assertEqual(len(results), 1)
        mock_gen.get_batch_status.assert_called_once_with("batch_xx")
        mock_gen.get_batch_results.assert_called_once_with("batch_xx")


class TestBatchFailFastOnPermanentError(unittest.TestCase):
    """Batch モードでも、バッチ作成・送信時に残高/認証/権限の恒久エラーが
    起きたら即停止する。``submit_batch`` が ``PermanentRunFailureError`` を
    上げたら、それを握りつぶさず ``run`` の外へ伝播させ、後続ステップ
    （結果取得 / PDF 生成）を実行しない。
    """

    @patch("src.batch_main.comment_generator")
    def test_step2_submit_propagates_permanent_failure(self, mock_gen):
        """``submit_batch`` の恒久エラーは ``step2_submit_batch`` から伝播する。"""
        mock_gen.plan_batch_chunks.side_effect = lambda items: [items]
        mock_gen.submit_batch.side_effect = PermanentRunFailureError(
            "Anthropic API のクレジット残高不足のため処理を中止しました。"
        )
        items = _make_batch_items(["001-01-1新規.pdf"])
        with self.assertRaises(PermanentRunFailureError):
            batch_main.step2_submit_batch(items)

    @patch("src.batch_main.step4_generate_pdfs")
    @patch("src.batch_main.step3_wait_and_get_results")
    @patch("src.batch_main.step2_submit_batch")
    @patch("src.batch_main.step1_prepare")
    @patch("src.batch_main.discover.resolve_run_config")
    def test_run_halts_at_submit_and_skips_results_and_pdfs(
        self,
        mock_resolve,
        mock_step1,
        mock_step2,
        mock_step3,
        mock_step4,
    ):
        """``run(step="all")`` は submit の恒久エラーで停止し、step3/step4 を
        呼ばない（無駄に結果取得 / PDF 生成へ進まない）。"""
        mock_resolve.return_value = _make_profile()
        mock_step1.return_value = _make_batch_items(["001-01-1新規.pdf"])
        mock_step2.side_effect = PermanentRunFailureError(
            "Anthropic API のクレジット残高不足のため処理を中止しました。"
        )

        with self.assertRaises(PermanentRunFailureError):
            batch_main.run(batch_mode=True, step="all")

        mock_step2.assert_called_once()
        mock_step3.assert_not_called()
        mock_step4.assert_not_called()


class TestCustomIdForFile(unittest.TestCase):
    """``_custom_id_for_file``: file id 由来の決定的・Anthropic 安全な custom_id。"""

    def test_deterministic_and_anthropic_safe(self):
        cid = batch_main._custom_id_for_file("1A2b_-Zk9")
        # 同じ file id なら必ず同じ（決定的）
        self.assertEqual(cid, batch_main._custom_id_for_file("1A2b_-Zk9"))
        # Anthropic custom_id 制約（英数・``_``・``-`` / 1〜64 文字）に収まる
        self.assertLessEqual(len(cid), 64)
        self.assertRegex(cid, r"^[A-Za-z0-9_-]{1,64}$")

    def test_long_or_unusual_id_falls_back_to_hash(self):
        """64 文字超 / 異文字を含む id はハッシュにフォールバック（決定的）。"""
        weird = "x/y z" + "Z" * 80  # 長い + ``/`` ``空白`` で制約違反
        cid = batch_main._custom_id_for_file(weird)
        self.assertLessEqual(len(cid), 64)
        self.assertRegex(cid, r"^[A-Za-z0-9_-]{1,64}$")
        # ハッシュフォールバックも決定的（同じ入力 → 同じ出力）
        self.assertEqual(cid, batch_main._custom_id_for_file(weird))
        # 別の id とは衝突しない
        self.assertNotEqual(cid, batch_main._custom_id_for_file("other"))


class TestReconstructItemsFromDrive(unittest.TestCase):
    """``reconstruct_items_from_drive``: Drive 再走査で items 再構築（CB-4 回収）。"""

    @patch("src.batch_main.drive_client")
    def test_rebuilds_main_items_and_persists_attachments(self, mock_drive):
        from pathlib import Path as _P
        import tempfile as _tf
        mock_drive.list_pdfs.return_value = [
            {"id": "driveA", "name": "001-01-1事例.pdf"},
            {"id": "driveB", "name": "002-02-2事例.pdf"},
            {"id": "driveC", "name": "002-02-2【添付資料】補足.pdf"},
            {"id": "driveD", "name": "管理番号なし【添付資料】.pdf"},
        ]
        profile = _make_profile(input_folder_id="in_xyz")
        with _tf.TemporaryDirectory() as tmp:
            with patch.object(batch_main, "LOGS_DIR", _P(tmp)):
                items = batch_main.reconstruct_items_from_drive(profile)
                # メインのみ items 化（添付資料は除外）
                self.assertEqual(len(items), 2)
                self.assertEqual(
                    [it["custom_id"] for it in items],
                    [batch_main._custom_id_for_file("driveA"),
                     batch_main._custom_id_for_file("driveB")],
                )
                self.assertEqual(
                    [it["pdf_data_id"] for it in items], ["driveA", "driveB"],
                )
                # 本文 DL しない → pdf_text を載せない
                self.assertNotIn("pdf_text", items[0])
                # 管理番号を持つ添付資料のみ batch_attachments.json に再構築
                att = json.loads(
                    (_P(tmp) / "batch_attachments.json").read_text()
                )
                self.assertEqual(len(att), 1)
                self.assertEqual(att[0]["file_id"], "driveC")
                self.assertEqual(att[0]["management_number"], "002-02-2")
        mock_drive.list_pdfs.assert_called_once_with(folder_id="in_xyz")
        # 軽量再構築：本文ダウンロードは一切しない
        mock_drive.download_pdf.assert_not_called()

    @patch("src.batch_main.drive_client")
    def test_custom_id_independent_of_scan_order(self, mock_drive):
        """走査順が入れ替わっても file→custom_id 対応は不変（位置依存しない）。

        これが回収の肝。投入時と再走査時で Drive の返却順 / 重複除外が変わっても
        同一ファイルは同じ custom_id になり、batch_results.json と突合できる。
        """
        from pathlib import Path as _P
        import tempfile as _tf
        files = [
            {"id": "fa", "name": "001-01-1.pdf"},
            {"id": "fb", "name": "002-02-2.pdf"},
            {"id": "fc", "name": "003-03-3.pdf"},
        ]
        profile = _make_profile()
        with _tf.TemporaryDirectory() as tmp:
            with patch.object(batch_main, "LOGS_DIR", _P(tmp)):
                mock_drive.list_pdfs.return_value = list(files)
                map1 = {
                    it["pdf_data_id"]: it["custom_id"]
                    for it in batch_main.reconstruct_items_from_drive(profile)
                }
                mock_drive.list_pdfs.return_value = list(reversed(files))
                map2 = {
                    it["pdf_data_id"]: it["custom_id"]
                    for it in batch_main.reconstruct_items_from_drive(profile)
                }
        self.assertEqual(map1, map2)


class TestResolveItemsForStep4(unittest.TestCase):
    """``_resolve_items_for_step4``: batch_prep.json 優先 → Drive 再走査。"""

    @patch("src.batch_main.drive_client")
    def test_prefers_batch_prep_when_present(self, mock_drive):
        from pathlib import Path as _P
        import tempfile as _tf
        profile = _make_profile()
        with _tf.TemporaryDirectory() as tmp:
            with patch.object(batch_main, "LOGS_DIR", _P(tmp)):
                (_P(tmp) / "batch_prep.json").write_text(json.dumps([
                    {"custom_id": "item_0001", "pdf_data_id": "x",
                     "pdf_file_name": "001-01-1.pdf", "pdf_text": "t"},
                ]))
                items = batch_main._resolve_items_for_step4(profile)
        self.assertEqual(len(items), 1)
        # 旧 positional custom_id のバッチも disk からそのまま復元できる
        self.assertEqual(items[0]["custom_id"], "item_0001")
        mock_drive.list_pdfs.assert_not_called()

    @patch("src.batch_main.drive_client")
    def test_falls_back_to_drive_when_prep_absent(self, mock_drive):
        from pathlib import Path as _P
        import tempfile as _tf
        mock_drive.list_pdfs.return_value = [
            {"id": "driveA", "name": "001-01-1事例.pdf"},
        ]
        profile = _make_profile(input_folder_id="in_zzz")
        with _tf.TemporaryDirectory() as tmp:
            with patch.object(batch_main, "LOGS_DIR", _P(tmp)):
                items = batch_main._resolve_items_for_step4(profile)
        self.assertEqual(len(items), 1)
        self.assertEqual(
            items[0]["custom_id"], batch_main._custom_id_for_file("driveA"),
        )
        mock_drive.list_pdfs.assert_called_once_with(folder_id="in_zzz")

    @patch("src.batch_main.drive_client")
    def test_empty_prep_falls_back_to_drive(self, mock_drive):
        """``batch_prep.json`` が空配列なら Drive 再走査にフォールバックする。"""
        from pathlib import Path as _P
        import tempfile as _tf
        mock_drive.list_pdfs.return_value = [
            {"id": "driveA", "name": "001-01-1事例.pdf"},
        ]
        profile = _make_profile()
        with _tf.TemporaryDirectory() as tmp:
            with patch.object(batch_main, "LOGS_DIR", _P(tmp)):
                (_P(tmp) / "batch_prep.json").write_text("[]")
                items = batch_main._resolve_items_for_step4(profile)
        self.assertEqual(len(items), 1)
        mock_drive.list_pdfs.assert_called_once()


class TestStep4SilentZeroGuard(unittest.TestCase):
    """results があるのに items 0 件なら loud に停止（無言の成功 0 件を防止、CB-4）。"""

    @patch("src.batch_main.pdf_merger")
    @patch("src.batch_main.pdf_creator")
    @patch("src.batch_main.drive_client")
    @patch("src.batch_main.sheets_client")
    @patch("src.batch_main.ensure_fonts")
    def test_raises_when_results_present_but_items_unresolvable(
        self, mock_fonts, mock_sheets, mock_drive, mock_creator, mock_merger,
    ):
        from pathlib import Path as _P
        import tempfile as _tf
        # batch_prep.json も無く Drive 再走査も 0 件 → items を解決できない
        mock_drive.list_pdfs.return_value = []
        profile = _make_profile()
        with _tf.TemporaryDirectory() as tmp:
            with patch.object(batch_main, "LOGS_DIR", _P(tmp)):
                with self.assertRaises(RuntimeError):
                    batch_main.step4_generate_pdfs(
                        profile, results=_make_batch_results(1), items=None,
                    )
        # 出力一覧シートへは 1 行も書かない（無言の 0 件を残さない）
        mock_sheets.append_output_record.assert_not_called()


class TestRunResultsRecoveryEndToEnd(unittest.TestCase):
    """``run(step="results", batch_id=X)``：結果取得 → Drive 再走査で items 再構築
    → step4 完走（回収バグ＝「結果は取れるが出力 0 件」の回帰防止）。"""

    @patch("src.config.ENABLE_GMAIL_DRAFTS", False)
    @patch("src.batch_main.ensure_fonts")
    @patch("src.batch_main.pdf_merger")
    @patch("src.batch_main.pdf_creator")
    @patch("src.batch_main.sheets_client")
    @patch("src.batch_main.drive_client")
    @patch("src.batch_main.comment_generator")
    def test_recovery_rescans_drive_and_completes_step4(
        self, mock_gen, mock_drive, mock_sheets, mock_creator, mock_merger,
        mock_fonts,
    ):
        from pathlib import Path as _P
        import tempfile as _tf
        # Drive 再走査で 1 件のメイン PDF が見つかる
        mock_drive.list_pdfs.return_value = [
            {"id": "driveA", "name": "001-01-1事例.pdf"},
        ]
        _install_step4_mocks(mock_drive, mock_merger, mock_sheets)
        mock_sheets.get_processed_management_numbers.return_value = set()
        mock_sheets.get_recorded_clinic_numbers.return_value = set()
        # 完了済みバッチ。結果は file id 由来の custom_id で返る（投入時と同じ）。
        cid = batch_main._custom_id_for_file("driveA")
        mock_gen.get_batch_status.return_value = {
            "id": "msgbatch_done", "status": "ended",
            "request_counts": {"processing": 0, "succeeded": 1,
                               "errored": 0, "canceled": 0, "expired": 0},
        }
        mock_gen.get_batch_results.return_value = ({cid: {
            "clinic_name": "山田歯科", "person_name": "田中太郎",
            "sample_title": "事例", "comment": "コメント本文",
        }}, [])
        with _tf.TemporaryDirectory() as tmp:
            with patch.object(batch_main, "LOGS_DIR", _P(tmp)), \
                 patch.object(batch_main.discover, "resolve_run_config",
                              return_value=_make_profile()):
                batch_main.run(batch_mode=True, step="results",
                               batch_id="msgbatch_done")
        # step4 に到達し出力一覧へ追記された＝回収完走（旧コードはここに来ない）
        mock_sheets.append_output_record.assert_called_once()
        kw = mock_sheets.append_output_record.call_args.kwargs
        self.assertEqual(kw["management_number"], "001-01-1")
        # Drive 再走査が profile の入力フォルダで行われた
        mock_drive.list_pdfs.assert_called_with(folder_id="input_folder_xxx")

    @patch("src.batch_main.step4_generate_pdfs")
    @patch("src.batch_main.comment_generator")
    def test_discrete_results_without_batch_id_does_not_run_step4(
        self, mock_gen, mock_step4,
    ):
        """``--step results`` を batch_id なし（disk から補完）で呼ぶ discrete 運用
        では step4 を走らせない。回収（batch_id 明示）との切り分けの回帰防止。
        長時間ポーリング直後に PDF 生成を始めて GHA 6h を超える事故を防ぐ意図。
        """
        from pathlib import Path as _P
        import tempfile as _tf
        mock_gen.get_batch_status.return_value = {
            "id": "b1", "status": "ended",
            "request_counts": {"processing": 0, "succeeded": 1,
                               "errored": 0, "canceled": 0, "expired": 0},
        }
        mock_gen.get_batch_results.return_value = ({"file-driveA": {
            "clinic_name": "X", "person_name": "Y",
            "sample_title": "Z", "comment": "C" * 30,
        }}, [])
        with _tf.TemporaryDirectory() as tmp:
            with patch.object(batch_main, "LOGS_DIR", _P(tmp)), \
                 patch.object(batch_main.discover, "resolve_run_config",
                              return_value=_make_profile()):
                (_P(tmp) / "batch_id.txt").write_text("b1")
                batch_main.run(batch_mode=True, step="results", batch_id=None)
        # 結果取得はするが step4 は走らない（discrete）
        mock_gen.get_batch_status.assert_called_once_with("b1")
        mock_step4.assert_not_called()


class TestBatchHardFailOnEmptyMasterInTargetFolderMode(unittest.TestCase):
    """target_folder モードで Step1（投入前ガード）と Step4（resume パスの保険）
    の両方が、参加者マスター不在 / 0 件で ``MasterSheetEmptyError`` を送出する。
    """

    @patch("src.batch_main.drive_client")
    @patch("src.batch_main.sheets_client")
    def test_step1_halts_before_anthropic_submit_when_master_is_empty(
        self, mock_sheets, mock_drive,
    ):
        """Step1: マスター空 → Anthropic Batch API 投入前に即停止。"""
        from src.run_common import MasterSheetEmptyError

        cfg = batch_main.RunConfig(
            display_name="自動検出: 新人育成塾",
            input_folder_id="auto_in",
            output_folder_id="auto_out",
            output_sheet_name="新人育成塾",
            master_sheet_name="参加者マスター(新人育成塾)",
            master_sheet_strict=True,
        )
        mock_sheets.read_master_records.return_value = []

        with self.assertRaises(MasterSheetEmptyError):
            batch_main.step1_prepare(cfg, test_count=0)

        # PDF 一覧の取得すら走らない（事前ガード）
        mock_drive.list_pdfs.assert_not_called()

    @patch("src.batch_main.drive_client")
    @patch("src.batch_main.sheets_client")
    def test_step1_runs_normally_when_master_has_records(
        self, mock_sheets, mock_drive,
    ):
        """Step1: マスターに行があれば従来通り進行する（HARD FAIL しない）。"""
        from src.sheets_client import MasterRecord

        cfg = batch_main.RunConfig(
            display_name="自動検出: 新人育成塾",
            input_folder_id="auto_in",
            output_folder_id="auto_out",
            output_sheet_name="新人育成塾",
            master_sheet_name="参加者マスター(新人育成塾)",
            master_sheet_strict=True,
        )
        mock_sheets.read_master_records.return_value = [
            MasterRecord(
                management_number="001-01",
                clinic_name="標準医院名",
                participant_name="田中太郎",
                venue="",
                email="t@example.com",
            ),
        ]
        mock_sheets.get_processed_management_numbers.return_value = set()
        mock_drive.list_pdfs.return_value = []

        # 例外は出ない（list_pdfs まで進む）
        batch_main.step1_prepare(cfg, test_count=0)
        mock_drive.list_pdfs.assert_called_once_with(folder_id="auto_in")

    def test_step1_profile_mode_does_not_halt_on_empty_master(self):
        """プロファイルモード（strict=False）はマスター空でも HARD FAIL しない。"""
        with patch("src.batch_main.drive_client") as mock_drive, \
             patch("src.batch_main.sheets_client") as mock_sheets:
            mock_sheets.read_master_records.return_value = []
            mock_sheets.get_processed_management_numbers.return_value = set()
            mock_drive.list_pdfs.return_value = []

            profile = _make_profile()  # ProfileConfig は strict 概念を持たない
            # 例外は出ない
            batch_main.step1_prepare(profile, test_count=0)
            mock_drive.list_pdfs.assert_called_once()


class TestRunCommonRequireNonEmptyMaster(unittest.TestCase):
    """``run_common.require_non_empty_master`` の単体挙動。"""

    def test_strict_false_is_noop(self):
        from src.run_common import require_non_empty_master
        import logging
        # 空 + strict=False → 何も起きない
        require_non_empty_master([], "any", False, logging.getLogger("test"))

    def test_strict_true_with_records_is_noop(self):
        from src.run_common import require_non_empty_master
        import logging
        # 行あり + strict=True → 何も起きない
        require_non_empty_master(
            ["dummy"], "any", True, logging.getLogger("test"),
        )

    def test_strict_true_with_empty_raises(self):
        from src.run_common import (
            MasterSheetEmptyError, require_non_empty_master,
        )
        import logging
        with self.assertRaises(MasterSheetEmptyError):
            require_non_empty_master(
                [], "参加者マスター(新人育成塾)", True,
                logging.getLogger("test"),
            )


class TestZeroNewItemsGracefulExit(unittest.TestCase):
    """新規 0 件（増分運用の定常状態）で step=all が正常終了する。

    従来は results ブロックが存在しない ``logs/batch_id.txt`` を読もうとして
    FileNotFoundError → 赤ランになっていた（定常ケースがクラッシュする欠陥）。
    """

    @patch("src.batch_main.step3_wait_and_get_results")
    @patch("src.batch_main.step2_submit_batch")
    @patch("src.batch_main.step1_prepare")
    @patch("src.batch_main.sheets_client")
    @patch("src.batch_main.discover.resolve_run_config")
    def test_zero_items_skips_results_and_exits_cleanly(
        self, mock_resolve, mock_sheets, mock_step1, mock_step2, mock_step3,
    ):
        mock_resolve.return_value = _make_profile()
        mock_sheets.get_open_batch_ids.return_value = []
        mock_step1.return_value = []  # 新規 0 件

        batch_main.run(batch_mode=True, step="all")  # 例外なく戻る

        mock_step2.assert_not_called()
        mock_step3.assert_not_called()

    @patch("src.batch_main.discover.resolve_run_config")
    def test_step_results_without_batch_id_file_gives_clear_error(
        self, mock_resolve,
    ):
        """batch_id.txt 不在の ``--step results`` は案内付きのエラーになる。"""
        from pathlib import Path as _P
        import tempfile as _tf
        mock_resolve.return_value = _make_profile()
        with _tf.TemporaryDirectory() as tmp:
            with patch.object(batch_main, "LOGS_DIR", _P(tmp)):
                with self.assertRaises(FileNotFoundError) as ctx:
                    batch_main.run(batch_mode=True, step="results")
        self.assertIn("--batch-id", str(ctx.exception))


class TestBatchResumeOpenBatches(unittest.TestCase):
    """未回収バッチの自動レジューム（二重課金防止）。

    GHA ジョブが step3/step4 の途中で kill された後の ``step=all`` 再実行は、
    ephemeral ランナーに ``batch_id.txt`` が無いため従来は全件を再投入していた
    （投入済みバッチの分が丸ごと二重課金）。``_バッチ管理`` タブから未回収
    バッチを検知して回収から再開する。
    """

    @patch("src.batch_main._mark_batches_done")
    @patch("src.batch_main.step4_generate_pdfs")
    @patch("src.batch_main.step3_wait_and_get_results")
    @patch("src.batch_main.sheets_client")
    def test_open_batches_are_collected_then_marked_done(
        self, mock_sheets, mock_step3, mock_step4, mock_mark,
    ):
        mock_sheets.get_open_batch_ids.return_value = ["msgbatch_a", "msgbatch_b"]
        mock_step3.side_effect = [
            {"file-1": {"comment": "A"}},
            {"file-2": {"comment": "B"}},
        ]
        profile = _make_profile()

        batch_main._resume_open_batches(profile, poll_max_seconds=600)

        self.assertEqual(mock_step3.call_count, 2)
        # 両バッチの結果がマージされて step4 に渡る
        merged = mock_step4.call_args.args[1]
        self.assertEqual(set(merged.keys()), {"file-1", "file-2"})
        mock_mark.assert_called_once_with(
            "出力一覧", ["msgbatch_a", "msgbatch_b"],
        )

    @patch("src.batch_main._mark_batches_done")
    @patch("src.batch_main.step4_generate_pdfs")
    @patch("src.batch_main.step3_wait_and_get_results")
    @patch("src.batch_main.sheets_client")
    def test_no_open_batches_is_noop(
        self, mock_sheets, mock_step3, mock_step4, mock_mark,
    ):
        mock_sheets.get_open_batch_ids.return_value = []
        batch_main._resume_open_batches(_make_profile(), poll_max_seconds=600)
        mock_step3.assert_not_called()
        mock_step4.assert_not_called()
        mock_mark.assert_not_called()

    @patch("src.batch_main._mark_batches_done")
    @patch("src.batch_main.step4_generate_pdfs")
    @patch("src.batch_main.step3_wait_and_get_results")
    @patch("src.batch_main.sheets_client")
    def test_state_read_failure_falls_back_to_normal_run(
        self, mock_sheets, mock_step3, mock_step4, mock_mark,
    ):
        """``_バッチ管理`` 読み取り失敗は fail-soft（新規実行として続行）。"""
        mock_sheets.get_open_batch_ids.side_effect = RuntimeError("Sheets down")
        batch_main._resume_open_batches(_make_profile(), poll_max_seconds=600)
        mock_step3.assert_not_called()

    @patch("src.batch_main._mark_batches_done")
    @patch("src.batch_main.step4_generate_pdfs")
    @patch("src.batch_main.step3_wait_and_get_results")
    @patch("src.batch_main.sheets_client")
    def test_expired_batch_recorded_and_skipped(
        self, mock_sheets, mock_step3, mock_step4, mock_mark,
    ):
        """29 日保持期限切れ（NotFound）のバッチは『期限切れ』として記録し、
        永久にレジュームを塞がない。"""
        import anthropic as _anthropic
        import httpx as _httpx
        mock_sheets.get_open_batch_ids.return_value = ["msgbatch_gone"]
        mock_sheets.BATCH_STATE_EXPIRED = "期限切れ(結果喪失)"
        request = _httpx.Request("GET", "https://api.anthropic.com")
        response = _httpx.Response(404, request=request)
        mock_step3.side_effect = _anthropic.NotFoundError(
            "not found", response=response, body=None,
        )
        profile = _make_profile()

        batch_main._resume_open_batches(profile, poll_max_seconds=600)

        mock_sheets.append_batch_record.assert_called_once_with(
            "出力一覧", "msgbatch_gone", "期限切れ(結果喪失)",
        )
        mock_step4.assert_not_called()
        # 期限切れは consumed に入らない（done 記録しない）
        mock_mark.assert_called_once_with("出力一覧", [])

    @patch("src.batch_main._resume_open_batches")
    @patch("src.batch_main.step1_prepare")
    @patch("src.batch_main.discover.resolve_run_config")
    def test_run_step_all_calls_resume_before_step1(
        self, mock_resolve, mock_step1, mock_resume,
    ):
        mock_resolve.return_value = _make_profile()
        mock_step1.return_value = []
        parent = MagicMock()
        parent.attach_mock(mock_resume, "resume")
        parent.attach_mock(mock_step1, "step1")

        batch_main.run(batch_mode=True, step="all")

        call_names = [c[0] for c in parent.mock_calls]
        self.assertLess(
            call_names.index("resume"), call_names.index("step1"),
        )


class TestChunkedBatchSubmission(unittest.TestCase):
    """チャンク分割送信（256MB 上限対策）と複数バッチ ID の取り回し。"""

    @patch("src.batch_main.sheets_client")
    @patch("src.batch_main.comment_generator")
    def test_multiple_chunks_submit_multiple_batches(
        self, mock_gen, mock_sheets,
    ):
        from pathlib import Path as _P
        import tempfile as _tf
        items = _make_batch_items(["001-01-1a.pdf", "002-02-2b.pdf"])
        mock_gen.plan_batch_chunks.return_value = [[items[0]], [items[1]]]
        mock_gen.submit_batch.side_effect = ["msgbatch_1", "msgbatch_2"]
        mock_sheets.BATCH_STATE_SUBMITTED = "投入済み"

        with _tf.TemporaryDirectory() as tmp:
            with patch.object(batch_main, "LOGS_DIR", _P(tmp)):
                batch_ids = batch_main.step2_submit_batch(
                    items, state_target="出力一覧",
                )
                persisted = (_P(tmp) / "batch_id.txt").read_text()

        self.assertEqual(batch_ids, ["msgbatch_1", "msgbatch_2"])
        self.assertEqual(mock_gen.submit_batch.call_count, 2)
        # 1 行 1 ID で永続化
        self.assertEqual(persisted.split(), ["msgbatch_1", "msgbatch_2"])
        # 投入済み記録が両バッチ分
        self.assertEqual(mock_sheets.append_batch_record.call_count, 2)

    @patch("src.batch_main.sheets_client")
    @patch("src.batch_main.comment_generator")
    def test_state_record_failure_does_not_block_submission(
        self, mock_gen, mock_sheets,
    ):
        """Sheets への状態記録失敗はランを止めない（fail-soft）。"""
        from pathlib import Path as _P
        import tempfile as _tf
        items = _make_batch_items(["001-01-1a.pdf"])
        mock_gen.plan_batch_chunks.return_value = [items]
        mock_gen.submit_batch.return_value = "msgbatch_1"
        mock_sheets.append_batch_record.side_effect = RuntimeError("quota")
        mock_sheets.BATCH_STATE_SUBMITTED = "投入済み"

        with _tf.TemporaryDirectory() as tmp:
            with patch.object(batch_main, "LOGS_DIR", _P(tmp)):
                batch_ids = batch_main.step2_submit_batch(
                    items, state_target="出力一覧",
                )
        self.assertEqual(batch_ids, ["msgbatch_1"])

    def test_load_batch_ids_multi_line(self):
        from pathlib import Path as _P
        import tempfile as _tf
        with _tf.TemporaryDirectory() as tmp:
            with patch.object(batch_main, "LOGS_DIR", _P(tmp)):
                (_P(tmp) / "batch_id.txt").write_text(
                    "msgbatch_1\nmsgbatch_2\n\n"
                )
                self.assertEqual(
                    batch_main._load_batch_ids_from_disk(),
                    ["msgbatch_1", "msgbatch_2"],
                )

    def test_load_batch_ids_legacy_single_line(self):
        """旧形式（改行なし単一 ID）も読める（後方互換）。"""
        from pathlib import Path as _P
        import tempfile as _tf
        with _tf.TemporaryDirectory() as tmp:
            with patch.object(batch_main, "LOGS_DIR", _P(tmp)):
                (_P(tmp) / "batch_id.txt").write_text("msgbatch_legacy")
                self.assertEqual(
                    batch_main._load_batch_ids_from_disk(), ["msgbatch_legacy"],
                )

    @patch("src.batch_main._mark_batches_done")
    @patch("src.batch_main.step4_generate_pdfs")
    @patch("src.batch_main.step3_wait_and_get_results")
    @patch("src.batch_main.discover.resolve_run_config")
    def test_recovery_accepts_comma_separated_batch_ids(
        self, mock_resolve, mock_step3, mock_step4, mock_mark,
    ):
        """``--step results --batch-id id1,id2`` で複数バッチを回収できる。"""
        mock_resolve.return_value = _make_profile()
        mock_step3.side_effect = [
            {"file-1": {"comment": "A"}},
            {"file-2": {"comment": "B"}},
        ]

        batch_main.run(
            batch_mode=True, step="results",
            batch_id="msgbatch_1, msgbatch_2",
        )

        self.assertEqual(mock_step3.call_count, 2)
        called_ids = [c.args[0] for c in mock_step3.call_args_list]
        self.assertEqual(called_ids, ["msgbatch_1", "msgbatch_2"])
        # 回収（is_recovery）なので step4 まで完走し、done 記録される
        mock_step4.assert_called_once()
        mock_mark.assert_called_once_with(
            "出力一覧", ["msgbatch_1", "msgbatch_2"],
        )


class TestStep4CaseMapForProcessedMains(unittest.TestCase):
    """CB-3 スキップでも case_map を構築し、添付資料を恒久ロストさせない。

    前回ランが「メイン記録後・添付コピー前」でクラッシュ → 再実行のとき、
    メインは処理済みスキップだが、添付資料はメインと同じ出力先へコピー
    されなければならない。
    """

    def setUp(self):
        from src.config import LOGS_DIR
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        self._att_file = LOGS_DIR / "batch_attachments.json"
        self._att_backup = (
            self._att_file.read_text() if self._att_file.exists() else None
        )

    def tearDown(self):
        if self._att_backup is None:
            self._att_file.unlink(missing_ok=True)
        else:
            self._att_file.write_text(self._att_backup)

    @patch("src.batch_main.pdf_merger")
    @patch("src.batch_main.pdf_creator")
    @patch("src.batch_main.drive_client")
    @patch("src.batch_main.sheets_client")
    @patch("src.batch_main.ensure_fonts")
    def test_attachment_of_skipped_main_still_copied(
        self, mock_fonts, mock_sheets, mock_drive, mock_creator, mock_merger,
    ):
        import json as _json
        _install_step4_mocks(mock_drive, mock_merger, mock_sheets)
        items = _make_batch_items(["001-01-0実践事例.pdf"])
        results = _make_batch_results(1)
        # メインは前回ラン処理済み（CB-3 スキップ対象）
        mock_sheets.get_processed_management_numbers.return_value = {"001-01-0"}
        self._att_file.write_text(_json.dumps([
            {
                "file_id": "id_att",
                "file_name": "001-01-0【添付資料】補足.pdf",
                "management_number": "001-01-0",
            }
        ]))

        batch_main.step4_generate_pdfs(_make_profile(), results, items=items)

        # メインは再アップロードされず、添付資料 1 件だけコピーされる
        upload_names = [
            c.kwargs["file_name"]
            for c in mock_drive.upload_pdf_to_clinic_person.call_args_list
        ]
        self.assertEqual(upload_names, ["001-01-0【添付資料】補足.pdf"])


class TestStep1FailFast(unittest.TestCase):
    """step1 の系統的失敗の fail-fast。"""

    @patch("src.batch_main.pdf_reader")
    @patch("src.batch_main.sheets_client")
    @patch("src.batch_main.drive_client")
    def test_refresh_error_aborts_immediately(
        self, mock_drive, mock_sheets, mock_reader,
    ):
        """Google 認証の恒久失敗（RefreshError）は per-item で握りつぶさず
        即停止する（1000 件ぶんの無駄な失敗ループを防ぐ）。"""
        from google.auth.exceptions import RefreshError as _RefreshError
        mock_drive.list_pdfs.return_value = [
            {"id": f"id_{i}", "name": f"00{i}-01-0実践事例.pdf"}
            for i in range(1, 4)
        ]
        mock_sheets.get_processed_management_numbers.return_value = set()
        mock_sheets.get_recorded_attachment_names.return_value = set()
        mock_drive.download_pdf.side_effect = _RefreshError("token expired")

        with self.assertRaises(_RefreshError):
            batch_main.step1_prepare(_make_profile(), test_count=0)

        # 1 件目で停止し、2 件目以降をダウンロードしに行かない
        self.assertEqual(mock_drive.download_pdf.call_count, 1)

    @patch("src.batch_main.pdf_reader")
    @patch("src.batch_main.sheets_client")
    @patch("src.batch_main.drive_client")
    def test_all_targets_failed_raises_loudly(
        self, mock_drive, mock_sheets, mock_reader,
    ):
        """新規対象があるのに 1 件も準備できないときは緑終了せず停止する。"""
        mock_drive.list_pdfs.return_value = [
            {"id": "id_1", "name": "001-01-0実践事例.pdf"},
        ]
        mock_sheets.get_processed_management_numbers.return_value = set()
        mock_sheets.get_recorded_attachment_names.return_value = set()
        mock_drive.download_pdf.side_effect = OSError("network unreachable")

        with self.assertRaises(RuntimeError) as ctx:
            batch_main.step1_prepare(_make_profile(), test_count=0)
        self.assertIn("すべての準備に失敗", str(ctx.exception))


class TestStep4MissingReportAccuracy(unittest.TestCase):
    """回収実行での「コメント未取得」誤報を修正（Phase 23 PR-1a）。

    ``--step results --batch-id`` 回収・自動レジュームは items を
    ``reconstruct_items_from_drive``（Drive 全メイン PDF 再走査）で作るため、
    今回のバッチに含まれない過去処理済み PDF や、そもそも投入されなかった
    PDF が items に混入する。これらを無条件で missing 扱いすると、回収実行の
    完了マーカー「未取得 N件」が実態と乖離する。
    """

    @patch("src.batch_main.pdf_merger")
    @patch("src.batch_main.pdf_creator")
    @patch("src.batch_main.drive_client")
    @patch("src.batch_main.sheets_client")
    @patch("src.batch_main.ensure_fonts")
    def test_recovery_scenario_classifies_three_cases_correctly(
        self, mock_fonts, mock_sheets, mock_drive, mock_creator, mock_merger,
    ):
        _install_step4_mocks(mock_drive, mock_merger, mock_sheets)
        # 001-01-0: 過去ラン処理済み（results に無い・Drive 再走査で混入）
        # 999-99-9: 管理番号抽出不可（Drive 再走査でのみ発生し得る）
        # 003-03-0: 新規・今回のバッチで成功
        items = _make_batch_items([
            "001-01-0旧事例.pdf",
            "noprefixファイル.pdf",
            "003-03-0新規事例.pdf",
        ])
        # _make_batch_items は custom_id=item_0001.. の連番。3件目だけ結果を持つ。
        results = {items[2]["custom_id"]: {
            "clinic_name": "山田歯科", "person_name": "田中太郎",
            "sample_title": "事例タイトル", "comment": "コメント本文",
        }}
        mock_sheets.get_processed_management_numbers.return_value = {"001-01-0"}

        with self.assertLogs("jissen_comment", level="INFO") as log_ctx:
            batch_main.step4_generate_pdfs(_make_profile(), results, items=items)

        joined = "\n".join(log_ctx.output)
        self.assertNotIn("コメント未取得", joined)
        # メイン処理は 1 件（新規分）だけ upload される
        self.assertEqual(mock_drive.upload_pdf_to_clinic_person.call_count, 1)

    @patch("src.batch_main.pdf_merger")
    @patch("src.batch_main.pdf_creator")
    @patch("src.batch_main.drive_client")
    @patch("src.batch_main.sheets_client")
    @patch("src.batch_main.ensure_fonts")
    def test_true_missing_still_warns(
        self, mock_fonts, mock_sheets, mock_drive, mock_creator, mock_merger,
    ):
        """投入されたはずなのに結果が無い真の missing は従来通り警告する。"""
        _install_step4_mocks(mock_drive, mock_merger, mock_sheets)
        items = _make_batch_items(["001-01-0新規事例.pdf"])
        mock_sheets.get_processed_management_numbers.return_value = set()

        with self.assertLogs("jissen_comment", level="WARNING") as log_ctx:
            batch_main.step4_generate_pdfs(_make_profile(), results={}, items=items)

        self.assertIn("コメント未取得", "\n".join(log_ctx.output))

    @patch("src.batch_main.pdf_merger")
    @patch("src.batch_main.pdf_creator")
    @patch("src.batch_main.drive_client")
    @patch("src.batch_main.sheets_client")
    @patch("src.batch_main.ensure_fonts")
    def test_processed_without_results_skips_silently(
        self, mock_fonts, mock_sheets, mock_drive, mock_creator, mock_merger,
    ):
        """過去ラン処理済みかつ results 無しは per-item WARNING を出さない。"""
        _install_step4_mocks(mock_drive, mock_merger, mock_sheets)
        items = _make_batch_items(["001-01-0旧事例.pdf"])
        mock_sheets.get_processed_management_numbers.return_value = {"001-01-0"}

        import logging
        logger = logging.getLogger("jissen_comment")
        records: list[logging.LogRecord] = []
        handler = logging.Handler()
        handler.emit = records.append  # type: ignore[assignment]
        logger.addHandler(handler)
        try:
            batch_main.step4_generate_pdfs(_make_profile(), results={}, items=items)
        finally:
            logger.removeHandler(handler)

        warnings = [r for r in records if r.levelno >= logging.WARNING]
        self.assertEqual(warnings, [])
        mock_drive.upload_pdf_to_clinic_person.assert_not_called()

    @patch("src.batch_main.pdf_merger")
    @patch("src.batch_main.pdf_creator")
    @patch("src.batch_main.drive_client")
    @patch("src.batch_main.sheets_client")
    @patch("src.batch_main.ensure_fonts")
    def test_unsubmitted_no_mgmt_number_skips_without_warning(
        self, mock_fonts, mock_sheets, mock_drive, mock_creator, mock_merger,
    ):
        """管理番号抽出不可の未投入ファイルは INFO のみで WARNING は出さない。"""
        _install_step4_mocks(mock_drive, mock_merger, mock_sheets)
        items = _make_batch_items(["管理番号なしファイル.pdf"])
        mock_sheets.get_processed_management_numbers.return_value = set()

        import logging
        logger = logging.getLogger("jissen_comment")
        records: list[logging.LogRecord] = []
        handler = logging.Handler()
        handler.emit = records.append  # type: ignore[assignment]
        logger.addHandler(handler)
        try:
            batch_main.step4_generate_pdfs(_make_profile(), results={}, items=items)
        finally:
            logger.removeHandler(handler)

        warnings = [r for r in records if r.levelno >= logging.WARNING]
        self.assertEqual(warnings, [])


class TestRunPollBudgetSharing(unittest.TestCase):
    """resume フェーズと通常フェーズがポーリング予算を分け合う（Phase 23 PR-1b）。

    従来は両フェーズに ``poll_max_seconds`` を独立に満額渡しており、両方
    走ると合計待機が GHA の 6h ジョブ上限を超えて kill され得た。
    """

    @patch("src.batch_main.step4_generate_pdfs")
    @patch("src.batch_main._collect_results_for_batches")
    @patch("src.batch_main.step2_submit_batch")
    @patch("src.batch_main.step1_prepare")
    @patch("src.batch_main._resume_open_batches")
    @patch("src.batch_main.discover.resolve_run_config")
    def test_collect_receives_reduced_budget_after_resume_consumes_time(
        self, mock_resolve, mock_resume, mock_step1, mock_step2,
        mock_collect, mock_step4,
    ):
        mock_resolve.return_value = _make_profile()
        mock_step1.return_value = _make_batch_items(["001-01-1a.pdf"])
        mock_step2.return_value = ["msgbatch_1"]
        mock_collect.return_value = {}

        # monotonic: run() 冒頭の deadline 計算で 1 回、resume 呼び出し前に
        # _remaining_seconds で 1 回、collect 呼び出し前にもう 1 回。
        # 1 時間経過をシミュレートする。
        clock = {"now": 0.0}

        def _fake_monotonic():
            return clock["now"]

        with patch("src.batch_main.time.monotonic", side_effect=_fake_monotonic):
            def _resume_side_effect(cfg, poll_max_seconds):
                clock["now"] += 3600.0  # resume が 1h 消費

            mock_resume.side_effect = _resume_side_effect
            batch_main.run(
                batch_mode=True, step="all", poll_max_minutes=300,  # 300分=18000秒
            )

        collect_call = mock_collect.call_args
        remaining = collect_call.args[1]
        # 18000 - 3600 = 14400 秒。多少の計算誤差を許容して近似確認。
        self.assertAlmostEqual(remaining, 14400, delta=2)

    @patch("src.batch_main.step4_generate_pdfs")
    @patch("src.batch_main._collect_results_for_batches")
    @patch("src.batch_main.step2_submit_batch")
    @patch("src.batch_main.step1_prepare")
    @patch("src.batch_main._resume_open_batches")
    @patch("src.batch_main.discover.resolve_run_config")
    def test_collect_gets_floor_seconds_when_budget_exhausted(
        self, mock_resolve, mock_resume, mock_step1, mock_step2,
        mock_collect, mock_step4,
    ):
        """resume が予算を使い切っても collect には最低 60 秒が渡る。"""
        mock_resolve.return_value = _make_profile()
        mock_step1.return_value = _make_batch_items(["001-01-1a.pdf"])
        mock_step2.return_value = ["msgbatch_1"]
        mock_collect.return_value = {}

        clock = {"now": 0.0}

        def _fake_monotonic():
            return clock["now"]

        with patch("src.batch_main.time.monotonic", side_effect=_fake_monotonic):
            def _resume_side_effect(cfg, poll_max_seconds):
                clock["now"] += 999999.0  # 予算を大幅に使い切る

            mock_resume.side_effect = _resume_side_effect
            batch_main.run(
                batch_mode=True, step="all", poll_max_minutes=300,
            )

        remaining = mock_collect.call_args.args[1]
        self.assertEqual(remaining, 60)

    def test_remaining_seconds_helper(self):
        with patch("src.batch_main.time.monotonic", return_value=100.0):
            self.assertEqual(batch_main._remaining_seconds(1000.0), 900)
            # deadline 超過時はフロア値
            self.assertEqual(batch_main._remaining_seconds(50.0), 60)


class TestLongRunHardening(unittest.TestCase):
    """長時間・反復動作のハードニング（Phase 23 PR-6）。

    - 一時ディレクトリ掃除の全域化（未ガード区間の例外でもリークしない）
    - disk 由来 JSON（batch_results / batch_attachments）の防御的パース
    - ドラフト OFF 時のディスク逐次解放
    """

    def setUp(self):
        from src.config import LOGS_DIR
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        self._att_file = LOGS_DIR / "batch_attachments.json"
        self._att_backup = (
            self._att_file.read_text() if self._att_file.exists() else None
        )

    def tearDown(self):
        if self._att_backup is None:
            self._att_file.unlink(missing_ok=True)
        else:
            self._att_file.write_text(self._att_backup)

    def _make_session_dir(self):
        """mkdtemp を差し替えて掃除の有無を観測できる実ディレクトリを作る。"""
        import tempfile as _tf
        from pathlib import Path as _P
        return _P(_tf.mkdtemp(prefix="test_session_"))

    def _selective_mkdtemp(self, session_dir):
        """session_outputs_dir の mkdtemp だけを差し替える fake を返す。

        step4 はループ内の中間ファイルにも ``tempfile.TemporaryDirectory``
        （内部で mkdtemp を呼ぶ）を使うため、無条件に固定パスを返すと
        TemporaryDirectory の exit がセッションディレクトリを消してしまう。
        prefix で選別し、それ以外は本物に委譲する。
        """
        import tempfile as _tf
        real_mkdtemp = _tf.mkdtemp

        def _fake(*args, **kwargs):
            prefix = kwargs.get("prefix") or (args[2] if len(args) > 2 else None)
            if prefix == "aicomment_batch_outputs_":
                return str(session_dir)
            return real_mkdtemp(*args, **kwargs)

        return _fake

    @patch("src.batch_main.pdf_merger")
    @patch("src.batch_main.pdf_creator")
    @patch("src.batch_main.drive_client")
    @patch("src.batch_main.sheets_client")
    @patch("src.batch_main.ensure_fonts")
    def test_corrupt_attachments_json_raises_clear_error_and_cleans_tempdir(
        self, mock_fonts, mock_sheets, mock_drive, mock_creator, mock_merger,
    ):
        """壊れた batch_attachments.json → 復旧手順つき RuntimeError。
        かつ、（未ガード区間だった箇所の例外でも）一時ディレクトリは削除される。"""
        _install_step4_mocks(mock_drive, mock_merger, mock_sheets)
        mock_sheets.get_processed_management_numbers.return_value = set()
        self._att_file.write_text("{ this is not valid json !!!")
        session_dir = self._make_session_dir()

        with patch(
            "src.batch_main.tempfile.mkdtemp",
            side_effect=self._selective_mkdtemp(session_dir),
        ):
            with self.assertRaises(RuntimeError) as ctx:
                batch_main.step4_generate_pdfs(
                    _make_profile(),
                    results=_make_batch_results(1),
                    items=_make_batch_items(["001-01-0事例.pdf"]),
                )

        self.assertIn("--step prepare", str(ctx.exception))
        # 例外経路でも一時ディレクトリが掃除されている（6a）
        self.assertFalse(session_dir.exists())

    @patch("src.batch_main.pdf_merger")
    @patch("src.batch_main.pdf_creator")
    @patch("src.batch_main.drive_client")
    @patch("src.batch_main.sheets_client")
    @patch("src.batch_main.ensure_fonts")
    def test_success_path_still_cleans_tempdir(
        self, mock_fonts, mock_sheets, mock_drive, mock_creator, mock_merger,
    ):
        """成功パスの掃除は従来どおり（回帰）。"""
        _install_step4_mocks(mock_drive, mock_merger, mock_sheets)
        mock_sheets.get_processed_management_numbers.return_value = set()
        session_dir = self._make_session_dir()

        with patch(
            "src.batch_main.tempfile.mkdtemp",
            side_effect=self._selective_mkdtemp(session_dir),
        ):
            batch_main.step4_generate_pdfs(
                _make_profile(),
                results=_make_batch_results(1),
                items=_make_batch_items(["001-01-0事例.pdf"]),
            )

        self.assertFalse(session_dir.exists())

    @patch("src.batch_main.pdf_merger")
    @patch("src.batch_main.pdf_creator")
    @patch("src.batch_main.drive_client")
    @patch("src.batch_main.sheets_client")
    @patch("src.batch_main.ensure_fonts")
    def test_missing_meta_fields_counted_as_error_not_crash(
        self, mock_fonts, mock_sheets, mock_drive, mock_creator, mock_merger,
    ):
        """disk 由来の旧形式/手編集 results（フィールド欠落）でも KeyError で
        ラン全体が死なず、per-item エラーとして続行する（6b）。"""
        _install_step4_mocks(mock_drive, mock_merger, mock_sheets)
        mock_sheets.get_processed_management_numbers.return_value = set()
        items = _make_batch_items(["001-01-0欠落.pdf", "002-02-0正常.pdf"])
        results = {
            items[0]["custom_id"]: {"clinic_name": "山田歯科"},  # comment 欠落
            items[1]["custom_id"]: {
                "clinic_name": "山田歯科", "person_name": "田中太郎",
                "sample_title": "事例", "comment": "コメント本文",
            },
        }

        with self.assertLogs("jissen_comment", level="ERROR") as log_ctx:
            batch_main.step4_generate_pdfs(_make_profile(), results, items=items)

        joined = "\n".join(log_ctx.output)
        self.assertIn("comment がありません", joined)
        # 欠落 item はスキップ、正常 item は処理される
        self.assertEqual(mock_drive.upload_pdf_to_clinic_person.call_count, 1)

    @patch("src.batch_main.pdf_merger")
    @patch("src.batch_main.pdf_creator")
    @patch("src.batch_main.drive_client")
    @patch("src.batch_main.sheets_client")
    @patch("src.batch_main.ensure_fonts")
    def test_drafts_off_deletes_outputs_incrementally(
        self, mock_fonts, mock_sheets, mock_drive, mock_creator, mock_merger,
    ):
        """ENABLE_GMAIL_DRAFTS=False: アップロード成功直後にサブディレクトリが
        消えている（集約下書きフェーズを待たない）。"""
        _install_step4_mocks(mock_drive, mock_merger, mock_sheets)
        mock_sheets.get_processed_management_numbers.return_value = set()
        session_dir = self._make_session_dir()
        observed: dict[str, bool] = {}

        def _drafts_probe(draft_items):
            # 集約下書きフェーズ到達時点で main_* サブディレクトリが残って
            # いないことを観測する
            observed["main_dirs"] = any(
                p.name.startswith("main_") for p in session_dir.iterdir()
            )

        with patch(
            "src.batch_main.tempfile.mkdtemp",
            side_effect=self._selective_mkdtemp(session_dir),
        ), patch(
            "src.batch_main._create_grouped_drafts_for_batch",
            side_effect=_drafts_probe,
        ), patch.object(batch_main.config, "ENABLE_GMAIL_DRAFTS", False):
            batch_main.step4_generate_pdfs(
                _make_profile(),
                results=_make_batch_results(1),
                items=_make_batch_items(["001-01-0事例.pdf"]),
            )

        self.assertFalse(observed["main_dirs"])

    @patch("src.batch_main.pdf_merger")
    @patch("src.batch_main.pdf_creator")
    @patch("src.batch_main.drive_client")
    @patch("src.batch_main.sheets_client")
    @patch("src.batch_main.ensure_fonts")
    def test_drafts_on_keeps_outputs_until_draft_creation(
        self, mock_fonts, mock_sheets, mock_drive, mock_creator, mock_merger,
    ):
        """ENABLE_GMAIL_DRAFTS=True: 集約下書きフェーズまで出力を保持する
        （現状維持の回帰）。"""
        _install_step4_mocks(mock_drive, mock_merger, mock_sheets)
        mock_sheets.get_processed_management_numbers.return_value = set()
        session_dir = self._make_session_dir()
        observed: dict[str, bool] = {}

        def _drafts_probe(draft_items):
            observed["main_dirs"] = any(
                p.name.startswith("main_") for p in session_dir.iterdir()
            )

        with patch(
            "src.batch_main.tempfile.mkdtemp",
            side_effect=self._selective_mkdtemp(session_dir),
        ), patch(
            "src.batch_main._create_grouped_drafts_for_batch",
            side_effect=_drafts_probe,
        ), patch.object(batch_main.config, "ENABLE_GMAIL_DRAFTS", True):
            batch_main.step4_generate_pdfs(
                _make_profile(),
                results=_make_batch_results(1),
                items=_make_batch_items(["001-01-0事例.pdf"]),
            )

        self.assertTrue(observed["main_dirs"])


class TestStep1ParallelDownload(unittest.TestCase):
    """step1 ダウンロード並列化（Phase 23 PR-2a）。

    既定 workers=1 は従来の逐次コードパス。workers>1 は ThreadPoolExecutor +
    executor.map（投入順 yield）で、items の順序は targets 順で決定的。
    """

    def _pdf_files(self, n: int) -> list[dict]:
        return [
            {"id": f"id_{i:03d}", "name": f"{i:03d}-01-0事例.pdf"}
            for i in range(1, n + 1)
        ]

    @patch("src.batch_main.pdf_reader")
    @patch("src.batch_main.sheets_client")
    @patch("src.batch_main.drive_client")
    def test_parallel_preserves_target_order(
        self, mock_drive, mock_sheets, mock_reader,
    ):
        """並列でも items の順序は targets 順（チャンク境界の決定論）。"""
        import time as _time
        files = self._pdf_files(5)
        mock_drive.list_pdfs.return_value = files
        mock_sheets.get_processed_management_numbers.return_value = set()
        mock_sheets.get_recorded_attachment_names.return_value = set()

        def _slow_download(file_id):
            # 先頭ほど遅くする（完了順 ≠ 投入順の状況を作る）
            _time.sleep(0.05 if file_id == "id_001" else 0.0)
            return f"%PDF {file_id}".encode()

        mock_drive.download_pdf.side_effect = _slow_download
        mock_reader.extract_text.side_effect = (
            lambda data: data.decode().replace("%PDF ", "text-")
        )

        items = batch_main.step1_prepare(
            _make_profile(), test_count=0, download_workers=3,
        )

        self.assertEqual(
            [it["pdf_data_id"] for it in items],
            [f["id"] for f in files],
        )
        self.assertEqual(items[0]["pdf_text"], "text-id_001")

    @patch("src.batch_main.pdf_reader")
    @patch("src.batch_main.sheets_client")
    @patch("src.batch_main.drive_client")
    def test_parallel_single_failure_is_isolated(
        self, mock_drive, mock_sheets, mock_reader,
    ):
        """並列中の 1 件失敗は skipped_records に入り、残りは items に入る。"""
        files = self._pdf_files(3)
        mock_drive.list_pdfs.return_value = files
        mock_sheets.get_processed_management_numbers.return_value = set()
        mock_sheets.get_recorded_attachment_names.return_value = set()

        def _download(file_id):
            if file_id == "id_002":
                raise OSError("network blip")
            return b"%PDF ok"

        mock_drive.download_pdf.side_effect = _download
        mock_reader.extract_text.return_value = "テキスト"

        items = batch_main.step1_prepare(
            _make_profile(), test_count=0, download_workers=3,
        )

        self.assertEqual(
            [it["pdf_data_id"] for it in items], ["id_001", "id_003"],
        )
        import json as _json
        from src.config import LOGS_DIR
        skips = _json.loads((LOGS_DIR / "batch_step1_skips.json").read_text())
        self.assertEqual(
            [s["file_id"] for s in skips if s["reason"] == "download_or_parse_error"],
            ["id_002"],
        )

    @patch("src.batch_main.pdf_reader")
    @patch("src.batch_main.sheets_client")
    @patch("src.batch_main.drive_client")
    def test_parallel_refresh_error_fails_fast(
        self, mock_drive, mock_sheets, mock_reader,
    ):
        """並列でも RefreshError は fail-fast で伝播する。"""
        from google.auth.exceptions import RefreshError as _RefreshError
        mock_drive.list_pdfs.return_value = self._pdf_files(4)
        mock_sheets.get_processed_management_numbers.return_value = set()
        mock_sheets.get_recorded_attachment_names.return_value = set()
        mock_drive.download_pdf.side_effect = _RefreshError("expired")

        with self.assertRaises(_RefreshError):
            batch_main.step1_prepare(
                _make_profile(), test_count=0, download_workers=3,
            )

    @patch("src.batch_main.ThreadPoolExecutor")
    @patch("src.batch_main.pdf_reader")
    @patch("src.batch_main.sheets_client")
    @patch("src.batch_main.drive_client")
    def test_workers_one_uses_sequential_path(
        self, mock_drive, mock_sheets, mock_reader, mock_executor,
    ):
        """workers=1（既定）は ThreadPoolExecutor を使わない（従来パス温存）。"""
        mock_drive.list_pdfs.return_value = self._pdf_files(2)
        mock_sheets.get_processed_management_numbers.return_value = set()
        mock_sheets.get_recorded_attachment_names.return_value = set()
        mock_drive.download_pdf.return_value = b"%PDF ok"
        mock_reader.extract_text.return_value = "テキスト"

        items = batch_main.step1_prepare(
            _make_profile(), test_count=0, download_workers=1,
        )

        self.assertEqual(len(items), 2)
        mock_executor.assert_not_called()


if __name__ == "__main__":
    unittest.main()
