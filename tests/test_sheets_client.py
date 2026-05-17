"""sheets_client モジュールの新規追加関数のテスト。"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from src import sheets_client


def _build_service_with_sheets(existing_sheet_titles: list[str]) -> MagicMock:
    """get/batchUpdate/values.update/values.append を持つ Sheets サービスのモック。"""
    service = MagicMock()
    service.spreadsheets.return_value.get.return_value.execute.return_value = {
        "sheets": [
            {"properties": {"title": title}} for title in existing_sheet_titles
        ]
    }
    # ヘッダー range は最初は空（書き込みが必要）
    service.spreadsheets.return_value.values.return_value.get.return_value.execute.return_value = {}
    return service


class TestAppendOutputRecord(unittest.TestCase):

    @patch("src.sheets_client.get_sheets_service")
    def test_creates_sheet_and_writes_header_when_missing(
        self, mock_get_service
    ):
        service = _build_service_with_sheets(["Sheet1"])
        mock_get_service.return_value = service

        sheets_client.append_output_record(
            management_number="000001",
            clinic_name="三浦歯科医院",
            person_name="白川 蓮",
            sample_name="新患獲得.pdf",
            drive_url="https://drive.google.com/file/d/abc/view",
            spreadsheet_id="sheet_id_xxx",
            sheet_name="出力一覧",
            processed_at="2026-05-01 12:00:00",
        )

        # addSheet が呼ばれた
        batch_update_call = service.spreadsheets.return_value.batchUpdate.call_args
        body = batch_update_call.kwargs["body"]
        add_sheet_request = body["requests"][0]["addSheet"]
        self.assertEqual(add_sheet_request["properties"]["title"], "出力一覧")

        # ヘッダー書き込み + データ追加 の両方が values.update / values.append で呼ばれた
        update_call = service.spreadsheets.return_value.values.return_value.update.call_args
        self.assertIn("出力一覧!A1:F1", update_call.kwargs["range"])
        header_values = update_call.kwargs["body"]["values"][0]
        self.assertEqual(
            header_values,
            ["管理番号", "医院名", "個人名", "実践事例名", "Drive URL", "処理日時"],
        )

        append_call = service.spreadsheets.return_value.values.return_value.append.call_args
        self.assertIn("出力一覧!A:F", append_call.kwargs["range"])
        appended_row = append_call.kwargs["body"]["values"][0]
        self.assertEqual(
            appended_row,
            [
                "000001",
                "三浦歯科医院",
                "白川 蓮",
                "新患獲得.pdf",
                "https://drive.google.com/file/d/abc/view",
                "2026-05-01 12:00:00",
            ],
        )

    @patch("src.sheets_client.get_sheets_service")
    def test_skips_sheet_creation_when_already_exists(self, mock_get_service):
        service = _build_service_with_sheets(["Sheet1", "出力一覧"])
        # ヘッダーが既にあると見せかける
        service.spreadsheets.return_value.values.return_value.get.return_value.execute.return_value = {
            "values": [
                ["管理番号", "医院名", "個人名", "実践事例名", "Drive URL", "処理日時"]
            ]
        }
        mock_get_service.return_value = service

        sheets_client.append_output_record(
            management_number="000042",
            clinic_name="A",
            person_name="B",
            sample_name="x.pdf",
            drive_url="url",
            spreadsheet_id="sid",
            sheet_name="出力一覧",
            processed_at="2026-05-01",
        )

        service.spreadsheets.return_value.batchUpdate.assert_not_called()
        service.spreadsheets.return_value.values.return_value.update.assert_not_called()
        service.spreadsheets.return_value.values.return_value.append.assert_called_once()

    @patch("src.sheets_client.get_sheets_service")
    def test_uses_default_sheet_name_when_omitted(self, mock_get_service):
        service = _build_service_with_sheets(["出力一覧"])
        service.spreadsheets.return_value.values.return_value.get.return_value.execute.return_value = {
            "values": [["管理番号"]]
        }
        mock_get_service.return_value = service

        sheets_client.append_output_record(
            management_number="000001",
            clinic_name="A",
            person_name="B",
            sample_name="x.pdf",
            drive_url="url",
            spreadsheet_id="sid",
            processed_at="2026-05-01",
        )

        append_call = service.spreadsheets.return_value.values.return_value.append.call_args
        self.assertIn(sheets_client.OUTPUT_SHEET_NAME, append_call.kwargs["range"])

    def test_raises_when_spreadsheet_id_missing(self):
        with patch.object(sheets_client, "SPREADSHEET_ID", ""):
            with self.assertRaises(ValueError):
                sheets_client.append_output_record(
                    management_number="000001",
                    clinic_name="A",
                    person_name="B",
                    sample_name="x.pdf",
                    drive_url="url",
                )

    @patch("src.sheets_client.get_sheets_service")
    def test_appends_six_columns_with_management_number(self, mock_get_service):
        """append_output_record が 6 項目（管理番号含む）で書き込むことを検証。"""
        service = _build_service_with_sheets(["出力一覧"])
        service.spreadsheets.return_value.values.return_value.get.return_value.execute.return_value = {
            "values": [["管理番号"]]
        }
        mock_get_service.return_value = service

        sheets_client.append_output_record(
            management_number="000123",
            clinic_name="クリニック",
            person_name="山田",
            sample_name="title.pdf",
            drive_url="https://drive.example/view",
            spreadsheet_id="sid",
            sheet_name="出力一覧",
            processed_at="2026-05-17 10:00:00",
        )

        append_call = service.spreadsheets.return_value.values.return_value.append.call_args
        appended_row = append_call.kwargs["body"]["values"][0]
        self.assertEqual(len(appended_row), 6)
        self.assertEqual(appended_row[0], "000123")
        self.assertEqual(appended_row[1], "クリニック")
        self.assertEqual(appended_row[2], "山田")
        self.assertEqual(appended_row[3], "title.pdf")
        self.assertEqual(appended_row[4], "https://drive.example/view")
        self.assertEqual(appended_row[5], "2026-05-17 10:00:00")


class TestGetMaxManagementNumber(unittest.TestCase):

    @patch("src.sheets_client.get_sheets_service")
    def test_returns_max_from_multiple_rows(self, mock_get_service):
        """複数行から数値の最大値を返す。"""
        service = _build_service_with_sheets(["出力一覧"])
        # _ensure_output_sheet 内のヘッダー確認 → 既にあるとして空にしない値を返す
        # その後 A:A の取得で実データを返す。
        # MagicMock の get().execute() は同じインスタンスを返すので side_effect で順序制御。
        service.spreadsheets.return_value.values.return_value.get.return_value.execute.side_effect = [
            # _ensure_output_sheet のヘッダー読み取り（既存あり）
            {"values": [["管理番号", "医院名", "個人名", "実践事例名", "Drive URL", "処理日時"]]},
            # A列の読み取り
            {
                "values": [
                    ["管理番号"],  # ヘッダー
                    ["000001"],
                    ["000005"],
                    ["000003"],
                ]
            },
        ]
        mock_get_service.return_value = service

        result = sheets_client.get_max_management_number(
            spreadsheet_id="sid", sheet_name="出力一覧"
        )
        self.assertEqual(result, 5)

    @patch("src.sheets_client.get_sheets_service")
    def test_returns_zero_for_empty_sheet(self, mock_get_service):
        """空シート（ヘッダーのみ or 完全に空）の場合は 0 を返す。"""
        service = _build_service_with_sheets(["出力一覧"])
        service.spreadsheets.return_value.values.return_value.get.return_value.execute.side_effect = [
            # _ensure_output_sheet のヘッダー読み取り（既存あり）
            {"values": [["管理番号", "医院名", "個人名", "実践事例名", "Drive URL", "処理日時"]]},
            # A列の読み取り → ヘッダー行のみ
            {"values": [["管理番号"]]},
        ]
        mock_get_service.return_value = service

        result = sheets_client.get_max_management_number(
            spreadsheet_id="sid", sheet_name="出力一覧"
        )
        self.assertEqual(result, 0)

    @patch("src.sheets_client.get_sheets_service")
    def test_returns_zero_for_fully_empty_sheet(self, mock_get_service):
        """シートに values キーすら無い場合は 0 を返す。"""
        service = _build_service_with_sheets(["出力一覧"])
        service.spreadsheets.return_value.values.return_value.get.return_value.execute.side_effect = [
            # _ensure_output_sheet のヘッダー読み取り（既存あり）
            {"values": [["管理番号"]]},
            # A列の読み取り → 完全に空
            {},
        ]
        mock_get_service.return_value = service

        result = sheets_client.get_max_management_number(
            spreadsheet_id="sid", sheet_name="出力一覧"
        )
        self.assertEqual(result, 0)

    @patch("src.sheets_client.get_sheets_service")
    def test_skips_non_numeric_values(self, mock_get_service):
        """非数値・空セルはスキップして数値の最大を返す。"""
        service = _build_service_with_sheets(["出力一覧"])
        service.spreadsheets.return_value.values.return_value.get.return_value.execute.side_effect = [
            {"values": [["管理番号"]]},
            {
                "values": [
                    ["管理番号"],  # ヘッダー（非数値）
                    ["000010"],
                    ["abc"],  # 非数値混入
                    [""],  # 空
                    [],  # 空行
                    ["000007"],
                    ["000099"],
                ]
            },
        ]
        mock_get_service.return_value = service

        result = sheets_client.get_max_management_number(
            spreadsheet_id="sid", sheet_name="出力一覧"
        )
        self.assertEqual(result, 99)

    def test_raises_when_spreadsheet_id_missing(self):
        with patch.object(sheets_client, "SPREADSHEET_ID", ""):
            with self.assertRaises(ValueError):
                sheets_client.get_max_management_number()


class TestGetMaxManagementNumberWithPrefix(unittest.TestCase):
    """マルチプロファイル：管理番号 prefix（例 ``J24Q1-``）対応のテスト。"""

    @patch("src.sheets_client.get_sheets_service")
    def test_extracts_max_from_prefixed_rows(self, mock_get_service):
        """prefix 指定時は prefix 付き行から数値部の最大値を取得する。"""
        service = _build_service_with_sheets(["実践事例_2024Q1_出力一覧"])
        service.spreadsheets.return_value.values.return_value.get.return_value.execute.side_effect = [
            {"values": [["管理番号"]]},  # _ensure_output_sheet
            {
                "values": [
                    ["管理番号"],  # ヘッダー
                    ["J24Q1-000001"],
                    ["J24Q1-000010"],
                    ["J24Q1-000003"],
                ]
            },
        ]
        mock_get_service.return_value = service

        result = sheets_client.get_max_management_number(
            spreadsheet_id="sid",
            sheet_name="実践事例_2024Q1_出力一覧",
            prefix="J24Q1-",
        )
        self.assertEqual(result, 10)

    @patch("src.sheets_client.get_sheets_service")
    def test_ignores_rows_with_other_prefixes(self, mock_get_service):
        """異なる prefix の行はカウントしない（プロファイル間の相互汚染防止）。"""
        service = _build_service_with_sheets(["s"])
        service.spreadsheets.return_value.values.return_value.get.return_value.execute.side_effect = [
            {"values": [["管理番号"]]},
            {
                "values": [
                    ["管理番号"],
                    ["J24Q1-000001"],
                    ["J24Q2-000999"],  # 別四半期 → 無視
                    ["000888"],  # prefix なし → 無視
                    ["J24Q1-000005"],
                ]
            },
        ]
        mock_get_service.return_value = service

        result = sheets_client.get_max_management_number(
            spreadsheet_id="sid",
            sheet_name="s",
            prefix="J24Q1-",
        )
        self.assertEqual(result, 5)

    @patch("src.sheets_client.get_sheets_service")
    def test_prefix_none_preserves_legacy_behavior(self, mock_get_service):
        """prefix=None（既存挙動）：純粋数値セルのみを対象、prefix 付きは無視。"""
        service = _build_service_with_sheets(["出力一覧"])
        service.spreadsheets.return_value.values.return_value.get.return_value.execute.side_effect = [
            {"values": [["管理番号"]]},
            {
                "values": [
                    ["管理番号"],
                    ["000001"],
                    ["J24Q1-999999"],  # prefix 付きは prefix=None 時に無視されるべき
                    ["000007"],
                ]
            },
        ]
        mock_get_service.return_value = service

        result = sheets_client.get_max_management_number(
            spreadsheet_id="sid",
            sheet_name="出力一覧",
            # prefix を渡さない＝既存挙動
        )
        self.assertEqual(result, 7)

    @patch("src.sheets_client.get_sheets_service")
    def test_prefix_empty_string_preserves_legacy_behavior(self, mock_get_service):
        """prefix=""（jissen_default の値）は None と同じ既存挙動。"""
        service = _build_service_with_sheets(["出力一覧"])
        service.spreadsheets.return_value.values.return_value.get.return_value.execute.side_effect = [
            {"values": [["管理番号"]]},
            {
                "values": [
                    ["管理番号"],
                    ["000003"],
                    ["000012"],
                ]
            },
        ]
        mock_get_service.return_value = service

        result = sheets_client.get_max_management_number(
            spreadsheet_id="sid",
            sheet_name="出力一覧",
            prefix="",  # falsy → 既存挙動
        )
        self.assertEqual(result, 12)

    @patch("src.sheets_client.get_sheets_service")
    def test_returns_zero_when_no_prefix_match(self, mock_get_service):
        """prefix に一致する行が一つもなければ 0。"""
        service = _build_service_with_sheets(["s"])
        service.spreadsheets.return_value.values.return_value.get.return_value.execute.side_effect = [
            {"values": [["管理番号"]]},
            {
                "values": [
                    ["管理番号"],
                    ["J24Q2-000001"],
                    ["000999"],
                ]
            },
        ]
        mock_get_service.return_value = service

        result = sheets_client.get_max_management_number(
            spreadsheet_id="sid",
            sheet_name="s",
            prefix="J24Q1-",
        )
        self.assertEqual(result, 0)


if __name__ == "__main__":
    unittest.main()
