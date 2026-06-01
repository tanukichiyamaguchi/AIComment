"""comment_generator モジュールのテスト（構造化出力 + 抽出版）。"""

import json
import unittest
from unittest.mock import MagicMock, patch

import anthropic
from anthropic.types import TextBlock

from src.comment_generator import (
    EXTRACTION_SCHEMA,
    LIG_REPORT_SYSTEM_PROMPT,
    PARTNER_SYSTEM_PROMPT,
    READING_SYSTEM_PROMPT,
    SYSTEM_PROMPT,
    TEAM_MTG_SYSTEM_PROMPT,
    _build_extraction_request_params,
    _build_user_prompt,
    _parse_extraction,
    _scrub_names_from_comment,
    create_batch_requests,
    extract_theme,
    generate_comment_with_metadata,
    get_system_prompt,
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


class TestExtractTheme(unittest.TestCase):
    """``extract_theme``：PDF ファイル名から 5 テーマを判定する。

    フォーマット: ``NNN-NN-N【B1】テーマ_…``
    """

    def test_reading_theme(self):
        self.assertEqual(
            extract_theme("101-01-02【B1】読書_35歳までに必ずやるべきこと_吉野浩史.pdf"),
            "読書",
        )

    def test_lig_report_theme(self):
        self.assertEqual(
            extract_theme("101-13【B1】LIGレポート _あつたの森歯科クリニック.pdf"),
            "LIGレポート",
        )

    def test_lig_report_without_space(self):
        self.assertEqual(
            extract_theme("103-01【B1】LIGレポート_医療法人フロンティアあま歯科.pdf"),
            "LIGレポート",
        )

    def test_partner_theme(self):
        self.assertEqual(
            extract_theme("101-01-01【B1】パートナー_医）ワールデント_吉野浩史.pdf"),
            "パートナー",
        )

    def test_team_mtg_theme(self):
        self.assertEqual(
            extract_theme("110-01-13【B1】チームMTG_医院での議事録.pdf"),
            "チームMTG",
        )

    def test_team_practice_theme(self):
        self.assertEqual(
            extract_theme("101-01-08-0【B1】チーム実践_みんなの疑問解決.pdf"),
            "チーム実践",
        )

    def test_team_practice_not_confused_with_team_mtg(self):
        """「チーム実践」と「チームMTG」が混同されないこと（前方一致誤判定の予防）。"""
        self.assertEqual(
            extract_theme("999-99【B1】チーム実践_x.pdf"), "チーム実践"
        )
        self.assertEqual(
            extract_theme("999-99【B1】チームMTG_x.pdf"), "チームMTG"
        )

    def test_unknown_theme_returns_empty(self):
        self.assertEqual(
            extract_theme("101-01-01【B1】実践事例_別のもの.pdf"), ""
        )

    def test_no_bracket_returns_empty(self):
        self.assertEqual(extract_theme("ただのファイル.pdf"), "")

    def test_empty_filename_returns_empty(self):
        self.assertEqual(extract_theme(""), "")

    def test_fullwidth_space_after_theme_is_stripped(self):
        """テーマ名直後の全角スペースも除去して一致させる。"""
        self.assertEqual(
            extract_theme("101-13【B1】LIGレポート　_xxx.pdf"), "LIGレポート"
        )


class TestGetSystemPrompt(unittest.TestCase):
    """``get_system_prompt``：テーマ → プロンプト の振り分け。"""

    def test_reading_returns_reading_prompt(self):
        self.assertEqual(get_system_prompt("読書"), READING_SYSTEM_PROMPT)

    def test_lig_partner_teammtg_return_dedicated_prompts(self):
        """LIGレポート / パートナー / チームMTG は専用プロンプトを返す。"""
        self.assertEqual(get_system_prompt("LIGレポート"), LIG_REPORT_SYSTEM_PROMPT)
        self.assertEqual(get_system_prompt("パートナー"), PARTNER_SYSTEM_PROMPT)
        self.assertEqual(get_system_prompt("チームMTG"), TEAM_MTG_SYSTEM_PROMPT)

    def test_unknown_theme_falls_back_to_default(self):
        self.assertEqual(get_system_prompt(""), SYSTEM_PROMPT)
        self.assertEqual(get_system_prompt("unknown"), SYSTEM_PROMPT)

    def test_team_jissen_still_falls_back_until_provided(self):
        """チーム実践 はプロンプト未提供のため既存プロンプトに fallback
        （提供時はこのテストを更新する）。"""
        self.assertEqual(get_system_prompt("チーム実践"), SYSTEM_PROMPT)

    def test_dedicated_prompts_are_distinct(self):
        """4テーマの専用プロンプトと既存プロンプトが互いに別物であること。"""
        prompts = {
            READING_SYSTEM_PROMPT,
            LIG_REPORT_SYSTEM_PROMPT,
            PARTNER_SYSTEM_PROMPT,
            TEAM_MTG_SYSTEM_PROMPT,
            SYSTEM_PROMPT,
        }
        self.assertEqual(len(prompts), 5, "プロンプトに重複がある")


class TestReadingSystemPrompt(unittest.TestCase):
    """READING_SYSTEM_PROMPT が読書プロンプトの必須要素を含むことの回帰防止。"""

    def test_includes_book_focus(self):
        self.assertIn("書籍名", READING_SYSTEM_PROMPT)

    def test_includes_character_count_100_to_250(self):
        # 文字数指定（100〜250文字程度）が消えていないこと
        self.assertIn("100", READING_SYSTEM_PROMPT)
        self.assertIn("250", READING_SYSTEM_PROMPT)

    def test_explicitly_forbids_asterisk(self):
        self.assertIn("アスタリスク", READING_SYSTEM_PROMPT)

    def test_forbids_proposer_name(self):
        self.assertIn("提出者の名前", READING_SYSTEM_PROMPT)


class TestPracticePraisePrompts(unittest.TestCase):
    """LIGレポート / パートナー / チームMTG 共通の称賛＋改善提案プロンプトの
    必須要素（ユーザー指示）が含まれていることの回帰防止。"""

    PROMPTS = None  # set in setUp

    def setUp(self):
        self.prompts = {
            "LIGレポート": LIG_REPORT_SYSTEM_PROMPT,
            "パートナー": PARTNER_SYSTEM_PROMPT,
            "チームMTG": TEAM_MTG_SYSTEM_PROMPT,
        }

    def test_char_count_100_to_200(self):
        for name, p in self.prompts.items():
            self.assertIn("100", p, name)
            self.assertIn("200", p, name)

    def test_forbids_asterisk(self):
        for name, p in self.prompts.items():
            self.assertIn("アスタリスク", p, name)

    def test_uses_exclamation_emphasis_rule(self):
        for name, p in self.prompts.items():
            self.assertIn("!", p, name)

    def test_spoken_style_closing_rule(self):
        for name, p in self.prompts.items():
            self.assertIn("ですね", p, name)

    def test_forbids_desuyo_closing(self):
        for name, p in self.prompts.items():
            self.assertIn("ですよ", p, name)  # 「ですよ」を使わない、という言及

    def test_each_has_theme_specific_example(self):
        self.assertIn("LIGレポート", LIG_REPORT_SYSTEM_PROMPT)
        self.assertIn("パートナー", PARTNER_SYSTEM_PROMPT)
        self.assertIn("チームMTG", TEAM_MTG_SYSTEM_PROMPT)

    def test_dropped_reading_leftover_no_book_reference(self):
        """読書プロンプトの転記（書籍名）を引きずっていないこと。"""
        for name, p in self.prompts.items():
            self.assertNotIn("書籍名", p, f"{name} に読書用の『書籍名』が混入している")


class TestExtractionRequestParamsUsesProvidedPrompt(unittest.TestCase):
    """``_build_extraction_request_params`` が渡したプロンプトを system に入れる。"""

    def test_uses_custom_system_prompt_when_provided(self):
        params = _build_extraction_request_params(
            system_prompt=READING_SYSTEM_PROMPT
        )
        self.assertEqual(params["system"][0]["text"], READING_SYSTEM_PROMPT)
        # キャッシュ制御は維持
        self.assertEqual(params["system"][0]["cache_control"]["type"], "ephemeral")

    def test_falls_back_to_default_when_omitted(self):
        params = _build_extraction_request_params()
        self.assertEqual(params["system"][0]["text"], SYSTEM_PROMPT)


class TestCreateBatchRequestsPicksThemePerItem(unittest.TestCase):
    """Batch モード：同一バッチ内でアイテムごとにテーマ別プロンプトが選ばれる。"""

    def test_mixed_themes_pick_different_system_prompts(self):
        items = [
            {
                "custom_id": "item_0001",
                "pdf_file_name": "101-01-02【B1】読書_本_田中.pdf",
                "pdf_text": "...",
            },
            {
                "custom_id": "item_0002",
                "pdf_file_name": "101-13【B1】LIGレポート _x.pdf",
                "pdf_text": "...",
            },
            {
                "custom_id": "item_0003",
                "pdf_file_name": "101-01-08-0【B1】チーム実践_x.pdf",  # 未提供→既存
                "pdf_text": "...",
            },
            {
                "custom_id": "item_0004",
                "pdf_file_name": "ただの実践事例.pdf",  # 該当なし → 既存
                "pdf_text": "...",
            },
        ]
        reqs = create_batch_requests(items)
        self.assertEqual(len(reqs), 4)
        sys_texts = [r["params"]["system"][0]["text"] for r in reqs]
        self.assertEqual(sys_texts[0], READING_SYSTEM_PROMPT)       # 読書
        self.assertEqual(sys_texts[1], LIG_REPORT_SYSTEM_PROMPT)    # LIGレポート
        self.assertEqual(sys_texts[2], SYSTEM_PROMPT)               # チーム実践（未提供）
        self.assertEqual(sys_texts[3], SYSTEM_PROMPT)               # テーマなし


if __name__ == "__main__":
    unittest.main()
