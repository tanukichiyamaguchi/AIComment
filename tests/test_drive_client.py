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


class TestFindOrCreateClinicFolder(unittest.TestCase):
    """``find_or_create_clinic_folder`` のテスト。

    医院フォルダの識別は **医院番号のみ**（フォルダ名の ``<医院番号>_``
    プレフィックス）で行う。AI 抽出の医院名表記揺れ（``三浦歯科医院`` vs
    ``三浦歯科`` vs ``医療法人三浦歯科``）で重複フォルダが作られない（P-019）。
    """

    def test_reuses_existing_folder_when_only_clinic_name_differs(self):
        """既存フォルダ ``001_三浦歯科医院`` があるとき、違う医院名表記
        （``三浦歯科``）で呼んでも同じフォルダを再利用する（リネームなし）。"""
        service = MagicMock()
        service.files.return_value.list.return_value.execute.return_value = {
            "files": [{"id": "existing_001", "name": "001_三浦歯科医院"}]
        }

        result = drive_client.find_or_create_clinic_folder(
            clinic_number="001",
            clinic_name="三浦歯科",
            parent_id="parent_id",
            service=service,
        )

        self.assertEqual(result, "existing_001")
        # リネームしない（create も update も呼ばない）
        service.files.return_value.create.assert_not_called()
        service.files.return_value.update.assert_not_called()

    def test_creates_new_folder_when_no_match(self):
        """医院番号でマッチするフォルダが無ければ
        ``<医院番号>_<医院名>`` で新規作成する。"""
        service = MagicMock()
        service.files.return_value.list.return_value.execute.return_value = {
            "files": []
        }
        service.files.return_value.create.return_value.execute.return_value = {
            "id": "new_folder_id"
        }

        result = drive_client.find_or_create_clinic_folder(
            clinic_number="001",
            clinic_name="三浦歯科",
            parent_id="parent_id",
            service=service,
        )

        self.assertEqual(result, "new_folder_id")
        body = service.files.return_value.create.call_args.kwargs["body"]
        self.assertEqual(body["name"], "001_三浦歯科")
        self.assertEqual(body["mimeType"], "application/vnd.google-apps.folder")
        self.assertEqual(body["parents"], ["parent_id"])

    def test_multiple_matches_returns_smallest_id_with_warning(self):
        """同じ医院番号で 2 つ以上のフォルダがある場合、フォルダ ID 昇順で
        先頭を決定論的に返し、警告ログに重複 ID 一覧を出す。"""
        service = MagicMock()
        service.files.return_value.list.return_value.execute.return_value = {
            "files": [
                # ID 昇順で意図的に並べ替えて、関数側のソートを検証する
                {"id": "id_zzz", "name": "001_三浦歯科"},
                {"id": "id_aaa", "name": "001_三浦歯科医院"},
                {"id": "id_mmm", "name": "001_医療法人三浦歯科"},
            ]
        }

        with self.assertLogs("jissen_comment", level="WARNING") as log_ctx:
            result = drive_client.find_or_create_clinic_folder(
                clinic_number="001",
                clinic_name="三浦歯科",
                parent_id="parent_id",
                service=service,
            )

        # ID 昇順の先頭 (id_aaa) を返す
        self.assertEqual(result, "id_aaa")
        service.files.return_value.create.assert_not_called()

        joined = "\n".join(log_ctx.output)
        # 重複 ID 一覧と「医院番号 001」、「手動統合を推奨」が含まれる
        self.assertIn("001", joined)
        self.assertIn("id_aaa", joined)
        self.assertIn("id_zzz", joined)
        self.assertIn("id_mmm", joined)
        # 手動統合推奨の文言（含意で OK）
        self.assertIn("手動統合", joined)

    def test_empty_clinic_number_falls_back_to_name_match(self):
        """``clinic_number == ""`` のとき ``find_or_create_folder`` に
        フォールバックする（旧来の名前ベース照合）。"""
        service = MagicMock()
        # 名前ベース照合用：既存フォルダ ``三浦歯科医院`` がある
        service.files.return_value.list.return_value.execute.return_value = {
            "files": [{"id": "fallback_id", "name": "三浦歯科医院"}]
        }

        result = drive_client.find_or_create_clinic_folder(
            clinic_number="",
            clinic_name="三浦歯科医院",
            parent_id="parent_id",
            service=service,
        )

        self.assertEqual(result, "fallback_id")
        service.files.return_value.create.assert_not_called()

    def test_pagination_loop_for_more_than_1000_subfolders(self):
        """1001 件超のサブフォルダがあるとき、pageToken ループで全件取得する
        （P-010 / 2 ページ目以降の医院フォルダを見落とさない）。"""
        service = MagicMock()
        # 1 ページ目: nextPageToken 付き、目的の医院番号は無し
        # 2 ページ目: 目的の医院番号 001 のフォルダがここで初登場
        service.files.return_value.list.return_value.execute.side_effect = [
            {
                "files": [{"id": f"other_{i}", "name": f"999_other_{i}"} for i in range(2)],
                "nextPageToken": "tok_page2",
            },
            {
                "files": [{"id": "target_id", "name": "001_三浦歯科医院"}],
                # nextPageToken なし → ループ終了
            },
        ]

        result = drive_client.find_or_create_clinic_folder(
            clinic_number="001",
            clinic_name="三浦歯科",
            parent_id="parent_id",
            service=service,
        )

        self.assertEqual(result, "target_id")
        # list が 2 回呼ばれ、2 回目は pageToken を渡している
        self.assertEqual(service.files.return_value.list.call_count, 2)
        second_call = service.files.return_value.list.call_args_list[1]
        self.assertEqual(second_call.kwargs["pageToken"], "tok_page2")
        # 既存フォルダを発見したので create は呼ばれない
        service.files.return_value.create.assert_not_called()

    def test_does_not_falsely_match_clinic_number_with_extra_digits(self):
        """``001_`` で前方一致しても ``0011_別医院`` には誤マッチしない。

        ``0011_`` は ``001_`` で始まらない（``_`` 込みで判定するため、
        ``0011_`` の 4 文字目は ``1`` で、``001_`` の 4 文字目 ``_`` と異なる）。
        """
        service = MagicMock()
        service.files.return_value.list.return_value.execute.return_value = {
            # 「医院番号 0011」のフォルダしかない（医院番号 001 のフォルダは無い）
            "files": [{"id": "id_0011", "name": "0011_別医院"}]
        }
        service.files.return_value.create.return_value.execute.return_value = {
            "id": "new_001_id"
        }

        result = drive_client.find_or_create_clinic_folder(
            clinic_number="001",
            clinic_name="三浦歯科医院",
            parent_id="parent_id",
            service=service,
        )

        # 0011_別医院 は 001_ で始まらないため誤マッチせず、001_三浦歯科医院 が
        # 新規作成される
        self.assertEqual(result, "new_001_id")
        body = service.files.return_value.create.call_args.kwargs["body"]
        self.assertEqual(body["name"], "001_三浦歯科医院")

    def test_raises_on_empty_parent_id(self):
        with self.assertRaises(ValueError):
            drive_client.find_or_create_clinic_folder(
                clinic_number="001",
                clinic_name="三浦歯科",
                parent_id="",
                service=MagicMock(),
            )

    def test_list_uses_shared_drive_flags_and_num_retries(self):
        """共有ドライブ対応フラグと ``num_retries`` がリスト呼び出しに渡る。"""
        service = MagicMock()
        service.files.return_value.list.return_value.execute.return_value = {
            "files": [{"id": "id", "name": "001_三浦歯科"}]
        }

        drive_client.find_or_create_clinic_folder(
            clinic_number="001",
            clinic_name="三浦歯科",
            parent_id="parent_id",
            service=service,
        )

        list_kwargs = service.files.return_value.list.call_args.kwargs
        self.assertTrue(list_kwargs.get("supportsAllDrives"))
        self.assertTrue(list_kwargs.get("includeItemsFromAllDrives"))
        list_execute = service.files.return_value.list.return_value.execute
        self.assertEqual(
            list_execute.call_args.kwargs.get("num_retries"),
            drive_client.GOOGLE_API_NUM_RETRIES,
        )

    def test_create_uses_shared_drive_flag_and_num_retries(self):
        """新規作成にも ``supportsAllDrives`` と ``num_retries`` が渡る。"""
        service = MagicMock()
        service.files.return_value.list.return_value.execute.return_value = {
            "files": []
        }
        service.files.return_value.create.return_value.execute.return_value = {
            "id": "new_id"
        }

        drive_client.find_or_create_clinic_folder(
            clinic_number="001",
            clinic_name="三浦歯科",
            parent_id="parent_id",
            service=service,
        )

        create_kwargs = service.files.return_value.create.call_args.kwargs
        self.assertTrue(create_kwargs.get("supportsAllDrives"))
        create_execute = service.files.return_value.create.return_value.execute
        self.assertEqual(
            create_execute.call_args.kwargs.get("num_retries"),
            drive_client.GOOGLE_API_NUM_RETRIES,
        )


class TestUploadPdfToClinicPerson(unittest.TestCase):

    @patch("src.drive_client.upload_pdf")
    @patch("src.drive_client.find_or_create_folder")
    @patch("src.drive_client.find_or_create_clinic_folder")
    @patch("src.drive_client.get_drive_service")
    def test_creates_two_level_hierarchy_then_uploads(
        self,
        mock_get_service,
        mock_find_or_create_clinic,
        mock_find_or_create,
        mock_upload,
    ):
        service = MagicMock()
        mock_get_service.return_value = service
        mock_find_or_create_clinic.return_value = "clinic_id"
        mock_find_or_create.return_value = "person_id"
        mock_upload.return_value = {"id": "f", "webViewLink": "u"}

        with TemporaryDirectory() as tmp:
            pdf_path = Path(tmp) / "事例A.pdf"
            pdf_path.write_bytes(b"%PDF")

            drive_client.upload_pdf_to_clinic_person(
                file_path=pdf_path,
                output_root_folder_id="root_id",
                clinic_number="001",
                clinic_name="三浦歯科医院",
                person_name="白川 蓮",
                file_name="事例A.pdf",
            )

        # 医院フォルダは find_or_create_clinic_folder（医院番号で識別）で取得
        clinic_call = mock_find_or_create_clinic.call_args
        self.assertEqual(clinic_call.kwargs["clinic_number"], "001")
        self.assertEqual(clinic_call.kwargs["clinic_name"], "三浦歯科医院")
        self.assertEqual(clinic_call.kwargs["parent_id"], "root_id")

        # 個人フォルダは従来通り find_or_create_folder で
        person_call = mock_find_or_create.call_args
        self.assertEqual(person_call.args, ("白川 蓮", "clinic_id"))

        upload_kwargs = mock_upload.call_args.kwargs
        self.assertEqual(upload_kwargs["folder_id"], "person_id")
        self.assertEqual(upload_kwargs["file_name"], "事例A.pdf")

    @patch("src.drive_client.upload_pdf")
    @patch("src.drive_client.find_or_create_folder")
    @patch("src.drive_client.find_or_create_clinic_folder")
    @patch("src.drive_client.get_drive_service")
    def test_return_value_includes_clinic_folder_id(
        self,
        mock_get_service,
        mock_find_or_create_clinic,
        mock_find_or_create,
        mock_upload,
    ):
        """戻り値に医院フォルダの ID（``clinic_folder_id``）が含まれる。

        医院フォルダ URL シートの記録に使うため、呼び出し側が医院フォルダ
        の Drive ID を受け取れる必要がある。
        """
        service = MagicMock()
        mock_get_service.return_value = service
        mock_find_or_create_clinic.return_value = "clinic_folder_xyz"
        mock_find_or_create.return_value = "person_id"
        mock_upload.return_value = {
            "id": "uploaded_file", "webViewLink": "https://drive/view",
        }

        with TemporaryDirectory() as tmp:
            pdf_path = Path(tmp) / "事例.pdf"
            pdf_path.write_bytes(b"%PDF")

            result = drive_client.upload_pdf_to_clinic_person(
                file_path=pdf_path,
                output_root_folder_id="root_id",
                clinic_number="001",
                clinic_name="三浦歯科医院",
                person_name="白川 蓮",
                file_name="事例.pdf",
            )

        # 既存キーは維持され、clinic_folder_id が追加されている
        self.assertEqual(result["clinic_folder_id"], "clinic_folder_xyz")
        self.assertEqual(result["id"], "uploaded_file")
        self.assertEqual(result["webViewLink"], "https://drive/view")

    @patch("src.drive_client.upload_pdf")
    @patch("src.drive_client.find_or_create_folder")
    @patch("src.drive_client.find_or_create_clinic_folder")
    @patch("src.drive_client.get_drive_service")
    def test_passes_clinic_number_and_name_separately(
        self,
        mock_get_service,
        mock_find_or_create_clinic,
        mock_find_or_create,
        mock_upload,
    ):
        """``clinic_number`` と ``clinic_name`` を別々の引数として
        ``find_or_create_clinic_folder`` に伝搬する（医院フォルダの
        識別は医院番号、新規作成時の名前は AI 抽出の医院名）。"""
        service = MagicMock()
        mock_get_service.return_value = service
        mock_find_or_create_clinic.return_value = "clinic_id"
        mock_find_or_create.return_value = "person_id"
        mock_upload.return_value = {"id": "x", "webViewLink": "u"}

        with TemporaryDirectory() as tmp:
            pdf_path = Path(tmp) / "x.pdf"
            pdf_path.write_bytes(b"%PDF")
            drive_client.upload_pdf_to_clinic_person(
                file_path=pdf_path,
                output_root_folder_id="root_id",
                clinic_number="088",
                clinic_name="医療法人 かがやき",
                person_name="田中",
                file_name="x.pdf",
            )

        kwargs = mock_find_or_create_clinic.call_args.kwargs
        # 医院名にプレフィックスを付けない（生の AI 抽出値が渡る）
        self.assertEqual(kwargs["clinic_number"], "088")
        self.assertEqual(kwargs["clinic_name"], "医療法人 かがやき")


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
