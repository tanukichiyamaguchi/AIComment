"""gmail_client モジュールのテスト。

Gmail API 呼び出しを mock し、``create_draft`` が指定の TO/CC/件名/本文/PDF 添付
を持つメッセージで Gmail Drafts API を呼ぶことを検証する。
"""

from __future__ import annotations

import base64
import tempfile
import unittest
from email import message_from_bytes
from email.header import decode_header, make_header
from unittest.mock import MagicMock, patch

from src import gmail_client


def _decode_raw_message(raw_b64: str):
    """create_draft が組み立てた raw メッセージを解析する。

    Gmail API には ``base64.urlsafe_b64encode`` した MIME メッセージを渡している。
    テスト側はそれをデコードして ``email.message.Message`` として読み戻す。
    """
    raw_bytes = base64.urlsafe_b64decode(raw_b64.encode("utf-8"))
    return message_from_bytes(raw_bytes)


def _decode_header_value(value: str) -> str:
    """RFC2047 エンコーディング（``=?utf-8?b?...?=`` 形式）を平文に戻す。"""
    if value is None:
        return ""
    return str(make_header(decode_header(value)))


class TestCreateDraftWithCC(unittest.TestCase):
    """``create_draft`` の cc_email 引数による CC ヘッダー付与の検証。"""

    @patch("src.gmail_client.get_gmail_service")
    def test_cc_email_set_when_provided(self, mock_get_service, tmp_path=None):
        """cc_email が空でない値で渡されたら message に cc ヘッダーが入る。"""
        service = MagicMock()
        service.users.return_value.drafts.return_value.create.return_value.execute.return_value = {
            "id": "draft_xyz"
        }
        mock_get_service.return_value = service

        # PDF 添付ファイルをテンポラリで用意
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as td:
            pdf_path = Path(td) / "report.pdf"
            pdf_path.write_bytes(b"%PDF-1.4 minimal")

            draft_id = gmail_client.create_draft(
                to_email="person@example.com",
                person_name="白川 蓮",
                pdf_paths=pdf_path,
                cc_email="clinic@example.com",
            )

        self.assertEqual(draft_id, "draft_xyz")
        # API 呼び出しの raw メッセージを解析
        create_call = service.users.return_value.drafts.return_value.create.call_args
        raw = create_call.kwargs["body"]["message"]["raw"]
        msg = _decode_raw_message(raw)
        self.assertEqual(msg["to"], "person@example.com")
        self.assertEqual(msg["cc"], "clinic@example.com")
        # 件名は日本語混在のため RFC2047 でエンコードされる。デコードして比較。
        self.assertEqual(
            _decode_header_value(msg["subject"]),
            "【実践事例】じっせん君コメント ─ 白川 蓮様",
        )

    @patch("src.gmail_client.get_gmail_service")
    def test_no_cc_header_when_cc_email_is_none(self, mock_get_service):
        """cc_email=None なら cc ヘッダーは付かない。"""
        service = MagicMock()
        service.users.return_value.drafts.return_value.create.return_value.execute.return_value = {
            "id": "draft_no_cc"
        }
        mock_get_service.return_value = service

        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as td:
            pdf_path = Path(td) / "report.pdf"
            pdf_path.write_bytes(b"%PDF-1.4 minimal")

            gmail_client.create_draft(
                to_email="person@example.com",
                person_name="田中 太郎",
                pdf_paths=pdf_path,
                cc_email=None,
            )

        create_call = service.users.return_value.drafts.return_value.create.call_args
        raw = create_call.kwargs["body"]["message"]["raw"]
        msg = _decode_raw_message(raw)
        self.assertEqual(msg["to"], "person@example.com")
        # cc ヘッダーは存在しない（None 扱い）
        self.assertIsNone(msg["cc"])

    @patch("src.gmail_client.get_gmail_service")
    def test_no_cc_header_when_cc_email_is_empty_string(self, mock_get_service):
        """cc_email="" なら cc ヘッダーは付かない（空文字列も None 相当）。"""
        service = MagicMock()
        service.users.return_value.drafts.return_value.create.return_value.execute.return_value = {
            "id": "draft_empty_cc"
        }
        mock_get_service.return_value = service

        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as td:
            pdf_path = Path(td) / "report.pdf"
            pdf_path.write_bytes(b"%PDF-1.4 minimal")

            gmail_client.create_draft(
                to_email="person@example.com",
                person_name="山田 花子",
                pdf_paths=pdf_path,
                cc_email="",
            )

        create_call = service.users.return_value.drafts.return_value.create.call_args
        raw = create_call.kwargs["body"]["message"]["raw"]
        msg = _decode_raw_message(raw)
        self.assertIsNone(msg["cc"])

    @patch("src.gmail_client.get_gmail_service")
    def test_default_cc_email_none_when_omitted(self, mock_get_service):
        """cc_email を省略するとデフォルト None になり cc は付かない（後方互換）。"""
        service = MagicMock()
        service.users.return_value.drafts.return_value.create.return_value.execute.return_value = {
            "id": "draft_omitted"
        }
        mock_get_service.return_value = service

        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as td:
            pdf_path = Path(td) / "report.pdf"
            pdf_path.write_bytes(b"%PDF-1.4 minimal")

            gmail_client.create_draft(
                to_email="person@example.com",
                person_name="個人 太郎",
                pdf_paths=pdf_path,
            )

        create_call = service.users.return_value.drafts.return_value.create.call_args
        raw = create_call.kwargs["body"]["message"]["raw"]
        msg = _decode_raw_message(raw)
        self.assertEqual(msg["to"], "person@example.com")
        self.assertIsNone(msg["cc"])


class TestCreateDraftLogging(unittest.TestCase):
    """``create_draft`` のログ出力に TO / CC のマスク済み形が含まれること。"""

    @patch("src.gmail_client.get_gmail_service")
    def test_log_contains_masked_to_and_cc(self, mock_get_service):
        """ログに TO / CC のマスク済みメールアドレスが出る。"""
        service = MagicMock()
        service.users.return_value.drafts.return_value.create.return_value.execute.return_value = {
            "id": "draft_log"
        }
        mock_get_service.return_value = service

        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as td:
            pdf_path = Path(td) / "report.pdf"
            pdf_path.write_bytes(b"%PDF-1.4 minimal")

            with self.assertLogs("jissen_comment", level="INFO") as log_ctx:
                gmail_client.create_draft(
                    to_email="abcdef@example.com",
                    person_name="白川 蓮",
                    pdf_paths=pdf_path,
                    cc_email="ghijkl@example.com",
                )

        joined = "\n".join(log_ctx.output)
        # マスク後の TO / CC が含まれる（local が "abcdef" → "a****f"）
        self.assertIn("a****f@example.com", joined)
        self.assertIn("g****l@example.com", joined)
        # 生のメールアドレスは平文では含まれない（マスクされた形のみ出る）
        self.assertNotIn("abcdef@example.com", joined)
        self.assertNotIn("ghijkl@example.com", joined)

    @patch("src.gmail_client.get_gmail_service")
    def test_log_omits_cc_when_no_cc(self, mock_get_service):
        """cc_email=None のときログに CC= は出ない。"""
        service = MagicMock()
        service.users.return_value.drafts.return_value.create.return_value.execute.return_value = {
            "id": "draft_log_no_cc"
        }
        mock_get_service.return_value = service

        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as td:
            pdf_path = Path(td) / "report.pdf"
            pdf_path.write_bytes(b"%PDF-1.4 minimal")

            with self.assertLogs("jissen_comment", level="INFO") as log_ctx:
                gmail_client.create_draft(
                    to_email="abcdef@example.com",
                    person_name="白川 蓮",
                    pdf_paths=pdf_path,
                )

        joined = "\n".join(log_ctx.output)
        self.assertNotIn("CC=", joined)


class TestCreateDraftRetry(unittest.TestCase):
    """``create_draft`` の指数バックオフリトライ + num_retries（下書きロスト防止）。"""

    def _make_service(self):
        service = MagicMock()
        return service, service.users.return_value.drafts.return_value.create

    @patch("src.gmail_client.time.sleep")
    @patch("src.gmail_client.get_gmail_service")
    def test_transient_failure_then_success(self, mock_get_service, mock_sleep):
        """1 回目の一過性失敗 → バックオフして 2 回目で成功する。"""
        service, create = self._make_service()
        mock_get_service.return_value = service
        create.return_value.execute.side_effect = [
            ConnectionError("503"),
            {"id": "draft_ok"},
        ]
        with tempfile.NamedTemporaryFile(suffix=".pdf") as f:
            draft_id = gmail_client.create_draft(
                to_email="a@example.com", person_name="田中太郎",
                pdf_paths=[f.name],
            )
        self.assertEqual(draft_id, "draft_ok")
        mock_sleep.assert_called_once()

    @patch("src.gmail_client.time.sleep")
    @patch("src.gmail_client.get_gmail_service")
    def test_retry_limit_raises_last_error(self, mock_get_service, mock_sleep):
        service, create = self._make_service()
        mock_get_service.return_value = service
        create.return_value.execute.side_effect = ConnectionError("down")
        with tempfile.NamedTemporaryFile(suffix=".pdf") as f:
            with self.assertRaises(ConnectionError):
                gmail_client.create_draft(
                    to_email="a@example.com", person_name="田中太郎",
                    pdf_paths=[f.name], max_retries=2,
                )
        # 初回 + リトライ 2 回 = 3 回試行
        self.assertEqual(create.return_value.execute.call_count, 3)

    @patch("src.gmail_client.get_gmail_service")
    def test_execute_uses_googleapiclient_num_retries(self, mock_get_service):
        """execute に num_retries を渡し、5xx/429 の内蔵リトライも効かせる。"""
        service, create = self._make_service()
        mock_get_service.return_value = service
        create.return_value.execute.return_value = {"id": "draft_ok"}
        with tempfile.NamedTemporaryFile(suffix=".pdf") as f:
            gmail_client.create_draft(
                to_email="a@example.com", person_name="田中太郎",
                pdf_paths=[f.name],
            )
        self.assertEqual(
            create.return_value.execute.call_args.kwargs["num_retries"], 5,
        )


if __name__ == "__main__":
    unittest.main()
