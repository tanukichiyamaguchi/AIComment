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
    mock_sheets_client=None,
) -> None:
    """``main.run()`` の処理ループを 1 周以上回すための標準モック。

    ``mock_sheets_client`` が渡された場合、参加者マスター系のデフォルト戻り値
    （未登録扱いで AI 抽出値にフォールバック / メール未登録）も同時設定する。
    """
    mock_drive.list_pdfs.return_value = pdf_files
    mock_drive.download_pdf.return_value = b"%PDF-1.4 fake"
    mock_drive.upload_pdf_to_clinic_person.return_value = {
        "webViewLink": "https://drive.google.com/fake",
        "clinic_folder_id": "clinic_folder_fake",
    }
    mock_reader.extract_text.return_value = "PDFテキスト"
    mock_gen.generate_comment_with_metadata.return_value = {
        "clinic_name": "山田歯科",
        "person_name": "田中太郎",
        "sample_title": "事例タイトル",
        "comment": "コメント本文",
    }
    mock_merger.make_output_filename.return_value = "out.pdf"
    if mock_sheets_client is not None:
        # 既定: マスター未登録 → AI 抽出値で代用 / メール未登録 → 宛先空で下書き
        mock_sheets_client.read_master_records.return_value = []
        mock_sheets_client.lookup_clinic_name.return_value = ""
        mock_sheets_client.lookup_email_by_clinic_and_person.return_value = ""




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
            mock_sheets_client=mock_sheets_client,
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
            mock_sheets_client=mock_sheets_client,
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
    def test_unextractable_filename_is_skipped_with_warning(
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
        """先頭が NNN-NN-N でないファイルはスキップされ、warning が出る（増分処理）。

        管理番号を持たない PDF は重複検知が原理的に不可能なため、毎回再処理
        せずスキップして可視化する（Q1=B / fail-loud）。
        """
        mock_load_profile.return_value = _make_profile()
        mock_sheets_client.get_processed_management_numbers.return_value = set()
        _install_run_mocks(
            mock_drive_client, mock_gen, mock_reader, mock_creator, mock_merger,
            mock_sheets_client=mock_sheets_client,
            pdf_files=[{"id": "id_1", "name": "管理番号なし.pdf"}],
        )

        with self.assertLogs("jissen_comment", level="WARNING") as log_ctx:
            main_module.run(test_count=0, profile_name="jissen_default")

        # スキップされるため append / download / Claude 呼び出しは一切なし
        mock_sheets_client.append_output_record.assert_not_called()
        mock_drive_client.download_pdf.assert_not_called()
        mock_gen.generate_comment_with_metadata.assert_not_called()
        # warning にファイル名が含まれる（サイレントにしない）
        joined = "\n".join(log_ctx.output)
        self.assertIn("管理番号なし.pdf", joined)


class TestRunIncrementalDedup(unittest.TestCase):
    """``run()`` の増分処理（重複検知）。

    管理番号をキーに、出力一覧シートに既存の PDF は download / Claude API
    呼び出しの前に無条件でスキップする（bypass なし）。再処理が必要な場合は
    出力一覧シートの該当行を手動削除すれば、その管理番号は次回実行で再処理
    される。
    """

    @patch("src.main.ensure_fonts")
    @patch("src.main.pdf_merger")
    @patch("src.main.pdf_creator")
    @patch("src.main.pdf_reader")
    @patch("src.main.comment_generator")
    @patch("src.main.sheets_client")
    @patch("src.main.drive_client")
    @patch("src.discover.load_profile")
    def test_processed_pdf_is_skipped_before_download(
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
        """処理済み管理番号の PDF はスキップされ、download / Claude を呼ばない。"""
        mock_load_profile.return_value = _make_profile()
        # 001-01-0 は処理済み、001-01-1 は新規
        mock_sheets_client.get_processed_management_numbers.return_value = {
            "001-01-0",
        }
        _install_run_mocks(
            mock_drive_client, mock_gen, mock_reader, mock_creator, mock_merger,
            mock_sheets_client=mock_sheets_client,
            pdf_files=[
                {"id": "id_1", "name": "001-01-0既存.pdf"},
                {"id": "id_2", "name": "001-01-1新規.pdf"},
            ],
        )

        main_module.run(test_count=0, profile_name="jissen_default")

        # 新規 1 件だけ処理される
        self.assertEqual(mock_sheets_client.append_output_record.call_count, 1)
        appended = mock_sheets_client.append_output_record.call_args.kwargs
        self.assertEqual(appended["management_number"], "001-01-1")
        # download は新規 1 件分のみ（処理済みは download すらしない＝コスト削減）
        self.assertEqual(mock_drive_client.download_pdf.call_count, 1)
        mock_drive_client.download_pdf.assert_called_once_with("id_2")
        # 重複判定は出力シート単位
        mock_sheets_client.get_processed_management_numbers.assert_called_once_with(
            sheet_name="出力一覧",
        )

    @patch("src.main.ensure_fonts")
    @patch("src.main.pdf_merger")
    @patch("src.main.pdf_creator")
    @patch("src.main.pdf_reader")
    @patch("src.main.comment_generator")
    @patch("src.main.sheets_client")
    @patch("src.main.drive_client")
    @patch("src.discover.load_profile")
    def test_all_processed_pdfs_are_skipped_unconditionally(
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
        """全 PDF が処理済みなら無条件でスキップされ、download / Claude を呼ばない。

        重複スキップに bypass はない。再処理は出力シートの行を手動削除して行う。
        """
        mock_load_profile.return_value = _make_profile()
        mock_sheets_client.get_processed_management_numbers.return_value = {
            "001-01-0",
            "001-01-1",
        }
        _install_run_mocks(
            mock_drive_client, mock_gen, mock_reader, mock_creator, mock_merger,
            mock_sheets_client=mock_sheets_client,
            pdf_files=[
                {"id": "id_1", "name": "001-01-0既存.pdf"},
                {"id": "id_2", "name": "001-01-1既存.pdf"},
            ],
        )

        main_module.run(test_count=0, profile_name="jissen_default")

        # 処理済みは無条件スキップ → append / download / Claude は一切なし
        mock_sheets_client.append_output_record.assert_not_called()
        mock_drive_client.download_pdf.assert_not_called()
        mock_gen.generate_comment_with_metadata.assert_not_called()

    @patch("src.main.ensure_fonts")
    @patch("src.main.pdf_merger")
    @patch("src.main.pdf_creator")
    @patch("src.main.pdf_reader")
    @patch("src.main.comment_generator")
    @patch("src.main.sheets_client")
    @patch("src.main.drive_client")
    @patch("src.discover.load_profile")
    def test_test_count_applies_to_new_targets_only(
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
        """``test_count`` は重複・管理番号なしを除外した新規 PDF に適用される。"""
        mock_load_profile.return_value = _make_profile()
        # 001-01-0 は処理済み。新規候補は 001-01-1 / 001-01-2 / 001-01-3。
        mock_sheets_client.get_processed_management_numbers.return_value = {
            "001-01-0",
        }
        _install_run_mocks(
            mock_drive_client, mock_gen, mock_reader, mock_creator, mock_merger,
            mock_sheets_client=mock_sheets_client,
            pdf_files=[
                {"id": "id_0", "name": "001-01-0既存.pdf"},
                {"id": "id_x", "name": "管理番号なし.pdf"},
                {"id": "id_1", "name": "001-01-1新規.pdf"},
                {"id": "id_2", "name": "001-01-2新規.pdf"},
                {"id": "id_3", "name": "001-01-3新規.pdf"},
            ],
        )

        with self.assertLogs("jissen_comment", level="WARNING"):
            main_module.run(test_count=2, profile_name="jissen_default")

        # test_count=2 → 新規候補（3 件）の先頭 2 件のみ処理
        self.assertEqual(mock_sheets_client.append_output_record.call_count, 2)
        mgmt_nums = [
            c.kwargs["management_number"]
            for c in mock_sheets_client.append_output_record.call_args_list
        ]
        self.assertEqual(mgmt_nums, ["001-01-1", "001-01-2"])


class TestRunAttachmentPassthrough(unittest.TestCase):
    """``run()`` の添付資料パススルー（AI 処理せず出力へコピー）。

    ファイル名に「【添付資料】」を含む PDF は実践事例の補足資料であり、
    AI 処理（テキスト抽出 / Claude API / コメントページ生成 / 結合）を
    一切せず、同じ管理番号のメイン実践事例 PDF と同じ ``<医院名>/<個人名>/``
    フォルダへ元ファイル名のままコピーする。出力一覧シートにも記録する。
    """

    @patch("src.main.ensure_fonts")
    @patch("src.main.pdf_merger")
    @patch("src.main.pdf_creator")
    @patch("src.main.pdf_reader")
    @patch("src.main.comment_generator")
    @patch("src.main.sheets_client")
    @patch("src.main.drive_client")
    @patch("src.discover.load_profile")
    def test_attachment_copied_to_same_folder_as_main(
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
        """添付資料は同じ管理番号のメインと同じ医院/個人フォルダにコピーされる。"""
        mock_load_profile.return_value = _make_profile()
        mock_sheets_client.get_processed_management_numbers.return_value = set()
        _install_run_mocks(
            mock_drive_client, mock_gen, mock_reader, mock_creator, mock_merger,
            mock_sheets_client=mock_sheets_client,
            pdf_files=[
                {"id": "id_main", "name": "001-01-0実践事例.pdf"},
                {"id": "id_att", "name": "001-01-0【添付資料】補足データ.pdf"},
            ],
        )

        main_module.run(test_count=0, profile_name="jissen_default")

        # upload は 2 回（メイン + 添付資料）。両方とも同じ医院番号 001 + 同じ
        # AI 抽出医院名で呼ばれ、find_or_create_clinic_folder が同じ
        # 医院フォルダへ合流させる（P-019）。
        self.assertEqual(
            mock_drive_client.upload_pdf_to_clinic_person.call_count, 2
        )
        for call in mock_drive_client.upload_pdf_to_clinic_person.call_args_list:
            self.assertEqual(call.kwargs["clinic_number"], "001")
            self.assertEqual(call.kwargs["clinic_name"], "山田歯科")
            self.assertEqual(call.kwargs["person_name"], "田中太郎")
            self.assertEqual(
                call.kwargs["output_root_folder_id"], "output_folder_yyy"
            )

    @patch("src.main.ensure_fonts")
    @patch("src.main.pdf_merger")
    @patch("src.main.pdf_creator")
    @patch("src.main.pdf_reader")
    @patch("src.main.comment_generator")
    @patch("src.main.sheets_client")
    @patch("src.main.drive_client")
    @patch("src.discover.load_profile")
    def test_attachment_not_sent_to_claude_api(
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
        """添付資料は Claude API に投げられない（generate_comment_with_metadata
        がメイン分しか呼ばれず、添付資料のファイル名では呼ばれない）。"""
        mock_load_profile.return_value = _make_profile()
        mock_sheets_client.get_processed_management_numbers.return_value = set()
        _install_run_mocks(
            mock_drive_client, mock_gen, mock_reader, mock_creator, mock_merger,
            mock_sheets_client=mock_sheets_client,
            pdf_files=[
                {"id": "id_main", "name": "001-01-0実践事例.pdf"},
                {"id": "id_att", "name": "001-01-0【添付資料】補足データ.pdf"},
            ],
        )

        main_module.run(test_count=0, profile_name="jissen_default")

        # Claude API はメイン 1 件分のみ
        self.assertEqual(mock_gen.generate_comment_with_metadata.call_count, 1)
        called_filenames = [
            c.kwargs["pdf_filename"]
            for c in mock_gen.generate_comment_with_metadata.call_args_list
        ]
        self.assertNotIn(
            "001-01-0【添付資料】補足データ.pdf", called_filenames
        )
        # コメントページ生成・マージも添付資料に対しては行われない（メイン1件分のみ）
        self.assertEqual(mock_creator.create_comment_page.call_count, 1)
        self.assertEqual(mock_merger.merge_pdfs.call_count, 1)

    @patch("src.main.ensure_fonts")
    @patch("src.main.pdf_merger")
    @patch("src.main.pdf_creator")
    @patch("src.main.pdf_reader")
    @patch("src.main.comment_generator")
    @patch("src.main.sheets_client")
    @patch("src.main.drive_client")
    @patch("src.discover.load_profile")
    def test_attachment_recorded_in_sheet_with_prefix(
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
        """添付資料は出力一覧シートに「【添付資料】<元名>」で記録される。"""
        mock_load_profile.return_value = _make_profile()
        mock_sheets_client.get_processed_management_numbers.return_value = set()
        _install_run_mocks(
            mock_drive_client, mock_gen, mock_reader, mock_creator, mock_merger,
            mock_sheets_client=mock_sheets_client,
            pdf_files=[
                {"id": "id_main", "name": "001-01-0実践事例.pdf"},
                {"id": "id_att", "name": "001-01-0【添付資料】補足データ.pdf"},
            ],
        )

        main_module.run(test_count=0, profile_name="jissen_default")

        # append は 2 回。添付資料行は sample_name が「【添付資料】<元名>」、
        # 管理番号・医院名・個人名はメインと同じ。
        self.assertEqual(mock_sheets_client.append_output_record.call_count, 2)
        att_call = mock_sheets_client.append_output_record.call_args_list[1]
        self.assertEqual(
            att_call.kwargs["sample_name"],
            "【添付資料】001-01-0【添付資料】補足データ.pdf",
        )
        self.assertEqual(att_call.kwargs["management_number"], "001-01-0")
        self.assertEqual(att_call.kwargs["clinic_name"], "山田歯科")
        self.assertEqual(att_call.kwargs["person_name"], "田中太郎")

    @patch("src.main.ensure_fonts")
    @patch("src.main.pdf_merger")
    @patch("src.main.pdf_creator")
    @patch("src.main.pdf_reader")
    @patch("src.main.comment_generator")
    @patch("src.main.sheets_client")
    @patch("src.main.drive_client")
    @patch("src.discover.load_profile")
    def test_attachment_output_filename_is_original(
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
        """添付資料の出力ファイル名は元のまま（make_output_filename を使わない）。"""
        mock_load_profile.return_value = _make_profile()
        mock_sheets_client.get_processed_management_numbers.return_value = set()
        _install_run_mocks(
            mock_drive_client, mock_gen, mock_reader, mock_creator, mock_merger,
            mock_sheets_client=mock_sheets_client,
            pdf_files=[
                {"id": "id_main", "name": "001-01-0実践事例.pdf"},
                {"id": "id_att", "name": "001-01-0【添付資料】補足データ.pdf"},
            ],
        )

        main_module.run(test_count=0, profile_name="jissen_default")

        # 添付資料 upload の file_name は元ファイル名そのまま
        att_upload = mock_drive_client.upload_pdf_to_clinic_person.call_args_list[1]
        self.assertEqual(
            att_upload.kwargs["file_name"],
            "001-01-0【添付資料】補足データ.pdf",
        )
        # make_output_filename は添付資料には使われない（メイン1件分のみ）
        self.assertEqual(mock_merger.make_output_filename.call_count, 1)

    @patch("src.main.ensure_fonts")
    @patch("src.main.pdf_merger")
    @patch("src.main.pdf_creator")
    @patch("src.main.pdf_reader")
    @patch("src.main.comment_generator")
    @patch("src.main.sheets_client")
    @patch("src.main.drive_client")
    @patch("src.discover.load_profile")
    def test_orphan_attachment_skipped_with_warning(
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
        """対応するメインがこの実行に無い添付資料はスキップされ warning が出る。"""
        mock_load_profile.return_value = _make_profile()
        mock_sheets_client.get_processed_management_numbers.return_value = set()
        # 添付資料の管理番号 002-02-0 に対応するメインは入力に無い
        _install_run_mocks(
            mock_drive_client, mock_gen, mock_reader, mock_creator, mock_merger,
            mock_sheets_client=mock_sheets_client,
            pdf_files=[
                {"id": "id_main", "name": "001-01-0実践事例.pdf"},
                {"id": "id_att", "name": "002-02-0【添付資料】孤児.pdf"},
            ],
        )

        with self.assertLogs("jissen_comment", level="WARNING") as log_ctx:
            main_module.run(test_count=0, profile_name="jissen_default")

        # メイン 1 件だけ append、孤児添付資料は append されない
        self.assertEqual(mock_sheets_client.append_output_record.call_count, 1)
        appended = mock_sheets_client.append_output_record.call_args.kwargs
        self.assertEqual(appended["management_number"], "001-01-0")
        # 孤児添付資料は download されない（メイン不在判定が先）
        mock_drive_client.download_pdf.assert_called_once_with("id_main")
        joined = "\n".join(log_ctx.output)
        self.assertIn("002-02-0【添付資料】孤児.pdf", joined)
        self.assertIn("メイン実践事例 PDF がこの実行で処理されていない", joined)

    @patch("src.main.ensure_fonts")
    @patch("src.main.pdf_merger")
    @patch("src.main.pdf_creator")
    @patch("src.main.pdf_reader")
    @patch("src.main.comment_generator")
    @patch("src.main.sheets_client")
    @patch("src.main.drive_client")
    @patch("src.discover.load_profile")
    def test_attachment_with_processed_mgmt_number_skipped(
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
        """前回実行で処理済みの管理番号を持つ添付資料はスキップされる。

        重複判定セットは実行開始時のスナップショット。同一実行内のメイン処理
        がこのスナップショットを変えないことも確認する。
        """
        mock_load_profile.return_value = _make_profile()
        # 001-01-0 は前回処理済み（添付資料も前回コピー済みと見なす）
        mock_sheets_client.get_processed_management_numbers.return_value = {
            "001-01-0",
        }
        _install_run_mocks(
            mock_drive_client, mock_gen, mock_reader, mock_creator, mock_merger,
            mock_sheets_client=mock_sheets_client,
            pdf_files=[
                {"id": "id_main", "name": "001-01-0実践事例.pdf"},
                {"id": "id_att", "name": "001-01-0【添付資料】補足.pdf"},
            ],
        )

        main_module.run(test_count=0, profile_name="jissen_default")

        # メインも添付資料も処理済みスキップ → append / download なし
        mock_sheets_client.append_output_record.assert_not_called()
        mock_drive_client.download_pdf.assert_not_called()

    @patch("src.main.ensure_fonts")
    @patch("src.main.pdf_merger")
    @patch("src.main.pdf_creator")
    @patch("src.main.pdf_reader")
    @patch("src.main.comment_generator")
    @patch("src.main.sheets_client")
    @patch("src.main.drive_client")
    @patch("src.discover.load_profile")
    def test_test_count_does_not_apply_to_attachments(
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
        """``test_count`` はメイン PDF にのみ適用される。添付資料は対応する
        メインがこの実行で処理されていれば test_count に関わらずコピーされる。"""
        mock_load_profile.return_value = _make_profile()
        mock_sheets_client.get_processed_management_numbers.return_value = set()
        _install_run_mocks(
            mock_drive_client, mock_gen, mock_reader, mock_creator, mock_merger,
            mock_sheets_client=mock_sheets_client,
            pdf_files=[
                {"id": "id_m1", "name": "001-01-1実践事例.pdf"},
                {"id": "id_m2", "name": "001-01-2実践事例.pdf"},
                {"id": "id_a1", "name": "001-01-1【添付資料】補足.pdf"},
            ],
        )

        # test_count=1 → メインは 001-01-1 のみ処理。添付資料 001-01-1 は
        # 対応メインが処理されたのでコピーされる。
        main_module.run(test_count=1, profile_name="jissen_default")

        # append は 2 回（メイン 001-01-1 + 添付資料 001-01-1）
        self.assertEqual(mock_sheets_client.append_output_record.call_count, 2)
        mgmt_nums = [
            c.kwargs["management_number"]
            for c in mock_sheets_client.append_output_record.call_args_list
        ]
        self.assertEqual(mgmt_nums, ["001-01-1", "001-01-1"])


class TestRunClinicNumberFolder(unittest.TestCase):
    """``run()`` の医院番号付きフォルダ名 + 医院フォルダURLシート記録。

    医院フォルダ名は ``<医院番号>_<医院名>``（医院番号 = 管理番号の先頭
    セグメント）。医院フォルダURLシート（``<出力シート名>_医院``）に
    医院番号 / 医院名 / フォルダURL を記録し、同一医院は 1 行のみ。
    """

    @patch("src.main.ensure_fonts")
    @patch("src.main.pdf_merger")
    @patch("src.main.pdf_creator")
    @patch("src.main.pdf_reader")
    @patch("src.main.comment_generator")
    @patch("src.main.sheets_client")
    @patch("src.main.drive_client")
    @patch("src.discover.load_profile")
    def test_clinic_number_and_name_passed_separately(
        self, mock_load_profile, mock_drive_client, mock_sheets_client,
        mock_gen, mock_reader, mock_creator, mock_merger, mock_ensure_fonts,
    ):
        """``upload_pdf_to_clinic_person`` に医院番号と医院名が別引数で渡る
        （P-019: 医院フォルダの識別は医院番号のみ、医院名は AI 抽出の生の値）。"""
        mock_load_profile.return_value = _make_profile()
        mock_sheets_client.get_processed_management_numbers.return_value = set()
        mock_sheets_client.get_recorded_clinic_numbers.return_value = set()
        _install_run_mocks(
            mock_drive_client, mock_gen, mock_reader, mock_creator, mock_merger,
            mock_sheets_client=mock_sheets_client,
            pdf_files=[{"id": "id_1", "name": "012-03-4実践事例.pdf"}],
        )

        main_module.run(test_count=0, profile_name="jissen_default")

        upload_kwargs = (
            mock_drive_client.upload_pdf_to_clinic_person.call_args.kwargs
        )
        # 医院番号は別引数として 012、医院名は AI 抽出の生の値（プレフィックス無し）
        self.assertEqual(upload_kwargs["clinic_number"], "012")
        self.assertEqual(upload_kwargs["clinic_name"], "山田歯科")

    @patch("src.main.ensure_fonts")
    @patch("src.main.pdf_merger")
    @patch("src.main.pdf_creator")
    @patch("src.main.pdf_reader")
    @patch("src.main.comment_generator")
    @patch("src.main.sheets_client")
    @patch("src.main.drive_client")
    @patch("src.discover.load_profile")
    def test_clinic_folder_url_recorded_in_clinic_sheet(
        self, mock_load_profile, mock_drive_client, mock_sheets_client,
        mock_gen, mock_reader, mock_creator, mock_merger, mock_ensure_fonts,
    ):
        """医院フォルダURLシート（``<出力シート名>_医院``）に医院が記録される。"""
        mock_load_profile.return_value = _make_profile(output_sheet_name="出力一覧")
        mock_sheets_client.get_processed_management_numbers.return_value = set()
        mock_sheets_client.get_recorded_clinic_numbers.return_value = set()
        mock_drive_client.upload_pdf_to_clinic_person.return_value = {
            "webViewLink": "https://drive.google.com/fake",
            "clinic_folder_id": "clinic_xyz",
        }
        _install_run_mocks(
            mock_drive_client, mock_gen, mock_reader, mock_creator, mock_merger,
            mock_sheets_client=mock_sheets_client,
            pdf_files=[{"id": "id_1", "name": "012-03-4実践事例.pdf"}],
        )
        mock_drive_client.upload_pdf_to_clinic_person.return_value = {
            "webViewLink": "https://drive.google.com/fake",
            "clinic_folder_id": "clinic_xyz",
        }

        main_module.run(test_count=0, profile_name="jissen_default")

        # 医院シート名は <出力シート名>_医院
        snapshot_call = (
            mock_sheets_client.get_recorded_clinic_numbers.call_args.kwargs
        )
        self.assertEqual(snapshot_call["sheet_name"], "出力一覧_医院")
        # append_clinic_folder_record が医院番号 / 医院名 / フォルダURL で呼ばれる
        rec_call = mock_sheets_client.append_clinic_folder_record.call_args.kwargs
        self.assertEqual(rec_call["clinic_number"], "012")
        self.assertEqual(rec_call["clinic_name"], "山田歯科")
        self.assertEqual(
            rec_call["clinic_folder_url"],
            "https://drive.google.com/drive/folders/clinic_xyz",
        )
        self.assertEqual(rec_call["sheet_name"], "出力一覧_医院")

    @patch("src.main.ensure_fonts")
    @patch("src.main.pdf_merger")
    @patch("src.main.pdf_creator")
    @patch("src.main.pdf_reader")
    @patch("src.main.comment_generator")
    @patch("src.main.sheets_client")
    @patch("src.main.drive_client")
    @patch("src.discover.load_profile")
    def test_same_clinic_recorded_only_once(
        self, mock_load_profile, mock_drive_client, mock_sheets_client,
        mock_gen, mock_reader, mock_creator, mock_merger, mock_ensure_fonts,
    ):
        """同一医院番号の PDF が複数あっても医院シートには 1 行のみ記録される。"""
        mock_load_profile.return_value = _make_profile()
        mock_sheets_client.get_processed_management_numbers.return_value = set()
        mock_sheets_client.get_recorded_clinic_numbers.return_value = set()
        # 医院番号 005 が 2 件、007 が 1 件
        _install_run_mocks(
            mock_drive_client, mock_gen, mock_reader, mock_creator, mock_merger,
            mock_sheets_client=mock_sheets_client,
            pdf_files=[
                {"id": "id_1", "name": "005-01-0実践事例A.pdf"},
                {"id": "id_2", "name": "005-01-1実践事例B.pdf"},
                {"id": "id_3", "name": "007-02-0実践事例C.pdf"},
            ],
        )

        main_module.run(test_count=0, profile_name="jissen_default")

        # append_clinic_folder_record は医院番号ごとに 1 回（005 と 007）
        recorded = [
            c.kwargs["clinic_number"]
            for c in mock_sheets_client.append_clinic_folder_record.call_args_list
        ]
        self.assertEqual(sorted(recorded), ["005", "007"])

    @patch("src.main.ensure_fonts")
    @patch("src.main.pdf_merger")
    @patch("src.main.pdf_creator")
    @patch("src.main.pdf_reader")
    @patch("src.main.comment_generator")
    @patch("src.main.sheets_client")
    @patch("src.main.drive_client")
    @patch("src.discover.load_profile")
    def test_already_recorded_clinic_is_not_appended_again(
        self, mock_load_profile, mock_drive_client, mock_sheets_client,
        mock_gen, mock_reader, mock_creator, mock_merger, mock_ensure_fonts,
    ):
        """医院シートに既に記録済みの医院番号は再追記されない（スナップショット）。"""
        mock_load_profile.return_value = _make_profile()
        mock_sheets_client.get_processed_management_numbers.return_value = set()
        # 医院番号 012 は前回実行で既に医院シートに記録済み
        mock_sheets_client.get_recorded_clinic_numbers.return_value = {"012"}
        _install_run_mocks(
            mock_drive_client, mock_gen, mock_reader, mock_creator, mock_merger,
            mock_sheets_client=mock_sheets_client,
            pdf_files=[{"id": "id_1", "name": "012-03-4実践事例.pdf"}],
        )

        main_module.run(test_count=0, profile_name="jissen_default")

        # 出力一覧シートには記録されるが、医院シートには追記されない
        mock_sheets_client.append_output_record.assert_called_once()
        mock_sheets_client.append_clinic_folder_record.assert_not_called()

    @patch("src.main.ensure_fonts")
    @patch("src.main.pdf_merger")
    @patch("src.main.pdf_creator")
    @patch("src.main.pdf_reader")
    @patch("src.main.comment_generator")
    @patch("src.main.sheets_client")
    @patch("src.main.drive_client")
    @patch("src.discover.load_profile")
    def test_attachment_uses_same_clinic_number(
        self, mock_load_profile, mock_drive_client, mock_sheets_client,
        mock_gen, mock_reader, mock_creator, mock_merger, mock_ensure_fonts,
    ):
        """添付資料もメインと同じ医院番号 + 医院名で
        ``upload_pdf_to_clinic_person`` が呼ばれる（同じ医院フォルダへ合流、P-019）。"""
        mock_load_profile.return_value = _make_profile()
        mock_sheets_client.get_processed_management_numbers.return_value = set()
        mock_sheets_client.get_recorded_clinic_numbers.return_value = set()
        _install_run_mocks(
            mock_drive_client, mock_gen, mock_reader, mock_creator, mock_merger,
            mock_sheets_client=mock_sheets_client,
            pdf_files=[
                {"id": "id_main", "name": "088-01-0実践事例.pdf"},
                {"id": "id_att", "name": "088-01-0【添付資料】補足.pdf"},
            ],
        )

        main_module.run(test_count=0, profile_name="jissen_default")

        # メイン・添付資料ともに同じ医院番号 088 + 同じ医院名で呼ばれる
        for call in mock_drive_client.upload_pdf_to_clinic_person.call_args_list:
            self.assertEqual(call.kwargs["clinic_number"], "088")
            self.assertEqual(call.kwargs["clinic_name"], "山田歯科")
        # 出力一覧シートの医院名列は AI 抽出値（医院番号なし）
        for call in mock_sheets_client.append_output_record.call_args_list:
            self.assertEqual(call.kwargs["clinic_name"], "山田歯科")


class TestRunMasterSheetIntegration(unittest.TestCase):
    """``run()`` の参加者マスター統合（医院名標準化 + Gmail 下書き作成）。

    PDF アップロード成功後 + シート追記後、参加者マスターシートから
    管理番号でメールアドレスを引き Gmail 下書きを作成する。CC は使わない。
    医院名は管理番号 prefix（医院番号）で標準表記を引き、未登録なら AI
    抽出値で代用 + 警告ログ。メール未登録時は下書きスキップ + 警告ログ。
    例外時は処理を止めず次の PDF へ進む（fail-soft）。
    """

    def _master_record(
        self, management_number, clinic_name, email,
        participant_name="田中太郎", venue="",
    ):
        """テスト用 MasterRecord。

        ``management_number`` は ``xxx-yy`` 形式（例 ``012-03``）で渡す。
        医院コードは property で先頭セグメントから派生する。
        ``participant_name`` のデフォルトは ``_install_run_mocks`` の AI
        抽出名と一致させ、突合がヒットするようにしてある。
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
    @patch("src.discover.load_profile")
    def test_gmail_draft_created_with_master_email_no_cc(
        self, mock_load_profile, mock_drive_client, mock_sheets_client,
        mock_gmail_client, mock_gen, mock_reader, mock_creator, mock_merger,
        mock_ensure_fonts,
    ):
        """管理番号 hit → TO=マスターのメール、CC=None で create_draft。"""
        from src import sheets_client as real_sheets_client

        mock_load_profile.return_value = _make_profile()
        mock_sheets_client.get_processed_management_numbers.return_value = set()
        mock_sheets_client.get_recorded_clinic_numbers.return_value = set()
        mock_sheets_client.read_master_records.return_value = [
            self._master_record(
                "012-03", "標準医院名", "tanaka@example.com",
            ),
        ]
        mock_sheets_client.lookup_clinic_name.side_effect = (
            lambda recs, cn: real_sheets_client.lookup_clinic_name(recs, cn)
        )
        mock_sheets_client.lookup_email_by_clinic_and_person.side_effect = (
            lambda recs, cn, pn: real_sheets_client.lookup_email_by_clinic_and_person(recs, cn, pn)
        )
        _install_run_mocks(
            mock_drive_client, mock_gen, mock_reader, mock_creator, mock_merger,
            pdf_files=[{"id": "id_1", "name": "012-03-4実践事例.pdf"}],
        )

        main_module.run(test_count=0, profile_name="jissen_default")

        mock_gmail_client.create_draft.assert_called_once()
        kwargs = mock_gmail_client.create_draft.call_args.kwargs
        self.assertEqual(kwargs["to_email"], "tanaka@example.com")
        self.assertIsNone(kwargs["cc_email"])  # CC は使わない
        self.assertEqual(kwargs["person_name"], "田中太郎")

    @patch("src.main.ensure_fonts")
    @patch("src.main.pdf_merger")
    @patch("src.main.pdf_creator")
    @patch("src.main.pdf_reader")
    @patch("src.main.comment_generator")
    @patch("src.main.gmail_client")
    @patch("src.main.sheets_client")
    @patch("src.main.drive_client")
    @patch("src.discover.load_profile")
    def test_clinic_name_from_master_replaces_ai_value(
        self, mock_load_profile, mock_drive_client, mock_sheets_client,
        mock_gmail_client, mock_gen, mock_reader, mock_creator, mock_merger,
        mock_ensure_fonts,
    ):
        """医院名 lookup hit → マスター標準名でフォルダ作成・シート記録。"""
        from src import sheets_client as real_sheets_client

        mock_load_profile.return_value = _make_profile()
        mock_sheets_client.get_processed_management_numbers.return_value = set()
        mock_sheets_client.get_recorded_clinic_numbers.return_value = set()
        mock_sheets_client.read_master_records.return_value = [
            self._master_record("012-03", "標準医院名", "t@example.com"),
        ]
        mock_sheets_client.lookup_clinic_name.side_effect = (
            lambda recs, cn: real_sheets_client.lookup_clinic_name(recs, cn)
        )
        mock_sheets_client.lookup_email_by_clinic_and_person.side_effect = (
            lambda recs, cn, pn: real_sheets_client.lookup_email_by_clinic_and_person(recs, cn, pn)
        )
        _install_run_mocks(
            mock_drive_client, mock_gen, mock_reader, mock_creator, mock_merger,
            pdf_files=[{"id": "id_1", "name": "012-03-4実践事例.pdf"}],
        )

        main_module.run(test_count=0, profile_name="jissen_default")

        # AI 抽出値は「山田歯科」だが、フォルダ作成・出力シート行・医院シート行
        # すべて「標準医院名」（マスターの値）に統一される
        upload_kwargs = mock_drive_client.upload_pdf_to_clinic_person.call_args.kwargs
        self.assertEqual(upload_kwargs["clinic_name"], "標準医院名")
        append_kwargs = mock_sheets_client.append_output_record.call_args.kwargs
        self.assertEqual(append_kwargs["clinic_name"], "標準医院名")
        rec_kwargs = mock_sheets_client.append_clinic_folder_record.call_args.kwargs
        self.assertEqual(rec_kwargs["clinic_name"], "標準医院名")

    @patch("src.main.ensure_fonts")
    @patch("src.main.pdf_merger")
    @patch("src.main.pdf_creator")
    @patch("src.main.pdf_reader")
    @patch("src.main.comment_generator")
    @patch("src.main.gmail_client")
    @patch("src.main.sheets_client")
    @patch("src.main.drive_client")
    @patch("src.discover.load_profile")
    def test_clinic_name_falls_back_to_ai_with_warning(
        self, mock_load_profile, mock_drive_client, mock_sheets_client,
        mock_gmail_client, mock_gen, mock_reader, mock_creator, mock_merger,
        mock_ensure_fonts,
    ):
        """医院名 lookup ミス → AI 抽出値で代用 + 警告ログ（PDF 処理は続行）。"""
        from src import sheets_client as real_sheets_client

        mock_load_profile.return_value = _make_profile()
        mock_sheets_client.get_processed_management_numbers.return_value = set()
        mock_sheets_client.get_recorded_clinic_numbers.return_value = set()
        mock_sheets_client.read_master_records.return_value = []  # マスター空
        mock_sheets_client.lookup_clinic_name.side_effect = (
            lambda recs, cn: real_sheets_client.lookup_clinic_name(recs, cn)
        )
        mock_sheets_client.lookup_email_by_clinic_and_person.side_effect = (
            lambda recs, cn, pn: real_sheets_client.lookup_email_by_clinic_and_person(recs, cn, pn)
        )
        _install_run_mocks(
            mock_drive_client, mock_gen, mock_reader, mock_creator, mock_merger,
            pdf_files=[{"id": "id_1", "name": "012-03-4実践事例.pdf"}],
        )

        with self.assertLogs("jissen_comment", level="WARNING") as log_ctx:
            main_module.run(test_count=0, profile_name="jissen_default")

        # AI 抽出値「山田歯科」がフォルダ命名・シート列に伝搬する
        upload_kwargs = mock_drive_client.upload_pdf_to_clinic_person.call_args.kwargs
        self.assertEqual(upload_kwargs["clinic_name"], "山田歯科")
        append_kwargs = mock_sheets_client.append_output_record.call_args.kwargs
        self.assertEqual(append_kwargs["clinic_name"], "山田歯科")
        # 警告ログには医院番号と AI 抽出値が含まれる
        joined = "\n".join(log_ctx.output)
        self.assertIn("参加者マスター未登録", joined)
        self.assertIn("012", joined)
        self.assertIn("山田歯科", joined)

    @patch("src.main.ensure_fonts")
    @patch("src.main.pdf_merger")
    @patch("src.main.pdf_creator")
    @patch("src.main.pdf_reader")
    @patch("src.main.comment_generator")
    @patch("src.main.gmail_client")
    @patch("src.main.sheets_client")
    @patch("src.main.drive_client")
    @patch("src.discover.load_profile")
    def test_draft_created_with_empty_to_when_email_unregistered(
        self, mock_load_profile, mock_drive_client, mock_sheets_client,
        mock_gmail_client, mock_gen, mock_reader, mock_creator, mock_merger,
        mock_ensure_fonts,
    ):
        """メール lookup ミス → 宛先空で create_draft 呼ばれる + 警告ログ。

        手動で宛先を入れてもらうための運用前提。誤発送防止と見落とし防止を
        両立させる（PDF 処理は引き続き完了する）。
        """
        from src import sheets_client as real_sheets_client

        mock_load_profile.return_value = _make_profile()
        mock_sheets_client.get_processed_management_numbers.return_value = set()
        mock_sheets_client.get_recorded_clinic_numbers.return_value = set()
        mock_sheets_client.read_master_records.return_value = []  # 未登録
        mock_sheets_client.lookup_clinic_name.side_effect = (
            lambda recs, cn: real_sheets_client.lookup_clinic_name(recs, cn)
        )
        mock_sheets_client.lookup_email_by_clinic_and_person.side_effect = (
            lambda recs, cn, pn: real_sheets_client.lookup_email_by_clinic_and_person(recs, cn, pn)
        )
        _install_run_mocks(
            mock_drive_client, mock_gen, mock_reader, mock_creator, mock_merger,
            pdf_files=[{"id": "id_1", "name": "012-03-4実践事例.pdf"}],
        )

        with self.assertLogs("jissen_comment", level="WARNING") as log_ctx:
            main_module.run(test_count=0, profile_name="jissen_default")

        # create_draft は呼ばれる（宛先空で）
        mock_gmail_client.create_draft.assert_called_once()
        kwargs = mock_gmail_client.create_draft.call_args.kwargs
        self.assertEqual(kwargs["to_email"], "")
        self.assertEqual(kwargs["person_name"], "田中太郎")
        # 警告ログに「メール未ヒット → 宛先空で下書き予定」 + 医院管理番号が含まれる
        joined = "\n".join(log_ctx.output)
        self.assertIn("メール未ヒット", joined)
        self.assertIn("012", joined)
        # PDF アップロード・シート追記は通常通り完了する（fail-soft）
        mock_drive_client.upload_pdf_to_clinic_person.assert_called_once()
        mock_sheets_client.append_output_record.assert_called_once()

    @patch("src.main.ensure_fonts")
    @patch("src.main.pdf_merger")
    @patch("src.main.pdf_creator")
    @patch("src.main.pdf_reader")
    @patch("src.main.comment_generator")
    @patch("src.main.gmail_client")
    @patch("src.main.sheets_client")
    @patch("src.main.drive_client")
    @patch("src.discover.load_profile")
    def test_draft_creation_exception_does_not_stop_processing(
        self, mock_load_profile, mock_drive_client, mock_sheets_client,
        mock_gmail_client, mock_gen, mock_reader, mock_creator, mock_merger,
        mock_ensure_fonts,
    ):
        """create_draft が例外を投げても次の PDF に進む（fail-soft）。"""
        from src import sheets_client as real_sheets_client

        mock_load_profile.return_value = _make_profile()
        mock_sheets_client.get_processed_management_numbers.return_value = set()
        mock_sheets_client.get_recorded_clinic_numbers.return_value = set()
        mock_sheets_client.read_master_records.return_value = [
            self._master_record("001-01", "三浦歯科医院", "p1@example.com"),
            self._master_record("002-01", "山本歯科", "p2@example.com"),
        ]
        mock_sheets_client.lookup_clinic_name.side_effect = (
            lambda recs, cn: real_sheets_client.lookup_clinic_name(recs, cn)
        )
        mock_sheets_client.lookup_email_by_clinic_and_person.side_effect = (
            lambda recs, cn, pn: real_sheets_client.lookup_email_by_clinic_and_person(recs, cn, pn)
        )
        # create_draft は最初の呼び出しで例外
        mock_gmail_client.create_draft.side_effect = [
            RuntimeError("Gmail API down"),
            "draft_ok_2",
        ]
        _install_run_mocks(
            mock_drive_client, mock_gen, mock_reader, mock_creator, mock_merger,
            pdf_files=[
                {"id": "id_1", "name": "001-01-0実践事例.pdf"},
                {"id": "id_2", "name": "002-01-0実践事例.pdf"},
            ],
        )

        main_module.run(test_count=0, profile_name="jissen_default")

        # 両方の PDF が処理される（最初の Gmail 失敗で停止しない）
        self.assertEqual(mock_drive_client.upload_pdf_to_clinic_person.call_count, 2)
        self.assertEqual(mock_sheets_client.append_output_record.call_count, 2)
        # create_draft は両方の PDF で呼ばれる
        self.assertEqual(mock_gmail_client.create_draft.call_count, 2)

    @patch("src.main.ensure_fonts")
    @patch("src.main.pdf_merger")
    @patch("src.main.pdf_creator")
    @patch("src.main.pdf_reader")
    @patch("src.main.comment_generator")
    @patch("src.main.gmail_client")
    @patch("src.main.sheets_client")
    @patch("src.main.drive_client")
    @patch("src.discover.load_profile")
    def test_master_records_read_once_for_whole_run(
        self, mock_load_profile, mock_drive_client, mock_sheets_client,
        mock_gmail_client, mock_gen, mock_reader, mock_creator, mock_merger,
        mock_ensure_fonts,
    ):
        """read_master_records はループ前に 1 回だけ呼ばれる（ループ内で再読しない）。"""
        from src import sheets_client as real_sheets_client

        mock_load_profile.return_value = _make_profile()
        mock_sheets_client.get_processed_management_numbers.return_value = set()
        mock_sheets_client.get_recorded_clinic_numbers.return_value = set()
        mock_sheets_client.read_master_records.return_value = []
        mock_sheets_client.lookup_clinic_name.side_effect = (
            lambda recs, cn: real_sheets_client.lookup_clinic_name(recs, cn)
        )
        mock_sheets_client.lookup_email_by_clinic_and_person.side_effect = (
            lambda recs, cn, pn: real_sheets_client.lookup_email_by_clinic_and_person(recs, cn, pn)
        )
        _install_run_mocks(
            mock_drive_client, mock_gen, mock_reader, mock_creator, mock_merger,
            pdf_files=[
                {"id": "id_1", "name": "001-01-0実践事例A.pdf"},
                {"id": "id_2", "name": "002-01-0実践事例B.pdf"},
                {"id": "id_3", "name": "003-01-0実践事例C.pdf"},
            ],
        )

        main_module.run(test_count=0, profile_name="jissen_default")

        # 3 件処理しても read_master_records は 1 回だけ
        self.assertEqual(mock_sheets_client.read_master_records.call_count, 1)

    @patch("src.main.ensure_fonts")
    @patch("src.main.pdf_merger")
    @patch("src.main.pdf_creator")
    @patch("src.main.pdf_reader")
    @patch("src.main.comment_generator")
    @patch("src.main.gmail_client")
    @patch("src.main.sheets_client")
    @patch("src.main.drive_client")
    @patch("src.discover.load_profile")
    def test_attachment_path_also_creates_gmail_draft(
        self, mock_load_profile, mock_drive_client, mock_sheets_client,
        mock_gmail_client, mock_gen, mock_reader, mock_creator, mock_merger,
        mock_ensure_fonts,
    ):
        """添付資料経路の PDF も同じメールアドレスのグループにまとめられる。

        メイン PDF + 添付資料 PDF が同じ個人 (=同じメールアドレス) のものなら、
        個別に下書きを作らず 1 通の下書きに両方添付される。
        """
        from src import sheets_client as real_sheets_client

        mock_load_profile.return_value = _make_profile()
        mock_sheets_client.get_processed_management_numbers.return_value = set()
        mock_sheets_client.get_recorded_clinic_numbers.return_value = set()
        mock_sheets_client.read_master_records.return_value = [
            self._master_record("001-01", "三浦歯科医院", "tanaka@example.com"),
        ]
        mock_sheets_client.lookup_clinic_name.side_effect = (
            lambda recs, cn: real_sheets_client.lookup_clinic_name(recs, cn)
        )
        mock_sheets_client.lookup_email_by_clinic_and_person.side_effect = (
            lambda recs, cn, pn: real_sheets_client.lookup_email_by_clinic_and_person(recs, cn, pn)
        )
        _install_run_mocks(
            mock_drive_client, mock_gen, mock_reader, mock_creator, mock_merger,
            pdf_files=[
                {"id": "id_m", "name": "001-01-0実践事例.pdf"},
                {"id": "id_a", "name": "001-01-0【添付資料】補足.pdf"},
            ],
        )

        main_module.run(test_count=0, profile_name="jissen_default")

        # メイン + 添付資料が同じ tanaka@example.com に集約 → 下書きは 1 通
        mock_gmail_client.create_draft.assert_called_once()
        kwargs = mock_gmail_client.create_draft.call_args.kwargs
        self.assertEqual(kwargs["to_email"], "tanaka@example.com")
        self.assertEqual(kwargs["person_name"], "田中太郎")
        self.assertIsNone(kwargs["cc_email"])
        # 添付ファイルは 2 件（メイン PDF + 添付資料 PDF）
        self.assertEqual(len(kwargs["pdf_paths"]), 2)

    @patch("src.main.ensure_fonts")
    @patch("src.main.pdf_merger")
    @patch("src.main.pdf_creator")
    @patch("src.main.pdf_reader")
    @patch("src.main.comment_generator")
    @patch("src.main.gmail_client")
    @patch("src.main.sheets_client")
    @patch("src.main.drive_client")
    @patch("src.discover.load_profile")
    def test_master_records_shared_with_attachment_path(
        self, mock_load_profile, mock_drive_client, mock_sheets_client,
        mock_gmail_client, mock_gen, mock_reader, mock_creator, mock_merger,
        mock_ensure_fonts,
    ):
        """master_records は 1 回読み込みでメイン + 添付資料経路で共有される。"""
        from src import sheets_client as real_sheets_client

        mock_load_profile.return_value = _make_profile()
        mock_sheets_client.get_processed_management_numbers.return_value = set()
        mock_sheets_client.get_recorded_clinic_numbers.return_value = set()
        mock_sheets_client.read_master_records.return_value = [
            self._master_record("001-01", "三浦歯科医院", "tanaka@example.com"),
        ]
        mock_sheets_client.lookup_clinic_name.side_effect = (
            lambda recs, cn: real_sheets_client.lookup_clinic_name(recs, cn)
        )
        mock_sheets_client.lookup_email_by_clinic_and_person.side_effect = (
            lambda recs, cn, pn: real_sheets_client.lookup_email_by_clinic_and_person(recs, cn, pn)
        )
        _install_run_mocks(
            mock_drive_client, mock_gen, mock_reader, mock_creator, mock_merger,
            pdf_files=[
                {"id": "id_m", "name": "001-01-0実践事例.pdf"},
                {"id": "id_a", "name": "001-01-0【添付資料】補足.pdf"},
            ],
        )

        main_module.run(test_count=0, profile_name="jissen_default")

        # メイン + 添付資料 = 2 件処理しても read_master_records は 1 回のみ
        self.assertEqual(mock_sheets_client.read_master_records.call_count, 1)


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
            mock_sheets_client=mock_sheets_client,
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


class TestCreateGroupedDraftsForRun(unittest.TestCase):
    """F-06 / pdf_paths 型 回帰防止: グループ化 + 下書き作成の挙動。"""

    def _items(self, *triples):
        """``[(email, person_name, pdf_path), ...]`` を draft_items に変換。"""
        return [
            {
                "email": email,
                "person_name": person,
                "pdf_path": path,
                "clinic_number": "101",
            }
            for email, person, path in triples
        ]

    def test_single_person_single_email_uses_name_as_is(self):
        gmail = MagicMock()
        items = self._items(("a@example.com", "山田太郎", "/tmp/a.pdf"))
        main_module._create_grouped_drafts_for_run(items, gmail)
        gmail.create_draft.assert_called_once()
        call = gmail.create_draft.call_args
        self.assertEqual(call.kwargs["person_name"], "山田太郎")
        self.assertEqual(call.kwargs["pdf_paths"], ["/tmp/a.pdf"])

    def test_multiple_persons_same_email_uses_combined_name(self):
        """同一メール × 別人 → 'X ほかN名' 形式 + WARNING。"""
        gmail = MagicMock()
        items = self._items(
            ("shared@example.com", "山田太郎", "/tmp/a.pdf"),
            ("shared@example.com", "鈴木花子", "/tmp/b.pdf"),
        )
        with self.assertLogs("jissen_comment", level="WARNING") as cm:
            main_module._create_grouped_drafts_for_run(items, gmail)
        gmail.create_draft.assert_called_once()
        call = gmail.create_draft.call_args
        # sorted set なので 鈴木花子 < 山田太郎 の順だが、安定して "ほか1名" を含む
        self.assertIn("ほか1名", call.kwargs["person_name"])
        self.assertEqual(
            sorted(call.kwargs["pdf_paths"]), ["/tmp/a.pdf", "/tmp/b.pdf"]
        )
        self.assertTrue(any("異なる個人名" in m for m in cm.output))

    def test_empty_email_creates_separate_draft_with_list_pdf_paths(self):
        """メール空 → 項目ごとに 1 通、pdf_paths は list でラップされる。"""
        gmail = MagicMock()
        items = self._items(("", "山田太郎", "/tmp/a.pdf"))
        main_module._create_grouped_drafts_for_run(items, gmail)
        gmail.create_draft.assert_called_once()
        call = gmail.create_draft.call_args
        self.assertEqual(call.kwargs["to_email"], "")
        # F: pdf_paths は list[Path] 型一貫性のためリストで渡す
        self.assertIsInstance(call.kwargs["pdf_paths"], list)
        self.assertEqual(call.kwargs["pdf_paths"], ["/tmp/a.pdf"])

    def test_three_persons_same_email_says_hoka_2_mei(self):
        gmail = MagicMock()
        items = self._items(
            ("g@example.com", "山田太郎", "/tmp/a.pdf"),
            ("g@example.com", "鈴木花子", "/tmp/b.pdf"),
            ("g@example.com", "佐藤次郎", "/tmp/c.pdf"),
        )
        with self.assertLogs("jissen_comment", level="WARNING"):
            main_module._create_grouped_drafts_for_run(items, gmail)
        call = gmail.create_draft.call_args
        self.assertIn("ほか2名", call.kwargs["person_name"])


if __name__ == "__main__":
    unittest.main()
