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
        # RAW で書き込むこと。USER_ENTERED だと処理日時が日付シリアル値に
        # 変換され、生の数値（46163.23... 等）として表示されてしまう。
        self.assertEqual(append_call.kwargs["valueInputOption"], "RAW")


class TestGetProcessedManagementNumbers(unittest.TestCase):
    """``get_processed_management_numbers``（増分処理の重複検知キー取得）。"""

    @staticmethod
    def _build_service(
        existing_sheet_titles: list[str],
        a_column_values: list[list[str]] | None,
    ) -> MagicMock:
        """シート一覧と A2:A の値を持つ Sheets サービスのモック。

        ``a_column_values`` が ``None`` のとき ``values`` キー無しを再現する。
        """
        service = MagicMock()
        service.spreadsheets.return_value.get.return_value.execute.return_value = {
            "sheets": [
                {"properties": {"title": t}} for t in existing_sheet_titles
            ]
        }
        get_result: dict = {}
        if a_column_values is not None:
            get_result["values"] = a_column_values
        service.spreadsheets.return_value.values.return_value.get.return_value.execute.return_value = get_result
        return service

    @patch("src.sheets_client.get_sheets_service")
    def test_returns_set_of_management_numbers(self, mock_get_service):
        """複数行の A列から管理番号の集合を返す。"""
        service = self._build_service(
            ["出力一覧"],
            [["001-01-0"], ["001-01-1"], ["002-03-4"]],
        )
        mock_get_service.return_value = service

        result = sheets_client.get_processed_management_numbers(
            spreadsheet_id="sid", sheet_name="出力一覧",
        )

        self.assertEqual(result, {"001-01-0", "001-01-1", "002-03-4"})
        # A2:A（ヘッダー除外）で取得していること
        get_call = service.spreadsheets.return_value.values.return_value.get.call_args
        self.assertEqual(get_call.kwargs["range"], "出力一覧!A2:A")

    @patch("src.sheets_client.get_sheets_service")
    def test_excludes_empty_and_whitespace_cells(self, mock_get_service):
        """空セル・空白のみセル・空行は集合から除外される。"""
        service = self._build_service(
            ["出力一覧"],
            [["001-01-0"], [""], ["   "], [], ["002-03-4"]],
        )
        mock_get_service.return_value = service

        result = sheets_client.get_processed_management_numbers(
            spreadsheet_id="sid", sheet_name="出力一覧",
        )

        self.assertEqual(result, {"001-01-0", "002-03-4"})

    @patch("src.sheets_client.get_sheets_service")
    def test_returns_empty_set_when_sheet_not_created(self, mock_get_service):
        """出力一覧シートが未作成なら空集合（values.get を呼ばない）。"""
        service = self._build_service(["Sheet1"], None)
        mock_get_service.return_value = service

        result = sheets_client.get_processed_management_numbers(
            spreadsheet_id="sid", sheet_name="出力一覧",
        )

        self.assertEqual(result, set())
        service.spreadsheets.return_value.values.return_value.get.assert_not_called()

    @patch("src.sheets_client.get_sheets_service")
    def test_returns_empty_set_when_only_header(self, mock_get_service):
        """ヘッダー行のみ（A2:A が空）なら空集合。"""
        service = self._build_service(["出力一覧"], [])
        mock_get_service.return_value = service

        result = sheets_client.get_processed_management_numbers(
            spreadsheet_id="sid", sheet_name="出力一覧",
        )

        self.assertEqual(result, set())

    @patch("src.sheets_client.get_sheets_service")
    def test_returns_empty_set_when_values_key_missing(self, mock_get_service):
        """``values`` キーが無いレスポンスでも空集合を返す（KeyError にしない）。"""
        service = self._build_service(["出力一覧"], None)
        # シートは存在するが values.get が空 dict を返す状態にする
        service.spreadsheets.return_value.values.return_value.get.return_value.execute.return_value = {}
        mock_get_service.return_value = service

        result = sheets_client.get_processed_management_numbers(
            spreadsheet_id="sid", sheet_name="出力一覧",
        )

        self.assertEqual(result, set())

    @patch("src.sheets_client.get_sheets_service")
    def test_uses_default_sheet_name_when_omitted(self, mock_get_service):
        """``sheet_name`` 省略時は ``OUTPUT_SHEET_NAME`` にフォールバックする。"""
        service = self._build_service(
            [sheets_client.OUTPUT_SHEET_NAME], [["003-04-5"]],
        )
        mock_get_service.return_value = service

        result = sheets_client.get_processed_management_numbers(
            spreadsheet_id="sid",
        )

        self.assertEqual(result, {"003-04-5"})
        get_call = service.spreadsheets.return_value.values.return_value.get.call_args
        self.assertIn(sheets_client.OUTPUT_SHEET_NAME, get_call.kwargs["range"])

    def test_raises_when_spreadsheet_id_missing(self):
        """``SPREADSHEET_ID`` 未設定なら ValueError。"""
        with patch.object(sheets_client, "SPREADSHEET_ID", ""):
            with self.assertRaises(ValueError):
                sheets_client.get_processed_management_numbers()


class TestAppendClinicFolderRecord(unittest.TestCase):
    """``append_clinic_folder_record``（医院フォルダURLシートへの 1 行追記）。"""

    @patch("src.sheets_client.get_sheets_service")
    def test_creates_sheet_and_writes_three_column_header_when_missing(
        self, mock_get_service
    ):
        """医院シート未作成なら作成し、3 列ヘッダーを書き込む。"""
        service = _build_service_with_sheets(["Sheet1"])
        mock_get_service.return_value = service

        sheets_client.append_clinic_folder_record(
            clinic_number="001",
            clinic_name="三浦歯科医院",
            clinic_folder_url="https://drive.google.com/drive/folders/abc",
            spreadsheet_id="sheet_id_xxx",
            sheet_name="出力一覧_医院",
        )

        # addSheet が呼ばれた
        batch_update_call = service.spreadsheets.return_value.batchUpdate.call_args
        add_sheet = batch_update_call.kwargs["body"]["requests"][0]["addSheet"]
        self.assertEqual(add_sheet["properties"]["title"], "出力一覧_医院")

        # ヘッダーは 3 列（医院番号 / 医院名 / 医院フォルダURL）、range は A1:C1
        update_call = service.spreadsheets.return_value.values.return_value.update.call_args
        self.assertIn("出力一覧_医院!A1:C1", update_call.kwargs["range"])
        self.assertEqual(
            update_call.kwargs["body"]["values"][0],
            ["医院番号", "医院名", "医院フォルダURL"],
        )

        # データ行は A:C に append
        append_call = service.spreadsheets.return_value.values.return_value.append.call_args
        self.assertIn("出力一覧_医院!A:C", append_call.kwargs["range"])
        appended_row = append_call.kwargs["body"]["values"][0]
        self.assertEqual(
            appended_row,
            ["001", "三浦歯科医院",
             "https://drive.google.com/drive/folders/abc"],
        )
        # RAW で書き込む（医院番号の先頭ゼロや URL を保持）
        self.assertEqual(append_call.kwargs["valueInputOption"], "RAW")

    @patch("src.sheets_client.get_sheets_service")
    def test_skips_sheet_creation_when_already_exists(self, mock_get_service):
        """医院シートが既にありヘッダーもあれば addSheet / ヘッダー書き込みなし。"""
        service = _build_service_with_sheets(["Sheet1", "出力一覧_医院"])
        service.spreadsheets.return_value.values.return_value.get.return_value.execute.return_value = {
            "values": [["医院番号", "医院名", "医院フォルダURL"]]
        }
        mock_get_service.return_value = service

        sheets_client.append_clinic_folder_record(
            clinic_number="002",
            clinic_name="山本歯科",
            clinic_folder_url="https://drive.google.com/drive/folders/def",
            spreadsheet_id="sid",
            sheet_name="出力一覧_医院",
        )

        service.spreadsheets.return_value.batchUpdate.assert_not_called()
        service.spreadsheets.return_value.values.return_value.update.assert_not_called()
        service.spreadsheets.return_value.values.return_value.append.assert_called_once()

    @patch("src.sheets_client.get_sheets_service")
    def test_appends_exactly_three_columns(self, mock_get_service):
        """append される行は医院番号 / 医院名 / 医院フォルダURL の 3 列。"""
        service = _build_service_with_sheets(["出力一覧_医院"])
        service.spreadsheets.return_value.values.return_value.get.return_value.execute.return_value = {
            "values": [["医院番号"]]
        }
        mock_get_service.return_value = service

        sheets_client.append_clinic_folder_record(
            clinic_number="00123",
            clinic_name="クリニック",
            clinic_folder_url="https://drive.google.com/drive/folders/ghi",
            spreadsheet_id="sid",
            sheet_name="出力一覧_医院",
        )

        append_call = service.spreadsheets.return_value.values.return_value.append.call_args
        appended_row = append_call.kwargs["body"]["values"][0]
        self.assertEqual(len(appended_row), 3)
        self.assertEqual(appended_row[0], "00123")
        self.assertEqual(appended_row[1], "クリニック")
        self.assertEqual(
            appended_row[2], "https://drive.google.com/drive/folders/ghi"
        )

    def test_raises_when_spreadsheet_id_missing(self):
        """``SPREADSHEET_ID`` 未設定なら ValueError。"""
        with patch.object(sheets_client, "SPREADSHEET_ID", ""):
            with self.assertRaises(ValueError):
                sheets_client.append_clinic_folder_record(
                    clinic_number="001",
                    clinic_name="A",
                    clinic_folder_url="url",
                )

    @patch("src.sheets_client.get_sheets_service")
    def test_does_not_break_output_sheet_six_column_header(
        self, mock_get_service
    ):
        """医院シート（3 列）作成は出力一覧シート（6 列）ヘッダーを壊さない。

        汎用ヘルパー ``_ensure_sheet_with_header`` が列数をヘッダー長から
        決めるため、医院シートは A1:C1、出力一覧シートは A1:F1 と独立する。
        """
        service = _build_service_with_sheets(["出力一覧"])
        mock_get_service.return_value = service

        # 出力一覧シートに 1 行追記 → ヘッダーは 6 列
        sheets_client.append_output_record(
            management_number="001-01-0",
            clinic_name="A",
            person_name="B",
            sample_name="x.pdf",
            drive_url="url",
            spreadsheet_id="sid",
            sheet_name="出力一覧",
            processed_at="2026-05-21",
        )
        output_update = service.spreadsheets.return_value.values.return_value.update.call_args
        self.assertEqual(len(output_update.kwargs["body"]["values"][0]), 6)
        self.assertIn("A1:F1", output_update.kwargs["range"])

    @patch("src.sheets_client.get_sheets_service")
    def test_append_clinic_folder_record_passes_num_retries(
        self, mock_get_service
    ):
        """``append_clinic_folder_record`` の全 ``execute()`` に num_retries が渡る。"""
        service = _build_service_with_sheets(["出力一覧_医院"])
        service.spreadsheets.return_value.values.return_value.get.return_value.execute.return_value = {
            "values": [["医院番号"]]
        }
        mock_get_service.return_value = service

        sheets_client.append_clinic_folder_record(
            clinic_number="001",
            clinic_name="A",
            clinic_folder_url="url",
            spreadsheet_id="sid",
            sheet_name="出力一覧_医院",
        )

        append_execute = (
            service.spreadsheets.return_value.values.return_value.append
            .return_value.execute
        )
        self.assertEqual(
            append_execute.call_args.kwargs.get("num_retries"),
            sheets_client.GOOGLE_API_NUM_RETRIES,
        )
        meta_execute = service.spreadsheets.return_value.get.return_value.execute
        self.assertEqual(
            meta_execute.call_args.kwargs.get("num_retries"),
            sheets_client.GOOGLE_API_NUM_RETRIES,
        )


class TestGetRecordedClinicNumbers(unittest.TestCase):
    """``get_recorded_clinic_numbers``（医院シート重複防止のキー取得）。"""

    @staticmethod
    def _build_service(
        existing_sheet_titles: list[str],
        a_column_values: list[list[str]] | None,
    ) -> MagicMock:
        """シート一覧と A2:A の値を持つ Sheets サービスのモック。

        ``a_column_values`` が ``None`` のとき ``values`` キー無しを再現する。
        """
        service = MagicMock()
        service.spreadsheets.return_value.get.return_value.execute.return_value = {
            "sheets": [
                {"properties": {"title": t}} for t in existing_sheet_titles
            ]
        }
        get_result: dict = {}
        if a_column_values is not None:
            get_result["values"] = a_column_values
        service.spreadsheets.return_value.values.return_value.get.return_value.execute.return_value = get_result
        return service

    @patch("src.sheets_client.get_sheets_service")
    def test_returns_set_of_clinic_numbers(self, mock_get_service):
        """複数行の A列から医院番号の集合を返す。"""
        service = self._build_service(
            ["出力一覧_医院"],
            [["001"], ["002"], ["00123"]],
        )
        mock_get_service.return_value = service

        result = sheets_client.get_recorded_clinic_numbers(
            spreadsheet_id="sid", sheet_name="出力一覧_医院",
        )

        self.assertEqual(result, {"001", "002", "00123"})
        get_call = service.spreadsheets.return_value.values.return_value.get.call_args
        self.assertEqual(get_call.kwargs["range"], "出力一覧_医院!A2:A")

    @patch("src.sheets_client.get_sheets_service")
    def test_excludes_empty_and_whitespace_cells(self, mock_get_service):
        """空セル・空白のみセル・空行は集合から除外される。"""
        service = self._build_service(
            ["出力一覧_医院"],
            [["001"], [""], ["   "], [], ["002"]],
        )
        mock_get_service.return_value = service

        result = sheets_client.get_recorded_clinic_numbers(
            spreadsheet_id="sid", sheet_name="出力一覧_医院",
        )

        self.assertEqual(result, {"001", "002"})

    @patch("src.sheets_client.get_sheets_service")
    def test_returns_empty_set_when_sheet_not_created(self, mock_get_service):
        """医院シートが未作成なら空集合（values.get を呼ばない）。"""
        service = self._build_service(["Sheet1"], None)
        mock_get_service.return_value = service

        result = sheets_client.get_recorded_clinic_numbers(
            spreadsheet_id="sid", sheet_name="出力一覧_医院",
        )

        self.assertEqual(result, set())
        service.spreadsheets.return_value.values.return_value.get.assert_not_called()

    @patch("src.sheets_client.get_sheets_service")
    def test_returns_empty_set_when_only_header(self, mock_get_service):
        """ヘッダー行のみ（A2:A が空）なら空集合。"""
        service = self._build_service(["出力一覧_医院"], [])
        mock_get_service.return_value = service

        result = sheets_client.get_recorded_clinic_numbers(
            spreadsheet_id="sid", sheet_name="出力一覧_医院",
        )

        self.assertEqual(result, set())

    @patch("src.sheets_client.get_sheets_service")
    def test_returns_empty_set_when_values_key_missing(self, mock_get_service):
        """``values`` キーが無いレスポンスでも空集合を返す（KeyError にしない）。"""
        service = self._build_service(["出力一覧_医院"], None)
        service.spreadsheets.return_value.values.return_value.get.return_value.execute.return_value = {}
        mock_get_service.return_value = service

        result = sheets_client.get_recorded_clinic_numbers(
            spreadsheet_id="sid", sheet_name="出力一覧_医院",
        )

        self.assertEqual(result, set())

    def test_raises_when_spreadsheet_id_missing(self):
        """``SPREADSHEET_ID`` 未設定なら ValueError。"""
        with patch.object(sheets_client, "SPREADSHEET_ID", ""):
            with self.assertRaises(ValueError):
                sheets_client.get_recorded_clinic_numbers()

    @patch("src.sheets_client.get_sheets_service")
    def test_passes_num_retries(self, mock_get_service):
        """全 ``execute()`` に num_retries が渡る（P-017 準拠）。"""
        service = self._build_service(["出力一覧_医院"], [["001"]])
        mock_get_service.return_value = service

        sheets_client.get_recorded_clinic_numbers(
            spreadsheet_id="sid", sheet_name="出力一覧_医院",
        )

        meta_execute = service.spreadsheets.return_value.get.return_value.execute
        self.assertEqual(
            meta_execute.call_args.kwargs.get("num_retries"),
            sheets_client.GOOGLE_API_NUM_RETRIES,
        )
        values_execute = (
            service.spreadsheets.return_value.values.return_value.get
            .return_value.execute
        )
        self.assertEqual(
            values_execute.call_args.kwargs.get("num_retries"),
            sheets_client.GOOGLE_API_NUM_RETRIES,
        )


class TestGoogleApiRetry(unittest.TestCase):
    """Sheets API 呼び出しが ``num_retries`` 付きで ``execute()`` されることを検証する。

    Google Sheets API は 503 / 429 などの一過性エラーを一定確率で返すため、
    ``execute(num_retries=N)`` で指数バックオフ・リトライさせる。これが無いと
    503 が 1 回起きただけでワークフロー全体がクラッシュする（P-017）。
    """

    @patch("src.sheets_client.get_sheets_service")
    def test_get_processed_management_numbers_passes_num_retries(
        self, mock_get_service
    ):
        """``get_processed_management_numbers`` の get/values.get に num_retries が渡る。"""
        service = TestGetProcessedManagementNumbers._build_service(
            ["出力一覧"], [["001-01-0"]],
        )
        mock_get_service.return_value = service

        sheets_client.get_processed_management_numbers(
            spreadsheet_id="sid", sheet_name="出力一覧",
        )

        meta_execute = service.spreadsheets.return_value.get.return_value.execute
        self.assertEqual(
            meta_execute.call_args.kwargs.get("num_retries"),
            sheets_client.GOOGLE_API_NUM_RETRIES,
        )
        values_execute = (
            service.spreadsheets.return_value.values.return_value.get
            .return_value.execute
        )
        self.assertEqual(
            values_execute.call_args.kwargs.get("num_retries"),
            sheets_client.GOOGLE_API_NUM_RETRIES,
        )

    @patch("src.sheets_client.get_sheets_service")
    def test_append_output_record_passes_num_retries(self, mock_get_service):
        """``append_output_record`` の values.append に num_retries=5 が渡る。"""
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
            sheet_name="出力一覧",
            processed_at="2026-05-21",
        )

        append_execute = (
            service.spreadsheets.return_value.values.return_value.append
            .return_value.execute
        )
        self.assertEqual(append_execute.call_args.kwargs.get("num_retries"), 5)

    @patch("src.sheets_client.get_sheets_service")
    def test_read_records_passes_num_retries(self, mock_get_service):
        """``read_records`` の values.get に num_retries が渡る。"""
        service = MagicMock()
        service.spreadsheets.return_value.values.return_value.get.return_value.execute.return_value = {
            "values": [["医院名", "氏名", "メール", "ステータス"]]
        }
        mock_get_service.return_value = service

        sheets_client.read_records(spreadsheet_id="sid")

        get_execute = (
            service.spreadsheets.return_value.values.return_value.get
            .return_value.execute
        )
        self.assertEqual(
            get_execute.call_args.kwargs.get("num_retries"),
            sheets_client.GOOGLE_API_NUM_RETRIES,
        )




# ─────────────────────────────────────────────────────────────────────
# 参加者マスターシート（医院名標準化 + Gmail 下書き用ルックアップ表）
# ─────────────────────────────────────────────────────────────────────


def _build_master_sheet_service(
    existing_sheet_titles: list[str],
    values: list[list[str]] | None,
) -> MagicMock:
    """参加者マスターシート読み取り用の Sheets サービス mock を作る。

    ``values`` は ``A:E`` の全行（ヘッダー含む）を表す。``None`` 指定で
    ``values`` キー無しを再現する。
    """
    service = MagicMock()
    service.spreadsheets.return_value.get.return_value.execute.return_value = {
        "sheets": [
            {"properties": {"title": t}} for t in existing_sheet_titles
        ]
    }
    # _ensure_sheet_with_header はヘッダー range で values.get を呼び、
    # その後 read_master_records が A:E で values.get を呼ぶ。両方を同じ
    # mock で返すと「ヘッダーは既にある」「データもこの内容」と見せかけられる。
    get_result: dict = {}
    if values is not None:
        get_result["values"] = values
    service.spreadsheets.return_value.values.return_value.get.return_value.execute.return_value = get_result
    return service


class TestReadMasterRecords(unittest.TestCase):
    """``read_master_records``（参加者マスターシート読み取り）。

    A 列は「管理番号」（``101-01`` のような ``xxx-yy`` 形式）であり、
    先頭セグメント（``-`` の前）が医院コードで、PDF ファイル名先頭の
    医院コードと突合する。
    """

    @patch("src.sheets_client.get_sheets_service")
    def test_reads_records_from_existing_sheet(self, mock_get_service):
        """既存シートから複数行を読み取り MasterRecord のリストを返す。"""
        service = _build_master_sheet_service(
            ["参加者マスター"],
            [
                ["管理番号", "医院名", "参加者名", "申し込み会場", "メールアドレス"],
                ["101-01", "三浦歯科医院", "白川 蓮", "東京会場", "ren@example.com"],
                ["101-02", "三浦歯科医院", "鈴木 一郎", "東京会場", "ichiro@example.com"],
                ["102-01", "山本歯科", "田中 太郎", "大阪会場", "tanaka@example.com"],
            ],
        )
        mock_get_service.return_value = service

        records = sheets_client.read_master_records(
            spreadsheet_id="sid", sheet_name="参加者マスター",
        )

        self.assertEqual(len(records), 3)
        self.assertEqual(records[0].management_number, "101-01")
        self.assertEqual(records[0].clinic_number, "101")  # 派生プロパティ
        self.assertEqual(records[0].clinic_name, "三浦歯科医院")
        self.assertEqual(records[0].participant_name, "白川 蓮")
        self.assertEqual(records[0].venue, "東京会場")
        self.assertEqual(records[0].email, "ren@example.com")
        self.assertEqual(records[2].management_number, "102-01")
        self.assertEqual(records[2].clinic_number, "102")
        self.assertEqual(records[2].email, "tanaka@example.com")

    @patch("src.sheets_client.get_sheets_service")
    def test_creates_sheet_when_not_exists_and_returns_empty(
        self, mock_get_service
    ):
        """シートが存在しなければ自動作成し（ヘッダー書き込み）空リストを返す。"""
        service = _build_master_sheet_service(["Sheet1"], None)
        mock_get_service.return_value = service

        records = sheets_client.read_master_records(
            spreadsheet_id="sid", sheet_name="参加者マスター",
        )

        batch_update_call = service.spreadsheets.return_value.batchUpdate.call_args
        add_sheet_request = batch_update_call.kwargs["body"]["requests"][0]["addSheet"]
        self.assertEqual(
            add_sheet_request["properties"]["title"], "参加者マスター"
        )
        update_call = service.spreadsheets.return_value.values.return_value.update.call_args
        self.assertIn("参加者マスター!A1:E1", update_call.kwargs["range"])
        self.assertEqual(
            update_call.kwargs["body"]["values"][0],
            ["管理番号", "医院名", "参加者名", "申し込み会場", "メールアドレス"],
        )
        self.assertEqual(records, [])

    @patch("src.sheets_client.get_sheets_service")
    def test_empty_rows_are_skipped(self, mock_get_service):
        """管理番号も医院名も空の行は読み飛ばす。"""
        service = _build_master_sheet_service(
            ["参加者マスター"],
            [
                ["管理番号", "医院名", "参加者名", "申し込み会場", "メールアドレス"],
                ["101-01", "三浦歯科医院", "白川 蓮", "東京会場", "ren@example.com"],
                ["", "", "", "", ""],  # 空行
                ["102-01", "山本歯科", "田中 太郎", "大阪会場", "tanaka@example.com"],
            ],
        )
        mock_get_service.return_value = service

        records = sheets_client.read_master_records(
            spreadsheet_id="sid", sheet_name="参加者マスター",
        )

        self.assertEqual(len(records), 2)
        self.assertEqual(records[0].management_number, "101-01")
        self.assertEqual(records[1].management_number, "102-01")

    @patch("src.sheets_client.get_sheets_service")
    def test_invalid_email_warns_but_row_remains(self, mock_get_service):
        """形式不正のメールは空扱い + warning。行自体は残る（医院名 lookup 用）。"""
        service = _build_master_sheet_service(
            ["参加者マスター"],
            [
                ["管理番号", "医院名", "参加者名", "申し込み会場", "メールアドレス"],
                ["101-01", "三浦歯科", "白川 蓮", "東京会場", "not-an-email"],
            ],
        )
        mock_get_service.return_value = service

        with self.assertLogs("jissen_comment", level="WARNING") as log_ctx:
            records = sheets_client.read_master_records(
                spreadsheet_id="sid", sheet_name="参加者マスター",
            )

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].management_number, "101-01")
        self.assertEqual(records[0].clinic_name, "三浦歯科")
        self.assertEqual(records[0].email, "")
        joined = "\n".join(log_ctx.output)
        self.assertIn("メールアドレス", joined)
        self.assertIn("形式が不正", joined)

    @patch("src.sheets_client.get_sheets_service")
    def test_passes_num_retries(self, mock_get_service):
        """全 ``execute()`` に num_retries=GOOGLE_API_NUM_RETRIES が渡る(P-017)。"""
        service = _build_master_sheet_service(
            ["参加者マスター"],
            [
                ["管理番号", "医院名", "参加者名", "申し込み会場", "メールアドレス"],
                ["101-01", "三浦歯科", "白川 蓮", "東京会場", "ren@example.com"],
            ],
        )
        mock_get_service.return_value = service

        sheets_client.read_master_records(
            spreadsheet_id="sid", sheet_name="参加者マスター",
        )

        meta_execute = service.spreadsheets.return_value.get.return_value.execute
        self.assertEqual(
            meta_execute.call_args.kwargs.get("num_retries"),
            sheets_client.GOOGLE_API_NUM_RETRIES,
        )
        values_execute = (
            service.spreadsheets.return_value.values.return_value.get
            .return_value.execute
        )
        self.assertEqual(
            values_execute.call_args.kwargs.get("num_retries"),
            sheets_client.GOOGLE_API_NUM_RETRIES,
        )

    @patch("src.sheets_client.get_sheets_service")
    def test_uses_default_sheet_name_when_omitted(self, mock_get_service):
        """sheet_name 省略時は MASTER_SHEET_NAME にフォールバックする。"""
        service = _build_master_sheet_service(
            [sheets_client.MASTER_SHEET_NAME],
            [
                ["管理番号", "医院名", "参加者名", "申し込み会場", "メールアドレス"],
                ["101-01", "A", "B", "東京", "b@example.com"],
            ],
        )
        mock_get_service.return_value = service

        sheets_client.read_master_records(spreadsheet_id="sid")

        get_call = service.spreadsheets.return_value.values.return_value.get.call_args
        self.assertIn(sheets_client.MASTER_SHEET_NAME, get_call.kwargs["range"])

    @patch("src.sheets_client.get_sheets_service")
    def test_uses_default_spreadsheet_id_when_omitted(self, mock_get_service):
        """spreadsheet_id 省略時は ``SPREADSHEET_ID`` を使う。"""
        service = _build_master_sheet_service(
            ["参加者マスター"],
            [
                ["管理番号", "医院名", "参加者名", "申し込み会場", "メールアドレス"],
            ],
        )
        mock_get_service.return_value = service

        with patch.object(sheets_client, "SPREADSHEET_ID", "default_sid"):
            sheets_client.read_master_records(sheet_name="参加者マスター")

        get_call = service.spreadsheets.return_value.values.return_value.get.call_args
        self.assertEqual(get_call.kwargs["spreadsheetId"], "default_sid")

    def test_raises_when_spreadsheet_id_missing(self):
        """``SPREADSHEET_ID`` 未設定 + 引数省略なら ValueError。"""
        with patch.object(sheets_client, "SPREADSHEET_ID", ""):
            with self.assertRaises(ValueError):
                sheets_client.read_master_records()


class TestLookupClinicName(unittest.TestCase):
    """``lookup_clinic_name`` の医院コード前方一致挙動。

    A 列の管理番号 ``101-01`` から派生する ``clinic_number == "101"`` を使う。
    """

    def _records(self) -> list[sheets_client.MasterRecord]:
        return [
            sheets_client.MasterRecord(
                management_number="101-01",
                clinic_name="三浦歯科医院",
                participant_name="白川 蓮",
                venue="東京",
                email="ren@example.com",
            ),
            sheets_client.MasterRecord(
                management_number="101-02",
                clinic_name="三浦歯科医院",
                participant_name="鈴木 一郎",
                venue="東京",
                email="ichiro@example.com",
            ),
            sheets_client.MasterRecord(
                management_number="102-01",
                clinic_name="山本歯科",
                participant_name="田中 太郎",
                venue="大阪",
                email="tanaka@example.com",
            ),
        ]

    def test_exact_match_returns_clinic_name(self):
        """医院コードに一致する最初の行の医院名を返す。"""
        records = self._records()
        self.assertEqual(
            sheets_client.lookup_clinic_name(records, "101"), "三浦歯科医院"
        )
        self.assertEqual(
            sheets_client.lookup_clinic_name(records, "102"), "山本歯科"
        )

    def test_multiple_rows_returns_first_clinic_name(self):
        """同じ医院コードの複数行がある場合、最初に見つかった値を返す。"""
        records = [
            sheets_client.MasterRecord(
                management_number="101-01",
                clinic_name="三浦歯科医院",
                participant_name="A",
                venue="",
                email="",
            ),
            sheets_client.MasterRecord(
                management_number="101-02",
                clinic_name="三浦歯科",  # 表記揺れ。順序的に拾われない
                participant_name="B",
                venue="",
                email="",
            ),
        ]
        self.assertEqual(
            sheets_client.lookup_clinic_name(records, "101"), "三浦歯科医院"
        )

    def test_no_match_returns_empty_string(self):
        """一致行がない（医院コードが未登録）→ 空文字。"""
        records = self._records()
        self.assertEqual(sheets_client.lookup_clinic_name(records, "999"), "")

    def test_empty_clinic_name_returns_empty_string(self):
        """一致行はあるが医院名が空 → 空文字。"""
        records = [
            sheets_client.MasterRecord(
                management_number="103-01",
                clinic_name="",
                participant_name="X",
                venue="",
                email="x@example.com",
            ),
        ]
        self.assertEqual(sheets_client.lookup_clinic_name(records, "103"), "")

    def test_management_number_without_hyphen_treated_as_clinic_code(self):
        """ハイフンなしの管理番号は医院コードとして直接扱われる。"""
        records = [
            sheets_client.MasterRecord(
                management_number="103",  # ハイフンなし
                clinic_name="あいうえ歯科",
                participant_name="X",
                venue="",
                email="x@example.com",
            ),
        ]
        self.assertEqual(sheets_client.lookup_clinic_name(records, "103"), "あいうえ歯科")


class TestNormalizePersonName(unittest.TestCase):
    """``_normalize_person_name`` の正規化規則を検証する。"""

    def test_strips_all_whitespace_variants(self):
        """半角/全角スペース・タブ・ノーブレークスペース全削除。"""
        self.assertEqual(
            sheets_client._normalize_person_name("山田 太郎"),
            sheets_client._normalize_person_name("山田太郎"),
        )
        self.assertEqual(
            sheets_client._normalize_person_name("山田　太郎"),  # 全角
            sheets_client._normalize_person_name("山田太郎"),
        )
        self.assertEqual(
            sheets_client._normalize_person_name("山田\t太郎"),
            sheets_client._normalize_person_name("山田太郎"),
        )

    def test_nfkc_handles_halfwidth_kana(self):
        """半角カナ → 全角カナ（NFKC）→ ひらがな。"""
        self.assertEqual(
            sheets_client._normalize_person_name("ﾔﾏﾀﾞ"),  # 半角
            sheets_client._normalize_person_name("ヤマダ"),
        )

    def test_katakana_normalized_to_hiragana(self):
        """カタカナとひらがなが同一視される。"""
        self.assertEqual(
            sheets_client._normalize_person_name("ヤマダタロウ"),
            sheets_client._normalize_person_name("やまだたろう"),
        )

    def test_lowercases_romaji(self):
        """ローマ字名の大文字小文字差を吸収。"""
        self.assertEqual(
            sheets_client._normalize_person_name("Taro Yamada"),
            sheets_client._normalize_person_name("taro yamada"),
        )

    def test_empty_input(self):
        self.assertEqual(sheets_client._normalize_person_name(""), "")


class TestLevenshteinDistance(unittest.TestCase):
    """``_levenshtein_distance`` の編集距離計算を検証する。"""

    def test_identical_strings_zero(self):
        self.assertEqual(
            sheets_client._levenshtein_distance("山田太郎", "山田太郎"), 0
        )

    def test_single_substitution_one(self):
        # 一文字違い（置換）
        self.assertEqual(
            sheets_client._levenshtein_distance("山田太郎", "山田太朗"), 1
        )

    def test_single_insertion_one(self):
        # 一文字追加
        self.assertEqual(
            sheets_client._levenshtein_distance("山田", "山田太"), 1
        )

    def test_single_deletion_one(self):
        # 一文字削除
        self.assertEqual(
            sheets_client._levenshtein_distance("山田太", "山田"), 1
        )

    def test_completely_different_strings(self):
        self.assertEqual(
            sheets_client._levenshtein_distance("ABC", "XYZ"), 3
        )

    def test_empty_strings(self):
        self.assertEqual(sheets_client._levenshtein_distance("", ""), 0)
        self.assertEqual(sheets_client._levenshtein_distance("abc", ""), 3)
        self.assertEqual(sheets_client._levenshtein_distance("", "abc"), 3)


class TestLookupEmailByClinicAndPerson(unittest.TestCase):
    """``lookup_email_by_clinic_and_person`` の医院 + 個人名突合挙動。

    完全一致 → ファジー一致（1 文字差）→ 不一致 の 3 段階。
    """

    def _records(self) -> list[sheets_client.MasterRecord]:
        return [
            sheets_client.MasterRecord(
                management_number="101-01",
                clinic_name="三浦歯科医院",
                participant_name="白川 蓮",
                venue="東京",
                email="ren@example.com",
            ),
            sheets_client.MasterRecord(
                management_number="101-02",
                clinic_name="三浦歯科医院",
                participant_name="鈴木 一郎",
                venue="東京",
                email="ichiro@example.com",
            ),
            sheets_client.MasterRecord(
                management_number="102-01",
                clinic_name="山本歯科",
                participant_name="田中 太郎",
                venue="大阪",
                email="tanaka@example.com",
            ),
        ]

    def test_exact_match_returns_email(self):
        """医院コードと個人名（正規化後完全一致）でメールを返す。"""
        records = self._records()
        self.assertEqual(
            sheets_client.lookup_email_by_clinic_and_person(
                records, "101", "白川 蓮"
            ),
            "ren@example.com",
        )
        self.assertEqual(
            sheets_client.lookup_email_by_clinic_and_person(
                records, "102", "田中 太郎"
            ),
            "tanaka@example.com",
        )

    def test_whitespace_difference_still_matches(self):
        """空白の有無は同一人物とみなす（正規化で吸収）。"""
        records = self._records()
        # PDF 抽出値にスペースが入っていない
        self.assertEqual(
            sheets_client.lookup_email_by_clinic_and_person(
                records, "101", "白川蓮"
            ),
            "ren@example.com",
        )
        # PDF 抽出値に全角スペースが入っている
        self.assertEqual(
            sheets_client.lookup_email_by_clinic_and_person(
                records, "101", "白川　蓮"
            ),
            "ren@example.com",
        )

    def test_katakana_hiragana_difference_still_matches(self):
        """カタカナとひらがな差は同一視される。"""
        records = [
            sheets_client.MasterRecord(
                management_number="101-01",
                clinic_name="クリニック",
                participant_name="ヤマダタロウ",  # マスターはカタカナ
                venue="",
                email="kana@example.com",
            ),
        ]
        self.assertEqual(
            sheets_client.lookup_email_by_clinic_and_person(
                records, "101", "やまだたろう"  # PDF からはひらがな
            ),
            "kana@example.com",
        )

    def test_fuzzy_match_one_char_difference(self):
        """1 文字違いの個人名はファジー一致でメールを返す。"""
        records = self._records()
        # "白川 蓮" → "白川 連"（蓮 → 連 の 1 文字違い）
        with self.assertLogs("jissen_comment", level="INFO") as log_ctx:
            email = sheets_client.lookup_email_by_clinic_and_person(
                records, "101", "白川 連"
            )
        self.assertEqual(email, "ren@example.com")
        joined = "\n".join(log_ctx.output)
        self.assertIn("ファジー一致", joined)

    def test_two_char_difference_does_not_match(self):
        """2 文字以上違うとファジー一致でも採用しない。"""
        records = self._records()
        # "白川 蓮" → "黒田 蓮"（白→黒, 川→田 の 2 文字違い）
        with self.assertLogs("jissen_comment", level="WARNING"):
            email = sheets_client.lookup_email_by_clinic_and_person(
                records, "101", "黒田 蓮"
            )
        self.assertEqual(email, "")

    def test_same_name_in_same_clinic_warns_and_picks_first(self):
        """同じ医院に同姓同名複数 → 警告 + 先頭採用。"""
        records = [
            sheets_client.MasterRecord(
                management_number="101-01",
                clinic_name="三浦歯科",
                participant_name="山田 太郎",
                venue="",
                email="first@example.com",
            ),
            sheets_client.MasterRecord(
                management_number="101-02",
                clinic_name="三浦歯科",
                participant_name="山田 太郎",  # 同姓同名
                venue="",
                email="second@example.com",
            ),
        ]
        with self.assertLogs("jissen_comment", level="WARNING") as log_ctx:
            email = sheets_client.lookup_email_by_clinic_and_person(
                records, "101", "山田 太郎"
            )
        self.assertEqual(email, "first@example.com")
        joined = "\n".join(log_ctx.output)
        self.assertIn("同姓同名", joined)

    def test_clinic_number_match_required(self):
        """医院コードが一致しない行のメールは引かない。"""
        records = self._records()
        # 個人名は 102 の田中太郎、医院は 101 を指定 → ヒットしない
        with self.assertLogs("jissen_comment", level="WARNING"):
            email = sheets_client.lookup_email_by_clinic_and_person(
                records, "101", "田中 太郎"
            )
        self.assertEqual(email, "")

    def test_no_match_returns_empty(self):
        """医院コードが未登録 → 空文字 + 警告。"""
        records = self._records()
        with self.assertLogs("jissen_comment", level="WARNING"):
            email = sheets_client.lookup_email_by_clinic_and_person(
                records, "999", "山田 太郎"
            )
        self.assertEqual(email, "")

    def test_empty_inputs_return_empty(self):
        records = self._records()
        self.assertEqual(
            sheets_client.lookup_email_by_clinic_and_person(records, "", "白川 蓮"),
            "",
        )
        self.assertEqual(
            sheets_client.lookup_email_by_clinic_and_person(records, "101", ""),
            "",
        )


class TestFuzzyMatchShortNameGuard(unittest.TestCase):
    """F-01 回帰防止: 1-2 文字 CJK 名は Levenshtein 距離 1 で他人を誤マッチ
    するリスクが高いため、ファジー一致をスキップして完全一致のみ採用する。
    """

    def _short_name_records(self) -> list[sheets_client.MasterRecord]:
        return [
            sheets_client.MasterRecord(
                management_number="101-01",
                clinic_name="森林歯科",
                participant_name="林",
                venue="東京",
                email="hayashi@example.com",
            ),
            sheets_client.MasterRecord(
                management_number="101-02",
                clinic_name="森林歯科",
                participant_name="森",
                venue="東京",
                email="mori@example.com",
            ),
        ]

    def test_one_char_target_does_not_fuzzy_match(self):
        """AI 抽出 '木' に対し、距離 1 で '林' / '森' に当たるが
        誤マッチ防止のためファジー一致を採用しない。空文字 + WARNING。"""
        records = self._short_name_records()
        with self.assertLogs("jissen_comment", level="WARNING") as cm:
            email = sheets_client.lookup_email_by_clinic_and_person(
                records, "101", "木"
            )
        self.assertEqual(email, "")
        self.assertTrue(
            any("短すぎてファジー一致をスキップ" in m for m in cm.output)
        )

    def test_two_char_target_does_not_fuzzy_match(self):
        """正規化後 2 文字も短すぎる扱い（完全一致のみ）。"""
        records = [
            sheets_client.MasterRecord(
                management_number="101-01",
                clinic_name="森林歯科",
                participant_name="山田",
                venue="東京",
                email="yamada@example.com",
            ),
        ]
        with self.assertLogs("jissen_comment", level="WARNING"):
            email = sheets_client.lookup_email_by_clinic_and_person(
                records, "101", "山口"  # 距離 1 だが 2 文字なので不採用
            )
        self.assertEqual(email, "")

    def test_three_char_target_still_fuzzy_matches(self):
        """3 文字以上は従来通りファジー一致が効く（誤マッチ率が十分低い）。"""
        records = [
            sheets_client.MasterRecord(
                management_number="101-01",
                clinic_name="森林歯科",
                participant_name="山田太郎",
                venue="東京",
                email="yamada@example.com",
            ),
        ]
        email = sheets_client.lookup_email_by_clinic_and_person(
            records, "101", "山田 太朗"  # 「郎」→「朗」距離 1
        )
        self.assertEqual(email, "yamada@example.com")

    def test_exact_match_still_works_for_short_names(self):
        """完全一致は短い名前でも有効（誤マッチでない）。"""
        records = self._short_name_records()
        email = sheets_client.lookup_email_by_clinic_and_person(
            records, "101", "林"
        )
        self.assertEqual(email, "hayashi@example.com")


class TestClinicNumberNormalization(unittest.TestCase):
    """F-02 回帰防止: 医院番号のゼロパディング桁数違いを吸収する。
    PDF 側 ``"00101"`` と マスター側 ``"101"`` を同一医院として扱う。
    """

    def test_normalize_clinic_number_strips_leading_zeros(self):
        self.assertEqual(sheets_client._normalize_clinic_number("001"), "1")
        self.assertEqual(sheets_client._normalize_clinic_number("00101"), "101")
        self.assertEqual(sheets_client._normalize_clinic_number("101"), "101")
        self.assertEqual(sheets_client._normalize_clinic_number(""), "")
        # 全部ゼロ → "0"（lstrip 後の空文字を避ける）
        self.assertEqual(sheets_client._normalize_clinic_number("000"), "0")

    def test_lookup_clinic_name_matches_across_padding(self):
        """マスターに '101-01' があり、PDF 側が '00101' でも医院名がヒットする。"""
        records = [
            sheets_client.MasterRecord(
                management_number="101-01",
                clinic_name="三浦歯科医院",
                participant_name="山田太郎",
                venue="東京",
                email="yamada@example.com",
            ),
        ]
        self.assertEqual(
            sheets_client.lookup_clinic_name(records, "00101"),
            "三浦歯科医院",
        )
        # 逆方向: マスター 5 桁、PDF 3 桁
        records2 = [
            sheets_client.MasterRecord(
                management_number="00101-01",
                clinic_name="三浦歯科医院",
                participant_name="山田太郎",
                venue="東京",
                email="yamada@example.com",
            ),
        ]
        self.assertEqual(
            sheets_client.lookup_clinic_name(records2, "101"),
            "三浦歯科医院",
        )

    def test_lookup_email_matches_across_padding(self):
        """メール lookup も桁数違いを吸収する。"""
        records = [
            sheets_client.MasterRecord(
                management_number="101-01",
                clinic_name="三浦歯科医院",
                participant_name="山田太郎",
                venue="東京",
                email="yamada@example.com",
            ),
        ]
        email = sheets_client.lookup_email_by_clinic_and_person(
            records, "00101", "山田太郎"
        )
        self.assertEqual(email, "yamada@example.com")


class TestEnsureMasterSheetWarnsOnCreation(unittest.TestCase):
    """F-05/F-11 回帰防止: マスタータブを **新規作成** した瞬間に
    INFO ではなく WARNING を出して、運用者にデータ未投入を視認させる。
    """

    def test_returns_true_when_creating_new_tab(self):
        """既存タブに含まれない名前を渡すと、addSheet が走り True を返す。"""
        service = _build_service_with_sheets(existing_sheet_titles=[])
        was_created = sheets_client._ensure_sheet_with_header(
            service, "SHEET_ID", "参加者マスター", ["管理番号", "医院名"]
        )
        self.assertTrue(was_created)
        service.spreadsheets.return_value.batchUpdate.assert_called_once()

    def test_returns_false_when_reusing_existing_tab(self):
        """既存タブを再利用するときは False を返し、addSheet は呼ばれない。"""
        service = _build_service_with_sheets(
            existing_sheet_titles=["参加者マスター"]
        )
        was_created = sheets_client._ensure_sheet_with_header(
            service, "SHEET_ID", "参加者マスター", ["管理番号", "医院名"]
        )
        self.assertFalse(was_created)
        service.spreadsheets.return_value.batchUpdate.assert_not_called()

    def test_ensure_master_sheet_emits_warning_on_creation(self):
        """マスタータブを新規作成したら WARNING を 1 件以上出す。"""
        service = _build_service_with_sheets(existing_sheet_titles=[])
        with self.assertLogs("jissen_comment", level="WARNING") as cm:
            was_created = sheets_client._ensure_master_sheet(
                service, "SHEET_ID", "参加者マスター"
            )
        self.assertTrue(was_created)
        self.assertTrue(
            any(
                "参加者マスタータブ" in m and "新規作成" in m and "WARNING" in m
                for m in cm.output
            )
        )

    def test_ensure_master_sheet_no_warning_when_existing(self):
        """既存タブ再利用なら WARNING を出さない（INFO のみ）。"""
        service = _build_service_with_sheets(
            existing_sheet_titles=["参加者マスター"]
        )
        # WARNING 以上のログが出ないことを assertNoLogs で確認（Python 3.10+）
        # 互換のため try/except で吸収
        try:
            with self.assertNoLogs("jissen_comment", level="WARNING"):
                was_created = sheets_client._ensure_master_sheet(
                    service, "SHEET_ID", "参加者マスター"
                )
            self.assertFalse(was_created)
        except AttributeError:
            # Python 3.9 以下: assertNoLogs が無いので INFO ハンドラを直接見る
            was_created = sheets_client._ensure_master_sheet(
                service, "SHEET_ID", "参加者マスター"
            )
            self.assertFalse(was_created)


class TestSheetsWriteThrottle(unittest.TestCase):
    """P-023: Sheets API rate limit (60/min) を能動的に抑える throttle ヘルパー。"""

    def setUp(self):
        # 各テスト前にグローバルキューをクリア
        sheets_client._SHEETS_WRITE_TIMES.clear()

    def test_under_threshold_does_not_sleep(self):
        """閾値未満なら sleep しない。"""
        with patch("src.sheets_client.time.sleep") as mock_sleep:
            for _ in range(10):
                sheets_client._throttle_sheets_write()
        mock_sleep.assert_not_called()

    def test_above_threshold_sleeps(self):
        """閾値（50）に達したら次の write は sleep する。"""
        # 直近 50 件の write を直前タイムスタンプで埋める
        import time as _time
        now = _time.monotonic()
        for _ in range(sheets_client._SHEETS_MAX_WRITES_PER_60S):
            sheets_client._SHEETS_WRITE_TIMES.append(now)
        with patch("src.sheets_client.time.sleep") as mock_sleep:
            sheets_client._throttle_sheets_write()
        mock_sleep.assert_called_once()
        # sleep 引数は正の数（60 - 経過時間 + 0.5）
        sleep_for = mock_sleep.call_args.args[0]
        self.assertGreater(sleep_for, 0)

    def test_old_writes_expire(self):
        """60 秒以上前の write はキューから除去される。"""
        # 100 秒前の write を 100 件入れても、throttle 後は新しい 1 件だけ残る。
        # 注意: throttle は time.monotonic()（絶対値は環境依存。起動直後の CI ランナー
        # では 60 未満になり得る）で経過判定する。「大昔」を絶対値 0.0 で表すと
        # monotonic()<60 の環境では「60 秒以上前」と判定されず失敗するため、必ず
        # 現在からの相対値で古さを表す（P-025: monotonic 前提の時刻はテストでも相対化）。
        import time as _time
        old = _time.monotonic() - 100.0
        for _ in range(100):
            sheets_client._SHEETS_WRITE_TIMES.append(old)
        with patch("src.sheets_client.time.sleep") as mock_sleep:
            sheets_client._throttle_sheets_write()
        # 古い記録は全部捨てられ、新しい 1 件だけ残る
        self.assertEqual(len(sheets_client._SHEETS_WRITE_TIMES), 1)
        mock_sleep.assert_not_called()


class TestReadMasterRecordsEmptyWarning(unittest.TestCase):
    """F-05/F-11 回帰防止: ``read_master_records`` が 0 件返すケースで、
    タブ新規作成時 OR ヘッダーのみ既存タブ時に WARNING を出すこと。
    """

    @patch.object(sheets_client, "get_sheets_service")
    @patch.object(sheets_client, "SPREADSHEET_ID", "SHEET_ID")
    def test_warning_on_freshly_created_tab(self, mock_get_service):
        """新規作成タブ → _ensure_master_sheet が WARNING を出す。"""
        service = _build_service_with_sheets(existing_sheet_titles=[])
        # values.get は完全空 → ``values`` キー無し
        service.spreadsheets.return_value.values.return_value.get.return_value\
            .execute.return_value = {}
        mock_get_service.return_value = service
        with self.assertLogs("jissen_comment", level="WARNING") as cm:
            records = sheets_client.read_master_records(sheet_name="参加者マスター")
        self.assertEqual(records, [])
        self.assertTrue(
            any("新規作成" in m for m in cm.output),
            f"WARNING with 新規作成 not found in: {cm.output}",
        )

    @patch.object(sheets_client, "get_sheets_service")
    @patch.object(sheets_client, "SPREADSHEET_ID", "SHEET_ID")
    def test_warning_on_existing_empty_tab(self, mock_get_service):
        """既存タブだがヘッダーのみ（0 件）→ WARNING を出す。"""
        service = _build_service_with_sheets(
            existing_sheet_titles=["参加者マスター"]
        )
        # values.get はヘッダー行のみ
        service.spreadsheets.return_value.values.return_value.get.return_value\
            .execute.return_value = {
                "values": [["管理番号", "医院名", "参加者名", "申し込み会場", "メールアドレス"]]
            }
        mock_get_service.return_value = service
        with self.assertLogs("jissen_comment", level="WARNING") as cm:
            records = sheets_client.read_master_records(sheet_name="参加者マスター")
        self.assertEqual(records, [])
        self.assertTrue(
            any("0 件" in m or "0件" in m for m in cm.output),
            f"WARNING about 0 records not found in: {cm.output}",
        )


class TestAppendCompletionMarker(unittest.TestCase):
    """``append_completion_marker`` は出力一覧シート末尾に「完了」行を 1 行追加する。"""

    @patch("src.sheets_client.get_sheets_service")
    def test_appends_completion_row_with_summary(self, mock_get_service):
        service = _build_service_with_sheets(["出力一覧"])
        service.spreadsheets.return_value.values.return_value.get.return_value.execute.return_value = {
            "values": [["管理番号"]]  # ヘッダーあり
        }
        mock_get_service.return_value = service

        sheets_client.append_completion_marker(
            spreadsheet_id="sid",
            sheet_name="出力一覧",
            completed_at="2026-05-29 04:30:00",
            summary="成功 42件 / エラー 0件",
        )

        append_call = service.spreadsheets.return_value.values.return_value.append.call_args
        self.assertIn("出力一覧!A:F", append_call.kwargs["range"])
        row = append_call.kwargs["body"]["values"][0]
        # 1 列目は「完了」、2 列目は日時、3 列目はサマリー、残りは空
        self.assertEqual(row[0], "完了")
        self.assertEqual(row[1], "2026-05-29 04:30:00")
        self.assertEqual(row[2], "成功 42件 / エラー 0件")
        self.assertEqual(row[3:], ["", "", ""])

    @patch("src.sheets_client.get_sheets_service")
    def test_passes_num_retries(self, mock_get_service):
        service = _build_service_with_sheets(["出力一覧"])
        service.spreadsheets.return_value.values.return_value.get.return_value.execute.return_value = {
            "values": [["管理番号"]]
        }
        mock_get_service.return_value = service

        sheets_client.append_completion_marker(
            spreadsheet_id="sid",
            sheet_name="出力一覧",
            completed_at="2026-05-29 04:30:00",
        )

        append_call = service.spreadsheets.return_value.values.return_value.append.return_value.execute.call_args
        self.assertEqual(append_call.kwargs["num_retries"], 5)

    @patch("src.sheets_client.get_sheets_service")
    def test_raises_when_spreadsheet_id_missing(self, mock_get_service):
        with patch("src.sheets_client.SPREADSHEET_ID", ""):
            with self.assertRaises(ValueError):
                sheets_client.append_completion_marker(sheet_name="出力一覧")


class TestListMasterSheetTabs(unittest.TestCase):
    """``list_master_sheet_tabs``: ``参加者マスター(...)`` 形式のタブ名を列挙。"""

    def _service_with_titles(self, titles: list[str]) -> MagicMock:
        service = MagicMock()
        service.spreadsheets.return_value.get.return_value.execute.return_value = {
            "sheets": [
                {"properties": {"title": t}} for t in titles
            ]
        }
        return service

    @patch("src.sheets_client.get_sheets_service")
    def test_returns_only_master_format_tabs(self, mock_get_service):
        mock_get_service.return_value = self._service_with_titles([
            "シート1",
            "出力一覧",
            "参加者マスター(新人育成塾)",
            "参加者マスター(経営塾ベーシック)",
            "参加者マスター",  # 括弧なし → 除外
            "メールアドレス一覧",
        ])
        result = sheets_client.list_master_sheet_tabs("ssid")
        self.assertEqual(
            sorted(result),
            sorted(["参加者マスター(新人育成塾)", "参加者マスター(経営塾ベーシック)"]),
        )

    @patch("src.sheets_client.get_sheets_service")
    def test_returns_empty_when_no_master_tabs(self, mock_get_service):
        mock_get_service.return_value = self._service_with_titles([
            "シート1", "出力一覧",
        ])
        self.assertEqual(sheets_client.list_master_sheet_tabs("ssid"), [])

    @patch("src.sheets_client.get_sheets_service")
    def test_uses_num_retries_on_metadata_call(self, mock_get_service):
        """Sheets metadata 取得も他の API 同様に一過性エラーをリトライする。"""
        service = self._service_with_titles([])
        mock_get_service.return_value = service
        sheets_client.list_master_sheet_tabs("ssid")
        execute_call = service.spreadsheets.return_value.get.return_value.execute
        self.assertEqual(execute_call.call_args.kwargs["num_retries"], 5)

    @patch("src.sheets_client.get_sheets_service")
    def test_raises_when_spreadsheet_id_missing(self, mock_get_service):
        with patch("src.sheets_client.SPREADSHEET_ID", ""):
            with self.assertRaises(ValueError):
                sheets_client.list_master_sheet_tabs()


class TestBatchStateRecords(unittest.TestCase):
    """``append_batch_record`` / ``get_open_batch_ids``: 未回収バッチの検知。

    GHA ジョブ kill 後の ``step=all`` 再実行が投入済みバッチを再投入（二重
    課金）しないための、スプレッドシート永続の状態遷移ログ。
    """

    def _service_with_state_rows(self, rows: list[list[str]]) -> MagicMock:
        service = MagicMock()
        service.spreadsheets.return_value.get.return_value.execute.return_value = {
            "sheets": [{"properties": {"title": "_バッチ管理"}}]
        }
        service.spreadsheets.return_value.values.return_value.get.return_value \
            .execute.return_value = {"values": rows}
        return service

    @patch("src.sheets_client.get_sheets_service")
    def test_open_when_submitted_without_done(self, mock_get_service):
        mock_get_service.return_value = self._service_with_state_rows([
            ["2026-01-01 00:00:00", "出力A", "msgbatch_1", "投入済み"],
        ])
        self.assertEqual(
            sheets_client.get_open_batch_ids("出力A", spreadsheet_id="ssid"),
            ["msgbatch_1"],
        )

    @patch("src.sheets_client.get_sheets_service")
    def test_closed_when_done_recorded(self, mock_get_service):
        mock_get_service.return_value = self._service_with_state_rows([
            ["t1", "出力A", "msgbatch_1", "投入済み"],
            ["t2", "出力A", "msgbatch_1", "回収完了"],
        ])
        self.assertEqual(
            sheets_client.get_open_batch_ids("出力A", spreadsheet_id="ssid"),
            [],
        )

    @patch("src.sheets_client.get_sheets_service")
    def test_closed_when_expired_recorded(self, mock_get_service):
        mock_get_service.return_value = self._service_with_state_rows([
            ["t1", "出力A", "msgbatch_1", "投入済み"],
            ["t2", "出力A", "msgbatch_1", "期限切れ(結果喪失)"],
        ])
        self.assertEqual(
            sheets_client.get_open_batch_ids("出力A", spreadsheet_id="ssid"),
            [],
        )

    @patch("src.sheets_client.get_sheets_service")
    def test_filters_by_target_sheet(self, mock_get_service):
        """別フォルダ（別出力シート）のバッチは対象外。"""
        mock_get_service.return_value = self._service_with_state_rows([
            ["t1", "出力A", "msgbatch_a", "投入済み"],
            ["t2", "出力B", "msgbatch_b", "投入済み"],
        ])
        self.assertEqual(
            sheets_client.get_open_batch_ids("出力B", spreadsheet_id="ssid"),
            ["msgbatch_b"],
        )

    @patch("src.sheets_client.get_sheets_service")
    def test_multiple_open_batches_preserve_order(self, mock_get_service):
        """チャンク分割で複数バッチが未回収の場合、投入順で返す。"""
        mock_get_service.return_value = self._service_with_state_rows([
            ["t1", "出力A", "msgbatch_1", "投入済み"],
            ["t2", "出力A", "msgbatch_2", "投入済み"],
            ["t3", "出力A", "msgbatch_3", "投入済み"],
            ["t4", "出力A", "msgbatch_2", "回収完了"],
        ])
        self.assertEqual(
            sheets_client.get_open_batch_ids("出力A", spreadsheet_id="ssid"),
            ["msgbatch_1", "msgbatch_3"],
        )

    @patch("src.sheets_client.get_sheets_service")
    def test_duplicate_submitted_rows_deduped_preserving_first_order(
        self, mock_get_service,
    ):
        """append-only ログに同一バッチの重複投入済み行があっても、初出順で
        1 回だけ返す（dict ベースの重複除去が list 版と同じ挙動であることの
        回帰確認、Phase 23 PR-1c）。"""
        mock_get_service.return_value = self._service_with_state_rows([
            ["t1", "出力A", "msgbatch_1", "投入済み"],
            ["t2", "出力A", "msgbatch_2", "投入済み"],
            ["t3", "出力A", "msgbatch_1", "投入済み"],  # 重複行
            ["t4", "出力A", "msgbatch_3", "投入済み"],
        ])
        self.assertEqual(
            sheets_client.get_open_batch_ids("出力A", spreadsheet_id="ssid"),
            ["msgbatch_1", "msgbatch_2", "msgbatch_3"],
        )

    @patch("src.sheets_client.get_sheets_service")
    def test_empty_when_state_sheet_missing(self, mock_get_service):
        """``_バッチ管理`` タブ未作成（初回運用）なら空リスト。"""
        service = MagicMock()
        service.spreadsheets.return_value.get.return_value.execute.return_value = {
            "sheets": [{"properties": {"title": "出力一覧"}}]
        }
        mock_get_service.return_value = service
        self.assertEqual(
            sheets_client.get_open_batch_ids("出力A", spreadsheet_id="ssid"),
            [],
        )

    @patch("src.sheets_client._throttle_sheets_write")
    @patch("src.sheets_client.get_sheets_service")
    def test_append_batch_record_appends_row(
        self, mock_get_service, mock_throttle,
    ):
        service = self._service_with_state_rows([])
        mock_get_service.return_value = service
        sheets_client.append_batch_record(
            "出力A", "msgbatch_9", sheets_client.BATCH_STATE_SUBMITTED,
            spreadsheet_id="ssid",
        )
        append_call = service.spreadsheets.return_value.values.return_value.append
        row = append_call.call_args.kwargs["body"]["values"][0]
        self.assertEqual(row[1], "出力A")
        self.assertEqual(row[2], "msgbatch_9")
        self.assertEqual(row[3], "投入済み")
        mock_throttle.assert_called_once()


class TestGetRecordedAttachmentNames(unittest.TestCase):
    """``get_recorded_attachment_names``: 添付資料の記録済みマーカー取得。"""

    def _service(self, d_values: list[list[str]], has_sheet: bool = True) -> MagicMock:
        service = MagicMock()
        titles = ["出力一覧"] if has_sheet else ["別シート"]
        service.spreadsheets.return_value.get.return_value.execute.return_value = {
            "sheets": [{"properties": {"title": t}} for t in titles]
        }
        service.spreadsheets.return_value.values.return_value.get.return_value \
            .execute.return_value = {"values": d_values}
        return service

    @patch("src.sheets_client.get_sheets_service")
    def test_returns_only_attachment_markers(self, mock_get_service):
        mock_get_service.return_value = self._service([
            ["実践事例タイトル"],
            ["【添付資料】001-01-0補足.pdf"],
            [""],
            ["【添付資料】002-02-0資料.pdf"],
        ])
        result = sheets_client.get_recorded_attachment_names(
            spreadsheet_id="ssid", sheet_name="出力一覧",
        )
        self.assertEqual(result, {
            "【添付資料】001-01-0補足.pdf",
            "【添付資料】002-02-0資料.pdf",
        })

    @patch("src.sheets_client.get_sheets_service")
    def test_empty_when_sheet_missing(self, mock_get_service):
        mock_get_service.return_value = self._service([], has_sheet=False)
        result = sheets_client.get_recorded_attachment_names(
            spreadsheet_id="ssid", sheet_name="出力一覧",
        )
        self.assertEqual(result, set())


class TestLookupParticipantByManagementNumber(unittest.TestCase):
    """``lookup_participant_by_management_number``: PDF 管理番号 → マスター行。"""

    def _record(self, mgmt: str, clinic: str = "山田歯科", person: str = "田中太郎"):
        return sheets_client.MasterRecord(
            management_number=mgmt, clinic_name=clinic,
            participant_name=person, venue="東京", email="a@example.com",
        )

    def test_prefix_match_pdf_mgmt_to_master_individual(self):
        """PDF ``001-01-0``（3セグメント）→ マスター ``001-01``（個人単位）。"""
        records = [self._record("001-01"), self._record("002-02", person="別人")]
        found = sheets_client.lookup_participant_by_management_number(
            records, "001-01-0",
        )
        assert found is not None
        self.assertEqual(found.participant_name, "田中太郎")

    def test_exact_match(self):
        records = [self._record("001-01")]
        found = sheets_client.lookup_participant_by_management_number(
            records, "001-01",
        )
        assert found is not None
        self.assertEqual(found.management_number, "001-01")

    def test_longest_match_wins(self):
        """``001`` と ``001-01`` の両方が前方一致するとき、より具体的な行を採用。"""
        records = [
            self._record("001", person="医院代表"),
            self._record("001-01", person="田中太郎"),
        ]
        found = sheets_client.lookup_participant_by_management_number(
            records, "001-01-0",
        )
        assert found is not None
        self.assertEqual(found.participant_name, "田中太郎")

    def test_no_false_prefix_match(self):
        """``001-01`` は ``001-011-0`` にマッチしない（セグメント境界）。"""
        records = [self._record("001-01")]
        self.assertIsNone(
            sheets_client.lookup_participant_by_management_number(
                records, "001-011-0",
            )
        )

    def test_none_when_not_found_or_empty(self):
        records = [self._record("001-01")]
        self.assertIsNone(
            sheets_client.lookup_participant_by_management_number(records, "999-99-9")
        )
        self.assertIsNone(
            sheets_client.lookup_participant_by_management_number(records, "")
        )


class TestEnsuredSheetsCache(unittest.TestCase):
    """``_ensure_sheet_with_header`` のプロセス内キャッシュ。

    1000 行の追記で 1 行ごとに read×2（メタ + ヘッダー）を発行すると Sheets の
    read quota（60/分・throttle 対象外）を食い潰すため、ensure 済みシートは
    2 回目以降 API を呼ばない。
    """

    def _service(self) -> MagicMock:
        service = MagicMock()
        service.spreadsheets.return_value.get.return_value.execute.return_value = {
            "sheets": [{"properties": {"title": "出力一覧"}}]
        }
        service.spreadsheets.return_value.values.return_value.get.return_value \
            .execute.return_value = {"values": [["ヘッダー"]]}
        return service

    def test_second_ensure_skips_api_calls(self):
        service = self._service()
        first = sheets_client._ensure_sheet_with_header(
            service, "ssid", "出力一覧", ["A", "B"],
        )
        calls_after_first = service.spreadsheets.return_value.get.call_count
        second = sheets_client._ensure_sheet_with_header(
            service, "ssid", "出力一覧", ["A", "B"],
        )
        self.assertFalse(first)
        self.assertFalse(second)
        # 2 回目はメタ取得 API を一切呼ばない
        self.assertEqual(
            service.spreadsheets.return_value.get.call_count, calls_after_first,
        )

    def test_cache_is_per_sheet(self):
        service = self._service()
        sheets_client._ensure_sheet_with_header(service, "ssid", "出力一覧", ["A"])
        calls_after_first = service.spreadsheets.return_value.get.call_count
        sheets_client._ensure_sheet_with_header(service, "ssid", "別シート", ["A"])
        self.assertGreater(
            service.spreadsheets.return_value.get.call_count, calls_after_first,
        )


if __name__ == "__main__":
    unittest.main()
