"""matcher モジュールのテスト。"""

import unittest

from src.matcher import extract_clinic_info, match_record, _normalize
from src.sheets_client import ClinicRecord


class TestNormalize(unittest.TestCase):

    def test_fullwidth_to_halfwidth(self):
        self.assertEqual(_normalize("ＡＢＣ"), _normalize("ABC"))

    def test_remove_spaces(self):
        self.assertEqual(_normalize("三浦 歯科 医院"), _normalize("三浦歯科医院"))


class TestExtractClinicInfo(unittest.TestCase):

    def test_extract_clinic_name(self):
        text = "医院名：三浦歯科医院\n氏名：白川蓮\nその他のテキスト"
        info = extract_clinic_info(text)
        self.assertIn("三浦歯科医院", info["clinic_name"])

    def test_extract_person_name(self):
        text = "医院名：テスト歯科\n氏名：山田太郎\n実践内容..."
        info = extract_clinic_info(text)
        self.assertEqual(info["person_name"], "山田太郎")

    def test_extract_from_pattern(self):
        text = "三浦歯科医院での取り組みについて報告します。"
        info = extract_clinic_info(text)
        self.assertIsNotNone(info["clinic_name"])

    def test_no_match(self):
        text = "特にパターンにマッチしないテキスト"
        info = extract_clinic_info(text)
        # clinic_nameがNoneでもエラーにならないこと
        self.assertIsNone(info["clinic_name"])


class TestMatchRecord(unittest.TestCase):

    def setUp(self):
        self.records = [
            ClinicRecord(2, "三浦歯科医院", "白川蓮", "test@example.com", ""),
            ClinicRecord(3, "山田デンタルクリニック", "山田太郎", "yamada@example.com", ""),
            ClinicRecord(4, "佐藤歯科", "佐藤花子", "sato@example.com", ""),
        ]

    def test_exact_match_clinic_name(self):
        text = "医院名：三浦歯科医院\n氏名：白川蓮\n実践内容..."
        result = match_record(text, self.records)
        self.assertIsNotNone(result)
        self.assertEqual(result.clinic_name, "三浦歯科医院")

    def test_partial_match(self):
        text = "三浦歯科医院の白川です。今月の取り組みを報告します。"
        result = match_record(text, self.records)
        self.assertIsNotNone(result)
        self.assertEqual(result.person_name, "白川蓮")

    def test_filename_match(self):
        text = "何もマッチしないテキスト"
        result = match_record(text, self.records, pdf_filename="三浦歯科医院_白川蓮.pdf")
        self.assertIsNotNone(result)
        self.assertEqual(result.clinic_name, "三浦歯科医院")

    def test_no_match(self):
        text = "存在しない医院のテキスト"
        result = match_record(text, self.records, pdf_filename="unknown.pdf")
        self.assertIsNone(result)

    def test_empty_records(self):
        result = match_record("テスト", [])
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
