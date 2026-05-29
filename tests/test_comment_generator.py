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
    _scrub_names_from_comment,
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


class TestScrubNamesFromComment(unittest.TestCase):
    """comment 本文から医院名・氏名・敬称を除去する保険ロジック。

    プロンプトで禁止しているが AI が偶発混入させた場合に備え、抽出後の
    sanitize を ``_parse_extraction`` の戻り値に必ず通す。
    """

    def test_removes_clinic_name_inline(self):
        result = _scrub_names_from_comment(
            comment="三浦歯科医院の取り組みは素晴らしい内容です。",
            clinic_name="三浦歯科医院",
            person_name="白川 蓮",
        )
        self.assertNotIn("三浦歯科医院", result)

    def test_removes_person_name_with_honorific(self):
        result = _scrub_names_from_comment(
            comment="白川 蓮様、自費率向上の取り組みが見事でした。",
            clinic_name="三浦歯科医院",
            person_name="白川 蓮",
        )
        self.assertNotIn("白川 蓮", result)
        self.assertNotIn("白川 蓮様", result)

    def test_removes_sensei_honorific(self):
        result = _scrub_names_from_comment(
            comment="白川 蓮先生のアプローチが印象的でした。",
            clinic_name="",
            person_name="白川 蓮",
        )
        self.assertNotIn("白川 蓮", result)
        self.assertNotIn("先生", result)

    def test_does_not_alter_clean_comment(self):
        original = "実践内容が素晴らしい構成で、特に集患の改善が明確に出ていました。"
        result = _scrub_names_from_comment(
            comment=original,
            clinic_name="三浦歯科医院",
            person_name="白川 蓮",
        )
        self.assertEqual(result, original)

    def test_empty_comment_returns_empty(self):
        self.assertEqual(
            _scrub_names_from_comment("", "三浦歯科医院", "白川 蓮"), ""
        )

    def test_single_char_name_is_not_scrubbed(self):
        """1 文字氏名は普通の語と衝突するため除去スキップ。"""
        original = "森を歩くような落ち着いた進行でした。"
        result = _scrub_names_from_comment(
            comment=original, clinic_name="", person_name="森",
        )
        self.assertEqual(result, original)

    def test_parse_extraction_strips_names(self):
        """``_parse_extraction`` の戻り値の comment が sanitized 済みであること。"""
        payload = json.dumps({
            "clinic_name": "三浦歯科医院",
            "person_name": "白川 蓮",
            "sample_title": "AI活用",
            "comment": "三浦歯科医院の白川 蓮様、素晴らしい取り組みでした。",
        })
        data = _parse_extraction(payload)
        self.assertNotIn("三浦歯科医院", data["comment"])
        self.assertNotIn("白川 蓮", data["comment"])
        # clinic_name / person_name 自体はメタデータとして保持される
        self.assertEqual(data["clinic_name"], "三浦歯科医院")
        self.assertEqual(data["person_name"], "白川 蓮")


class TestSystemPromptForbidsNames(unittest.TestCase):
    """SYSTEM_PROMPT に「comment 内で医院名・氏名を使わない」明示が
    含まれていることの回帰防止（プロンプト更新が事故で消えないように）。"""

    def test_prompt_explicitly_forbids_clinic_name_in_comment(self):
        self.assertIn("医院名", SYSTEM_PROMPT)
        self.assertIn("禁止", SYSTEM_PROMPT)

    def test_prompt_explicitly_forbids_honorific_address(self):
        # 「○○様」「○○先生」を禁じる文言があること
        self.assertTrue(
            "様" in SYSTEM_PROMPT and "先生" in SYSTEM_PROMPT,
            "プロンプトに敬称 (様/先生) 禁止の明示が必要",
        )


if __name__ == "__main__":
    unittest.main()
