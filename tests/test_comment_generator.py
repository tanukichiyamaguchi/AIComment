"""comment_generator モジュールのテスト。"""

import unittest
from unittest.mock import patch, MagicMock

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

    def test_system_prompt_has_knowledge_base(self):
        self.assertIn("業界ベンチマーク", SYSTEM_PROMPT)
        self.assertIn("新患獲得", SYSTEM_PROMPT)
        self.assertIn("自費率向上", SYSTEM_PROMPT)
        self.assertIn("キャンセル削減", SYSTEM_PROMPT)
        self.assertIn("スタッフ定着", SYSTEM_PROMPT)

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


if __name__ == "__main__":
    unittest.main()
