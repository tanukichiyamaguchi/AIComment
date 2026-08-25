"""src/run_common.py: 共通後段処理の単体テスト（Phase 22 追加分）。

対象:
    - ``_chunk_paths_by_size`` / ``_create_draft_with_size_guard``:
      Gmail 25MB/メッセージ上限の添付分割
    - ``resolve_case_via_master``: 添付資料のメイン非依存ルーティング
"""

from __future__ import annotations

import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from src import run_common
from src.sheets_client import MasterRecord

_logger = logging.getLogger("jissen_comment")


def _write_file(directory: Path, name: str, size: int) -> Path:
    path = directory / name
    path.write_bytes(b"x" * size)
    return path


class TestChunkPathsBySize(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_all_fit_single_chunk(self):
        paths = [_write_file(self.dir, f"{i}.pdf", 100) for i in range(3)]
        chunks, oversized = run_common._chunk_paths_by_size(paths, 1000, _logger)
        self.assertEqual(chunks, [paths])
        self.assertEqual(oversized, [])

    def test_splits_when_total_exceeds_limit(self):
        paths = [_write_file(self.dir, f"{i}.pdf", 400) for i in range(3)]
        chunks, oversized = run_common._chunk_paths_by_size(paths, 1000, _logger)
        # 400+400 = 800 ≤ 1000、+400 で超過 → [2, 1]
        self.assertEqual([len(c) for c in chunks], [2, 1])
        self.assertEqual(oversized, [])
        # 順序保存
        self.assertEqual([p for c in chunks for p in c], paths)

    def test_single_oversized_file_excluded(self):
        big = _write_file(self.dir, "big.pdf", 2000)
        small = _write_file(self.dir, "small.pdf", 100)
        chunks, oversized = run_common._chunk_paths_by_size(
            [big, small], 1000, _logger,
        )
        self.assertEqual(chunks, [[small]])
        self.assertEqual(oversized, [big])

    def test_missing_file_counted_as_zero(self):
        """stat 不能ファイル（テストのダミーパス等）はサイズ 0 として含める。"""
        missing = self.dir / "no_such.pdf"
        chunks, oversized = run_common._chunk_paths_by_size(
            [missing], 1000, _logger,
        )
        self.assertEqual(chunks, [[missing]])
        self.assertEqual(oversized, [])


class TestCreateDraftWithSizeGuard(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_within_limit_creates_single_draft(self):
        gmail = MagicMock()
        paths = [_write_file(self.dir, "a.pdf", 100)]
        run_common._create_draft_with_size_guard(
            gmail, to_email="a@example.com", person_name="田中太郎",
            pdf_paths=paths, logger=_logger,
        )
        gmail.create_draft.assert_called_once()
        self.assertEqual(gmail.create_draft.call_args.kwargs["pdf_paths"], paths)

    def test_over_limit_splits_into_multiple_drafts(self):
        gmail = MagicMock()
        paths = [_write_file(self.dir, f"{i}.pdf", 400) for i in range(3)]
        with patch.object(
            run_common, "_GMAIL_ATTACH_TOTAL_LIMIT_BYTES", 1000,
        ):
            with self.assertLogs("jissen_comment", level="WARNING") as log_ctx:
                run_common._create_draft_with_size_guard(
                    gmail, to_email="a@example.com", person_name="田中太郎",
                    pdf_paths=paths, logger=_logger,
                )
        self.assertEqual(gmail.create_draft.call_count, 2)
        self.assertIn("分割", "\n".join(log_ctx.output))

    def test_oversized_single_file_dropped_with_error_log(self):
        """単独で上限超過の PDF は添付から除外され、他は 1 通で作成される。"""
        gmail = MagicMock()
        big = _write_file(self.dir, "big.pdf", 5000)
        small = _write_file(self.dir, "small.pdf", 100)
        with patch.object(
            run_common, "_GMAIL_ATTACH_TOTAL_LIMIT_BYTES", 1000,
        ):
            with self.assertLogs("jissen_comment", level="ERROR") as log_ctx:
                run_common._create_draft_with_size_guard(
                    gmail, to_email="a@example.com", person_name="田中太郎",
                    pdf_paths=[big, small], logger=_logger,
                )
        gmail.create_draft.assert_called_once()
        self.assertEqual(
            gmail.create_draft.call_args.kwargs["pdf_paths"], [small],
        )
        self.assertIn("big.pdf", "\n".join(log_ctx.output))

    def test_all_oversized_creates_no_draft_but_logs_error(self):
        gmail = MagicMock()
        big = _write_file(self.dir, "big.pdf", 5000)
        with patch.object(
            run_common, "_GMAIL_ATTACH_TOTAL_LIMIT_BYTES", 1000,
        ):
            with self.assertLogs("jissen_comment", level="ERROR"):
                run_common._create_draft_with_size_guard(
                    gmail, to_email="a@example.com", person_name="田中太郎",
                    pdf_paths=[big], logger=_logger,
                )
        gmail.create_draft.assert_not_called()


class TestResolveCaseViaMaster(unittest.TestCase):
    """添付資料のメイン非依存ルーティング（恒久ロスト防止）。"""

    def _sheets_with_record(self, record: MasterRecord | None) -> MagicMock:
        sheets = MagicMock()
        sheets.lookup_participant_by_management_number.return_value = record
        return sheets

    def test_resolves_from_master(self):
        record = MasterRecord(
            management_number="001-01", clinic_name="山田歯科",
            participant_name="田中太郎", venue="東京", email="a@example.com",
        )
        sheets = self._sheets_with_record(record)
        case = run_common.resolve_case_via_master(
            sheets, [record], "001-01-0", _logger,
        )
        self.assertEqual(case, ("001", "山田歯科", "田中太郎"))

    def test_none_when_master_missing(self):
        sheets = self._sheets_with_record(None)
        self.assertIsNone(
            run_common.resolve_case_via_master(sheets, [], "999-99-9", _logger)
        )

    def test_none_when_clinic_name_empty(self):
        record = MasterRecord(
            management_number="001-01", clinic_name="",
            participant_name="田中太郎", venue="", email="",
        )
        sheets = self._sheets_with_record(record)
        self.assertIsNone(
            run_common.resolve_case_via_master(sheets, [record], "001-01-0", _logger)
        )

    def test_fallback_person_name_when_empty(self):
        record = MasterRecord(
            management_number="001-01", clinic_name="山田歯科",
            participant_name="", venue="", email="",
        )
        sheets = self._sheets_with_record(record)
        case = run_common.resolve_case_via_master(
            sheets, [record], "001-01-0", _logger,
        )
        self.assertEqual(case, ("001", "山田歯科", "unknown_person"))


class TestDistributeTeamCopies(unittest.TestCase):
    """``distribute_team_copies``: チーム全員のフォルダへの配布（Phase 24）。"""

    def _rec(self, mgmt, clinic="山田歯科", person="田中太郎", team="A班"):
        return MasterRecord(
            management_number=mgmt, clinic_name=clinic,
            participant_name=person, venue="", email="", team=team,
        )

    def _sheets_with(self, records, reporter):
        sheets = MagicMock()
        sheets.lookup_participant_by_management_number.return_value = reporter
        sheets.find_team_members.side_effect = (
            lambda recs, team: [r for r in records if r.team == team]
        )
        return sheets

    def test_distributes_to_all_members_except_reporter(self):
        reporter = self._rec("001-01", person="田中太郎")
        records = [
            reporter,
            self._rec("002-01", clinic="佐藤歯科", person="佐藤花子"),
            self._rec("003-01", clinic="鈴木歯科", person="鈴木一郎"),
        ]
        drive = MagicMock()
        sheets = self._sheets_with(records, reporter)

        drive.upload_pdf_to_clinic_person.return_value = {
            "webViewLink": "https://drive.example/x",
            "clinic_folder_id": "folder_x",
        }

        result = run_common.distribute_team_copies(
            drive, sheets, _logger,
            master_records=records,
            reporter_mgmt_num="001-01-0",
            file_path=Path("/tmp/out.pdf"),
            file_name="チーム実践_接遇.pdf",
            output_folder_id="root_x",
        )

        self.assertEqual(len(result), 2)
        self.assertEqual(
            {m["person_name"] for m in result}, {"佐藤花子", "鈴木一郎"},
        )
        self.assertTrue(
            all(m["drive_url"] == "https://drive.example/x" for m in result)
        )
        # 医院フォルダURLシート記録用に、メンバーの医院番号と配布先の
        # 医院フォルダ ID も返す（Phase 27）
        self.assertEqual(
            {m["clinic_number"] for m in result}, {"002", "003"},
        )
        self.assertTrue(
            all(m["clinic_folder_id"] == "folder_x" for m in result)
        )
        self.assertEqual(drive.upload_pdf_to_clinic_person.call_count, 2)
        kwargs_list = [
            c.kwargs for c in drive.upload_pdf_to_clinic_person.call_args_list
        ]
        self.assertEqual(
            {k["person_name"] for k in kwargs_list}, {"佐藤花子", "鈴木一郎"},
        )
        # マスター由来の確定名なのでフォルダ名同期を許可
        self.assertTrue(all(k["clinic_name_authoritative"] for k in kwargs_list))
        # 全員に同じファイル名で配布
        self.assertTrue(
            all(k["file_name"] == "チーム実践_接遇.pdf" for k in kwargs_list)
        )

    def test_reporter_not_in_master_falls_back_with_warning(self):
        drive = MagicMock()
        sheets = MagicMock()
        sheets.lookup_participant_by_management_number.return_value = None

        with self.assertLogs("jissen_comment", level="WARNING") as log_ctx:
            result = run_common.distribute_team_copies(
                drive, sheets, _logger,
                master_records=[],
                reporter_mgmt_num="999-99-9",
                file_path=Path("/tmp/out.pdf"),
                file_name="チームMTG_議事.pdf",
                output_folder_id="root_x",
            )

        self.assertEqual(result, [])
        drive.upload_pdf_to_clinic_person.assert_not_called()
        self.assertIn("配布先を解決できません", "\n".join(log_ctx.output))

    def test_reporter_with_empty_team_falls_back(self):
        reporter = self._rec("001-01", team="")
        drive = MagicMock()
        sheets = MagicMock()
        sheets.lookup_participant_by_management_number.return_value = reporter

        result = run_common.distribute_team_copies(
            drive, sheets, _logger,
            master_records=[reporter],
            reporter_mgmt_num="001-01-0",
            file_path=Path("/tmp/out.pdf"),
            file_name="チーム実践_x.pdf",
            output_folder_id="root_x",
        )

        self.assertEqual(result, [])
        drive.upload_pdf_to_clinic_person.assert_not_called()

    def test_member_upload_failure_propagates(self):
        """メンバーへの配布失敗は raise（fail-soft にしない、P-031 契約）。"""
        reporter = self._rec("001-01")
        records = [reporter, self._rec("002-01", person="佐藤花子")]
        drive = MagicMock()
        drive.upload_pdf_to_clinic_person.side_effect = OSError("drive down")
        sheets = self._sheets_with(records, reporter)

        with self.assertRaises(OSError):
            run_common.distribute_team_copies(
                drive, sheets, _logger,
                master_records=records,
                reporter_mgmt_num="001-01-0",
                file_path=Path("/tmp/out.pdf"),
                file_name="チーム実践_x.pdf",
                output_folder_id="root_x",
            )

    def test_member_dicts_carry_clinic_folder_fields(self):
        """戻り値の各メンバー辞書は医院フォルダURLシート記録に必要な
        ``clinic_number`` / ``clinic_folder_id`` を持つ（Phase 27 契約）。"""
        reporter = self._rec("001-01")
        records = [reporter, self._rec("002-01", clinic="佐藤歯科", person="佐藤花子")]
        drive = MagicMock()
        sheets = self._sheets_with(records, reporter)
        drive.upload_pdf_to_clinic_person.return_value = {
            "webViewLink": "https://drive.example/y",
            "clinic_folder_id": "member_clinic_folder",
        }

        result = run_common.distribute_team_copies(
            drive, sheets, _logger,
            master_records=records,
            reporter_mgmt_num="001-01-0",
            file_path=Path("/tmp/out.pdf"),
            file_name="チームMTG_y.pdf",
            output_folder_id="root_x",
        )

        self.assertEqual(
            result,
            [{
                "clinic_number": "002",
                "clinic_name": "佐藤歯科",
                "person_name": "佐藤花子",
                "drive_url": "https://drive.example/y",
                "clinic_folder_id": "member_clinic_folder",
            }],
        )

    def test_member_without_clinic_name_skipped_with_warning(self):
        reporter = self._rec("001-01")
        records = [reporter, self._rec("002-01", clinic="", person="医院名なし")]
        drive = MagicMock()
        sheets = self._sheets_with(records, reporter)

        with self.assertLogs("jissen_comment", level="WARNING") as log_ctx:
            result = run_common.distribute_team_copies(
                drive, sheets, _logger,
                master_records=records,
                reporter_mgmt_num="001-01-0",
                file_path=Path("/tmp/out.pdf"),
                file_name="チーム実践_x.pdf",
                output_folder_id="root_x",
            )

        self.assertEqual(result, [])
        self.assertIn("医院名が空", "\n".join(log_ctx.output))


class TestClinicFolderRecorder(unittest.TestCase):
    """``ClinicFolderRecorder``: 医院フォルダURLシート記録のデデュープ。

    チーム配布（Phase 27）で同じ医院が複数のチーム事例・複数メンバーから
    繰り返し record() されても、シートには 1 医院 1 行しか追記されないことを
    固定する。
    """

    def _recorder(self, recorded=None):
        sheets = MagicMock()
        sheets.get_recorded_clinic_numbers.return_value = recorded or set()
        return run_common.ClinicFolderRecorder(sheets, "出力一覧"), sheets

    def test_records_new_clinic_once(self):
        recorder, sheets = self._recorder()
        recorder.record("002", "佐藤歯科", "folder_002")
        kwargs = sheets.append_clinic_folder_record.call_args.kwargs
        self.assertEqual(kwargs["clinic_number"], "002")
        self.assertEqual(kwargs["clinic_name"], "佐藤歯科")
        self.assertEqual(
            kwargs["clinic_folder_url"],
            "https://drive.google.com/drive/folders/folder_002",
        )
        self.assertEqual(kwargs["sheet_name"], "出力一覧_医院")

    def test_same_clinic_recorded_only_once_in_run(self):
        """同一実行内で同じ医院を複数回 record → 追記は 1 回のみ。"""
        recorder, sheets = self._recorder()
        recorder.record("002", "佐藤歯科", "folder_002")
        recorder.record("002", "佐藤歯科", "folder_002")
        self.assertEqual(sheets.append_clinic_folder_record.call_count, 1)

    def test_clinic_recorded_in_prior_run_not_duplicated(self):
        """前回実行までに記録済みの医院は再記録しない。"""
        recorder, sheets = self._recorder(recorded={"002"})
        recorder.record("002", "佐藤歯科", "folder_002")
        sheets.append_clinic_folder_record.assert_not_called()

    def test_empty_clinic_number_not_recorded(self):
        """医院番号なし（管理番号抽出不能）は記録しない。"""
        recorder, sheets = self._recorder()
        recorder.record("", "医院名のみ", "folder_x")
        sheets.append_clinic_folder_record.assert_not_called()


if __name__ == "__main__":
    unittest.main()
