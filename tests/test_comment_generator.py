"""comment_generator モジュールのテスト。"""

import unittest
from unittest.mock import patch, MagicMock

import anthropic

from src.comment_generator import (
    _build_user_prompt,
    create_batch_requests,
    SYSTEM_PROMPT,
)


class TestCommentGenerator(unittest.TestCase):

    def test_build_user_prompt(self):
        prompt = _build_user_prompt("三浦歯科医院", "白川蓮", "テスト事例テキスト")
        self.assertIn("三浦歯科医院", prompt)
        self.assertIn("白川蓮", prompt)
        self.assertIn("テスト事例テキスト", prompt)

    def test_system_prompt_has_key_sections(self):
        self.assertIn("あなたの役割", SYSTEM_PROMPT)
        self.assertIn("やってほしいこと", SYSTEM_PROMPT)
        self.assertIn("禁止事項", SYSTEM_PROMPT)
        self.assertIn("人間味を出すヒント", SYSTEM_PROMPT)

    def test_system_prompt_has_rules(self):
        self.assertIn("毎回違う文章にする", SYSTEM_PROMPT)
        self.assertIn("禁止事項", SYSTEM_PROMPT)
        self.assertIn("200〜350文字", SYSTEM_PROMPT)

    def test_create_batch_requests(self):
        items = [
            {
                "custom_id": "item_0001",
                "clinic_name": "三浦歯科医院",
                "person_name": "白川蓮",
                "pdf_text": "テスト事例テキスト",
            }
        ]
        requests = create_batch_requests(items)
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["custom_id"], "item_0001")
        self.assertIn("params", requests[0])
        params = requests[0]["params"]
        self.assertEqual(params["temperature"], 0.9)
        self.assertEqual(params["max_tokens"], 1024)
        # キャッシュ制御の確認
        system = params["system"]
        self.assertEqual(system[0]["cache_control"]["type"], "ephemeral")

    @patch("src.comment_generator._create_client")
    def test_generate_comment(self, mock_create_client):
        mock_client = MagicMock()
        mock_create_client.return_value = mock_client

        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="テストコメント（200文字以上の内容）")]
        mock_client.messages.create.return_value = mock_response

        from src.comment_generator import generate_comment
        comment = generate_comment("三浦歯科医院", "白川蓮", "テスト事例")
        self.assertIn("テストコメント", comment)
        mock_client.messages.create.assert_called_once()


    @patch("src.comment_generator._create_client")
    @patch("src.comment_generator.time.sleep")
    def test_api_timeout_handling(self, mock_sleep, mock_create_client):
        """APIタイムアウト（APIConnectionError）でリトライ後に例外が発生すること。"""
        mock_client = MagicMock()
        mock_create_client.return_value = mock_client

        mock_client.messages.create.side_effect = anthropic.APIConnectionError(
            request=MagicMock()
        )

        from src.comment_generator import generate_comment
        with self.assertRaises(anthropic.APIConnectionError):
            generate_comment("テスト歯科", "テスト太郎", "テスト事例", max_retries=2)
        # リトライが行われたことを確認
        self.assertEqual(mock_sleep.call_count, 2)

    @patch("src.comment_generator._create_client")
    @patch("src.comment_generator.time.sleep")
    def test_rate_limit_retry_then_success(self, mock_sleep, mock_create_client):
        """RateLimitErrorでリトライ後に成功すること。"""
        mock_client = MagicMock()
        mock_create_client.return_value = mock_client

        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="リトライ後の成功コメント")]

        # 最初の2回はRateLimitError、3回目に成功
        mock_client.messages.create.side_effect = [
            anthropic.RateLimitError(
                message="rate limit",
                response=MagicMock(status_code=429, headers={}),
                body=None,
            ),
            anthropic.RateLimitError(
                message="rate limit",
                response=MagicMock(status_code=429, headers={}),
                body=None,
            ),
            mock_response,
        ]

        from src.comment_generator import generate_comment
        comment = generate_comment("テスト歯科", "テスト太郎", "テスト事例")
        self.assertIn("リトライ後の成功コメント", comment)
        self.assertEqual(mock_sleep.call_count, 2)

    @patch("src.comment_generator._create_client")
    @patch("src.comment_generator.time.sleep")
    def test_rate_limit_retry_exhausted(self, mock_sleep, mock_create_client):
        """RateLimitErrorでリトライ上限に達した場合に例外が発生すること。"""
        mock_client = MagicMock()
        mock_create_client.return_value = mock_client

        mock_client.messages.create.side_effect = anthropic.RateLimitError(
            message="rate limit",
            response=MagicMock(status_code=429, headers={}),
            body=None,
        )

        from src.comment_generator import generate_comment
        with self.assertRaises(anthropic.RateLimitError):
            generate_comment("テスト歯科", "テスト太郎", "テスト事例", max_retries=2)

    @patch("src.comment_generator._create_client")
    @patch("src.comment_generator.time.sleep")
    def test_empty_response_content(self, mock_sleep, mock_create_client):
        """APIが空のcontentを返した場合にリトライ後ValueErrorが発生すること。"""
        mock_client = MagicMock()
        mock_create_client.return_value = mock_client

        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="")]
        mock_client.messages.create.return_value = mock_response

        from src.comment_generator import generate_comment
        with self.assertRaises(ValueError):
            generate_comment("テスト歯科", "テスト太郎", "テスト事例", max_retries=1)

    @patch("src.comment_generator._create_client")
    @patch("src.comment_generator.time.sleep")
    def test_malformed_response_no_content(self, mock_sleep, mock_create_client):
        """APIレスポンスのcontentが空リストの場合にリトライ後ValueErrorが発生すること。"""
        mock_client = MagicMock()
        mock_create_client.return_value = mock_client

        mock_response = MagicMock()
        mock_response.content = []  # 空リスト
        mock_client.messages.create.return_value = mock_response

        from src.comment_generator import generate_comment
        with self.assertRaises(ValueError):
            generate_comment("テスト歯科", "テスト太郎", "テスト事例", max_retries=1)

    def test_create_batch_requests_empty_list(self):
        """空のアイテムリストで空のリクエストリストが返ること。"""
        requests = create_batch_requests([])
        self.assertEqual(requests, [])

    def test_create_batch_requests_multiple_items(self):
        """複数アイテムで正しい数のリクエストが生成されること。"""
        items = [
            {
                "custom_id": f"item_{i:04d}",
                "clinic_name": f"テスト歯科{i}",
                "person_name": f"テスト{i}",
                "pdf_text": f"テスト事例{i}",
            }
            for i in range(5)
        ]
        requests = create_batch_requests(items)
        self.assertEqual(len(requests), 5)
        for i, req in enumerate(requests):
            self.assertEqual(req["custom_id"], f"item_{i:04d}")


if __name__ == "__main__":
    unittest.main()
