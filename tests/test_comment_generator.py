"""comment_generator モジュールのテスト（構造化出力 + 抽出版）。"""

import json
import unittest
from unittest.mock import MagicMock, patch

import anthropic
from anthropic.types import TextBlock

from src.comment_generator import (
    EXTRACTION_SCHEMA,
    SYSTEM_PROMPT,
    _build_user_prompt,
    _parse_extraction,
    create_batch_requests,
    generate_comment_with_metadata,
)


def _text_block(text: str) -> TextBlock:
    return TextBlock(type="text", text=text, citations=None)


def _valid_payload(comment: str = "テストコメント本文（200文字以上の想定）") -> str:
    return json.dumps(
        {
            "clinic_name": "三浦歯科医院",
            "person_name": "白川 蓮",
            "sample_title": "AI活用インプラント新患獲得",
            "comment": comment,
        },
        ensure_ascii=False,
    )


class TestSystemPromptAndSchema(unittest.TestCase):

    def test_system_prompt_describes_structured_output_task(self):
        self.assertIn("出力タスク", SYSTEM_PROMPT)
        self.assertIn("clinic_name", SYSTEM_PROMPT)
        self.assertIn("person_name", SYSTEM_PROMPT)
        self.assertIn("sample_title", SYSTEM_PROMPT)
        self.assertIn("comment", SYSTEM_PROMPT)

    def test_extraction_schema_requires_all_four_fields(self):
        self.assertEqual(
            set(EXTRACTION_SCHEMA["required"]),
            {"clinic_name", "person_name", "sample_title", "comment"},
        )
        self.assertFalse(EXTRACTION_SCHEMA["additionalProperties"])

    def test_build_user_prompt_includes_pdf_text_and_filename(self):
        prompt = _build_user_prompt("本文テキスト", "事例.pdf")
        self.assertIn("本文テキスト", prompt)
        self.assertIn("事例.pdf", prompt)

    def test_build_user_prompt_without_filename(self):
        prompt = _build_user_prompt("本文テキスト")
        self.assertIn("本文テキスト", prompt)
        self.assertNotIn("ファイル名", prompt)


class TestParseExtraction(unittest.TestCase):

    def test_parses_valid_json(self):
        data = _parse_extraction(_valid_payload())
        self.assertEqual(data["clinic_name"], "三浦歯科医院")
        self.assertEqual(data["person_name"], "白川 蓮")
        self.assertEqual(data["sample_title"], "AI活用インプラント新患獲得")
        self.assertTrue(data["comment"])

    def test_missing_fields_become_empty_strings(self):
        partial = json.dumps({"comment": "C"}, ensure_ascii=False)
        data = _parse_extraction(partial)
        self.assertEqual(data["clinic_name"], "")
        self.assertEqual(data["person_name"], "")
        self.assertEqual(data["sample_title"], "")
        self.assertEqual(data["comment"], "C")

    def test_invalid_json_raises_value_error(self):
        with self.assertRaises(ValueError):
            _parse_extraction("not json {")

    def test_non_object_raises_value_error(self):
        with self.assertRaises(ValueError):
            _parse_extraction(json.dumps(["a", "b"]))

    def test_strips_whitespace_in_string_fields(self):
        payload = json.dumps(
            {"clinic_name": "  X  ", "person_name": "Y", "sample_title": "Z", "comment": "  C  "},
            ensure_ascii=False,
        )
        data = _parse_extraction(payload)
        self.assertEqual(data["clinic_name"], "X")
        self.assertEqual(data["comment"], "C")


class TestGenerateCommentWithMetadata(unittest.TestCase):

    @patch("src.comment_generator._create_client")
    def test_returns_parsed_metadata_on_success(self, mock_create_client):
        mock_client = MagicMock()
        mock_create_client.return_value = mock_client
        mock_response = MagicMock()
        mock_response.content = [_text_block(_valid_payload())]
        mock_client.messages.create.return_value = mock_response

        data = generate_comment_with_metadata("PDF全文", pdf_filename="x.pdf")

        self.assertEqual(data["clinic_name"], "三浦歯科医院")
        self.assertEqual(data["person_name"], "白川 蓮")
        self.assertIn("テストコメント", data["comment"])
        mock_client.messages.create.assert_called_once()

        # 構造化出力パラメータが渡っていること
        kwargs = mock_client.messages.create.call_args.kwargs
        self.assertIn("output_config", kwargs)
        self.assertEqual(
            kwargs["output_config"]["format"]["type"], "json_schema"
        )

    @patch("src.comment_generator._create_client")
    @patch("src.comment_generator.time.sleep")
    def test_retries_on_rate_limit_then_succeeds(self, mock_sleep, mock_create_client):
        mock_client = MagicMock()
        mock_create_client.return_value = mock_client
        success = MagicMock()
        success.content = [_text_block(_valid_payload("OK"))]
        mock_client.messages.create.side_effect = [
            anthropic.RateLimitError(
                message="rate limit",
                response=MagicMock(status_code=429, headers={}),
                body=None,
            ),
            success,
        ]
        data = generate_comment_with_metadata("PDF全文")
        self.assertEqual(data["comment"], "OK")
        self.assertEqual(mock_sleep.call_count, 1)

    @patch("src.comment_generator._create_client")
    @patch("src.comment_generator.time.sleep")
    def test_raises_when_retries_exhausted(self, mock_sleep, mock_create_client):
        mock_client = MagicMock()
        mock_create_client.return_value = mock_client
        mock_client.messages.create.side_effect = anthropic.APIConnectionError(
            request=MagicMock()
        )
        with self.assertRaises(anthropic.APIConnectionError):
            generate_comment_with_metadata("PDF全文", max_retries=2)
        self.assertEqual(mock_sleep.call_count, 2)

    @patch("src.comment_generator._create_client")
    @patch("src.comment_generator.time.sleep")
    def test_raises_value_error_when_comment_empty(self, mock_sleep, mock_create_client):
        mock_client = MagicMock()
        mock_create_client.return_value = mock_client
        mock_response = MagicMock()
        mock_response.content = [_text_block(_valid_payload(""))]
        mock_client.messages.create.return_value = mock_response
        with self.assertRaises(ValueError):
            generate_comment_with_metadata("PDF全文", max_retries=1)

    @patch("src.comment_generator._create_client")
    @patch("src.comment_generator.time.sleep")
    def test_raises_value_error_when_response_content_empty(self, mock_sleep, mock_create_client):
        mock_client = MagicMock()
        mock_create_client.return_value = mock_client
        mock_response = MagicMock()
        mock_response.content = []
        mock_client.messages.create.return_value = mock_response
        with self.assertRaises(ValueError):
            generate_comment_with_metadata("PDF全文", max_retries=1)


class TestCreateBatchRequests(unittest.TestCase):

    def test_creates_one_request_per_item_with_structured_output(self):
        items = [
            {
                "custom_id": "item_0001",
                "pdf_text": "本文1",
                "pdf_file_name": "事例1.pdf",
            },
            {
                "custom_id": "item_0002",
                "pdf_text": "本文2",
                "pdf_file_name": "事例2.pdf",
            },
        ]
        requests = create_batch_requests(items)
        self.assertEqual(len(requests), 2)
        for req, expected_id in zip(requests, ["item_0001", "item_0002"]):
            self.assertEqual(req["custom_id"], expected_id)
            params = req["params"]
            self.assertIn("output_config", params)
            self.assertEqual(
                params["output_config"]["format"]["type"], "json_schema"
            )
            # キャッシュ制御も保たれていること
            self.assertEqual(
                params["system"][0]["cache_control"]["type"], "ephemeral"
            )

    def test_empty_input_returns_empty_list(self):
        self.assertEqual(create_batch_requests([]), [])


if __name__ == "__main__":
    unittest.main()
