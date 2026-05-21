"""drive_client モジュールの新規追加関数のテスト（共有ドライブ対応含む）。"""

from __future__ import annotations

import json
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

    def test_reuses_folder_with_minor_whitespace_difference(self):
        """半角空白の有無だけが違う表記揺れは既存フォルダを再利用する。"""
        service = MagicMock()
        service.files.return_value.list.return_value.execute.return_value = {
            "files": [
                {"id": "existing_id", "name": "医療法人 かがやき歯科クリニック"}
            ]
        }

        result = drive_client.find_or_create_folder(
            "医療法人かがやき歯科クリニック", "parent_id", service=service
        )

        self.assertEqual(result, "existing_id")
        service.files.return_value.create.assert_not_called()

    def test_reuses_folder_with_fullwidth_alphanum_difference(self):
        """全角英数字と半角英数字の表記揺れも同一視する。"""
        service = MagicMock()
        service.files.return_value.list.return_value.execute.return_value = {
            "files": [
                {"id": "existing_id", "name": "ｗｋｗｋ歯科クリニック"}
            ]
        }

        result = drive_client.find_or_create_folder(
            "wkwk歯科クリニック", "parent_id", service=service
        )

        self.assertEqual(result, "existing_id")
        service.files.return_value.create.assert_not_called()

    def test_creates_new_folder_when_genuinely_different_name(self):
        """正規化しても異なる名前は別フォルダとして新規作成する。"""
        service = MagicMock()
        service.files.return_value.list.return_value.execute.return_value = {
            "files": [
                {"id": "other_id", "name": "森本歯科"}
            ]
        }
        service.files.return_value.create.return_value.execute.return_value = {
            "id": "new_id"
        }

        result = drive_client.find_or_create_folder(
            "森本歯科クリニック", "parent_id", service=service
        )

        self.assertEqual(result, "new_id")
        service.files.return_value.create.assert_called_once()

    def test_creates_folder_with_original_name_not_normalized(self):
        """新規フォルダは AI 抽出時の元の表記で作成する（正規化前）。"""
        service = MagicMock()
        service.files.return_value.list.return_value.execute.return_value = {
            "files": []
        }
        service.files.return_value.create.return_value.execute.return_value = {
            "id": "new_id"
        }

        drive_client.find_or_create_folder(
            "医療法人 かがやき歯科クリニック", "parent_id", service=service
        )

        create_args = service.files.return_value.create.call_args
        body = create_args.kwargs["body"]
        # 半角空白を含む元の表記がそのままフォルダ名として使われる
        self.assertEqual(body["name"], "医療法人 かがやき歯科クリニック")

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

    @patch("src.drive_client.upload_pdf")
    @patch("src.drive_client.find_or_create_folder")
    @patch("src.drive_client.get_drive_service")
    def test_return_value_includes_clinic_folder_id(
        self, mock_get_service, mock_find_or_create, mock_upload
    ):
        """戻り値に医院フォルダの ID（``clinic_folder_id``）が含まれる。

        医院フォルダ URL シートの記録に使うため、呼び出し側が医院フォルダ
        の Drive ID を受け取れる必要がある。
        """
        service = MagicMock()
        mock_get_service.return_value = service
        # 1 回目 = 医院フォルダ、2 回目 = 個人フォルダ
        mock_find_or_create.side_effect = ["clinic_folder_xyz", "person_id"]
        mock_upload.return_value = {
            "id": "uploaded_file", "webViewLink": "https://drive/view",
        }

        with TemporaryDirectory() as tmp:
            pdf_path = Path(tmp) / "事例.pdf"
            pdf_path.write_bytes(b"%PDF")

            result = drive_client.upload_pdf_to_clinic_person(
                file_path=pdf_path,
                output_root_folder_id="root_id",
                clinic_name="001_三浦歯科医院",
                person_name="白川 蓮",
                file_name="事例.pdf",
            )

        # 既存キーは維持され、clinic_folder_id が追加されている
        self.assertEqual(result["clinic_folder_id"], "clinic_folder_xyz")
        self.assertEqual(result["id"], "uploaded_file")
        self.assertEqual(result["webViewLink"], "https://drive/view")


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


class TestGoogleApiRetry(unittest.TestCase):
    """Drive API 呼び出しが ``num_retries`` 付きで実行されることを検証する。

    Drive API も 503 / 429 などの一過性エラーを返すため、
    ``execute(num_retries=N)`` / ``next_chunk(num_retries=N)`` で
    指数バックオフ・リトライさせる（P-017）。
    """

    @patch("src.drive_client.get_drive_service")
    def test_list_pdfs_passes_num_retries(self, mock_get_service):
        """``list_pdfs`` の files.list.execute に num_retries が渡る。"""
        service = MagicMock()
        mock_get_service.return_value = service
        service.files.return_value.list.return_value.execute.return_value = {
            "files": []
        }

        drive_client.list_pdfs(folder_id="folder_id")

        list_execute = service.files.return_value.list.return_value.execute
        self.assertEqual(
            list_execute.call_args.kwargs.get("num_retries"),
            drive_client.GOOGLE_API_NUM_RETRIES,
        )

    def test_find_or_create_folder_list_passes_num_retries(self):
        """``find_or_create_folder`` の files.list.execute に num_retries が渡る。"""
        service = MagicMock()
        service.files.return_value.list.return_value.execute.return_value = {
            "files": [{"id": "id", "name": "X"}]
        }

        drive_client.find_or_create_folder("X", "parent_id", service=service)

        list_execute = service.files.return_value.list.return_value.execute
        self.assertEqual(
            list_execute.call_args.kwargs.get("num_retries"),
            drive_client.GOOGLE_API_NUM_RETRIES,
        )

    def test_find_or_create_folder_create_passes_num_retries(self):
        """``find_or_create_folder`` の files.create.execute に num_retries が渡る。"""
        service = MagicMock()
        service.files.return_value.list.return_value.execute.return_value = {
            "files": []
        }
        service.files.return_value.create.return_value.execute.return_value = {
            "id": "new_id"
        }

        drive_client.find_or_create_folder("Y", "parent_id", service=service)

        create_execute = service.files.return_value.create.return_value.execute
        self.assertEqual(create_execute.call_args.kwargs.get("num_retries"), 5)

    @patch("src.drive_client.get_drive_service")
    def test_download_pdf_passes_num_retries_to_next_chunk(self, mock_get_service):
        """``download_pdf`` の next_chunk に num_retries が渡る。"""
        service = MagicMock()
        mock_get_service.return_value = service
        chunk_mock = MagicMock()
        chunk_mock.next_chunk.return_value = (None, True)
        with patch("src.drive_client.MediaIoBaseDownload", return_value=chunk_mock):
            drive_client.download_pdf("file_xxx")

        self.assertEqual(
            chunk_mock.next_chunk.call_args.kwargs.get("num_retries"),
            drive_client.GOOGLE_API_NUM_RETRIES,
        )


class TestCredentialPriority(unittest.TestCase):
    """認証情報の選択優先順位を検証する。

    OAuth ユーザートークンが設定されているときは、サービスアカウント認証より優先
    される必要がある。サービスアカウントは個人 My Drive にファイルを
    アップロードできない（storageQuotaExceeded）ため、OAuth ユーザー認可で
    実行することでファイル所有者をユーザーに固定し、アップロードを成立させる。
    """

    def _fake_user_token_json(self) -> str:
        return json.dumps({
            "client_id": "x",
            "client_secret": "y",
            "refresh_token": "z",
            "token": "t",
        })

    def _fake_service_account_json(self) -> str:
        return json.dumps({
            "type": "service_account",
            "client_email": "sa@proj.iam.gserviceaccount.com",
            "private_key": "-----BEGIN PRIVATE KEY-----\nFAKE\n-----END PRIVATE KEY-----\n",
            "token_uri": "https://oauth2.googleapis.com/token",
        })

    def test_prefers_oauth_user_token_over_service_account(self):
        with patch.object(
            drive_client, "GOOGLE_OAUTH_TOKEN_JSON", self._fake_user_token_json()
        ), patch.object(
            drive_client, "GOOGLE_CREDENTIALS_JSON", self._fake_service_account_json()
        ), patch(
            "google.oauth2.credentials.Credentials.from_authorized_user_info"
        ) as mock_user_creds, patch(
            "google.oauth2.service_account.Credentials.from_service_account_info"
        ) as mock_sa_creds:
            mock_user_creds.return_value = MagicMock(name="user_creds")
            drive_client._get_credentials()

            mock_user_creds.assert_called_once()
            mock_sa_creds.assert_not_called()

    def test_falls_back_to_service_account_when_no_oauth_token(self):
        with patch.object(
            drive_client, "GOOGLE_OAUTH_TOKEN_JSON", ""
        ), patch.object(
            drive_client, "GOOGLE_CREDENTIALS_JSON", self._fake_service_account_json()
        ), patch(
            "google.oauth2.service_account.Credentials.from_service_account_info"
        ) as mock_sa_creds:
            mock_sa_creds.return_value = MagicMock(name="sa_creds")
            drive_client._get_credentials()

            mock_sa_creds.assert_called_once()


if __name__ == "__main__":
    unittest.main()
