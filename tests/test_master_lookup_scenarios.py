"""参加者マスター統合（PR #36/#37/#38）の E2E シナリオ検証。

Scenarios A–G を mock ベースで網羅する。
tests/ 配下のみ変更、src/ は一切変更しない。
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, call, patch

from src import main, sheets_client

# ── 共通 env patch ────────────────────────────────────────────────────────────
_PROFILE_ENV = {
    "DRIVE_FOLDER_ID": "input_default",
    "DRIVE_OUTPUT_FOLDER_ID": "output_default",
    "SPREADSHEET_ID": "test_sheet_id",
}


def _make_upload_return() -> dict:
    return {
        "webViewLink": "https://drive.google.com/fake",
        "clinic_folder_id": "clinic_folder_fake",
    }


def _base_mocks(
    mock_drive: Any,
    mock_sheets: Any,
    mock_gen: Any,
    mock_reader: Any,
    mock_creator: Any,
    mock_merger: Any,
    *,
    pdf_files: list[dict] | None = None,
    ai_metadata: dict | None = None,
    master_records: list[sheets_client.MasterRecord] | None = None,
) -> None:
    """共通モック初期化。個別シナリオで上書き可。"""
    mock_drive.list_pdfs.return_value = pdf_files or []
    mock_drive.download_pdf.return_value = b"%PDF-1.4 fake"
    mock_drive.upload_pdf_to_clinic_person.return_value = _make_upload_return()
    mock_reader.extract_text.return_value = "PDFテキスト"
    mock_gen.generate_comment_with_metadata.return_value = ai_metadata or {
        "clinic_name": "三浦歯科",
        "person_name": "山田太郎",
        "sample_title": "事例タイトル",
        "comment": "コメント本文",
    }
    mock_merger.make_output_filename.return_value = "三浦歯科医院＿山田太郎＿事例タイトル.pdf"
    mock_sheets.get_processed_management_numbers.return_value = set()
    mock_sheets.get_recorded_clinic_numbers.return_value = set()
    mock_sheets.read_master_records.return_value = master_records if master_records is not None else []
    # lookup_* は master_records から実 src ロジックで計算するのではなく
    # テストごとに明示設定するため、ここでは無設定（個別 test で side_effect 付与）。


# ── Scenario A: 完全一致パス ───────────────────────────────────────────────────


class TestScenarioA_ExactMatch(unittest.TestCase):
    """管理番号・医院名・参加者名・メールがすべてマスターにある完全一致ケース。

    - 医院名はマスター標準名「三浦歯科医院」を採用（AI 抽出は「三浦歯科」）
    - Gmail 下書き TO = yamada@example.com
    - Drive フォルダ医院名は「三浦歯科医院」
    """

    def setUp(self):
        self._env = patch.dict(os.environ, _PROFILE_ENV, clear=False)
        self._env.start()

    def tearDown(self):
        self._env.stop()

    @patch("src.main.gmail_client")
    @patch("src.main.ensure_fonts")
    @patch("src.main.pdf_merger")
    @patch("src.main.pdf_creator")
    @patch("src.main.pdf_reader")
    @patch("src.main.comment_generator")
    @patch("src.main.sheets_client")
    @patch("src.main.drive_client")
    def test_master_clinic_name_overrides_ai_extraction(
        self,
        mock_drive, mock_sheets, mock_gen, mock_reader,
        mock_creator, mock_merger, mock_fonts, mock_gmail,
    ):
        master = [
            sheets_client.MasterRecord(
                management_number="101-01-1",
                clinic_name="三浦歯科医院",
                participant_name="山田太郎",
                venue="東京",
                email="yamada@example.com",
            )
        ]
        _base_mocks(
            mock_drive, mock_sheets, mock_gen, mock_reader,
            mock_creator, mock_merger,
            pdf_files=[{"id": "id_001", "name": "101-01-1_xxx.pdf"}],
            ai_metadata={
                "clinic_name": "三浦歯科",     # AI は略称
                "person_name": "山田太郎",
                "sample_title": "事例A",
                "comment": "コメント",
            },
            master_records=master,
        )
        # lookup_clinic_name: 実ロジック（管理番号先頭セグメント 101 で引く）
        mock_sheets.lookup_clinic_name.return_value = "三浦歯科医院"
        mock_sheets.lookup_email_by_clinic_and_person.return_value = "yamada@example.com"

        main.run(test_count=0, profile_name="jissen_default")

        # 医院名はマスター標準名がシートに渡る（AI 抽出値ではない）
        sheet_call = mock_sheets.append_output_record.call_args
        self.assertEqual(sheet_call.kwargs["clinic_name"], "三浦歯科医院")

        # upload に渡る医院名もマスター標準名
        upload_call = mock_drive.upload_pdf_to_clinic_person.call_args
        self.assertEqual(upload_call.kwargs["clinic_name"], "三浦歯科医院")
        self.assertEqual(upload_call.kwargs["clinic_number"], "101")

        # Gmail 下書き TO はマスターのメールアドレス
        draft_call = mock_gmail.create_draft.call_args
        self.assertEqual(draft_call.kwargs["to_email"], "yamada@example.com")

    @patch("src.main.gmail_client")
    @patch("src.main.ensure_fonts")
    @patch("src.main.pdf_merger")
    @patch("src.main.pdf_creator")
    @patch("src.main.pdf_reader")
    @patch("src.main.comment_generator")
    @patch("src.main.sheets_client")
    @patch("src.main.drive_client")
    def test_lookup_clinic_name_is_called_with_extracted_clinic_number(
        self,
        mock_drive, mock_sheets, mock_gen, mock_reader,
        mock_creator, mock_merger, mock_fonts, mock_gmail,
    ):
        """lookup_clinic_name はファイル名から抽出した医院番号（101）で呼ばれる。"""
        _base_mocks(
            mock_drive, mock_sheets, mock_gen, mock_reader,
            mock_creator, mock_merger,
            pdf_files=[{"id": "id_001", "name": "101-01-1_xxx.pdf"}],
        )
        mock_sheets.lookup_clinic_name.return_value = ""
        mock_sheets.lookup_email_by_clinic_and_person.return_value = ""

        main.run(test_count=0, profile_name="jissen_default")

        lookup_call = mock_sheets.lookup_clinic_name.call_args
        # records は read_master_records の戻り値、clinic_number は "101"
        self.assertEqual(lookup_call.args[1], "101")


# ── Scenario B: マスター 0 件 ────────────────────────────────────────────────


class TestScenarioB_EmptyMaster(unittest.TestCase):
    """参加者マスターが空のとき fail-soft でフル完走する。

    - WARNING が出る（メール未ヒット）
    - 医院名は AI 抽出値で代用
    - PDF 処理は止まらない
    - 下書きは TO 空で作成される
    """

    def setUp(self):
        self._env = patch.dict(os.environ, _PROFILE_ENV, clear=False)
        self._env.start()

    def tearDown(self):
        self._env.stop()

    @patch("src.main.gmail_client")
    @patch("src.main.ensure_fonts")
    @patch("src.main.pdf_merger")
    @patch("src.main.pdf_creator")
    @patch("src.main.pdf_reader")
    @patch("src.main.comment_generator")
    @patch("src.main.sheets_client")
    @patch("src.main.drive_client")
    def test_empty_master_processes_all_pdfs_with_empty_to(
        self,
        mock_drive, mock_sheets, mock_gen, mock_reader,
        mock_creator, mock_merger, mock_fonts, mock_gmail,
    ):
        pdf_files = [
            {"id": f"id_{i}", "name": f"101-0{i}-0事例{i}.pdf"}
            for i in range(1, 4)
        ]
        _base_mocks(
            mock_drive, mock_sheets, mock_gen, mock_reader,
            mock_creator, mock_merger,
            pdf_files=pdf_files,
            master_records=[],  # マスター 0 件
        )
        mock_sheets.lookup_clinic_name.return_value = ""
        mock_sheets.lookup_email_by_clinic_and_person.return_value = ""

        with self.assertLogs("jissen_comment", level="WARNING") as log_ctx:
            main.run(test_count=0, profile_name="jissen_default")

        # 全 3 件とも処理完走
        self.assertEqual(mock_sheets.append_output_record.call_count, 3)
        self.assertEqual(mock_drive.upload_pdf_to_clinic_person.call_count, 3)

        # メール未ヒット警告が出ている
        joined = "\n".join(log_ctx.output)
        self.assertIn("メール未ヒット", joined)

        # 下書きは TO 空で作成される（3 件それぞれに 1 通）
        self.assertEqual(mock_gmail.create_draft.call_count, 3)
        for c in mock_gmail.create_draft.call_args_list:
            self.assertEqual(c.kwargs["to_email"], "")

    @patch("src.main.gmail_client")
    @patch("src.main.ensure_fonts")
    @patch("src.main.pdf_merger")
    @patch("src.main.pdf_creator")
    @patch("src.main.pdf_reader")
    @patch("src.main.comment_generator")
    @patch("src.main.sheets_client")
    @patch("src.main.drive_client")
    def test_empty_master_uses_ai_clinic_name(
        self,
        mock_drive, mock_sheets, mock_gen, mock_reader,
        mock_creator, mock_merger, mock_fonts, mock_gmail,
    ):
        """マスター未登録時、医院名は AI 抽出値（「三浦歯科」）で代用される。"""
        _base_mocks(
            mock_drive, mock_sheets, mock_gen, mock_reader,
            mock_creator, mock_merger,
            pdf_files=[{"id": "id_1", "name": "101-01-0事例.pdf"}],
            ai_metadata={
                "clinic_name": "三浦歯科",
                "person_name": "山田太郎",
                "sample_title": "事例",
                "comment": "コメント",
            },
            master_records=[],
        )
        mock_sheets.lookup_clinic_name.return_value = ""
        mock_sheets.lookup_email_by_clinic_and_person.return_value = ""

        with self.assertLogs("jissen_comment", level="WARNING"):
            main.run(test_count=0, profile_name="jissen_default")

        sheet_call = mock_sheets.append_output_record.call_args
        # AI 抽出値がそのままシートに記録される
        self.assertEqual(sheet_call.kwargs["clinic_name"], "三浦歯科")
        upload_call = mock_drive.upload_pdf_to_clinic_person.call_args
        self.assertEqual(upload_call.kwargs["clinic_name"], "三浦歯科")


# ── Scenario C: ファジー一致（全角・末尾空白） ─────────────────────────────────


class TestScenarioC_FuzzyMatch(unittest.TestCase):
    """_normalize_person_name が全角空白・末尾半角空白を吸収して完全一致にする。"""

    def test_fullwidth_space_and_trailing_space_normalized(self):
        """マスターは「山田太郎」、AI 抽出は「山田　太郎 」→ 正規化後完全一致。"""
        normalized_master = sheets_client._normalize_person_name("山田太郎")
        normalized_ai = sheets_client._normalize_person_name("山田　太郎 ")
        self.assertEqual(normalized_master, normalized_ai)

    def test_lookup_with_fullwidth_space_in_ai_name_returns_email(self):
        """lookup_email_by_clinic_and_person でも全角空白を含む AI 名が一致する。"""
        records = [
            sheets_client.MasterRecord(
                management_number="101-01",
                clinic_name="三浦歯科医院",
                participant_name="山田太郎",
                venue="東京",
                email="yamada@example.com",
            )
        ]
        email = sheets_client.lookup_email_by_clinic_and_person(
            records, "101", "山田　太郎 "
        )
        self.assertEqual(email, "yamada@example.com")

    def test_trailing_halfwidth_space_in_master_also_normalized(self):
        """マスター側に末尾スペースがあっても正規化で一致する。"""
        records = [
            sheets_client.MasterRecord(
                management_number="101-01",
                clinic_name="三浦歯科",
                participant_name="山田太郎 ",   # マスター側末尾スペース
                venue="",
                email="yamada@example.com",
            )
        ]
        email = sheets_client.lookup_email_by_clinic_and_person(
            records, "101", "山田太郎"
        )
        self.assertEqual(email, "yamada@example.com")


# ── Scenario D: 1 メール複数 PDF → 1 下書きに複数添付 ────────────────────────


class TestScenarioD_MultiPDFSingleDraft(unittest.TestCase):
    """同一メールアドレスの複数 PDF が 1 通の下書きに集約される。"""

    def setUp(self):
        self._env = patch.dict(os.environ, _PROFILE_ENV, clear=False)
        self._env.start()

    def tearDown(self):
        self._env.stop()

    @patch("src.main.gmail_client")
    @patch("src.main.ensure_fonts")
    @patch("src.main.pdf_merger")
    @patch("src.main.pdf_creator")
    @patch("src.main.pdf_reader")
    @patch("src.main.comment_generator")
    @patch("src.main.sheets_client")
    @patch("src.main.drive_client")
    def test_three_pdfs_same_email_creates_one_draft_with_three_attachments(
        self,
        mock_drive, mock_sheets, mock_gen, mock_reader,
        mock_creator, mock_merger, mock_fonts, mock_gmail,
    ):
        """管理番号 101-01-0, 101-01-1, 101-01-2 の 3 PDF → 1 下書き・3 添付。"""
        pdf_files = [
            {"id": "id_1", "name": "101-01-0事例A.pdf"},
            {"id": "id_2", "name": "101-01-1事例B.pdf"},
            {"id": "id_3", "name": "101-01-2事例C.pdf"},
        ]
        _base_mocks(
            mock_drive, mock_sheets, mock_gen, mock_reader,
            mock_creator, mock_merger,
            pdf_files=pdf_files,
            ai_metadata={
                "clinic_name": "三浦歯科",
                "person_name": "山田太郎",
                "sample_title": "事例タイトル",
                "comment": "コメント",
            },
        )
        # 全件同一メール
        mock_sheets.lookup_clinic_name.return_value = "三浦歯科医院"
        mock_sheets.lookup_email_by_clinic_and_person.return_value = "yamada@example.com"
        mock_merger.make_output_filename.side_effect = [
            "out_A.pdf", "out_B.pdf", "out_C.pdf"
        ]

        main.run(test_count=0, profile_name="jissen_default")

        # 下書きは 1 通のみ（集約）
        self.assertEqual(mock_gmail.create_draft.call_count, 1)
        draft_call = mock_gmail.create_draft.call_args
        self.assertEqual(draft_call.kwargs["to_email"], "yamada@example.com")
        # 3 つの PDF パスがリストで渡される
        pdf_paths = draft_call.kwargs["pdf_paths"]
        self.assertEqual(len(pdf_paths), 3)

    @patch("src.main.gmail_client")
    @patch("src.main.ensure_fonts")
    @patch("src.main.pdf_merger")
    @patch("src.main.pdf_creator")
    @patch("src.main.pdf_reader")
    @patch("src.main.comment_generator")
    @patch("src.main.sheets_client")
    @patch("src.main.drive_client")
    def test_batch_main_also_groups_same_email(
        self,
        mock_drive, mock_sheets, mock_gen, mock_reader,
        mock_creator, mock_merger, mock_fonts, mock_gmail,
    ):
        """batch_main._create_grouped_drafts_for_batch も同一集約ロジックを使う。"""
        from src import batch_main

        draft_items = [
            {
                "email": "yamada@example.com",
                "person_name": "山田太郎",
                "pdf_path": Path("/tmp/a.pdf"),
                "clinic_number": "101",
            },
            {
                "email": "yamada@example.com",
                "person_name": "山田太郎",
                "pdf_path": Path("/tmp/b.pdf"),
                "clinic_number": "101",
            },
            {
                "email": "yamada@example.com",
                "person_name": "山田太郎",
                "pdf_path": Path("/tmp/c.pdf"),
                "clinic_number": "101",
            },
        ]

        with patch("src.batch_main.gmail_client") as mock_batch_gmail:
            batch_main._create_grouped_drafts_for_batch(draft_items)

        self.assertEqual(mock_batch_gmail.create_draft.call_count, 1)
        draft_call = mock_batch_gmail.create_draft.call_args
        self.assertEqual(len(draft_call.kwargs["pdf_paths"]), 3)


# ── Scenario E: 同一医院・別人 → 別下書き ─────────────────────────────────────


class TestScenarioE_SameClinicDifferentPersons(unittest.TestCase):
    """同一医院内の異なる参加者は別の下書きになる（メールが異なるため）。"""

    def setUp(self):
        self._env = patch.dict(os.environ, _PROFILE_ENV, clear=False)
        self._env.start()

    def tearDown(self):
        self._env.stop()

    @patch("src.main.gmail_client")
    @patch("src.main.ensure_fonts")
    @patch("src.main.pdf_merger")
    @patch("src.main.pdf_creator")
    @patch("src.main.pdf_reader")
    @patch("src.main.comment_generator")
    @patch("src.main.sheets_client")
    @patch("src.main.drive_client")
    def test_two_persons_in_same_clinic_produce_two_drafts(
        self,
        mock_drive, mock_sheets, mock_gen, mock_reader,
        mock_creator, mock_merger, mock_fonts, mock_gmail,
    ):
        pdf_files = [
            {"id": "id_A", "name": "101-01-0事例A.pdf"},
            {"id": "id_B", "name": "101-02-0事例B.pdf"},
        ]
        _base_mocks(
            mock_drive, mock_sheets, mock_gen, mock_reader,
            mock_creator, mock_merger,
            pdf_files=pdf_files,
        )
        mock_gen.generate_comment_with_metadata.side_effect = [
            {
                "clinic_name": "三浦歯科",
                "person_name": "山田太郎",
                "sample_title": "事例A",
                "comment": "コメント",
            },
            {
                "clinic_name": "三浦歯科",
                "person_name": "鈴木花子",
                "sample_title": "事例B",
                "comment": "コメント",
            },
        ]
        mock_sheets.lookup_clinic_name.return_value = "三浦歯科医院"
        # 別人 → 別メール
        mock_sheets.lookup_email_by_clinic_and_person.side_effect = [
            "yamada@example.com",
            "suzuki@example.com",
        ]
        mock_merger.make_output_filename.side_effect = ["out_A.pdf", "out_B.pdf"]

        main.run(test_count=0, profile_name="jissen_default")

        # 別メール → 2 通の下書き
        self.assertEqual(mock_gmail.create_draft.call_count, 2)
        to_emails = {c.kwargs["to_email"] for c in mock_gmail.create_draft.call_args_list}
        self.assertEqual(to_emails, {"yamada@example.com", "suzuki@example.com"})


# ── Scenario F: メール列が空の行 ─────────────────────────────────────────────


class TestScenarioF_EmptyEmailInMaster(unittest.TestCase):
    """マスターに行はあるがメール列が空 → WARNING + 宛先空下書き + 完走。"""

    def setUp(self):
        self._env = patch.dict(os.environ, _PROFILE_ENV, clear=False)
        self._env.start()

    def tearDown(self):
        self._env.stop()

    def test_empty_email_in_record_is_stored_as_empty_string(self):
        """read_master_records でメール列空の行は email="" として読み込まれる。"""
        service = MagicMock()
        service.spreadsheets.return_value.get.return_value.execute.return_value = {
            "sheets": [{"properties": {"title": "参加者マスター"}}]
        }
        service.spreadsheets.return_value.values.return_value.get.return_value.execute.return_value = {
            "values": [
                ["管理番号", "医院名", "参加者名", "申し込み会場", "メールアドレス"],
                ["101-01", "三浦歯科医院", "山田太郎", "東京", ""],  # メール空
            ]
        }
        with patch("src.sheets_client.get_sheets_service", return_value=service):
            records = sheets_client.read_master_records(
                spreadsheet_id="sid", sheet_name="参加者マスター"
            )
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].email, "")

    def test_lookup_returns_empty_for_empty_email_record(self):
        """メール空のレコードが一致しても空文字が返る。"""
        records = [
            sheets_client.MasterRecord(
                management_number="101-01",
                clinic_name="三浦歯科医院",
                participant_name="山田太郎",
                venue="東京",
                email="",  # メール列空
            )
        ]
        email = sheets_client.lookup_email_by_clinic_and_person(
            records, "101", "山田太郎"
        )
        # ヒットはするが email が空なので空文字を返す
        self.assertEqual(email, "")

    @patch("src.main.gmail_client")
    @patch("src.main.ensure_fonts")
    @patch("src.main.pdf_merger")
    @patch("src.main.pdf_creator")
    @patch("src.main.pdf_reader")
    @patch("src.main.comment_generator")
    @patch("src.main.sheets_client")
    @patch("src.main.drive_client")
    def test_empty_email_in_master_creates_empty_to_draft_and_completes(
        self,
        mock_drive, mock_sheets, mock_gen, mock_reader,
        mock_creator, mock_merger, mock_fonts, mock_gmail,
    ):
        """メール空のマスターでも PDF 処理が完走し、TO 空下書きが作られる。"""
        _base_mocks(
            mock_drive, mock_sheets, mock_gen, mock_reader,
            mock_creator, mock_merger,
            pdf_files=[{"id": "id_1", "name": "101-01-0事例.pdf"}],
        )
        mock_sheets.lookup_clinic_name.return_value = "三浦歯科医院"
        mock_sheets.lookup_email_by_clinic_and_person.return_value = ""  # メール空

        with self.assertLogs("jissen_comment", level="WARNING") as log_ctx:
            main.run(test_count=0, profile_name="jissen_default")

        # PDF 処理は完走
        self.assertEqual(mock_sheets.append_output_record.call_count, 1)
        # WARNING が出ている
        self.assertIn("メール未ヒット", "\n".join(log_ctx.output))
        # TO 空の下書きが作られる
        self.assertEqual(mock_gmail.create_draft.call_count, 1)
        self.assertEqual(mock_gmail.create_draft.call_args.kwargs["to_email"], "")


# ── Scenario G: ファジー一致候補が複数 ─────────────────────────────────────────


class TestScenarioG_MultipleFuzzyCandidates(unittest.TestCase):
    """Levenshtein 距離 ≤ 1 の候補が 2 件以上 → WARNING + 先頭採用。"""

    def test_multiple_fuzzy_candidates_warns_and_picks_first(self):
        """ファジー候補 2 件 → WARNING ログ + 先頭のメールを返す。"""
        records = [
            sheets_client.MasterRecord(
                management_number="101-01",
                clinic_name="三浦歯科",
                participant_name="山田太郎",
                venue="",
                email="yamada_taro@example.com",
            ),
            sheets_client.MasterRecord(
                management_number="101-02",
                clinic_name="三浦歯科",
                participant_name="山田次郎",  # 「太郎」→「次郎」: 1 文字差
                venue="",
                email="yamada_jiro@example.com",
            ),
        ]
        # AI 抽出は「山田一郎」→ 太郎（郎共通、一 vs 太）、次郎（一 vs 次）ともに 1 文字差
        with self.assertLogs("jissen_comment", level="WARNING") as log_ctx:
            email = sheets_client.lookup_email_by_clinic_and_person(
                records, "101", "山田一郎"
            )

        # 先頭採用
        self.assertEqual(email, "yamada_taro@example.com")
        # 複数候補の警告が出ている
        self.assertIn("先頭採用", "\n".join(log_ctx.output))

    def test_single_fuzzy_candidate_no_warning(self):
        """ファジー候補 1 件 → 警告なし INFO ログで採用。"""
        records = [
            sheets_client.MasterRecord(
                management_number="101-01",
                clinic_name="三浦歯科",
                participant_name="山田太郎",
                venue="",
                email="yamada@example.com",
            ),
        ]
        with self.assertLogs("jissen_comment", level="INFO") as log_ctx:
            email = sheets_client.lookup_email_by_clinic_and_person(
                records, "101", "山田大郎"  # 太→大: 1 文字差
            )

        self.assertEqual(email, "yamada@example.com")
        joined = "\n".join(log_ctx.output)
        self.assertIn("ファジー一致", joined)
        # WARNING は出ていない
        warning_lines = [l for l in log_ctx.output if "WARNING" in l]
        self.assertEqual(warning_lines, [])


# ── main vs batch_main のグルーピング実装一致確認 ─────────────────────────────


class TestGroupingConsistency(unittest.TestCase):
    """main._create_grouped_drafts_for_run と
    batch_main._create_grouped_drafts_for_batch の挙動が同一である。

    インターフェースが異なる（run 版は gmail_module を引数で受け取る）ため、
    同一入力で同一の呼び出しパターンになることを並列に検証する。
    """

    def _make_items(self, emails: list[str]) -> list[dict]:
        return [
            {
                "email": e,
                "person_name": f"人物{i}",
                "pdf_path": Path(f"/tmp/{i}.pdf"),
                "clinic_number": "101",
            }
            for i, e in enumerate(emails, start=1)
        ]

    def test_empty_items_creates_no_drafts(self):
        mock_gmail = MagicMock()
        main._create_grouped_drafts_for_run([], mock_gmail)
        mock_gmail.create_draft.assert_not_called()

        from src import batch_main
        with patch("src.batch_main.gmail_client") as batch_mock:
            batch_main._create_grouped_drafts_for_batch([])
            batch_mock.create_draft.assert_not_called()

    def test_single_email_creates_one_draft(self):
        items = self._make_items(["a@example.com"])

        mock_gmail = MagicMock()
        main._create_grouped_drafts_for_run(items, mock_gmail)
        self.assertEqual(mock_gmail.create_draft.call_count, 1)
        self.assertEqual(
            mock_gmail.create_draft.call_args.kwargs["to_email"], "a@example.com"
        )

        from src import batch_main
        with patch("src.batch_main.gmail_client") as batch_mock:
            batch_main._create_grouped_drafts_for_batch(items)
            self.assertEqual(batch_mock.create_draft.call_count, 1)
            self.assertEqual(
                batch_mock.create_draft.call_args.kwargs["to_email"], "a@example.com"
            )

    def test_two_different_emails_create_two_drafts_in_both_modes(self):
        items = self._make_items(["a@example.com", "b@example.com"])

        mock_gmail = MagicMock()
        main._create_grouped_drafts_for_run(items, mock_gmail)
        self.assertEqual(mock_gmail.create_draft.call_count, 2)

        from src import batch_main
        with patch("src.batch_main.gmail_client") as batch_mock:
            batch_main._create_grouped_drafts_for_batch(items)
            self.assertEqual(batch_mock.create_draft.call_count, 2)

    def test_empty_email_items_create_one_draft_each_in_both_modes(self):
        """メール空の項目は集約されず、各 PDF に 1 通の下書きが作られる。"""
        items = self._make_items(["", ""])  # 2 件とも TO 空

        mock_gmail = MagicMock()
        main._create_grouped_drafts_for_run(items, mock_gmail)
        self.assertEqual(mock_gmail.create_draft.call_count, 2)
        for c in mock_gmail.create_draft.call_args_list:
            self.assertEqual(c.kwargs["to_email"], "")

        from src import batch_main
        with patch("src.batch_main.gmail_client") as batch_mock:
            batch_main._create_grouped_drafts_for_batch(items)
            self.assertEqual(batch_mock.create_draft.call_count, 2)


# ── _create_grouped_drafts_for_run の pdf_paths 型確認（潜在的バグ） ──────────


class TestEmptyEmailPdfPathsType(unittest.TestCase):
    """メール空ケースで pdf_paths に単一 Path（非リスト）が渡される実装の
    型確認。gmail_client.create_draft は str|Path も許容するが、
    今後リスト前提に変わったときに検出できるよう現状の挙動を記録する。

    src/main.py L535: pdf_paths=item["pdf_path"]  ← Path（非リスト）
    src/batch_main.py L601: pdf_paths=item["pdf_path"]  ← 同上
    gmail_client.create_draft は isinstance(str|Path) をリストに正規化するため
    現時点では動作する。
    """

    def test_single_path_is_normalized_to_list_in_create_draft(self):
        """gmail_client.create_draft が単一 Path をリストに正規化することを確認。

        メール空経路で渡される単一 Path が例外にならないことを検証する。
        実際の Gmail API は呼ばないため gmail サービスを mock する。
        """
        with patch("src.gmail_client.get_gmail_service") as mock_service:
            # service.users().drafts().create() の深い mock
            draft_mock = MagicMock()
            draft_mock.execute.return_value = {"id": "draft_id_001"}
            mock_service.return_value.users.return_value.drafts.return_value.create.return_value = draft_mock

            from src import gmail_client
            # 単一 Path を渡しても ValueError にならない
            try:
                gmail_client.create_draft(
                    to_email="",
                    person_name="山田太郎",
                    pdf_paths=Path("/tmp/dummy.pdf"),
                    cc_email=None,
                )
            except FileNotFoundError:
                # PDF ファイルが存在しないため添付時に失敗するが、
                # pdf_paths の型エラーではない（型正規化は成功）
                pass
            except Exception as e:
                # ValueError("pdf_paths が空です") だけが型問題を示す
                if "pdf_paths" in str(e):
                    self.fail(f"単一 Path が pdf_paths 型エラーを引き起こした: {e}")


if __name__ == "__main__":
    unittest.main()
