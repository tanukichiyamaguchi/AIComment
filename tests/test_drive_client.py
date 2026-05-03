"""drive_client モジュールの新規追加関数のテスト（共有ドライブ対応含む）。"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

from src import drive_client


class TestFindOrCreateFolder(unittest.TestCase):

    def test_returns_existing_folder_id_if_found(self):
        service = MagicMock()
        service.files.return_value.list.return_value.execute.return_value = {
            "files": [{"id": "existing_folder_id", "name": "三浦歯科医院"}]
        }

        result = drive_client.find_or_create_folder(
            "三浦歯科医院", "parent_id", service=service
        )

        self.assertEqual(result, "existing_folder_id")
        service.files.return_value.create.assert_not_called()

    def test_creates_new_folder_if_not_found(self):
        service = MagicMock()
        service.files.return_value.list.return_value.execute.return_value = {
            "files": []
        }
        service.files.return_value.create.return_value.execute.return_value = {
            "id": "new_folder_id"
        }

        result = drive_client.find_or_create_folder(
            "新規医院", "parent_id", service=service
        )

        self.assertEqual(result, "new_folder_id")
        create_args = service.files.return_value.create.call_args
        body = create_args.kwargs["body"]
        self.assertEqual(body["name"], "新規医院")
        self.assertEqual(body["mimeType"], "application/vnd.google-apps.folder")
        self.assertEqual(body["parents"], ["parent_id"])

    def test_escapes_single_quote_in_folder_name(self):
        service = MagicMock()
        service.files.return_value.list.return_value.execute.return_value = {
            "files": [{"id": "id1", "name": "O'brien歯科"}]
        }

        drive_client.find_or_create_folder(
            "O'brien歯科", "parent_id", service=service
        )

        list_args = service.files.return_value.list.call_args
        query = list_args.kwargs["q"]
        self.assertIn("name='O\\'brien歯科'", query)

    def test_raises_on_empty_folder_name(self):
        with self.assertRaises(ValueError):
            drive_client.find_or_create_folder("", "parent_id", service=MagicMock())

    def test_raises_on_empty_parent_id(self):
        with self.assertRaises(ValueError):
            drive_client.find_or_create_folder("name", "", service=MagicMock())


class TestUploadPdf(unittest.TestCase):

    def test_uploads_with_returned_id_and_link(self):
        service = MagicMock()
        service.files.return_value.create.return_value.execute.return_value = {
            "id": "uploaded_file_id",
            "webViewLink": "https://drive.google.com/file/d/uploaded_file_id/view",
        }

        with TemporaryDirectory() as tmp:
            pdf_path = Path(tmp) / "sample.pdf"
            pdf_path.write_bytes(b"%PDF-1.4 fake content")

            result = drive_client.upload_pdf(
                file_path=pdf_path,
                folder_id="folder_id",
                service=service,
            )

        self.assertEqual(result["id"], "uploaded_file_id")
        self.assertEqual(
            result["webViewLink"],
            "https://drive.google.com/file/d/uploaded_file_id/view",
        )
        body = service.files.return_value.create.call_args.kwargs["body"]
        self.assertEqual(body["name"], "sample.pdf")
        self.assertEqual(body["parents"], ["folder_id"])

    def test_uses_explicit_file_name_when_provided(self):
        service = MagicMock()
        service.files.return_value.create.return_value.execute.return_value = {
            "id": "id", "webViewLink": "url",
        }

        with TemporaryDirectory() as tmp:
            pdf_path = Path(tmp) / "local_temp.pdf"
            pdf_path.write_bytes(b"%PDF-1.4")

            drive_client.upload_pdf(
                file_path=pdf_path,
                folder_id="folder_id",
                file_name="元PDFファイル名.pdf",
                service=service,
            )

        body = service.files.return_value.create.call_args.kwargs["body"]
        self.assertEqual(body["name"], "元PDFファイル名.pdf")

    def test_raises_when_file_missing(self):
        with self.assertRaises(FileNotFoundError):
            drive_client.upload_pdf(
                file_path="/nonexistent/file.pdf",
                folder_id="id",
                service=MagicMock(),
            )

    def test_raises_on_empty_folder_id(self):
        with TemporaryDirectory() as tmp:
            pdf_path = Path(tmp) / "x.pdf"
            pdf_path.write_bytes(b"%PDF")
            with self.assertRaises(ValueError):
                drive_client.upload_pdf(pdf_path, "", service=MagicMock())


class TestUploadPdfToClinicPerson(unittest.TestCase):

    @patch("src.drive_client.upload_pdf")
    @patch("src.drive_client.find_or_create_folder")
    @patch("src.drive_client.get_drive_service")
    def test_creates_two_level_hierarchy_then_uploads(
        self, mock_get_service, mock_find_or_create, mock_upload
    ):
        service = MagicMock()
        mock_get_service.return_value = service
        mock_find_or_create.side_effect = ["clinic_id", "person_id"]
        mock_upload.return_value = {"id": "f", "webViewLink": "u"}

        with TemporaryDirectory() as tmp:
            pdf_path = Path(tmp) / "事例A.pdf"
            pdf_path.write_bytes(b"%PDF")

            drive_client.upload_pdf_to_clinic_person(
                file_path=pdf_path,
                output_root_folder_id="root_id",
                clinic_name="三浦歯科医院",
                person_name="白川 蓮",
                file_name="事例A.pdf",
            )

        self.assertEqual(mock_find_or_create.call_count, 2)
        first_call = mock_find_or_create.call_args_list[0]
        second_call = mock_find_or_create.call_args_list[1]
        self.assertEqual(first_call.args, ("三浦歯科医院", "root_id"))
        self.assertEqual(second_call.args, ("白川 蓮", "clinic_id"))

        upload_kwargs = mock_upload.call_args.kwargs
        self.assertEqual(upload_kwargs["folder_id"], "person_id")
        self.assertEqual(upload_kwargs["file_name"], "事例A.pdf")


class TestSharedDriveSupport(unittest.TestCase):
    """共有ドライブ対応：すべての Drive API 呼び出しに supportsAllDrives=True が
    渡されていることを検証する。これが無いと共有ドライブのフォルダにアクセスできず、
    個人 Drive ではサービスアカウントの容量制限で 403 storageQuotaExceeded になる。
    """

    @patch("src.drive_client.get_drive_service")
    def test_list_pdfs_passes_shared_drive_flags(self, mock_get_service):
        service = MagicMock()
        mock_get_service.return_value = service
        service.files.return_value.list.return_value.execute.return_value = {
            "files": []
        }

        drive_client.list_pdfs(folder_id="folder_id")

        list_kwargs = service.files.return_value.list.call_args.kwargs
        self.assertTrue(list_kwargs.get("supportsAllDrives"))
        self.assertTrue(list_kwargs.get("includeItemsFromAllDrives"))

    def test_find_or_create_folder_list_passes_shared_drive_flags(self):
        service = MagicMock()
        service.files.return_value.list.return_value.execute.return_value = {
            "files": [{"id": "id", "name": "X"}]
        }

        drive_client.find_or_create_folder("X", "parent_id", service=service)

        list_kwargs = service.files.return_value.list.call_args.kwargs
        self.assertTrue(list_kwargs.get("supportsAllDrives"))
        self.assertTrue(list_kwargs.get("includeItemsFromAllDrives"))

    def test_find_or_create_folder_create_passes_shared_drive_flag(self):
        service = MagicMock()
        service.files.return_value.list.return_value.execute.return_value = {
            "files": []
        }
        service.files.return_value.create.return_value.execute.return_value = {
            "id": "new_id"
        }

        drive_client.find_or_create_folder("Y", "parent_id", service=service)

        create_kwargs = service.files.return_value.create.call_args.kwargs
        self.assertTrue(create_kwargs.get("supportsAllDrives"))

    def test_upload_pdf_create_passes_shared_drive_flag(self):
        service = MagicMock()
        service.files.return_value.create.return_value.execute.return_value = {
            "id": "id", "webViewLink": "url",
        }
        with TemporaryDirectory() as tmp:
            pdf_path = Path(tmp) / "x.pdf"
            pdf_path.write_bytes(b"%PDF")
            drive_client.upload_pdf(
                file_path=pdf_path,
                folder_id="folder_id",
                service=service,
            )

        create_kwargs = service.files.return_value.create.call_args.kwargs
        self.assertTrue(create_kwargs.get("supportsAllDrives"))

    @patch("src.drive_client.get_drive_service")
    def test_download_pdf_passes_shared_drive_flag(self, mock_get_service):
        service = MagicMock()
        mock_get_service.return_value = service
        # MediaIoBaseDownload の next_chunk が即終了するようモック
        chunk_mock = MagicMock()
        chunk_mock.next_chunk.return_value = (None, True)
        with patch("src.drive_client.MediaIoBaseDownload", return_value=chunk_mock):
            drive_client.download_pdf("file_xxx")

        get_media_kwargs = service.files.return_value.get_media.call_args.kwargs
        self.assertTrue(get_media_kwargs.get("supportsAllDrives"))


if __name__ == "__main__":
    unittest.main()
