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


class TestNormalizeEdgeCases(unittest.TestCase):

    def test_halfwidth_katakana_to_fullwidth(self):
        """半角カタカナが全角カタカナに正規化されること（NFKC）。"""
        self.assertEqual(_normalize("ｶﾞｷﾞｸﾞ"), _normalize("ガギグ"))

    def test_fullwidth_digits_to_halfwidth(self):
        """全角数字が半角数字に正規化されること。"""
        self.assertEqual(_normalize("１２３"), _normalize("123"))

    def test_mixed_fullwidth_halfwidth(self):
        """全角半角混在のテキストが同一に正規化されること。"""
        self.assertEqual(
            _normalize("ＡＢＣデンタル　クリニック"),
            _normalize("ABCデンタルクリニック"),
        )

    def test_empty_string(self):
        """空文字列の正規化が空文字列を返すこと。"""
        self.assertEqual(_normalize(""), "")


class TestExtractClinicInfoEdgeCases(unittest.TestCase):

    def test_empty_text(self):
        """空テキストからの抽出でエラーにならないこと。"""
        info = extract_clinic_info("")
        self.assertIsNone(info["clinic_name"])
        self.assertIsNone(info["person_name"])

    def test_unicode_clinic_name(self):
        """全角コロンを含む医院名パターンで正しく抽出されること。"""
        text = "医院名：テスト歯科クリニック\n氏名：鈴木一郎\n"
        info = extract_clinic_info(text)
        self.assertIn("テスト歯科クリニック", info["clinic_name"])


class TestMatchRecordEdgeCases(unittest.TestCase):

    def setUp(self):
        self.records = [
            ClinicRecord(2, "三浦歯科医院", "白川蓮", "test@example.com", ""),
            ClinicRecord(3, "山田デンタルクリニック", "山田太郎", "yamada@example.com", ""),
            ClinicRecord(4, "佐藤歯科", "佐藤花子", "sato@example.com", ""),
        ]

    def test_empty_text_and_empty_records(self):
        """空テキスト・空レコードでNoneが返ること。"""
        result = match_record("", [])
        self.assertIsNone(result)

    def test_empty_text_with_records(self):
        """空テキストでもファイル名からマッチできること。"""
        result = match_record("", self.records, pdf_filename="三浦歯科医院_報告.pdf")
        self.assertIsNotNone(result)
        self.assertEqual(result.clinic_name, "三浦歯科医院")

    def test_empty_text_no_filename(self):
        """空テキスト・ファイル名なしでNoneが返ること。"""
        result = match_record("", self.records)
        self.assertIsNone(result)

    def test_fullwidth_clinic_name_in_text(self):
        """全角英数の医院名でもマッチすること（NFKC正規化）。"""
        records = [
            ClinicRecord(2, "ABCデンタルクリニック", "田中太郎", "abc@example.com", ""),
        ]
        text = "医院名：ＡＢＣデンタルクリニック\n氏名：田中太郎\n"
        result = match_record(text, records)
        self.assertIsNotNone(result)
        self.assertEqual(result.clinic_name, "ABCデンタルクリニック")

    def test_duplicate_matches_returns_highest_score(self):
        """複数レコードがマッチする場合、最もスコアの高いものが返ること。"""
        records = [
            ClinicRecord(2, "三浦歯科医院", "白川蓮", "test@example.com", ""),
            ClinicRecord(3, "三浦歯科医院", "山田太郎", "yamada@example.com", ""),
        ]
        # 医院名と氏名の両方がマッチするレコードが優先される
        text = "医院名：三浦歯科医院\n氏名：白川蓮\n実践内容..."
        result = match_record(text, records)
        self.assertIsNotNone(result)
        self.assertEqual(result.person_name, "白川蓮")

    def test_records_with_similar_names(self):
        """類似名称のレコードで正しいものがマッチすること。"""
        records = [
            ClinicRecord(2, "佐藤歯科", "佐藤花子", "sato@example.com", ""),
            ClinicRecord(3, "佐藤歯科クリニック", "佐藤太郎", "sato2@example.com", ""),
        ]
        text = "医院名：佐藤歯科クリニック\n氏名：佐藤太郎\n報告内容"
        result = match_record(text, records)
        self.assertIsNotNone(result)
        self.assertEqual(result.person_name, "佐藤太郎")


if __name__ == "__main__":
    unittest.main()
