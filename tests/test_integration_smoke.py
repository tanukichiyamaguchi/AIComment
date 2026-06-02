"""統合スモークテスト（mock ベース E2E）。

ユニットテスト（個別関数）だけでは検出できない「結合バグ」を mock で網羅検証する。

検証対象:
    1. main.py / batch_main.py のフロー全体（呼び出し順・引数受け渡し）
    2. profile.py から drive_client / sheets_client / pdf_merger / comment_generator
       への設定の伝搬
    3. 各プロファイル（default + 2024Q1-Q4）の完走
    4. jissen_default の既存挙動完全維持（リグレッション防止）
    5. エラーパス（API 失敗、PDF 抽出失敗）での状態遷移

設計方針:
    - 本物の API は一切呼ばない。全 mock。
    - profile loader だけは実 YAML を読み込んで検証（環境変数で folder ID を注入）。
    - assert は call_args / call_count を使い、呼び出し順序と引数も検証する。
"""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from src import batch_main, main
from src.config import LOGS_DIR


# ── 共通 env vars（プロファイル loader が必要とするシークレット名） ──
_PROFILE_ENV = {
    "DRIVE_FOLDER_ID": "input_default",
    "DRIVE_OUTPUT_FOLDER_ID": "output_default",
    "SPREADSHEET_ID": "test_sheet_id",
    "DRIVE_FOLDER_JISSEN_2024_Q1": "input_q1",
    "DRIVE_OUTPUT_JISSEN_2024_Q1": "output_q1",
    "DRIVE_FOLDER_JISSEN_2024_Q2": "input_q2",
    "DRIVE_OUTPUT_JISSEN_2024_Q2": "output_q2",
    "DRIVE_FOLDER_JISSEN_2024_Q3": "input_q3",
    "DRIVE_OUTPUT_JISSEN_2024_Q3": "output_q3",
    "DRIVE_FOLDER_JISSEN_2024_Q4": "input_q4",
    "DRIVE_OUTPUT_JISSEN_2024_Q4": "output_q4",
}


def _make_pdf_files(n: int) -> list[dict]:
    """仮想 PDF メタデータを n 件返す。

    実践事例 PDF と同様に、ファイル名先頭へ ``NNN-NN-N`` 形式の管理番号を
    埋め込む（``extract_management_number`` の入力になる）。i 件目は
    ``00i-00-0pdf_000i.pdf`` → 管理番号 ``00i-00-0``。
    """
    return [
        {"id": f"id_{i:04d}", "name": f"{i:03d}-00-0pdf_{i:04d}.pdf"}
        for i in range(1, n + 1)
    ]


def _expected_mgmt_number(i: int) -> str:
    """``_make_pdf_files`` の i 件目（1-indexed）から抽出される管理番号。"""
    return f"{i:03d}-00-0"


def _make_metadata(suffix: str = "") -> dict[str, str]:
    """仮想 metadata（comment_generator の戻り値）。"""
    return {
        "clinic_name": f"山田歯科{suffix}",
        "person_name": f"田中太郎{suffix}",
        "sample_title": f"事例タイトル{suffix}",
        "comment": "コメント本文",
    }


def _install_main_mocks(
    mock_drive, mock_sheets, mock_gen, mock_reader, mock_creator,
    mock_merger, mock_fonts, *, pdf_count: int = 5,
) -> None:
    """main.run() 用の標準モック挙動を一括セットアップする。

    参加者マスター系のデフォルト戻り値（未登録扱い）も同時設定する:
        - read_master_records → []
        - lookup_clinic_name → "" (AI 抽出値にフォールバック)
        - lookup_email_by_clinic_and_person → "" (宛先空で下書き作成)
    """
    mock_drive.list_pdfs.return_value = _make_pdf_files(pdf_count)
    mock_drive.download_pdf.return_value = b"%PDF-1.4 fake"
    mock_drive.upload_pdf_to_clinic_person.return_value = {
        "webViewLink": "https://drive.google.com/fake",
        "clinic_folder_id": "clinic_folder_fake",
    }
    mock_reader.extract_text.return_value = "PDFテキスト"
    mock_gen.generate_comment_with_metadata.return_value = _make_metadata()
    # pdf_merger.make_output_filename は本物のロジックでテストしたいが、
    # 仕様に従い mock 化。ファイル名は呼び出し側で固定値を返す。
    mock_merger.make_output_filename.return_value = (
        "山田歯科＿田中太郎＿事例タイトル.pdf"
    )
    # 参加者マスター系の既定: マスター未登録 → AI 抽出値で代用 / メール未登録
    mock_sheets.read_master_records.return_value = []
    mock_sheets.lookup_clinic_name.return_value = ""
    mock_sheets.lookup_email_by_clinic_and_person.return_value = ""


# ─────────────────────────────────────────────────────────────────────
# 1. 通常モード（main.py）の E2E スモークテスト
# ─────────────────────────────────────────────────────────────────────


class TestMainE2EDefaultProfile(unittest.TestCase):
    """jissen_default プロファイルでの E2E。"""

    def setUp(self):
        self._env_patcher = patch.dict(os.environ, _PROFILE_ENV, clear=False)
        self._env_patcher.start()

    def tearDown(self):
        self._env_patcher.stop()

    @patch("src.main.ensure_fonts")
    @patch("src.main.pdf_merger")
    @patch("src.main.pdf_creator")
    @patch("src.main.pdf_reader")
    @patch("src.main.comment_generator")
    @patch("src.main.sheets_client")
    @patch("src.main.drive_client")
    def test_main_e2e_default_profile_smoke(
        self, mock_drive, mock_sheets, mock_gen, mock_reader,
        mock_creator, mock_merger, mock_fonts,
    ):
        _install_main_mocks(
            mock_drive, mock_sheets, mock_gen, mock_reader,
            mock_creator, mock_merger, mock_fonts,
        )

        main.run(test_count=0, profile_name="jissen_default")

        # 5 件処理 → sheets.append_output_record が 5 回呼ばれる
        self.assertEqual(mock_sheets.append_output_record.call_count, 5)
        # PDF 取得は 1 回（list_pdfs）
        mock_drive.list_pdfs.assert_called_once_with(folder_id="input_default")
        # 各 PDF が download / upload される
        self.assertEqual(mock_drive.download_pdf.call_count, 5)
        self.assertEqual(mock_drive.upload_pdf_to_clinic_person.call_count, 5)
        # 1 件目の管理番号は PDF ファイル名先頭から抽出した値
        first_call = mock_sheets.append_output_record.call_args_list[0]
        self.assertEqual(
            first_call.kwargs["management_number"], _expected_mgmt_number(1)
        )
        self.assertEqual(first_call.kwargs["sheet_name"], "出力一覧")

    @patch("src.main.ensure_fonts")
    @patch("src.main.pdf_merger")
    @patch("src.main.pdf_creator")
    @patch("src.main.pdf_reader")
    @patch("src.main.comment_generator")
    @patch("src.main.sheets_client")
    @patch("src.main.drive_client")
    def test_main_e2e_q1_profile_smoke(
        self, mock_drive, mock_sheets, mock_gen, mock_reader,
        mock_creator, mock_merger, mock_fonts,
    ):
        _install_main_mocks(
            mock_drive, mock_sheets, mock_gen, mock_reader,
            mock_creator, mock_merger, mock_fonts,
        )

        main.run(test_count=0, profile_name="jissen_2024_q1")

        self.assertEqual(mock_sheets.append_output_record.call_count, 5)
        mock_drive.list_pdfs.assert_called_once_with(folder_id="input_q1")
        first_call = mock_sheets.append_output_record.call_args_list[0]
        # 管理番号はプロファイルに依らずファイル名先頭から抽出される
        self.assertEqual(
            first_call.kwargs["management_number"], _expected_mgmt_number(1)
        )
        self.assertEqual(
            first_call.kwargs["sheet_name"], "実践事例_2024Q1_出力一覧"
        )
        # 5 件目はファイル名先頭の管理番号
        fifth = mock_sheets.append_output_record.call_args_list[4]
        self.assertEqual(
            fifth.kwargs["management_number"], _expected_mgmt_number(5)
        )

    @patch("src.main.ensure_fonts")
    @patch("src.main.pdf_merger")
    @patch("src.main.pdf_creator")
    @patch("src.main.pdf_reader")
    @patch("src.main.comment_generator")
    @patch("src.main.sheets_client")
    @patch("src.main.drive_client")
    def test_main_e2e_q2_profile_smoke(
        self, mock_drive, mock_sheets, mock_gen, mock_reader,
        mock_creator, mock_merger, mock_fonts,
    ):
        _install_main_mocks(
            mock_drive, mock_sheets, mock_gen, mock_reader,
            mock_creator, mock_merger, mock_fonts,
        )

        main.run(test_count=0, profile_name="jissen_2024_q2")

        mock_drive.list_pdfs.assert_called_once_with(folder_id="input_q2")
        first_call = mock_sheets.append_output_record.call_args_list[0]
        self.assertEqual(
            first_call.kwargs["management_number"], _expected_mgmt_number(1)
        )
        self.assertEqual(
            first_call.kwargs["sheet_name"], "実践事例_2024Q2_出力一覧"
        )

    @patch("src.main.ensure_fonts")
    @patch("src.main.pdf_merger")
    @patch("src.main.pdf_creator")
    @patch("src.main.pdf_reader")
    @patch("src.main.comment_generator")
    @patch("src.main.sheets_client")
    @patch("src.main.drive_client")
    def test_main_e2e_q3_profile_smoke(
        self, mock_drive, mock_sheets, mock_gen, mock_reader,
        mock_creator, mock_merger, mock_fonts,
    ):
        _install_main_mocks(
            mock_drive, mock_sheets, mock_gen, mock_reader,
            mock_creator, mock_merger, mock_fonts,
        )

        main.run(test_count=0, profile_name="jissen_2024_q3")

        mock_drive.list_pdfs.assert_called_once_with(folder_id="input_q3")
        first_call = mock_sheets.append_output_record.call_args_list[0]
        self.assertEqual(
            first_call.kwargs["management_number"], _expected_mgmt_number(1)
        )
        self.assertEqual(
            first_call.kwargs["sheet_name"], "実践事例_2024Q3_出力一覧"
        )

    @patch("src.main.ensure_fonts")
    @patch("src.main.pdf_merger")
    @patch("src.main.pdf_creator")
    @patch("src.main.pdf_reader")
    @patch("src.main.comment_generator")
    @patch("src.main.sheets_client")
    @patch("src.main.drive_client")
    def test_main_e2e_q4_profile_smoke(
        self, mock_drive, mock_sheets, mock_gen, mock_reader,
        mock_creator, mock_merger, mock_fonts,
    ):
        _install_main_mocks(
            mock_drive, mock_sheets, mock_gen, mock_reader,
            mock_creator, mock_merger, mock_fonts,
        )

        main.run(test_count=0, profile_name="jissen_2024_q4")

        mock_drive.list_pdfs.assert_called_once_with(folder_id="input_q4")
        first_call = mock_sheets.append_output_record.call_args_list[0]
        self.assertEqual(
            first_call.kwargs["management_number"], _expected_mgmt_number(1)
        )
        self.assertEqual(
            first_call.kwargs["sheet_name"], "実践事例_2024Q4_出力一覧"
        )


# ─────────────────────────────────────────────────────────────────────
# 2. プロファイル間相互汚染テスト
# ─────────────────────────────────────────────────────────────────────


class TestProfileCrossContamination(unittest.TestCase):
    """異なるプロファイルが互いの sheet / folder に書き込まない。"""

    def setUp(self):
        self._env_patcher = patch.dict(os.environ, _PROFILE_ENV, clear=False)
        self._env_patcher.start()

    def tearDown(self):
        self._env_patcher.stop()

    @patch("src.main.ensure_fonts")
    @patch("src.main.pdf_merger")
    @patch("src.main.pdf_creator")
    @patch("src.main.pdf_reader")
    @patch("src.main.comment_generator")
    @patch("src.main.sheets_client")
    @patch("src.main.drive_client")
    def test_q1_profile_does_not_pollute_q2_sheet(
        self, mock_drive, mock_sheets, mock_gen, mock_reader,
        mock_creator, mock_merger, mock_fonts,
    ):
        _install_main_mocks(
            mock_drive, mock_sheets, mock_gen, mock_reader,
            mock_creator, mock_merger, mock_fonts,
        )

        main.run(test_count=0, profile_name="jissen_2024_q1")

        # 全 append 呼び出しが Q1 シートに対するもの。Q2 シートには触れない。
        sheet_names = {
            c.kwargs["sheet_name"]
            for c in mock_sheets.append_output_record.call_args_list
        }
        self.assertEqual(sheet_names, {"実践事例_2024Q1_出力一覧"})
        self.assertNotIn("実践事例_2024Q2_出力一覧", sheet_names)
        # フォルダ ID も Q2 には触れない
        upload_kwargs = mock_drive.upload_pdf_to_clinic_person.call_args_list
        upload_folders = {c.kwargs["output_root_folder_id"] for c in upload_kwargs}
        self.assertEqual(upload_folders, {"output_q1"})

    @patch("src.main.ensure_fonts")
    @patch("src.main.pdf_merger")
    @patch("src.main.pdf_creator")
    @patch("src.main.pdf_reader")
    @patch("src.main.comment_generator")
    @patch("src.main.sheets_client")
    @patch("src.main.drive_client")
    def test_default_profile_uses_legacy_sheet(
        self, mock_drive, mock_sheets, mock_gen, mock_reader,
        mock_creator, mock_merger, mock_fonts,
    ):
        _install_main_mocks(
            mock_drive, mock_sheets, mock_gen, mock_reader,
            mock_creator, mock_merger, mock_fonts, pdf_count=2,
        )

        main.run(test_count=0, profile_name="jissen_default")

        for c in mock_sheets.append_output_record.call_args_list:
            self.assertEqual(c.kwargs["sheet_name"], "出力一覧")

    @patch("src.main.ensure_fonts")
    @patch("src.main.pdf_merger")
    @patch("src.main.pdf_creator")
    @patch("src.main.pdf_reader")
    @patch("src.main.comment_generator")
    @patch("src.main.sheets_client")
    @patch("src.main.drive_client")
    def test_quarterly_profile_uses_dedicated_sheet(
        self, mock_drive, mock_sheets, mock_gen, mock_reader,
        mock_creator, mock_merger, mock_fonts,
    ):
        expected = {
            "jissen_2024_q1": "実践事例_2024Q1_出力一覧",
            "jissen_2024_q2": "実践事例_2024Q2_出力一覧",
            "jissen_2024_q3": "実践事例_2024Q3_出力一覧",
            "jissen_2024_q4": "実践事例_2024Q4_出力一覧",
        }
        for profile_name, expected_sheet in expected.items():
            mock_drive.reset_mock()
            mock_sheets.reset_mock()
            mock_gen.reset_mock()
            mock_reader.reset_mock()
            mock_creator.reset_mock()
            mock_merger.reset_mock()
            _install_main_mocks(
                mock_drive, mock_sheets, mock_gen, mock_reader,
                mock_creator, mock_merger, mock_fonts, pdf_count=1,
            )

            main.run(test_count=0, profile_name=profile_name)

            sheet_names = {
                c.kwargs["sheet_name"]
                for c in mock_sheets.append_output_record.call_args_list
            }
            self.assertEqual(
                sheet_names, {expected_sheet},
                f"profile={profile_name} の sheet_name 不一致: {sheet_names}",
            )


# ─────────────────────────────────────────────────────────────────────
# 3. 管理番号（PDF ファイル名先頭からの抽出）の結合テスト
# ─────────────────────────────────────────────────────────────────────


class TestManagementNumberFromFilename(unittest.TestCase):
    """管理番号は実践事例 PDF のファイル名先頭（NNN-NN-N）から抽出される。

    自動採番は廃止済み。各 PDF の管理番号は、その PDF のファイル名先頭の
    8 文字コードと一致しなければならない。
    """

    def setUp(self):
        self._env_patcher = patch.dict(os.environ, _PROFILE_ENV, clear=False)
        self._env_patcher.start()

    def tearDown(self):
        self._env_patcher.stop()

    @patch("src.main.ensure_fonts")
    @patch("src.main.pdf_merger")
    @patch("src.main.pdf_creator")
    @patch("src.main.pdf_reader")
    @patch("src.main.comment_generator")
    @patch("src.main.sheets_client")
    @patch("src.main.drive_client")
    def test_default_profile_extracts_each_filename_management_number(
        self, mock_drive, mock_sheets, mock_gen, mock_reader,
        mock_creator, mock_merger, mock_fonts,
    ):
        """各 PDF の管理番号 = その PDF のファイル名先頭コード（default プロファイル）。"""
        _install_main_mocks(
            mock_drive, mock_sheets, mock_gen, mock_reader,
            mock_creator, mock_merger, mock_fonts, pdf_count=3,
        )

        main.run(test_count=0, profile_name="jissen_default")

        mgmt_nums = [
            c.kwargs["management_number"]
            for c in mock_sheets.append_output_record.call_args_list
        ]
        self.assertEqual(
            mgmt_nums,
            [_expected_mgmt_number(i) for i in (1, 2, 3)],
        )

    @patch("src.main.ensure_fonts")
    @patch("src.main.pdf_merger")
    @patch("src.main.pdf_creator")
    @patch("src.main.pdf_reader")
    @patch("src.main.comment_generator")
    @patch("src.main.sheets_client")
    @patch("src.main.drive_client")
    def test_quarterly_profile_uses_same_filename_extraction(
        self, mock_drive, mock_sheets, mock_gen, mock_reader,
        mock_creator, mock_merger, mock_fonts,
    ):
        """プロファイルに依らず管理番号はファイル名抽出（Q1 でも prefix を付けない）。"""
        _install_main_mocks(
            mock_drive, mock_sheets, mock_gen, mock_reader,
            mock_creator, mock_merger, mock_fonts, pdf_count=3,
        )

        main.run(test_count=0, profile_name="jissen_2024_q1")

        mgmt_nums = [
            c.kwargs["management_number"]
            for c in mock_sheets.append_output_record.call_args_list
        ]
        self.assertEqual(
            mgmt_nums,
            [_expected_mgmt_number(i) for i in (1, 2, 3)],
        )

    @patch("src.main.ensure_fonts")
    @patch("src.main.pdf_merger")
    @patch("src.main.pdf_creator")
    @patch("src.main.pdf_reader")
    @patch("src.main.comment_generator")
    @patch("src.main.sheets_client")
    @patch("src.main.drive_client")
    def test_management_number_preserves_existing_embedded_codes(
        self, mock_drive, mock_sheets, mock_gen, mock_reader,
        mock_creator, mock_merger, mock_fonts,
    ):
        """ファイル名先頭の既存コードがそのまま管理番号になる（採番しない）。"""
        mock_drive.list_pdfs.return_value = [
            {"id": "id_a", "name": "088-12-3実践事例タイトル.pdf"},
            {"id": "id_b", "name": "088-12-4_別タイトル.pdf"},
        ]
        mock_drive.download_pdf.return_value = b"%PDF-1.4 fake"
        mock_drive.upload_pdf_to_clinic_person.return_value = {
            "webViewLink": "https://drive.google.com/fake",
            "clinic_folder_id": "clinic_folder_fake",
        }
        mock_reader.extract_text.return_value = "PDFテキスト"
        mock_gen.generate_comment_with_metadata.return_value = _make_metadata()
        mock_merger.make_output_filename.return_value = "out.pdf"

        main.run(test_count=0, profile_name="jissen_default")

        mgmt_nums = [
            c.kwargs["management_number"]
            for c in mock_sheets.append_output_record.call_args_list
        ]
        self.assertEqual(mgmt_nums, ["088-12-3", "088-12-4"])

    @patch("src.main.ensure_fonts")
    @patch("src.main.pdf_merger")
    @patch("src.main.pdf_creator")
    @patch("src.main.pdf_reader")
    @patch("src.main.comment_generator")
    @patch("src.main.sheets_client")
    @patch("src.main.drive_client")
    def test_unextractable_filename_is_skipped_and_warns(
        self, mock_drive, mock_sheets, mock_gen, mock_reader,
        mock_creator, mock_merger, mock_fonts,
    ):
        """先頭が NNN-NN-N でない PDF はスキップされ、warning にファイル名が出る。

        管理番号を持たない PDF は重複検知が原理的に不可能なため、再実行のたび
        サイレントに再処理せずスキップして可視化する（増分処理 / Q1=B 仕様）。
        """
        mock_drive.list_pdfs.return_value = [
            {"id": "id_a", "name": "012-03-4正常な事例.pdf"},
            {"id": "id_b", "name": "管理番号のないファイル.pdf"},
        ]
        mock_sheets.get_processed_management_numbers.return_value = set()
        mock_drive.download_pdf.return_value = b"%PDF-1.4 fake"
        mock_drive.upload_pdf_to_clinic_person.return_value = {
            "webViewLink": "https://drive.google.com/fake",
            "clinic_folder_id": "clinic_folder_fake",
        }
        mock_reader.extract_text.return_value = "PDFテキスト"
        mock_gen.generate_comment_with_metadata.return_value = _make_metadata()
        mock_merger.make_output_filename.return_value = "out.pdf"

        with self.assertLogs("jissen_comment", level="WARNING") as log_ctx:
            main.run(test_count=0, profile_name="jissen_default")

        mgmt_nums = [
            c.kwargs["management_number"]
            for c in mock_sheets.append_output_record.call_args_list
        ]
        # 管理番号ありの 1 件だけ処理され、管理番号なしはスキップ
        self.assertEqual(mgmt_nums, ["012-03-4"])
        # スキップされた PDF は download すらされない（コスト削減）
        download_ids = [c.args[0] for c in mock_drive.download_pdf.call_args_list]
        self.assertEqual(download_ids, ["id_a"])
        # warning に抽出不能ファイル名が含まれる
        self.assertIn("管理番号のないファイル.pdf", "\n".join(log_ctx.output))


# ─────────────────────────────────────────────────────────────────────
# 4. エラーパスの状態遷移テスト
# ─────────────────────────────────────────────────────────────────────


class TestMainErrorPaths(unittest.TestCase):
    """API 失敗・PDF 抽出失敗時に stats と副作用が想定通り遷移する。"""

    def setUp(self):
        self._env_patcher = patch.dict(os.environ, _PROFILE_ENV, clear=False)
        self._env_patcher.start()

    def tearDown(self):
        self._env_patcher.stop()

    @patch("src.main.ensure_fonts")
    @patch("src.main.pdf_merger")
    @patch("src.main.pdf_creator")
    @patch("src.main.pdf_reader")
    @patch("src.main.comment_generator")
    @patch("src.main.sheets_client")
    @patch("src.main.drive_client")
    def test_main_pdf_extract_failure_skips_safely(
        self, mock_drive, mock_sheets, mock_gen, mock_reader,
        mock_creator, mock_merger, mock_fonts,
    ):
        """pdf_reader.extract_text が空文字を返す → スキップ、シート追記しない。"""
        _install_main_mocks(
            mock_drive, mock_sheets, mock_gen, mock_reader,
            mock_creator, mock_merger, mock_fonts, pdf_count=3,
        )
        # 抽出失敗を全件にシミュレート
        mock_reader.extract_text.return_value = ""

        main.run(test_count=0, profile_name="jissen_default")

        # シート追記 / Claude / Drive アップロードは一切走らない
        mock_sheets.append_output_record.assert_not_called()
        mock_gen.generate_comment_with_metadata.assert_not_called()
        mock_drive.upload_pdf_to_clinic_person.assert_not_called()

    @patch("src.main.ensure_fonts")
    @patch("src.main.pdf_merger")
    @patch("src.main.pdf_creator")
    @patch("src.main.pdf_reader")
    @patch("src.main.comment_generator")
    @patch("src.main.sheets_client")
    @patch("src.main.drive_client")
    def test_main_claude_api_failure_records_error(
        self, mock_drive, mock_sheets, mock_gen, mock_reader,
        mock_creator, mock_merger, mock_fonts,
    ):
        """comment_generator が例外 → 当該件はエラーに、次の件は処理される。"""
        _install_main_mocks(
            mock_drive, mock_sheets, mock_gen, mock_reader,
            mock_creator, mock_merger, mock_fonts, pdf_count=3,
        )
        # 1 件目だけ例外を投げる
        mock_gen.generate_comment_with_metadata.side_effect = [
            RuntimeError("Claude APIエラー"),
            _make_metadata(suffix="_2"),
            _make_metadata(suffix="_3"),
        ]

        main.run(test_count=0, profile_name="jissen_default")

        # 1 件目はエラー → シート追記なし、2-3 件目は成功
        self.assertEqual(mock_sheets.append_output_record.call_count, 2)
        # 最初の成功は 2 件目。管理番号はその PDF のファイル名先頭から抽出され、
        # 1 件目のエラーに影響されない（採番ではなく抽出のため）。
        first_success = mock_sheets.append_output_record.call_args_list[0]
        self.assertEqual(
            first_success.kwargs["management_number"], _expected_mgmt_number(2)
        )

    @patch("src.main.ensure_fonts")
    @patch("src.main.pdf_merger")
    @patch("src.main.pdf_creator")
    @patch("src.main.pdf_reader")
    @patch("src.main.comment_generator")
    @patch("src.main.sheets_client")
    @patch("src.main.drive_client")
    def test_main_drive_upload_failure_records_error(
        self, mock_drive, mock_sheets, mock_gen, mock_reader,
        mock_creator, mock_merger, mock_fonts,
    ):
        """drive_client.upload が例外 → エラー扱い、シート追記しない。"""
        _install_main_mocks(
            mock_drive, mock_sheets, mock_gen, mock_reader,
            mock_creator, mock_merger, mock_fonts, pdf_count=2,
        )
        mock_drive.upload_pdf_to_clinic_person.side_effect = RuntimeError("upload失敗")

        main.run(test_count=0, profile_name="jissen_default")

        mock_sheets.append_output_record.assert_not_called()

    @patch("src.main.ensure_fonts")
    @patch("src.main.pdf_merger")
    @patch("src.main.pdf_creator")
    @patch("src.main.pdf_reader")
    @patch("src.main.comment_generator")
    @patch("src.main.sheets_client")
    @patch("src.main.drive_client")
    def test_main_sheets_write_failure_records_error(
        self, mock_drive, mock_sheets, mock_gen, mock_reader,
        mock_creator, mock_merger, mock_fonts,
    ):
        """sheets_client.append が例外 → エラー扱い。

        ただし PDF は既に Drive にアップロード済みのため、
        部分成功状態（Drive あり / シートなし）になることをテストで明示する。
        """
        _install_main_mocks(
            mock_drive, mock_sheets, mock_gen, mock_reader,
            mock_creator, mock_merger, mock_fonts, pdf_count=2,
        )
        mock_sheets.append_output_record.side_effect = RuntimeError("sheets失敗")

        main.run(test_count=0, profile_name="jissen_default")

        # シート追記は 2 件試行されたが、全て例外
        self.assertEqual(mock_sheets.append_output_record.call_count, 2)
        # Drive アップロードは試行され、部分成功状態が残る
        self.assertEqual(mock_drive.upload_pdf_to_clinic_person.call_count, 2)


# ─────────────────────────────────────────────────────────────────────
# 5. Batch モード（batch_main.py）の E2E スモークテスト
# ─────────────────────────────────────────────────────────────────────


class TestBatchE2E(unittest.TestCase):
    """batch_main の Step1→Step2→Step3→Step4 全体フロー。"""

    def setUp(self):
        self._env_patcher = patch.dict(os.environ, _PROFILE_ENV, clear=False)
        self._env_patcher.start()
        # batch_prep.json の汚染を避けるためバックアップ
        self._prep_file = LOGS_DIR / "batch_prep.json"
        self._batch_id_file = LOGS_DIR / "batch_id.txt"
        self._prep_backup = (
            self._prep_file.read_text() if self._prep_file.exists() else None
        )
        self._batch_id_backup = (
            self._batch_id_file.read_text()
            if self._batch_id_file.exists() else None
        )

    def tearDown(self):
        self._env_patcher.stop()
        # 元の状態を復元
        if self._prep_backup is None:
            self._prep_file.unlink(missing_ok=True)
        else:
            self._prep_file.write_text(self._prep_backup)
        if self._batch_id_backup is None:
            self._batch_id_file.unlink(missing_ok=True)
        else:
            self._batch_id_file.write_text(self._batch_id_backup)

    def _install_batch_mocks(
        self, mock_drive, mock_sheets, mock_gen, mock_reader,
        mock_creator, mock_merger, mock_fonts, *, pdf_count: int = 5,
    ) -> None:
        mock_drive.list_pdfs.return_value = _make_pdf_files(pdf_count)
        mock_drive.download_pdf.return_value = b"%PDF-1.4 fake"
        mock_drive.upload_pdf_to_clinic_person.return_value = {
            "webViewLink": "https://drive.google.com/fake",
            "clinic_folder_id": "clinic_folder_fake",
        }
        mock_reader.extract_text.return_value = "PDFテキスト"
        mock_gen.submit_batch.return_value = "batch_test_001"
        mock_gen.get_batch_status.return_value = {
            "status": "ended",
            "request_counts": {
                "processing": 0, "succeeded": pdf_count,
                "errored": 0, "canceled": 0, "expired": 0,
            },
        }
        # custom_id は Drive file id 由来の安定 ID（step1 が _custom_id_for_file
        # で生成）。回収のための内容アドレス化に合わせ、結果も同じ key で返す。
        results = {
            batch_main._custom_id_for_file(f["id"]): _make_metadata(suffix=f"_{i}")
            for i, f in enumerate(_make_pdf_files(pdf_count), start=1)
        }
        mock_gen.get_batch_results.return_value = (results, [])
        mock_merger.make_output_filename.return_value = (
            "山田歯科＿田中太郎＿事例タイトル.pdf"
        )

    @patch("src.batch_main.ensure_fonts")
    @patch("src.batch_main.pdf_merger")
    @patch("src.batch_main.pdf_creator")
    @patch("src.batch_main.pdf_reader")
    @patch("src.batch_main.comment_generator")
    @patch("src.batch_main.sheets_client")
    @patch("src.batch_main.drive_client")
    def test_batch_e2e_default_profile_smoke(
        self, mock_drive, mock_sheets, mock_gen, mock_reader,
        mock_creator, mock_merger, mock_fonts,
    ):
        self._install_batch_mocks(
            mock_drive, mock_sheets, mock_gen, mock_reader,
            mock_creator, mock_merger, mock_fonts,
        )

        # step3 の polling を即終了させるため poll_interval=0、max_wait は大きく
        with patch("src.batch_main.time.sleep"):
            batch_main.run(
                batch_mode=True, test_count=0, step="all",
                profile_name="jissen_default",
            )

        # 5 件処理 → シート 5 件、PDF 5 件アップロード
        self.assertEqual(mock_sheets.append_output_record.call_count, 5)
        self.assertEqual(
            mock_drive.upload_pdf_to_clinic_person.call_count, 5,
        )
        # Step1〜4 が順に呼ばれた
        mock_drive.list_pdfs.assert_called_once_with(folder_id="input_default")
        mock_gen.submit_batch.assert_called_once()
        mock_gen.get_batch_status.assert_called()
        mock_gen.get_batch_results.assert_called_once_with("batch_test_001")
        # シート名は default、管理番号はファイル名先頭から抽出
        first_call = mock_sheets.append_output_record.call_args_list[0]
        self.assertEqual(first_call.kwargs["sheet_name"], "出力一覧")
        self.assertEqual(
            first_call.kwargs["management_number"], _expected_mgmt_number(1)
        )

    @patch("src.batch_main.ensure_fonts")
    @patch("src.batch_main.pdf_merger")
    @patch("src.batch_main.pdf_creator")
    @patch("src.batch_main.pdf_reader")
    @patch("src.batch_main.comment_generator")
    @patch("src.batch_main.sheets_client")
    @patch("src.batch_main.drive_client")
    def test_batch_e2e_q1_profile_smoke(
        self, mock_drive, mock_sheets, mock_gen, mock_reader,
        mock_creator, mock_merger, mock_fonts,
    ):
        self._install_batch_mocks(
            mock_drive, mock_sheets, mock_gen, mock_reader,
            mock_creator, mock_merger, mock_fonts,
        )

        with patch("src.batch_main.time.sleep"):
            batch_main.run(
                batch_mode=True, test_count=0, step="all",
                profile_name="jissen_2024_q1",
            )

        mock_drive.list_pdfs.assert_called_once_with(folder_id="input_q1")
        first_call = mock_sheets.append_output_record.call_args_list[0]
        self.assertEqual(
            first_call.kwargs["sheet_name"], "実践事例_2024Q1_出力一覧",
        )
        self.assertEqual(
            first_call.kwargs["management_number"], _expected_mgmt_number(1),
        )

    @patch("src.batch_main.ensure_fonts")
    @patch("src.batch_main.pdf_merger")
    @patch("src.batch_main.pdf_creator")
    @patch("src.batch_main.pdf_reader")
    @patch("src.batch_main.comment_generator")
    @patch("src.batch_main.sheets_client")
    @patch("src.batch_main.drive_client")
    def test_batch_step4_handles_missing_custom_id(
        self, mock_drive, mock_sheets, mock_gen, mock_reader,
        mock_creator, mock_merger, mock_fonts,
    ):
        """results に欠落 custom_id がある場合、stats["missing"] でカウントされる。"""
        self._install_batch_mocks(
            mock_drive, mock_sheets, mock_gen, mock_reader,
            mock_creator, mock_merger, mock_fonts, pdf_count=3,
        )
        # 3 件のうち 1 件だけ結果が欠落
        items = [
            {"custom_id": f"item_{i:04d}",
             "pdf_data_id": f"id_{i:04d}",
             "pdf_file_name": f"{i:03d}-00-0pdf_{i:04d}.pdf"}
            for i in range(1, 4)
        ]
        results = {
            "item_0001": _make_metadata(suffix="_1"),
            # item_0002 が抜けている
            "item_0003": _make_metadata(suffix="_3"),
        }
        from src.profile import load_profile
        profile = load_profile("jissen_default")

        batch_main.step4_generate_pdfs(profile, results=results, items=items)

        # シート追記は 2 件のみ（欠落分はスキップ）
        self.assertEqual(mock_sheets.append_output_record.call_count, 2)
        # 管理番号は各 item の pdf_file_name 先頭から抽出（item_0002 欠落の影響なし）
        mgmt_nums = [
            c.kwargs["management_number"]
            for c in mock_sheets.append_output_record.call_args_list
        ]
        self.assertEqual(mgmt_nums, ["001-00-0", "003-00-0"])

    @patch("src.batch_main.ensure_fonts")
    @patch("src.batch_main.pdf_merger")
    @patch("src.batch_main.pdf_creator")
    @patch("src.batch_main.pdf_reader")
    @patch("src.batch_main.comment_generator")
    @patch("src.batch_main.sheets_client")
    @patch("src.batch_main.drive_client")
    def test_batch_prep_persists_pdf_text_for_split_step_runs(
        self, mock_drive, mock_sheets, mock_gen, mock_reader,
        mock_creator, mock_merger, mock_fonts,
    ):
        """Step1 の prep ファイルに **pdf_text が含まれる**（CB-1 修正）。

        旧仕様（pdf_text を捨てる）だと、別 GHA 実行から `--step submit`
        を呼んだとき items から pdf_text が消えて Claude Batch API に
        空 prompt を投げてしまう本番事故が起こる。CB-1 で pdf_text を
        永続化するように修正したので、その回帰防止。
        """
        self._install_batch_mocks(
            mock_drive, mock_sheets, mock_gen, mock_reader,
            mock_creator, mock_merger, mock_fonts, pdf_count=2,
        )
        # Step 1 実行 → prep_file に保存（pdf_text を含む新仕様）
        from src.profile import load_profile
        profile = load_profile("jissen_default")
        items = batch_main.step1_prepare(profile, test_count=0)

        # prep_file の中身に **pdf_text が含まれる**ことを確認（CB-1）
        prep_data = json.loads((LOGS_DIR / "batch_prep.json").read_text())
        self.assertGreater(len(prep_data), 0)
        for item in prep_data:
            self.assertIn("pdf_text", item, "CB-1: pdf_text must persist")
            self.assertIn("pdf_data_id", item)
            self.assertIn("pdf_file_name", item)
            self.assertTrue(item["pdf_text"], "pdf_text 非空")

        # Step4 で prep_file 経由（items=None で再読込）でも再ダウンロード可能
        results = {
            batch_main._custom_id_for_file("id_0001"): _make_metadata(suffix="_1"),
            batch_main._custom_id_for_file("id_0002"): _make_metadata(suffix="_2"),
        }
        mock_drive.download_pdf.reset_mock()
        # items=None だとプレファイルからロード
        batch_main.step4_generate_pdfs(profile, results=results, items=None)

        # 各 item の pdf_data_id で download_pdf が再呼び出しされた
        download_args = [
            c.args[0] for c in mock_drive.download_pdf.call_args_list
        ]
        self.assertEqual(set(download_args), {"id_0001", "id_0002"})


# ─────────────────────────────────────────────────────────────────────
# 6. プロファイル切り替えの整合性テスト
# ─────────────────────────────────────────────────────────────────────


class TestProfileSwitching(unittest.TestCase):
    """ProfileConfig 取得経路と引数フォールバックの整合性。"""

    def setUp(self):
        self._env_patcher = patch.dict(os.environ, _PROFILE_ENV, clear=False)
        self._env_patcher.start()

    def tearDown(self):
        self._env_patcher.stop()

    @patch("src.main.ensure_fonts")
    @patch("src.main.pdf_merger")
    @patch("src.main.pdf_creator")
    @patch("src.main.pdf_reader")
    @patch("src.main.comment_generator")
    @patch("src.main.sheets_client")
    @patch("src.main.drive_client")
    def test_workflow_input_profile_overrides_default(
        self, mock_drive, mock_sheets, mock_gen, mock_reader,
        mock_creator, mock_merger, mock_fonts,
    ):
        """--profile jissen_2024_q1 が drive_client.list_pdfs(input_q1) を呼ぶ。"""
        _install_main_mocks(
            mock_drive, mock_sheets, mock_gen, mock_reader,
            mock_creator, mock_merger, mock_fonts, pdf_count=0,
        )

        main.run(test_count=0, profile_name="jissen_2024_q1")

        mock_drive.list_pdfs.assert_called_once_with(folder_id="input_q1")

    @patch("src.main.ensure_fonts")
    @patch("src.main.pdf_merger")
    @patch("src.main.pdf_creator")
    @patch("src.main.pdf_reader")
    @patch("src.main.comment_generator")
    @patch("src.main.sheets_client")
    @patch("src.main.drive_client")
    def test_profile_not_specified_falls_back_to_default(
        self, mock_drive, mock_sheets, mock_gen, mock_reader,
        mock_creator, mock_merger, mock_fonts,
    ):
        """profile_name 引数省略時は jissen_default。"""
        _install_main_mocks(
            mock_drive, mock_sheets, mock_gen, mock_reader,
            mock_creator, mock_merger, mock_fonts, pdf_count=0,
        )

        main.run(test_count=0)

        mock_drive.list_pdfs.assert_called_once_with(folder_id="input_default")

    @patch("src.main.ensure_fonts")
    @patch("src.main.pdf_merger")
    @patch("src.main.pdf_creator")
    @patch("src.main.pdf_reader")
    @patch("src.main.comment_generator")
    @patch("src.main.sheets_client")
    @patch("src.main.drive_client")
    def test_profile_with_missing_secret_raises_before_processing(
        self, mock_drive, mock_sheets, mock_gen, mock_reader,
        mock_creator, mock_merger, mock_fonts,
    ):
        """必要な env var 欠落 → ValueError、PDF 処理に進まない。"""
        # Q2 の secret を欠落させる
        clean_env = {
            k: v for k, v in _PROFILE_ENV.items()
            if k not in (
                "DRIVE_FOLDER_JISSEN_2024_Q2",
                "DRIVE_OUTPUT_JISSEN_2024_Q2",
            )
        }
        with patch.dict(os.environ, clean_env, clear=True):
            with self.assertRaises(ValueError):
                main.run(test_count=0, profile_name="jissen_2024_q2")

        # フロー開始前なので list_pdfs は呼ばれていない
        mock_drive.list_pdfs.assert_not_called()


# ─────────────────────────────────────────────────────────────────────
# 7. 既存挙動リグレッションテスト
# ─────────────────────────────────────────────────────────────────────


class TestLegacyBehaviorRegression(unittest.TestCase):
    """PR #14 / #17 以前の挙動が default プロファイルで完全維持される。"""

    def setUp(self):
        self._env_patcher = patch.dict(os.environ, _PROFILE_ENV, clear=False)
        self._env_patcher.start()

    def tearDown(self):
        self._env_patcher.stop()

    @patch("src.main.ensure_fonts")
    @patch("src.main.pdf_merger")
    @patch("src.main.pdf_creator")
    @patch("src.main.pdf_reader")
    @patch("src.main.comment_generator")
    @patch("src.main.sheets_client")
    @patch("src.main.drive_client")
    def test_management_number_is_eight_char_code_from_filename(
        self, mock_drive, mock_sheets, mock_gen, mock_reader,
        mock_creator, mock_merger, mock_fonts,
    ):
        """管理番号は PDF ファイル名先頭の 8 文字 NNN-NN-N コードと一致する。

        自動採番（6 桁ゼロパディング連番）は廃止済み。
        """
        import re
        _pattern = re.compile(r"^\d{3}-\d{2}-\d$")
        _install_main_mocks(
            mock_drive, mock_sheets, mock_gen, mock_reader,
            mock_creator, mock_merger, mock_fonts, pdf_count=2,
        )

        main.run(test_count=0, profile_name="jissen_default")

        for c in mock_sheets.append_output_record.call_args_list:
            mgmt = c.kwargs["management_number"]
            # NNN-NN-N 形式、計 8 文字
            self.assertEqual(len(mgmt), 8)
            self.assertRegex(mgmt, _pattern)

    @patch("src.main.ensure_fonts")
    @patch("src.main.pdf_creator")
    @patch("src.main.pdf_reader")
    @patch("src.main.comment_generator")
    @patch("src.main.sheets_client")
    @patch("src.main.drive_client")
    def test_filename_format_matches_pr14_specification(
        self, mock_drive, mock_sheets, mock_gen, mock_reader,
        mock_creator, mock_fonts,
    ):
        """出力ファイル名が <医院名>＿<個人名>＿<タイトル>.pdf 形式（全角アンダースコア）。

        pdf_merger.make_output_filename は本物のロジックを使ってフォーマット確認。
        """
        mock_drive.list_pdfs.return_value = _make_pdf_files(1)
        mock_drive.download_pdf.return_value = b"%PDF-1.4 fake"
        mock_drive.upload_pdf_to_clinic_person.return_value = {
            "webViewLink": "https://drive.google.com/fake",
            "clinic_folder_id": "clinic_folder_fake",
        }
        mock_reader.extract_text.return_value = "PDFテキスト"
        mock_gen.generate_comment_with_metadata.return_value = {
            "clinic_name": "山田歯科",
            "person_name": "田中太郎",
            "sample_title": "売上向上の取り組み",
            "comment": "コメント本文",
        }
        # マスター未登録 → AI 抽出値で代用
        mock_sheets.read_master_records.return_value = []
        mock_sheets.lookup_clinic_name.return_value = ""
        mock_sheets.lookup_email_by_clinic_and_person.return_value = ""

        # pdf_merger は merge_pdfs だけモックし、make_output_filename は本物を使う
        with patch("src.main.pdf_merger.merge_pdfs"):
            main.run(test_count=0, profile_name="jissen_default")

        # upload に渡された file_name を確認
        upload_call = mock_drive.upload_pdf_to_clinic_person.call_args
        file_name = upload_call.kwargs["file_name"]
        # 全角アンダースコア ＿（U+FF3F）で連結されている
        self.assertEqual(file_name, "山田歯科＿田中太郎＿売上向上の取り組み.pdf")

    @patch("src.main.ensure_fonts")
    @patch("src.main.pdf_merger")
    @patch("src.main.pdf_creator")
    @patch("src.main.pdf_reader")
    @patch("src.main.comment_generator")
    @patch("src.main.sheets_client")
    @patch("src.main.drive_client")
    def test_drive_folder_normalization_active_in_smoke(
        self, mock_drive, mock_sheets, mock_gen, mock_reader,
        mock_creator, mock_merger, mock_fonts,
    ):
        """upload_pdf_to_clinic_person に医院名/個人名が正しく渡される。

        実 find_or_create_folder の表記揺れ吸収ロジックは別ユニットテストで
        担保しているため、ここでは「upload に正しい clinic_name/person_name
        が渡る」ことだけを確認する。
        """
        _install_main_mocks(
            mock_drive, mock_sheets, mock_gen, mock_reader,
            mock_creator, mock_merger, mock_fonts, pdf_count=2,
        )
        # 表記揺れする 2 件の clinic_name
        mock_gen.generate_comment_with_metadata.side_effect = [
            {
                "clinic_name": "医療法人 かがやき",
                "person_name": "田中太郎",
                "sample_title": "事例A",
                "comment": "コメント1",
            },
            {
                "clinic_name": "医療法人かがやき",  # 半角スペース無し
                "person_name": "田中太郎",
                "sample_title": "事例B",
                "comment": "コメント2",
            },
        ]

        main.run(test_count=0, profile_name="jissen_default")

        # upload 呼び出しで医院番号と医院名が別引数で渡される。医院名部分の
        # 表記揺れはそのまま保持され、識別は医院番号で行われる（P-019）。
        # PDF は 001-00-0... / 002-00-0... なので医院番号は 001 / 002。
        upload_calls = mock_drive.upload_pdf_to_clinic_person.call_args_list
        self.assertEqual(len(upload_calls), 2)
        self.assertEqual(upload_calls[0].kwargs["clinic_number"], "001")
        self.assertEqual(
            upload_calls[0].kwargs["clinic_name"], "医療法人 かがやき"
        )
        self.assertEqual(upload_calls[1].kwargs["clinic_number"], "002")
        self.assertEqual(
            upload_calls[1].kwargs["clinic_name"], "医療法人かがやき"
        )


# ─────────────────────────────────────────────────────────────────────
# 8. フォルダ自動検出モード E2E（target_folder）
# ─────────────────────────────────────────────────────────────────────


class TestTargetFolderE2E(unittest.TestCase):
    """``--target-folder`` 指定時の E2E スモークテスト。

    discover.resolve_context を mock しつつ、main.run() / batch_main.run()
    の全パイプラインが「自動検出した設定」を正しく伝搬することを検証する。
    """

    def setUp(self):
        # target_folder モードは config の 3 つの ROOT/ID を必要とする
        self._env_patcher = patch.dict(
            os.environ,
            {
                **_PROFILE_ENV,
                "DRIVE_INPUT_ROOT": "discover_input_root",
                "DRIVE_OUTPUT_ROOT": "discover_output_root",
            },
            clear=False,
        )
        self._env_patcher.start()
        # config モジュールはモジュールロード時に env を読むため patch で上書き
        self._cfg_patches = [
            patch("src.config.DRIVE_INPUT_ROOT", "discover_input_root"),
            patch("src.config.DRIVE_OUTPUT_ROOT", "discover_output_root"),
            patch("src.config.SPREADSHEET_ID", "test_sheet_id"),
        ]
        for p in self._cfg_patches:
            p.start()

    def tearDown(self):
        for p in self._cfg_patches:
            p.stop()
        self._env_patcher.stop()

    def _install_discovery_mocks(self, mock_resolve_context):
        """discover.resolve_context をモック化し、固定の DiscoveredContext を返す。"""
        from src.discover import DiscoveredContext
        mock_resolve_context.return_value = DiscoveredContext(
            target_folder_name="2024_Q1_実践事例",
            input_folder_id="auto_input_id",
            output_folder_id="auto_output_id",
            output_sheet_name="2024_Q1_実践事例",
        )

    @patch("src.discover.resolve_context")
    @patch("src.main.ensure_fonts")
    @patch("src.main.pdf_merger")
    @patch("src.main.pdf_creator")
    @patch("src.main.pdf_reader")
    @patch("src.main.comment_generator")
    @patch("src.main.sheets_client")
    @patch("src.main.drive_client")
    def test_main_e2e_target_folder_smoke(
        self, mock_drive, mock_sheets, mock_gen, mock_reader,
        mock_creator, mock_merger, mock_fonts, mock_resolve_context,
    ):
        """target_folder 指定で 5 件処理 → シートに 5 行追記。

        管理番号は target_folder モードでも PDF ファイル名先頭から抽出する。
        """
        _install_main_mocks(
            mock_drive, mock_sheets, mock_gen, mock_reader,
            mock_creator, mock_merger, mock_fonts,
        )
        self._install_discovery_mocks(mock_resolve_context)

        main.run(test_count=0, target_folder="2024_Q1_実践事例")

        # 5 件処理 → シート 5 件
        self.assertEqual(mock_sheets.append_output_record.call_count, 5)
        # 自動検出した input_folder_id が drive_client に渡る
        mock_drive.list_pdfs.assert_called_once_with(folder_id="auto_input_id")
        # 自動検出した output_folder_id が upload に渡る
        upload_kwargs = mock_drive.upload_pdf_to_clinic_person.call_args_list
        upload_folders = {c.kwargs["output_root_folder_id"] for c in upload_kwargs}
        self.assertEqual(upload_folders, {"auto_output_id"})
        # 管理番号は PDF ファイル名先頭から抽出（自動採番ではない）
        first = mock_sheets.append_output_record.call_args_list[0]
        self.assertEqual(
            first.kwargs["management_number"], _expected_mgmt_number(1),
        )
        # シート名は自動派生（フォルダ名そのまま）
        self.assertEqual(first.kwargs["sheet_name"], "2024_Q1_実践事例")

    @patch("src.discover.resolve_context")
    @patch("src.batch_main.ensure_fonts")
    @patch("src.batch_main.pdf_merger")
    @patch("src.batch_main.pdf_creator")
    @patch("src.batch_main.pdf_reader")
    @patch("src.batch_main.comment_generator")
    @patch("src.batch_main.sheets_client")
    @patch("src.batch_main.drive_client")
    def test_batch_e2e_target_folder_smoke(
        self, mock_drive, mock_sheets, mock_gen, mock_reader,
        mock_creator, mock_merger, mock_fonts, mock_resolve_context,
    ):
        """Batch モード + target_folder の E2E。"""
        mock_drive.list_pdfs.return_value = _make_pdf_files(3)
        mock_drive.download_pdf.return_value = b"%PDF-1.4 fake"
        mock_drive.upload_pdf_to_clinic_person.return_value = {
            "webViewLink": "https://drive.google.com/fake",
            "clinic_folder_id": "clinic_folder_fake",
        }
        mock_reader.extract_text.return_value = "PDFテキスト"
        mock_gen.submit_batch.return_value = "batch_test_001"
        mock_gen.get_batch_status.return_value = {
            "status": "ended",
            "request_counts": {
                "processing": 0, "succeeded": 3,
                "errored": 0, "canceled": 0, "expired": 0,
            },
        }
        results = {
            batch_main._custom_id_for_file(f["id"]): _make_metadata(suffix=f"_{i}")
            for i, f in enumerate(_make_pdf_files(3), start=1)
        }
        mock_gen.get_batch_results.return_value = (results, [])
        mock_merger.make_output_filename.return_value = (
            "山田歯科＿田中太郎＿事例タイトル.pdf"
        )
        self._install_discovery_mocks(mock_resolve_context)

        with patch("src.batch_main.time.sleep"):
            batch_main.run(
                batch_mode=True, test_count=0, step="all",
                target_folder="2024_Q1_実践事例",
            )

        # 3 件処理 → シート 3 件
        self.assertEqual(mock_sheets.append_output_record.call_count, 3)
        mock_drive.list_pdfs.assert_called_once_with(folder_id="auto_input_id")
        first = mock_sheets.append_output_record.call_args_list[0]
        self.assertEqual(
            first.kwargs["management_number"], _expected_mgmt_number(1),
        )
        self.assertEqual(first.kwargs["sheet_name"], "2024_Q1_実践事例")

    @patch("src.discover.resolve_context")
    @patch("src.main.ensure_fonts")
    @patch("src.main.pdf_merger")
    @patch("src.main.pdf_creator")
    @patch("src.main.pdf_reader")
    @patch("src.main.comment_generator")
    @patch("src.main.sheets_client")
    @patch("src.main.drive_client")
    def test_target_folder_management_number_from_filename(
        self, mock_drive, mock_sheets, mock_gen, mock_reader,
        mock_creator, mock_merger, mock_fonts, mock_resolve_context,
    ):
        """target_folder モードでも管理番号は PDF ファイル名先頭から抽出される。

        フォルダ名から prefix を派生する旧挙動は廃止済み。
        """
        mock_drive.list_pdfs.return_value = [
            {"id": "id_a", "name": "077-08-9事例A.pdf"},
            {"id": "id_b", "name": "077-08-9事例B再提出.pdf"},
        ]
        mock_drive.download_pdf.return_value = b"%PDF-1.4 fake"
        mock_drive.upload_pdf_to_clinic_person.return_value = {
            "webViewLink": "https://drive.google.com/fake",
            "clinic_folder_id": "clinic_folder_fake",
        }
        mock_reader.extract_text.return_value = "PDFテキスト"
        mock_gen.generate_comment_with_metadata.return_value = _make_metadata()
        mock_merger.make_output_filename.return_value = "out.pdf"
        self._install_discovery_mocks(mock_resolve_context)

        main.run(test_count=0, target_folder="2024_Q1_実践事例")

        # 管理番号はフォルダ名 prefix ではなく PDF ファイル名先頭コード
        mgmt_nums = [
            c.kwargs["management_number"]
            for c in mock_sheets.append_output_record.call_args_list
        ]
        self.assertEqual(mgmt_nums, ["077-08-9", "077-08-9"])
        # シート名は自動派生（フォルダ名そのまま）
        for c in mock_sheets.append_output_record.call_args_list:
            self.assertEqual(c.kwargs["sheet_name"], "2024_Q1_実践事例")


# ─────────────────────────────────────────────────────────────────────
# 9. フォルダ自動検出モード追加に伴う既存 profile モードのリグレッション
# ─────────────────────────────────────────────────────────────────────


class TestProfileModeRegressionAfterDiscoveryAdded(unittest.TestCase):
    """``target_folder`` 引数追加後も ``--profile`` モードが完全に従来通り動く。"""

    def setUp(self):
        self._env_patcher = patch.dict(os.environ, _PROFILE_ENV, clear=False)
        self._env_patcher.start()

    def tearDown(self):
        self._env_patcher.stop()

    @patch("src.discover.resolve_context")
    @patch("src.main.ensure_fonts")
    @patch("src.main.pdf_merger")
    @patch("src.main.pdf_creator")
    @patch("src.main.pdf_reader")
    @patch("src.main.comment_generator")
    @patch("src.main.sheets_client")
    @patch("src.main.drive_client")
    def test_profile_mode_does_not_call_discover(
        self, mock_drive, mock_sheets, mock_gen, mock_reader,
        mock_creator, mock_merger, mock_fonts, mock_resolve_context,
    ):
        """``--profile`` 指定時、``discover.resolve_context`` は一切呼ばれない。"""
        _install_main_mocks(
            mock_drive, mock_sheets, mock_gen, mock_reader,
            mock_creator, mock_merger, mock_fonts, pdf_count=2,
        )

        main.run(test_count=0, profile_name="jissen_2024_q1")

        mock_resolve_context.assert_not_called()
        # 管理番号はファイル名抽出、シートはプロファイル定義
        first = mock_sheets.append_output_record.call_args_list[0]
        self.assertEqual(
            first.kwargs["management_number"], _expected_mgmt_number(1)
        )
        self.assertEqual(first.kwargs["sheet_name"], "実践事例_2024Q1_出力一覧")

    @patch("src.discover.resolve_context")
    @patch("src.main.ensure_fonts")
    @patch("src.main.pdf_merger")
    @patch("src.main.pdf_creator")
    @patch("src.main.pdf_reader")
    @patch("src.main.comment_generator")
    @patch("src.main.sheets_client")
    @patch("src.main.drive_client")
    def test_default_profile_unchanged_after_discovery_added(
        self, mock_drive, mock_sheets, mock_gen, mock_reader,
        mock_creator, mock_merger, mock_fonts, mock_resolve_context,
    ):
        """``--profile`` 省略時も従来通り ``jissen_default``、discover は呼ばれない。"""
        _install_main_mocks(
            mock_drive, mock_sheets, mock_gen, mock_reader,
            mock_creator, mock_merger, mock_fonts, pdf_count=2,
        )

        main.run(test_count=0)

        mock_resolve_context.assert_not_called()
        mock_drive.list_pdfs.assert_called_once_with(folder_id="input_default")
        first = mock_sheets.append_output_record.call_args_list[0]
        # 管理番号は PDF ファイル名先頭から抽出される
        self.assertEqual(
            first.kwargs["management_number"], _expected_mgmt_number(1)
        )
        self.assertEqual(first.kwargs["sheet_name"], "出力一覧")

    @patch("src.discover.resolve_context")
    @patch("src.batch_main.ensure_fonts")
    @patch("src.batch_main.pdf_merger")
    @patch("src.batch_main.pdf_creator")
    @patch("src.batch_main.pdf_reader")
    @patch("src.batch_main.comment_generator")
    @patch("src.batch_main.sheets_client")
    @patch("src.batch_main.drive_client")
    def test_batch_profile_mode_does_not_call_discover(
        self, mock_drive, mock_sheets, mock_gen, mock_reader,
        mock_creator, mock_merger, mock_fonts, mock_resolve_context,
    ):
        """Batch モードでも ``--profile`` 指定時は discover を呼ばない。"""
        mock_drive.list_pdfs.return_value = _make_pdf_files(2)
        mock_drive.download_pdf.return_value = b"%PDF-1.4 fake"
        mock_drive.upload_pdf_to_clinic_person.return_value = {
            "webViewLink": "url",
            "clinic_folder_id": "clinic_folder_fake",
        }
        mock_reader.extract_text.return_value = "テキスト"
        mock_gen.submit_batch.return_value = "batch_001"
        mock_gen.get_batch_status.return_value = {
            "status": "ended",
            "request_counts": {
                "processing": 0, "succeeded": 2,
                "errored": 0, "canceled": 0, "expired": 0,
            },
        }
        mock_gen.get_batch_results.return_value = (
            {
                f"item_{i:04d}": _make_metadata(suffix=f"_{i}")
                for i in range(1, 3)
            },
            [],
        )
        mock_merger.make_output_filename.return_value = "f.pdf"

        with patch("src.batch_main.time.sleep"):
            batch_main.run(
                batch_mode=True, test_count=0, step="all",
                profile_name="jissen_2024_q2",
            )

        mock_resolve_context.assert_not_called()
        mock_drive.list_pdfs.assert_called_once_with(folder_id="input_q2")


# ─────────────────────────────────────────────────────────────────────
# 10. 増分処理（重複検知）の E2E スモークテスト
# ─────────────────────────────────────────────────────────────────────


class TestIncrementalDedupE2E(unittest.TestCase):
    """増分処理（管理番号での重複検知）の結合テスト。

    入力フォルダに PDF を継続追加して再実行する運用を再現する:
        1 回目は全件処理、2 回目は出力一覧シートに既存の管理番号をスキップし、
        新規追加分だけを処理する。重複判定は download / Claude API 呼び出しの
        前に行われ、処理済み PDF にはコストが発生しない。
    """

    def setUp(self):
        self._env_patcher = patch.dict(os.environ, _PROFILE_ENV, clear=False)
        self._env_patcher.start()

    def tearDown(self):
        self._env_patcher.stop()

    @patch("src.main.ensure_fonts")
    @patch("src.main.pdf_merger")
    @patch("src.main.pdf_creator")
    @patch("src.main.pdf_reader")
    @patch("src.main.comment_generator")
    @patch("src.main.sheets_client")
    @patch("src.main.drive_client")
    def test_first_run_all_then_second_run_skips_duplicates(
        self, mock_drive, mock_sheets, mock_gen, mock_reader,
        mock_creator, mock_merger, mock_fonts,
    ):
        """1 回目: 全 3 件処理。2 回目: 既存 3 件 + 新規 2 件のうち新規だけ処理。"""
        _install_main_mocks(
            mock_drive, mock_sheets, mock_gen, mock_reader,
            mock_creator, mock_merger, mock_fonts,
        )

        # ── 1 回目: 出力一覧シートは空 → 3 件すべて新規 ──
        mock_drive.list_pdfs.return_value = _make_pdf_files(3)
        mock_sheets.get_processed_management_numbers.return_value = set()

        main.run(test_count=0, profile_name="jissen_default")

        # 3 件すべて処理（download / シート追記）
        self.assertEqual(mock_sheets.append_output_record.call_count, 3)
        self.assertEqual(mock_drive.download_pdf.call_count, 3)
        first_run_mgmt = [
            c.kwargs["management_number"]
            for c in mock_sheets.append_output_record.call_args_list
        ]
        self.assertEqual(
            first_run_mgmt, [_expected_mgmt_number(i) for i in (1, 2, 3)],
        )

        # ── 2 回目: PDF が 2 件追加され計 5 件。最初の 3 件は処理済み ──
        mock_drive.reset_mock()
        mock_sheets.reset_mock()
        mock_gen.reset_mock()
        _install_main_mocks(
            mock_drive, mock_sheets, mock_gen, mock_reader,
            mock_creator, mock_merger, mock_fonts,
        )
        mock_drive.list_pdfs.return_value = _make_pdf_files(5)
        # 1 回目で記録された 3 件が処理済み集合になっている
        mock_sheets.get_processed_management_numbers.return_value = {
            _expected_mgmt_number(i) for i in (1, 2, 3)
        }

        main.run(test_count=0, profile_name="jissen_default")

        # 新規 2 件（4, 5 件目）だけ処理される
        self.assertEqual(mock_sheets.append_output_record.call_count, 2)
        second_run_mgmt = [
            c.kwargs["management_number"]
            for c in mock_sheets.append_output_record.call_args_list
        ]
        self.assertEqual(
            second_run_mgmt, [_expected_mgmt_number(i) for i in (4, 5)],
        )
        # 処理済み 3 件は download / Claude API を呼ばない（コスト削減）
        self.assertEqual(mock_drive.download_pdf.call_count, 2)
        self.assertEqual(mock_gen.generate_comment_with_metadata.call_count, 2)

    @patch("src.batch_main.ensure_fonts")
    @patch("src.batch_main.pdf_merger")
    @patch("src.batch_main.pdf_creator")
    @patch("src.batch_main.pdf_reader")
    @patch("src.batch_main.comment_generator")
    @patch("src.batch_main.sheets_client")
    @patch("src.batch_main.drive_client")
    def test_batch_step1_excludes_processed_from_batch(
        self, mock_drive, mock_sheets, mock_gen, mock_reader,
        mock_creator, mock_merger, mock_fonts,
    ):
        """Batch モード: 処理済み PDF は Step1 で除外され Batch API に投げない。"""
        mock_drive.list_pdfs.return_value = _make_pdf_files(5)
        mock_drive.download_pdf.return_value = b"%PDF-1.4 fake"
        mock_reader.extract_text.return_value = "PDFテキスト"
        # 最初の 3 件は処理済み
        mock_sheets.get_processed_management_numbers.return_value = {
            _expected_mgmt_number(i) for i in (1, 2, 3)
        }
        from src.profile import load_profile
        profile = load_profile("jissen_default")

        items = batch_main.step1_prepare(profile, test_count=0)

        # 新規 2 件だけが Batch 投入対象
        self.assertEqual(len(items), 2)
        names = [it["pdf_file_name"] for it in items]
        self.assertEqual(
            names,
            [f"{i:03d}-00-0pdf_{i:04d}.pdf" for i in (4, 5)],
        )
        # 処理済み 3 件は download すらされない
        self.assertEqual(mock_drive.download_pdf.call_count, 2)


class TestAttachmentPassthroughE2E(unittest.TestCase):
    """添付資料パススルーの E2E（mock ベース）。

    ファイル名に「【添付資料】」を含む PDF は AI 処理せず、同じ管理番号の
    メイン実践事例 PDF と同じ ``<医院名>/<個人名>/`` フォルダへ元ファイル名
    のままコピーされる。メイン経路と passthrough 経路が出力（Drive / シート）
    で合流することを検証する。
    """

    def setUp(self):
        self._env_patcher = patch.dict(os.environ, _PROFILE_ENV, clear=False)
        self._env_patcher.start()
        # batch_main 経由のテストで batch_*.json の汚染を避けるためバックアップ
        self._prep_file = LOGS_DIR / "batch_prep.json"
        self._att_file = LOGS_DIR / "batch_attachments.json"
        self._batch_id_file = LOGS_DIR / "batch_id.txt"
        self._backups = {
            p: (p.read_text() if p.exists() else None)
            for p in (self._prep_file, self._att_file, self._batch_id_file)
        }

    def tearDown(self):
        self._env_patcher.stop()
        for path, backup in self._backups.items():
            if backup is None:
                path.unlink(missing_ok=True)
            else:
                path.write_text(backup)

    @patch("src.main.ensure_fonts")
    @patch("src.main.pdf_merger")
    @patch("src.main.pdf_creator")
    @patch("src.main.pdf_reader")
    @patch("src.main.comment_generator")
    @patch("src.main.sheets_client")
    @patch("src.main.drive_client")
    def test_main_e2e_main_and_attachment_same_folder(
        self, mock_drive, mock_sheets, mock_gen, mock_reader,
        mock_creator, mock_merger, mock_fonts,
    ):
        """通常モード E2E: メイン + 添付資料が同じ管理番号・同じフォルダに出力。"""
        mock_drive.list_pdfs.return_value = [
            {"id": "id_main", "name": "001-01-0実践事例.pdf"},
            {"id": "id_att", "name": "001-01-0【添付資料】補足資料.pdf"},
        ]
        mock_drive.download_pdf.return_value = b"%PDF-1.4 fake"
        mock_drive.upload_pdf_to_clinic_person.return_value = {
            "webViewLink": "https://drive.google.com/fake",
            "clinic_folder_id": "clinic_folder_fake",
        }
        mock_sheets.get_processed_management_numbers.return_value = set()
        mock_reader.extract_text.return_value = "PDFテキスト"
        mock_gen.generate_comment_with_metadata.return_value = _make_metadata()
        mock_merger.make_output_filename.return_value = (
            "山田歯科＿田中太郎＿事例タイトル.pdf"
        )
        # マスター未登録 → AI 抽出値で代用 / メール未登録 → 下書きスキップ
        mock_sheets.read_master_records.return_value = []
        mock_sheets.lookup_clinic_name.return_value = ""
        mock_sheets.lookup_email_by_clinic_and_person.return_value = ""

        main.run(test_count=0, profile_name="jissen_default")

        # メイン 1 件は Claude API に投げる、添付資料は投げない
        self.assertEqual(mock_gen.generate_comment_with_metadata.call_count, 1)
        # upload は 2 回、両方とも同じ医院番号 + 医院名で呼ばれる
        # （find_or_create_clinic_folder が同じ医院フォルダへ合流させる、P-019）。
        self.assertEqual(mock_drive.upload_pdf_to_clinic_person.call_count, 2)
        clinic_numbers = {
            c.kwargs["clinic_number"]
            for c in mock_drive.upload_pdf_to_clinic_person.call_args_list
        }
        clinics = {
            c.kwargs["clinic_name"]
            for c in mock_drive.upload_pdf_to_clinic_person.call_args_list
        }
        persons = {
            c.kwargs["person_name"]
            for c in mock_drive.upload_pdf_to_clinic_person.call_args_list
        }
        self.assertEqual(clinic_numbers, {"001"})
        self.assertEqual(clinics, {"山田歯科"})
        self.assertEqual(persons, {"田中太郎"})
        # シートは 2 行。両方とも管理番号 001-01-0。メイン処理が先、添付資料が後。
        self.assertEqual(mock_sheets.append_output_record.call_count, 2)
        rows = mock_sheets.append_output_record.call_args_list
        self.assertEqual(
            {r.kwargs["management_number"] for r in rows}, {"001-01-0"}
        )
        # メイン行（実践事例タイトル）→ 添付資料行（【添付資料】始まり）の順
        self.assertEqual(rows[0].kwargs["sample_name"], "事例タイトル")
        self.assertEqual(
            rows[1].kwargs["sample_name"],
            "【添付資料】001-01-0【添付資料】補足資料.pdf",
        )

    @patch("src.batch_main.ensure_fonts")
    @patch("src.batch_main.pdf_merger")
    @patch("src.batch_main.pdf_creator")
    @patch("src.batch_main.pdf_reader")
    @patch("src.batch_main.comment_generator")
    @patch("src.batch_main.sheets_client")
    @patch("src.batch_main.drive_client")
    def test_batch_e2e_main_and_attachment_same_folder(
        self, mock_drive, mock_sheets, mock_gen, mock_reader,
        mock_creator, mock_merger, mock_fonts,
    ):
        """Batch モード E2E: 添付資料は Batch API に投げず step4 でコピーされる。"""
        mock_drive.list_pdfs.return_value = [
            {"id": "id_main", "name": "001-01-0実践事例.pdf"},
            {"id": "id_att", "name": "001-01-0【添付資料】補足資料.pdf"},
        ]
        mock_drive.download_pdf.return_value = b"%PDF-1.4 fake"
        mock_drive.upload_pdf_to_clinic_person.return_value = {
            "webViewLink": "https://drive.google.com/fake",
            "clinic_folder_id": "clinic_folder_fake",
        }
        mock_sheets.get_processed_management_numbers.return_value = set()
        mock_reader.extract_text.return_value = "PDFテキスト"
        mock_gen.submit_batch.return_value = "batch_att_001"
        mock_gen.get_batch_status.return_value = {
            "status": "ended",
            "request_counts": {
                "processing": 0, "succeeded": 1,
                "errored": 0, "canceled": 0, "expired": 0,
            },
        }
        # メインは 1 件だけ。custom_id は file id 由来（_custom_id_for_file）。
        mock_gen.get_batch_results.return_value = (
            {batch_main._custom_id_for_file("id_main"): _make_metadata()}, [],
        )
        mock_merger.make_output_filename.return_value = (
            "山田歯科＿田中太郎＿事例タイトル.pdf"
        )
        # マスター未登録 → AI 抽出値で代用 / メール未登録 → 下書きスキップ
        mock_sheets.read_master_records.return_value = []
        mock_sheets.lookup_clinic_name.return_value = ""
        mock_sheets.lookup_email_by_clinic_and_person.return_value = ""

        with patch("src.batch_main.time.sleep"):
            batch_main.run(
                batch_mode=True, test_count=0, step="all",
                profile_name="jissen_default",
            )

        # Batch API に投げられたのはメイン 1 件のみ
        submit_arg = mock_gen.submit_batch.call_args.args[0]
        self.assertEqual(len(submit_arg), 1)
        self.assertEqual(submit_arg[0]["pdf_file_name"], "001-01-0実践事例.pdf")
        # upload は 2 回（メイン + 添付資料）、両方とも医院番号 001 + 医院名
        # 山田歯科で呼ばれる（同じ医院フォルダへ合流、P-019）。
        self.assertEqual(mock_drive.upload_pdf_to_clinic_person.call_count, 2)
        att_upload = mock_drive.upload_pdf_to_clinic_person.call_args_list[1]
        self.assertEqual(att_upload.kwargs["clinic_number"], "001")
        self.assertEqual(att_upload.kwargs["clinic_name"], "山田歯科")
        self.assertEqual(att_upload.kwargs["person_name"], "田中太郎")
        self.assertEqual(
            att_upload.kwargs["file_name"], "001-01-0【添付資料】補足資料.pdf"
        )
        # シート 2 行、両方とも管理番号 001-01-0
        self.assertEqual(mock_sheets.append_output_record.call_count, 2)
        rows = mock_sheets.append_output_record.call_args_list
        self.assertEqual(
            {r.kwargs["management_number"] for r in rows}, {"001-01-0"}
        )
        self.assertEqual(
            rows[1].kwargs["sample_name"],
            "【添付資料】001-01-0【添付資料】補足資料.pdf",
        )


# ─────────────────────────────────────────────────────────────────────
# 11. 医院番号付きフォルダ + 医院フォルダURLシートの E2E スモークテスト
# ─────────────────────────────────────────────────────────────────────


class TestClinicNumberFolderE2E(unittest.TestCase):
    """医院番号付きフォルダ名 + 医院フォルダURLシート記録の結合テスト。

    医院フォルダ名は ``<医院番号>_<医院名>``（医院番号 = 管理番号の先頭
    セグメント）。医院フォルダURLシート（``<出力シート名>_医院``）に
    医院番号 / 医院名 / フォルダURL が記録され、同一医院は 1 行のみ。
    """

    def setUp(self):
        self._env_patcher = patch.dict(os.environ, _PROFILE_ENV, clear=False)
        self._env_patcher.start()

    def tearDown(self):
        self._env_patcher.stop()

    @patch("src.main.ensure_fonts")
    @patch("src.main.pdf_merger")
    @patch("src.main.pdf_creator")
    @patch("src.main.pdf_reader")
    @patch("src.main.comment_generator")
    @patch("src.main.sheets_client")
    @patch("src.main.drive_client")
    def test_main_e2e_clinic_number_folder_and_sheet(
        self, mock_drive, mock_sheets, mock_gen, mock_reader,
        mock_creator, mock_merger, mock_fonts,
    ):
        """通常モード E2E: 医院番号付きフォルダ + 医院シート記録。"""
        # 医院番号 001 が 2 件、002 が 1 件
        mock_drive.list_pdfs.return_value = [
            {"id": "id_1", "name": "001-01-0実践事例A.pdf"},
            {"id": "id_2", "name": "001-01-1実践事例B.pdf"},
            {"id": "id_3", "name": "002-02-0実践事例C.pdf"},
        ]
        mock_drive.download_pdf.return_value = b"%PDF-1.4 fake"
        mock_drive.upload_pdf_to_clinic_person.return_value = {
            "webViewLink": "https://drive.google.com/fake",
            "clinic_folder_id": "clinic_folder_id_xyz",
        }
        mock_sheets.get_processed_management_numbers.return_value = set()
        mock_sheets.get_recorded_clinic_numbers.return_value = set()
        # マスター未登録 → AI 抽出値で代用 / メール未登録 → 下書きスキップ
        mock_sheets.read_master_records.return_value = []
        mock_sheets.lookup_clinic_name.return_value = ""
        mock_sheets.lookup_email_by_clinic_and_person.return_value = ""
        mock_reader.extract_text.return_value = "PDFテキスト"
        mock_gen.generate_comment_with_metadata.return_value = _make_metadata()
        mock_merger.make_output_filename.return_value = "out.pdf"

        main.run(test_count=0, profile_name="jissen_default")

        # 全 3 件、upload には医院番号 + 医院名が別引数で渡る
        clinic_numbers = [
            c.kwargs["clinic_number"]
            for c in mock_drive.upload_pdf_to_clinic_person.call_args_list
        ]
        clinic_names_upload = [
            c.kwargs["clinic_name"]
            for c in mock_drive.upload_pdf_to_clinic_person.call_args_list
        ]
        self.assertEqual(clinic_numbers, ["001", "001", "002"])
        self.assertEqual(
            clinic_names_upload, ["山田歯科", "山田歯科", "山田歯科"]
        )
        # 出力一覧シートの医院名列は AI 抽出値（医院番号なし）
        for c in mock_sheets.append_output_record.call_args_list:
            self.assertEqual(c.kwargs["clinic_name"], "山田歯科")
        # 医院フォルダURLシート（出力一覧_医院）に医院番号ごとに 1 行
        # （001 と 002、001 は重複しない）
        recorded = [
            c.kwargs["clinic_number"]
            for c in mock_sheets.append_clinic_folder_record.call_args_list
        ]
        self.assertEqual(recorded, ["001", "002"])
        for c in mock_sheets.append_clinic_folder_record.call_args_list:
            self.assertEqual(c.kwargs["sheet_name"], "出力一覧_医院")
            self.assertEqual(
                c.kwargs["clinic_folder_url"],
                "https://drive.google.com/drive/folders/clinic_folder_id_xyz",
            )

    @patch("src.main.ensure_fonts")
    @patch("src.main.pdf_merger")
    @patch("src.main.pdf_creator")
    @patch("src.main.pdf_reader")
    @patch("src.main.comment_generator")
    @patch("src.main.sheets_client")
    @patch("src.main.drive_client")
    def test_main_e2e_clinic_sheet_name_derives_from_output_sheet(
        self, mock_drive, mock_sheets, mock_gen, mock_reader,
        mock_creator, mock_merger, mock_fonts,
    ):
        """医院シート名は ``<出力シート名>_医院``（プロファイル依存）。"""
        _install_main_mocks(
            mock_drive, mock_sheets, mock_gen, mock_reader,
            mock_creator, mock_merger, mock_fonts, pdf_count=1,
        )
        mock_sheets.get_recorded_clinic_numbers.return_value = set()

        main.run(test_count=0, profile_name="jissen_2024_q1")

        # Q1 プロファイルの出力シートは 実践事例_2024Q1_出力一覧
        # → 医院シートは 実践事例_2024Q1_出力一覧_医院
        snapshot = mock_sheets.get_recorded_clinic_numbers.call_args.kwargs
        self.assertEqual(snapshot["sheet_name"], "実践事例_2024Q1_出力一覧_医院")
        rec = mock_sheets.append_clinic_folder_record.call_args.kwargs
        self.assertEqual(rec["sheet_name"], "実践事例_2024Q1_出力一覧_医院")

    @patch("src.batch_main.ensure_fonts")
    @patch("src.batch_main.pdf_merger")
    @patch("src.batch_main.pdf_creator")
    @patch("src.batch_main.pdf_reader")
    @patch("src.batch_main.comment_generator")
    @patch("src.batch_main.sheets_client")
    @patch("src.batch_main.drive_client")
    def test_batch_e2e_clinic_number_folder_and_sheet(
        self, mock_drive, mock_sheets, mock_gen, mock_reader,
        mock_creator, mock_merger, mock_fonts,
    ):
        """Batch モード E2E: 医院番号付きフォルダ + 医院シート記録。"""
        mock_drive.list_pdfs.return_value = [
            {"id": "id_1", "name": "001-01-0実践事例A.pdf"},
            {"id": "id_2", "name": "002-02-0実践事例B.pdf"},
        ]
        mock_drive.download_pdf.return_value = b"%PDF-1.4 fake"
        mock_drive.upload_pdf_to_clinic_person.return_value = {
            "webViewLink": "https://drive.google.com/fake",
            "clinic_folder_id": "clinic_folder_batch",
        }
        mock_sheets.get_processed_management_numbers.return_value = set()
        mock_sheets.get_recorded_clinic_numbers.return_value = set()
        # マスター未登録 → AI 抽出値で代用 / メール未登録 → 下書きスキップ
        mock_sheets.read_master_records.return_value = []
        mock_sheets.lookup_clinic_name.return_value = ""
        mock_sheets.lookup_email_by_clinic_and_person.return_value = ""
        mock_reader.extract_text.return_value = "PDFテキスト"
        mock_gen.submit_batch.return_value = "batch_clinic_001"
        mock_gen.get_batch_status.return_value = {
            "status": "ended",
            "request_counts": {
                "processing": 0, "succeeded": 2,
                "errored": 0, "canceled": 0, "expired": 0,
            },
        }
        mock_gen.get_batch_results.return_value = (
            {batch_main._custom_id_for_file(f"id_{i}"): _make_metadata()
             for i in range(1, 3)}, [],
        )
        mock_merger.make_output_filename.return_value = "out.pdf"

        with patch("src.batch_main.time.sleep"):
            batch_main.run(
                batch_mode=True, test_count=0, step="all",
                profile_name="jissen_default",
            )

        # 全 2 件、upload には医院番号 + 医院名が別引数で渡る
        clinic_numbers_upload = sorted(
            c.kwargs["clinic_number"]
            for c in mock_drive.upload_pdf_to_clinic_person.call_args_list
        )
        self.assertEqual(clinic_numbers_upload, ["001", "002"])
        for c in mock_drive.upload_pdf_to_clinic_person.call_args_list:
            self.assertEqual(c.kwargs["clinic_name"], "山田歯科")
        recorded = sorted(
            c.kwargs["clinic_number"]
            for c in mock_sheets.append_clinic_folder_record.call_args_list
        )
        self.assertEqual(recorded, ["001", "002"])


# ─────────────────────────────────────────────────────────────────────
# 12. 医院フォルダ識別が医院番号ベースであることの E2E スモークテスト
# ─────────────────────────────────────────────────────────────────────


class TestClinicFolderByNumberE2E(unittest.TestCase):
    """同じ医院の複数 PDF が、AI 抽出医院名の表記揺れに関わらず同じフォルダに
    入る E2E（P-019: 医院フォルダの識別は医院番号のみ、医院名表記揺れは
    無視）。"""

    def setUp(self):
        self._env_patcher = patch.dict(os.environ, _PROFILE_ENV, clear=False)
        self._env_patcher.start()

    def tearDown(self):
        self._env_patcher.stop()

    @patch("src.main.ensure_fonts")
    @patch("src.main.pdf_merger")
    @patch("src.main.pdf_creator")
    @patch("src.main.pdf_reader")
    @patch("src.main.comment_generator")
    @patch("src.main.sheets_client")
    @patch("src.main.drive_client")
    def test_same_clinic_number_with_name_variations_uses_same_folder(
        self, mock_drive, mock_sheets, mock_gen, mock_reader,
        mock_creator, mock_merger, mock_fonts,
    ):
        """同じ医院番号 001 の 3 件の PDF で、AI が異なる医院名表記
        （``三浦歯科医院`` / ``三浦歯科`` / ``医療法人三浦歯科``）を抽出しても、
        ``upload_pdf_to_clinic_person`` は同じ医院番号で呼ばれ、
        ``find_or_create_clinic_folder`` が同じ医院フォルダへ合流させる
        前提を確認する（識別は医院番号のみ、P-019）。"""
        mock_drive.list_pdfs.return_value = [
            {"id": "id_1", "name": "001-01-0実践事例A.pdf"},
            {"id": "id_2", "name": "001-01-1実践事例B.pdf"},
            {"id": "id_3", "name": "001-01-2実践事例C.pdf"},
        ]
        mock_drive.download_pdf.return_value = b"%PDF-1.4 fake"
        mock_drive.upload_pdf_to_clinic_person.return_value = {
            "webViewLink": "https://drive.google.com/fake",
            "clinic_folder_id": "clinic_folder_id_xyz",
        }
        mock_sheets.get_processed_management_numbers.return_value = set()
        mock_sheets.get_recorded_clinic_numbers.return_value = set()
        # マスター未登録 → AI 抽出値で代用 / メール未登録 → 下書きスキップ
        mock_sheets.read_master_records.return_value = []
        mock_sheets.lookup_clinic_name.return_value = ""
        mock_sheets.lookup_email_by_clinic_and_person.return_value = ""
        mock_reader.extract_text.return_value = "PDFテキスト"
        # AI 抽出側の医院名表記揺れ — 同じ医院番号 001 でも語が違う
        mock_gen.generate_comment_with_metadata.side_effect = [
            {
                "clinic_name": "三浦歯科医院",
                "person_name": "白川 蓮",
                "sample_title": "事例A",
                "comment": "コメント1",
            },
            {
                "clinic_name": "三浦歯科",  # 「医院」が抜けた揺れ（P-009 では吸収不可）
                "person_name": "白川 蓮",
                "sample_title": "事例B",
                "comment": "コメント2",
            },
            {
                "clinic_name": "医療法人三浦歯科",  # 法人名が前に付いた揺れ
                "person_name": "白川 蓮",
                "sample_title": "事例C",
                "comment": "コメント3",
            },
        ]
        mock_merger.make_output_filename.return_value = "out.pdf"

        main.run(test_count=0, profile_name="jissen_default")

        # 3 件全て upload され、全て医院番号 001 で呼ばれる
        upload_calls = mock_drive.upload_pdf_to_clinic_person.call_args_list
        self.assertEqual(len(upload_calls), 3)
        for c in upload_calls:
            self.assertEqual(c.kwargs["clinic_number"], "001")
        # 医院名は表記が違ったまま、生の AI 抽出値が伝搬する
        # （新規作成時のフォルダ名末尾用 / 出力一覧シート列用）
        clinic_names_upload = [c.kwargs["clinic_name"] for c in upload_calls]
        self.assertEqual(
            clinic_names_upload,
            ["三浦歯科医院", "三浦歯科", "医療法人三浦歯科"],
        )
        # 医院フォルダURLシートには医院番号 001 で 1 行のみ
        # （find_or_create_clinic_folder が同じフォルダに合流させる前提なので、
        # clinics_recorded_this_run のスナップショット重複防止が効く）
        recorded = [
            c.kwargs["clinic_number"]
            for c in mock_sheets.append_clinic_folder_record.call_args_list
        ]
        self.assertEqual(recorded, ["001"])




class TestMasterSheetIntegrationE2E(unittest.TestCase):
    """参加者マスター統合の E2E スモークテスト。

    参加者マスターシート読み込み → 医院名標準化 + メールルックアップ → 下書き
    作成までを mock で通し、main.run と batch_main.step4 の両方が同じ
    振る舞いをすることを検証する。
    """

    def setUp(self):
        self._env_patcher = patch.dict(os.environ, _PROFILE_ENV, clear=False)
        self._env_patcher.start()

    def tearDown(self):
        self._env_patcher.stop()

    def _master_record(
        self, management_number, clinic_name, email,
        participant_name="田中太郎", venue="",
    ):
        """テスト用 MasterRecord。

        ``management_number`` は ``xxx-yy`` 形式（例 ``001-01``）で渡す。
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

    @patch("src.main.ensure_fonts")
    @patch("src.main.pdf_merger")
    @patch("src.main.pdf_creator")
    @patch("src.main.pdf_reader")
    @patch("src.main.comment_generator")
    @patch("src.main.gmail_client")
    @patch("src.main.sheets_client")
    @patch("src.main.drive_client")
    def test_main_e2e_reads_master_sheet_and_creates_drafts(
        self, mock_drive, mock_sheets, mock_gmail, mock_gen, mock_reader,
        mock_creator, mock_merger, mock_fonts,
    ):
        """通常モード E2E: マスターシート読み込み → 医院名標準化 + メール lookup → create_draft。

        4 件の PDF（管理番号 001-00-0 / 002-00-0 / 003-00-0 / 004-00-0）のうち
        003 までマスター登録済み、004 は未登録。登録済み 3 件は TO がマスターの
        メール、CC=None で create_draft。未登録 1 件は下書きスキップ + 警告。
        """
        from src import sheets_client as real_sheets_client

        _install_main_mocks(
            mock_drive, mock_sheets, mock_gen, mock_reader,
            mock_creator, mock_merger, mock_fonts, pdf_count=4,
        )
        mock_sheets.read_master_records.return_value = [
            self._master_record("001-00", "001 標準名", "p1@example.com"),
            self._master_record("002-00", "002 標準名", "p2@example.com"),
            self._master_record("003-00", "003 標準名", "p3@example.com"),
            # 004 (clinic_number) はマスター未登録
        ]
        mock_sheets.lookup_clinic_name.side_effect = (
            lambda recs, cn: real_sheets_client.lookup_clinic_name(recs, cn)
        )
        mock_sheets.lookup_email_by_clinic_and_person.side_effect = (
            lambda recs, cn, pn: real_sheets_client.lookup_email_by_clinic_and_person(recs, cn, pn)
        )
        mock_sheets.get_processed_management_numbers.return_value = set()
        mock_sheets.get_recorded_clinic_numbers.return_value = set()

        with self.assertLogs("jissen_comment", level="WARNING") as log_ctx:
            main.run(test_count=0, profile_name="jissen_default")

        # 4 件すべて create_draft が呼ばれる（未登録は宛先空）
        self.assertEqual(mock_gmail.create_draft.call_count, 4)
        to_emails = sorted(
            c.kwargs["to_email"] for c in mock_gmail.create_draft.call_args_list
        )
        self.assertEqual(
            to_emails, ["", "p1@example.com", "p2@example.com", "p3@example.com"]
        )
        for call in mock_gmail.create_draft.call_args_list:
            self.assertIsNone(call.kwargs["cc_email"])

        # 004 はマスター未ヒットで宛先空の警告（メール未ヒットログが出る）
        joined = "\n".join(log_ctx.output)
        self.assertIn("メール未ヒット", joined)
        self.assertIn("004", joined)
        # 医院名 lookup ミスも警告
        self.assertIn("参加者マスター未登録", joined)

        # 4 件全て出力一覧シートには記録される（PDF 処理自体は完了）
        self.assertEqual(mock_sheets.append_output_record.call_count, 4)
        # read_master_records は実行全体で 1 回だけ
        self.assertEqual(mock_sheets.read_master_records.call_count, 1)

    @patch("src.batch_main.pdf_merger")
    @patch("src.batch_main.pdf_creator")
    @patch("src.batch_main.gmail_client")
    @patch("src.batch_main.drive_client")
    @patch("src.batch_main.sheets_client")
    @patch("src.batch_main.ensure_fonts")
    def test_batch_step4_e2e_reads_master_sheet_and_creates_drafts(
        self, mock_fonts, mock_sheets, mock_drive, mock_gmail,
        mock_creator, mock_merger,
    ):
        """Batch モード E2E: Step4 でマスターシート読み込み → 医院名標準化 + メール lookup → create_draft。

        通常モードと同じ振る舞いを Step4 でも保証する。
        """
        from src import sheets_client as real_sheets_client
        from src.config import LOGS_DIR

        # batch_attachments.json は空にして添付資料経路の副作用を除く
        att_file = LOGS_DIR / "batch_attachments.json"
        att_backup = att_file.read_text() if att_file.exists() else None
        att_file.write_text("[]")
        try:
            mock_sheets.get_recorded_clinic_numbers.return_value = set()
            mock_sheets.read_master_records.return_value = [
                self._master_record(
                    "111-22", "標準医院名", "p@example.com"
                ),
            ]
            mock_sheets.lookup_clinic_name.side_effect = (
                lambda recs, cn: real_sheets_client.lookup_clinic_name(recs, cn)
            )
            mock_sheets.lookup_email_by_clinic_and_person.side_effect = (
                lambda recs, cn, pn: real_sheets_client.lookup_email_by_clinic_and_person(recs, cn, pn)
            )
            mock_drive.download_pdf.return_value = b"%PDF-1.4 fake"
            mock_drive.upload_pdf_to_clinic_person.return_value = {
                "webViewLink": "https://drive.google.com/fake",
                "clinic_folder_id": "clinic_folder_fake",
            }
            mock_merger.make_output_filename.return_value = "out.pdf"
            from src.profile import ProfileConfig
            profile = ProfileConfig(
                name="jissen_default",
                display_name="default",
                document_type="jissen_practice_case",
                period="default",
                input_folder_id="input_default",
                output_folder_id="output_default",
                output_sheet_name="出力一覧",
                prompt_template="jissen_practice_case",
            )
            results = {
                "item_0001": {
                    "clinic_name": "山田歯科",
                    "person_name": "田中太郎",
                    "sample_title": "事例タイトル",
                    "comment": "コメント",
                }
            }
            items = [
                {
                    "custom_id": "item_0001",
                    "pdf_data_id": "id_0001",
                    "pdf_file_name": "111-22-3実践事例.pdf",
                }
            ]

            batch_main.step4_generate_pdfs(profile, results=results, items=items)

            # マスター登録済み → create_draft 1 回（CC は None）
            mock_gmail.create_draft.assert_called_once()
            kwargs = mock_gmail.create_draft.call_args.kwargs
            self.assertEqual(kwargs["to_email"], "p@example.com")
            self.assertIsNone(kwargs["cc_email"])
            self.assertEqual(kwargs["person_name"], "田中太郎")
            # 医院名はマスター標準名で upload される
            upload_kwargs = mock_drive.upload_pdf_to_clinic_person.call_args.kwargs
            self.assertEqual(upload_kwargs["clinic_name"], "標準医院名")
            # read_master_records は Step4 全体で 1 回
            self.assertEqual(mock_sheets.read_master_records.call_count, 1)
        finally:
            if att_backup is None:
                att_file.unlink(missing_ok=True)
            else:
                att_file.write_text(att_backup)


if __name__ == "__main__":
    unittest.main()
