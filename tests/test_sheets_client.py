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
# メールアドレス一覧シート（Gmail 下書き用ルックアップ表）
# ─────────────────────────────────────────────────────────────────────


def _build_email_sheet_service(
    existing_sheet_titles: list[str],
    values: list[list[str]] | None,
) -> MagicMock:
    """メールシート読み取り用の Sheets サービス mock を作る。

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
    # その後 read_email_records が A:E で values.get を呼ぶ。両方を同じ
    # mock で返すと「ヘッダーは既にある」「データもこの内容」と見せかけられる。
    get_result: dict = {}
    if values is not None:
        get_result["values"] = values
    service.spreadsheets.return_value.values.return_value.get.return_value.execute.return_value = get_result
    return service


class TestReadEmailRecords(unittest.TestCase):
    """``read_email_records``（メールアドレスシート読み取り）。"""

    @patch("src.sheets_client.get_sheets_service")
    def test_reads_records_from_existing_sheet(self, mock_get_service):
        """既存シートから複数行を読み取り EmailRecord のリストを返す。"""
        service = _build_email_sheet_service(
            ["メールアドレス一覧"],
            [
                ["医院番号", "医院名", "個人名", "個人メール", "医院メール"],
                ["001", "三浦歯科医院", "白川 蓮", "ren@example.com", "clinic@example.com"],
                ["002", "山本歯科", "田中 太郎", "tanaka@example.com", "ymt@example.com"],
            ],
        )
        mock_get_service.return_value = service

        records = sheets_client.read_email_records(
            spreadsheet_id="sid", sheet_name="メールアドレス一覧",
        )

        self.assertEqual(len(records), 2)
        self.assertEqual(records[0].clinic_number, "001")
        self.assertEqual(records[0].clinic_name, "三浦歯科医院")
        self.assertEqual(records[0].person_name, "白川 蓮")
        self.assertEqual(records[0].person_email, "ren@example.com")
        self.assertEqual(records[0].clinic_email, "clinic@example.com")
        self.assertEqual(records[1].clinic_number, "002")
        self.assertEqual(records[1].person_email, "tanaka@example.com")

    @patch("src.sheets_client.get_sheets_service")
    def test_creates_sheet_when_not_exists_and_returns_empty(
        self, mock_get_service
    ):
        """シートが存在しなければ自動作成し（ヘッダー書き込み）空リストを返す。"""
        service = _build_email_sheet_service(["Sheet1"], None)
        mock_get_service.return_value = service

        records = sheets_client.read_email_records(
            spreadsheet_id="sid", sheet_name="メールアドレス一覧",
        )

        # addSheet が呼ばれた（シート作成）
        batch_update_call = service.spreadsheets.return_value.batchUpdate.call_args
        add_sheet_request = batch_update_call.kwargs["body"]["requests"][0]["addSheet"]
        self.assertEqual(
            add_sheet_request["properties"]["title"], "メールアドレス一覧"
        )
        # ヘッダー書き込みが行われた（5 列）
        update_call = service.spreadsheets.return_value.values.return_value.update.call_args
        self.assertIn("メールアドレス一覧!A1:E1", update_call.kwargs["range"])
        self.assertEqual(
            update_call.kwargs["body"]["values"][0],
            ["医院番号", "医院名", "個人名", "個人メール", "医院メール"],
        )
        # 空リストが返る（シート新規作成直後はデータ 0 件）
        self.assertEqual(records, [])

    @patch("src.sheets_client.get_sheets_service")
    def test_invalid_person_email_is_masked_with_warning(self, mock_get_service):
        """形式不正の個人メールは空扱い + warning ログ。"""
        service = _build_email_sheet_service(
            ["メールアドレス一覧"],
            [
                ["医院番号", "医院名", "個人名", "個人メール", "医院メール"],
                ["001", "三浦歯科", "白川 蓮", "not-an-email", "clinic@example.com"],
            ],
        )
        mock_get_service.return_value = service

        with self.assertLogs("jissen_comment", level="WARNING") as log_ctx:
            records = sheets_client.read_email_records(
                spreadsheet_id="sid", sheet_name="メールアドレス一覧",
            )

        self.assertEqual(len(records), 1)
        # 個人メールは形式不正のため空扱い、医院メールはそのまま
        self.assertEqual(records[0].person_email, "")
        self.assertEqual(records[0].clinic_email, "clinic@example.com")
        joined = "\n".join(log_ctx.output)
        self.assertIn("個人メール", joined)
        self.assertIn("形式が不正", joined)

    @patch("src.sheets_client.get_sheets_service")
    def test_invalid_clinic_email_is_masked_with_warning(self, mock_get_service):
        """形式不正の医院メールも空扱い + warning ログ。"""
        service = _build_email_sheet_service(
            ["メールアドレス一覧"],
            [
                ["医院番号", "医院名", "個人名", "個人メール", "医院メール"],
                ["001", "三浦歯科", "白川 蓮", "ren@example.com", "broken-clinic"],
            ],
        )
        mock_get_service.return_value = service

        with self.assertLogs("jissen_comment", level="WARNING") as log_ctx:
            records = sheets_client.read_email_records(
                spreadsheet_id="sid", sheet_name="メールアドレス一覧",
            )

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].person_email, "ren@example.com")
        self.assertEqual(records[0].clinic_email, "")
        joined = "\n".join(log_ctx.output)
        self.assertIn("医院メール", joined)

    @patch("src.sheets_client.get_sheets_service")
    def test_empty_rows_are_skipped(self, mock_get_service):
        """医院番号も個人名も空の行は読み飛ばす。"""
        service = _build_email_sheet_service(
            ["メールアドレス一覧"],
            [
                ["医院番号", "医院名", "個人名", "個人メール", "医院メール"],
                ["001", "三浦歯科", "白川 蓮", "ren@example.com", ""],
                ["", "", "", "", ""],  # 空行
                ["002", "山本歯科", "田中 太郎", "tanaka@example.com", ""],
            ],
        )
        mock_get_service.return_value = service

        records = sheets_client.read_email_records(
            spreadsheet_id="sid", sheet_name="メールアドレス一覧",
        )

        self.assertEqual(len(records), 2)
        self.assertEqual(records[0].clinic_number, "001")
        self.assertEqual(records[1].clinic_number, "002")

    @patch("src.sheets_client.get_sheets_service")
    def test_passes_num_retries(self, mock_get_service):
        """全 ``execute()`` に num_retries=GOOGLE_API_NUM_RETRIES が渡る（P-017）。"""
        service = _build_email_sheet_service(
            ["メールアドレス一覧"],
            [
                ["医院番号", "医院名", "個人名", "個人メール", "医院メール"],
                ["001", "三浦歯科", "白川 蓮", "ren@example.com", ""],
            ],
        )
        mock_get_service.return_value = service

        sheets_client.read_email_records(
            spreadsheet_id="sid", sheet_name="メールアドレス一覧",
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
        """sheet_name 省略時は EMAIL_SHEET_NAME にフォールバックする。"""
        service = _build_email_sheet_service(
            [sheets_client.EMAIL_SHEET_NAME],
            [
                ["医院番号", "医院名", "個人名", "個人メール", "医院メール"],
                ["001", "A", "B", "b@example.com", "a@example.com"],
            ],
        )
        mock_get_service.return_value = service

        sheets_client.read_email_records(spreadsheet_id="sid")

        get_call = service.spreadsheets.return_value.values.return_value.get.call_args
        self.assertIn(sheets_client.EMAIL_SHEET_NAME, get_call.kwargs["range"])

    def test_raises_when_spreadsheet_id_missing(self):
        """``SPREADSHEET_ID`` 未設定なら ValueError。"""
        with patch.object(sheets_client, "SPREADSHEET_ID", ""):
            with self.assertRaises(ValueError):
                sheets_client.read_email_records()


class TestLookupEmail(unittest.TestCase):
    """``lookup_email`` のルックアップ・フォールバックロジック。"""

    def _records(self) -> list[sheets_client.EmailRecord]:
        return [
            sheets_client.EmailRecord(
                clinic_number="001",
                clinic_name="三浦歯科医院",
                person_name="白川 蓮",
                person_email="ren@example.com",
                clinic_email="miura@example.com",
            ),
            sheets_client.EmailRecord(
                clinic_number="001",
                clinic_name="三浦歯科医院",
                person_name="鈴木 一郎",
                person_email="",
                clinic_email="miura@example.com",
            ),
            sheets_client.EmailRecord(
                clinic_number="002",
                clinic_name="山本歯科",
                person_name="田中 太郎",
                person_email="",
                clinic_email="yamamoto@example.com",
            ),
            sheets_client.EmailRecord(
                clinic_number="003",
                clinic_name="佐藤医院",
                person_name="佐藤 二郎",
                person_email="",
                clinic_email="",
            ),
        ]

    def test_exact_match_with_person_email_returns_person_and_clinic(self):
        """完全一致で個人メールあり → (個人メール, 医院メール)。"""
        records = self._records()
        to_email, cc_email = sheets_client.lookup_email(records, "001", "白川 蓮")
        self.assertEqual(to_email, "ren@example.com")
        self.assertEqual(cc_email, "miura@example.com")

    def test_exact_match_with_person_email_no_clinic_returns_person_only(self):
        """完全一致で個人メールあり・医院メール空 → (個人メール, "")。"""
        records = [
            sheets_client.EmailRecord(
                clinic_number="010",
                clinic_name="X 医院",
                person_name="X 個人",
                person_email="x@example.com",
                clinic_email="",  # 医院メール空
            ),
        ]
        to_email, cc_email = sheets_client.lookup_email(records, "010", "X 個人")
        self.assertEqual(to_email, "x@example.com")
        self.assertEqual(cc_email, "")

    def test_exact_match_with_empty_person_email_uses_clinic(self):
        """完全一致で個人メール空、医院メールあり → (医院メール, "")。"""
        records = self._records()
        to_email, cc_email = sheets_client.lookup_email(records, "001", "鈴木 一郎")
        self.assertEqual(to_email, "miura@example.com")
        self.assertEqual(cc_email, "")

    def test_no_exact_match_falls_back_to_clinic_email_from_other_row(self):
        """完全一致なし → 同じ医院番号の他の行から医院メールを拾う → (医院メール, "")。"""
        records = self._records()
        # 医院番号 001 の他の個人 → メールシートに直接の行がない人物
        to_email, cc_email = sheets_client.lookup_email(records, "001", "未登録 個人")
        self.assertEqual(to_email, "miura@example.com")
        self.assertEqual(cc_email, "")

    def test_no_exact_match_no_clinic_email_returns_empty(self):
        """完全一致なし・同じ医院番号の他の行も医院メール空 → ("", "")。"""
        records = self._records()
        # 医院番号 003 は佐藤医院があるが、その行の医院メールも空。
        to_email, cc_email = sheets_client.lookup_email(records, "003", "未登録 X")
        self.assertEqual(to_email, "")
        self.assertEqual(cc_email, "")

    def test_no_match_for_unknown_clinic_returns_empty(self):
        """医院番号が一切ない → ("", "")。"""
        records = self._records()
        to_email, cc_email = sheets_client.lookup_email(records, "999", "未登録 個人")
        self.assertEqual(to_email, "")
        self.assertEqual(cc_email, "")

    def test_all_empty_records_returns_empty(self):
        """records 全部空メール → ("", "")。"""
        records = [
            sheets_client.EmailRecord(
                clinic_number="001",
                clinic_name="A",
                person_name="P",
                person_email="",
                clinic_email="",
            ),
        ]
        to_email, cc_email = sheets_client.lookup_email(records, "001", "P")
        self.assertEqual(to_email, "")
        self.assertEqual(cc_email, "")

    def test_empty_records_list_returns_empty(self):
        """records が空リスト → ("", "")。"""
        to_email, cc_email = sheets_client.lookup_email([], "001", "X")
        self.assertEqual(to_email, "")
        self.assertEqual(cc_email, "")


if __name__ == "__main__":
    unittest.main()
