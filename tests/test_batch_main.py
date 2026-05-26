"""batch_main.py のエントリポイントテスト：``--profile`` のパースと、
プロファイルが各ステップ（特に Step1/Step4）に渡されることを検証。
``--target-folder`` 引数の自動検出モードもここでテストする。
"""

from __future__ import annotations

import json
import sys
import unittest
from unittest.mock import patch

from src import batch_main
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
        # 既定: マスター未登録 → AI 抽出値で代用 / メール未登録 → 下書きスキップ
        mock_sheets.read_master_records.return_value = []
        mock_sheets.lookup_clinic_name.return_value = ""
        mock_sheets.lookup_email_by_management_number.return_value = ""


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
        mock_sheets.lookup_email_by_management_number.return_value = ""
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
        """処理済み管理番号の添付資料は batch_attachments.json に保存されない。"""
        mock_drive.list_pdfs.return_value = [
            {"id": "id_att", "name": "001-01-0【添付資料】補足.pdf"},
        ]
        mock_sheets.get_processed_management_numbers.return_value = {"001-01-0"}
        _install_step1_mocks(mock_drive, mock_reader)
        profile = _make_profile()

        batch_main.step1_prepare(profile, test_count=0)

        records = json.loads(self._att_file.read_text())
        self.assertEqual(records, [])

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
        """対応するメインが results に無い添付資料はスキップされ warning が出る。"""
        _install_step4_mocks(mock_drive, mock_merger, mock_sheets)
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
        participant_name="X", venue="",
    ):
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
                "111-22-3", "標準医院名", "tanaka@example.com"
            ),
        ]
        mock_sheets.lookup_clinic_name.side_effect = (
            lambda recs, cn: real_sheets_client.lookup_clinic_name(recs, cn)
        )
        mock_sheets.lookup_email_by_management_number.side_effect = (
            lambda recs, mn: real_sheets_client.lookup_email_by_management_number(recs, mn)
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
            self._master_record("111-22-3", "標準医院名", "t@example.com"),
        ]
        mock_sheets.lookup_clinic_name.side_effect = (
            lambda recs, cn: real_sheets_client.lookup_clinic_name(recs, cn)
        )
        mock_sheets.lookup_email_by_management_number.side_effect = (
            lambda recs, mn: real_sheets_client.lookup_email_by_management_number(recs, mn)
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
        mock_sheets.lookup_email_by_management_number.side_effect = (
            lambda recs, mn: real_sheets_client.lookup_email_by_management_number(recs, mn)
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
        self.assertIn("山田歯科", joined)

    @patch("src.batch_main.pdf_merger")
    @patch("src.batch_main.pdf_creator")
    @patch("src.batch_main.gmail_client")
    @patch("src.batch_main.drive_client")
    @patch("src.batch_main.sheets_client")
    @patch("src.batch_main.ensure_fonts")
    def test_unregistered_email_skips_draft_with_warning(
        self, mock_fonts, mock_sheets, mock_drive, mock_gmail,
        mock_creator, mock_merger,
    ):
        """メール lookup ミス → create_draft 呼ばれず警告。PDF 処理は完了。"""
        from src import sheets_client as real_sheets_client

        mock_sheets.get_recorded_clinic_numbers.return_value = set()
        mock_sheets.read_master_records.return_value = []
        mock_sheets.lookup_clinic_name.side_effect = (
            lambda recs, cn: real_sheets_client.lookup_clinic_name(recs, cn)
        )
        mock_sheets.lookup_email_by_management_number.side_effect = (
            lambda recs, mn: real_sheets_client.lookup_email_by_management_number(recs, mn)
        )
        _install_step4_mocks(mock_drive, mock_merger)
        profile = _make_profile()

        with self.assertLogs("jissen_comment", level="WARNING") as log_ctx:
            batch_main.step4_generate_pdfs(
                profile,
                results=_make_batch_results(1),
                items=_make_batch_items(["111-22-3実践事例.pdf"]),
            )

        mock_gmail.create_draft.assert_not_called()
        joined = "\n".join(log_ctx.output)
        self.assertIn("メール未登録", joined)
        self.assertIn("111-22-3", joined)
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
            self._master_record("111-22-3", "三浦歯科医院", "t@example.com"),
            self._master_record("222-22-3", "山本歯科", "next@example.com"),
        ]
        mock_sheets.lookup_clinic_name.side_effect = (
            lambda recs, cn: real_sheets_client.lookup_clinic_name(recs, cn)
        )
        mock_sheets.lookup_email_by_management_number.side_effect = (
            lambda recs, mn: real_sheets_client.lookup_email_by_management_number(recs, mn)
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
        mock_sheets.lookup_email_by_management_number.side_effect = (
            lambda recs, mn: real_sheets_client.lookup_email_by_management_number(recs, mn)
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
        """添付資料経路でも create_draft が呼ばれる（同じ管理番号、件名はメインと同じ）。"""
        from src import sheets_client as real_sheets_client

        mock_sheets.get_recorded_clinic_numbers.return_value = set()
        mock_sheets.read_master_records.return_value = [
            self._master_record(
                "111-22-3", "三浦歯科医院", "tanaka@example.com"
            ),
        ]
        mock_sheets.lookup_clinic_name.side_effect = (
            lambda recs, cn: real_sheets_client.lookup_clinic_name(recs, cn)
        )
        mock_sheets.lookup_email_by_management_number.side_effect = (
            lambda recs, mn: real_sheets_client.lookup_email_by_management_number(recs, mn)
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

        # メイン + 添付資料 で create_draft が 2 回呼ばれる
        self.assertEqual(mock_gmail.create_draft.call_count, 2)
        # 両方とも同じ to/person_name、CC なし
        for call in mock_gmail.create_draft.call_args_list:
            self.assertEqual(call.kwargs["to_email"], "tanaka@example.com")
            self.assertEqual(call.kwargs["person_name"], "田中太郎")
            self.assertIsNone(call.kwargs["cc_email"])


if __name__ == "__main__":
    unittest.main()
