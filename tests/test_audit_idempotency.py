"""scripts.audit_idempotency のテスト。冪等性チェックロジック。"""

import unittest
from unittest.mock import patch

from scripts.audit_idempotency import (
    DONE_STATUS,
    RESET_STATUS,
    STUCK_PREFIX,
    _parse_subject_person,
    detect_anomalies,
    recover_stuck_rows,
)
from src.gmail_client import SUBJECT_TEMPLATE
from src.sheets_client import ClinicRecord


def _record(row: int, name: str, person: str, status: str = "") -> ClinicRecord:
    return ClinicRecord(
        row_number=row,
        clinic_name=name,
        person_name=person,
        email=f"{person}@example.com",
        status=status,
    )


class TestStuckRowRecovery(unittest.TestCase):

    def test_dry_run_does_not_call_update(self):
        records = [
            _record(2, "A歯科", "山田太郎", "処理中"),
            _record(3, "B歯科", "佐藤花子", "完了"),
        ]
        with patch("scripts.audit_idempotency.update_status") as mock:
            recovered = recover_stuck_rows(records, stuck_threshold_hours=2, dry_run=True)
        self.assertEqual(recovered, [2])
        mock.assert_not_called()

    def test_apply_calls_update_for_stuck_rows_only(self):
        records = [
            _record(2, "A歯科", "山田", "処理中"),
            _record(3, "B歯科", "佐藤", "処理中: ステップ4"),
            _record(4, "C歯科", "鈴木", "完了"),
            _record(5, "D歯科", "田中", "未処理"),
        ]
        with patch("scripts.audit_idempotency.update_status") as mock:
            recovered = recover_stuck_rows(records, stuck_threshold_hours=2, dry_run=False)
        self.assertEqual(recovered, [2, 3])
        self.assertEqual(mock.call_count, 2)
        # All resets must use the canonical reset status
        for call in mock.call_args_list:
            self.assertEqual(call.args[1], RESET_STATUS)

    def test_safety_floor_rejects_zero_threshold(self):
        records = [_record(2, "A歯科", "山田", "処理中")]
        with self.assertRaises(ValueError):
            recover_stuck_rows(records, stuck_threshold_hours=0, dry_run=True)


class TestSubjectParsing(unittest.TestCase):

    def test_parse_subject_extracts_person_name(self):
        subject = SUBJECT_TEMPLATE.format(person_name="白川蓮")
        self.assertEqual(_parse_subject_person(subject), "白川蓮")

    def test_parse_subject_returns_none_for_unrelated(self):
        self.assertIsNone(_parse_subject_person("Re: 別のメール"))


class TestAnomalyDetection(unittest.TestCase):

    def _draft(self, person: str, draft_id: str = "d1") -> dict:
        return {
            "id": draft_id,
            "subject": SUBJECT_TEMPLATE.format(person_name=person),
            "to": f"{person}@example.com",
            "date": "",
        }

    def test_completed_without_draft_is_anomaly(self):
        records = [_record(2, "A歯科", "山田", DONE_STATUS)]
        anomalies = detect_anomalies(records, drafts=[])
        types = [a["type"] for a in anomalies]
        self.assertIn("完了_without_draft", types)

    def test_draft_without_completion_is_anomaly(self):
        records = [_record(2, "A歯科", "山田", "未処理")]
        drafts = [self._draft("山田")]
        anomalies = detect_anomalies(records, drafts)
        types = [a["type"] for a in anomalies]
        self.assertIn("draft_without_完了", types)

    def test_duplicate_drafts_for_same_person_is_anomaly(self):
        records = [_record(2, "A歯科", "山田", DONE_STATUS)]
        drafts = [self._draft("山田", "d1"), self._draft("山田", "d2")]
        anomalies = detect_anomalies(records, drafts)
        types = [a["type"] for a in anomalies]
        self.assertIn("duplicate_drafts", types)

    def test_clean_state_yields_no_anomalies(self):
        records = [_record(2, "A歯科", "山田", DONE_STATUS)]
        drafts = [self._draft("山田")]
        anomalies = detect_anomalies(records, drafts)
        self.assertEqual(anomalies, [])


if __name__ == "__main__":
    unittest.main()
