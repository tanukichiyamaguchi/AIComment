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


if __name__ == "__main__":
    unittest.main()
