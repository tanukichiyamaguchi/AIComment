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
    PermanentRunFailureError,
    _backoff_seconds,
    _build_extraction_request_params,
    _build_user_prompt,
    _OverloadedError,
    _parse_extraction,
    _RETRYABLE_API_ERRORS,
    _scrub_names_from_comment,
    create_batch_requests,
    extract_theme,
    generate_comment_with_metadata,
    get_batch_results,
    get_batch_status,
    get_system_prompt,
    is_permanent_run_failure,
    submit_batch,
)


def _overloaded_error() -> Exception:
    """本番で観測された 529 Overloaded（一過性のサーバー過負荷）を再現する。"""
    return _OverloadedError(
        message="Overloaded",
        response=MagicMock(status_code=529, headers={}),
        body={"type": "error", "error": {"type": "overloaded_error"}},
    )


def _internal_server_error_503() -> anthropic.InternalServerError:
    """本番 Batch ラン（run_id=26811653746）で観測された 503 を再現する。

    ``get_batch_status()`` 内の ``client.messages.batches.retrieve(batch_id)`` が
    3 時間ポーリングの最終ループで以下を投げ、リトライされずに 3 時間ぶんの
    待機が水の泡になった。本テストの存在意義は「これが起きてもリトライで
    吸収できる」ことの回帰防止。
    """
    return anthropic.InternalServerError(
        message=(
            "Error code: 503 - {'type':'overloaded_error',"
            "'message':'API key validation is temporarily unavailable. "
            "Please retry.'}"
        ),
        response=MagicMock(status_code=503, headers={}),
        body={
            "type": "overloaded_error",
            "message": "API key validation is temporarily unavailable. Please retry.",
        },
    )


def _text_block(text: str) -> TextBlock:
    return TextBlock(type="text", text=text, citations=None)


def _credit_balance_error() -> anthropic.BadRequestError:
    """本番で観測された「クレジット残高不足」400 を再現する。"""
    body = {
        "type": "error",
        "error": {
            "type": "invalid_request_error",
            "message": (
                "Your credit balance is too low to access the Anthropic API. "
                "Please go to Plans & Billing to upgrade or purchase credits."
            ),
        },
    }
    return anthropic.BadRequestError(
        message=body["error"]["message"],
        response=MagicMock(status_code=400, headers={}),
        body=body,
    )


def _auth_error() -> anthropic.AuthenticationError:
    return anthropic.AuthenticationError(
        message="invalid x-api-key",
        response=MagicMock(status_code=401, headers={}),
        body=None,
    )


def _permission_error() -> anthropic.PermissionDeniedError:
    return anthropic.PermissionDeniedError(
        message="Request not allowed",
        response=MagicMock(status_code=403, headers={}),
        body=None,
    )


def _request_specific_bad_request() -> anthropic.BadRequestError:
    """特定 PDF 固有の 400（プロンプト過大など）。ラン全体を止めてはいけない。"""
    return anthropic.BadRequestError(
        message="prompt is too long: 250000 tokens > 200000 maximum",
        response=MagicMock(status_code=400, headers={}),
        body=None,
    )


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
    def test_retries_on_overloaded_529_then_succeeds(self, mock_sleep, mock_create_client):
        """529 Overloaded（一過性過負荷）はリトライして成功できること。

        本番で 67 件失敗した事象の回帰防止。OverloadedError は InternalServerError の
        派生ではないため、明示的に retry 対象へ含めていないと 1 度も再試行されず即失敗する。
        """
        mock_client = MagicMock()
        mock_create_client.return_value = mock_client
        success = MagicMock()
        success.content = [_text_block(_valid_payload("OK"))]
        mock_client.messages.create.side_effect = [
            _overloaded_error(),
            _overloaded_error(),
            success,
        ]
        data = generate_comment_with_metadata("PDF全文")
        self.assertEqual(data["comment"], "OK")
        self.assertEqual(mock_sleep.call_count, 2)  # 2回失敗 → 2回リトライ前 sleep

    @patch("src.comment_generator._create_client")
    @patch("src.comment_generator.time.sleep")
    def test_raises_when_overloaded_exhausted(self, mock_sleep, mock_create_client):
        """529 がリトライ上限まで続いたら最終的に送出（per-PDF fail-soft に委ねる）。"""
        mock_client = MagicMock()
        mock_create_client.return_value = mock_client
        mock_client.messages.create.side_effect = _overloaded_error()
        with self.assertRaises(_OverloadedError):
            generate_comment_with_metadata("PDF全文", max_retries=3)
        self.assertEqual(mock_sleep.call_count, 3)

    def test_overloaded_error_is_in_retryable_set(self):
        """529 Overloaded が確実にリトライ対象集合に含まれること。"""
        self.assertIn(_OverloadedError, _RETRYABLE_API_ERRORS)
        # 恒久エラー（認証/権限）はリトライ対象に含めない
        self.assertNotIn(anthropic.AuthenticationError, _RETRYABLE_API_ERRORS)

    def test_overloaded_not_classified_as_permanent(self):
        """529 は恒久エラーではない（fail-fast 停止の対象にしない）。"""
        self.assertFalse(is_permanent_run_failure(_overloaded_error()))


class TestBackoffSeconds(unittest.TestCase):
    """指数バックオフ + ジッターの境界。"""

    def test_first_attempt_in_range(self):
        for _ in range(50):
            w = _backoff_seconds(0)
            self.assertGreaterEqual(w, 2.0)
            self.assertLess(w, 3.0)  # 2 + [0,1)

    def test_capped_at_max(self):
        # 大きな attempt でも _BACKOFF_CAP_SECONDS(30) + ジッター(<1) を超えない
        for _ in range(50):
            w = _backoff_seconds(20)
            self.assertGreaterEqual(w, 30.0)
            self.assertLess(w, 31.0)

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


class TestIsPermanentRunFailure(unittest.TestCase):
    """``is_permanent_run_failure`` は「以降のどの API 呼び出しも必ず失敗する」
    恒久条件（残高不足 / 認証 / 権限）だけを True と判定する。一過性エラーや
    PDF 固有の 400 は False（＝従来通り per-PDF fail-soft / リトライ）。"""

    def test_credit_balance_too_low_is_permanent(self):
        self.assertTrue(is_permanent_run_failure(_credit_balance_error()))

    def test_authentication_error_is_permanent(self):
        self.assertTrue(is_permanent_run_failure(_auth_error()))

    def test_permission_denied_error_is_permanent(self):
        self.assertTrue(is_permanent_run_failure(_permission_error()))

    def test_request_specific_bad_request_is_not_permanent(self):
        # プロンプト過大など、その PDF 固有の 400 はラン全体を止めない。
        self.assertFalse(is_permanent_run_failure(_request_specific_bad_request()))

    def test_rate_limit_error_is_not_permanent(self):
        # 一過性。リトライ対象であり、ラン停止条件ではない。
        rate = anthropic.RateLimitError(
            message="rate limit",
            response=MagicMock(status_code=429, headers={}),
            body=None,
        )
        self.assertFalse(is_permanent_run_failure(rate))

    def test_internal_server_error_is_not_permanent(self):
        err = anthropic.InternalServerError(
            message="overloaded",
            response=MagicMock(status_code=500, headers={}),
            body=None,
        )
        self.assertFalse(is_permanent_run_failure(err))

    def test_billing_wording_variants_are_permanent(self):
        # message 表現が変わっても billing 系の語を含めば恒久扱い。
        for msg in (
            "Your credit balance is too low.",
            "Please go to Plans & Billing to upgrade.",
            "billing issue: purchase credits to continue",
        ):
            err = anthropic.BadRequestError(
                message=msg,
                response=MagicMock(status_code=400, headers={}),
                body=None,
            )
            self.assertTrue(
                is_permanent_run_failure(err), f"should be permanent: {msg!r}"
            )

    def test_non_anthropic_exception_is_not_permanent(self):
        self.assertFalse(is_permanent_run_failure(ValueError("boom")))


class TestGenerateCommentRaisesPermanentImmediately(unittest.TestCase):
    """``generate_comment_with_metadata`` は恒久エラーを **リトライせず** 即座に
    ``PermanentRunFailureError`` として送出する（無駄な API 再試行をしない）。"""

    @patch("src.comment_generator._create_client")
    @patch("src.comment_generator.time.sleep")
    def test_credit_balance_raises_permanent_without_retry(
        self, mock_sleep, mock_create_client,
    ):
        mock_client = MagicMock()
        mock_create_client.return_value = mock_client
        mock_client.messages.create.side_effect = _credit_balance_error()

        with self.assertRaises(PermanentRunFailureError):
            generate_comment_with_metadata("PDF全文", max_retries=3)

        # 恒久エラーはリトライしない（1 回だけ呼ぶ・sleep しない）
        self.assertEqual(mock_client.messages.create.call_count, 1)
        mock_sleep.assert_not_called()

    @patch("src.comment_generator._create_client")
    @patch("src.comment_generator.time.sleep")
    def test_authentication_error_raises_permanent_without_retry(
        self, mock_sleep, mock_create_client,
    ):
        mock_client = MagicMock()
        mock_create_client.return_value = mock_client
        mock_client.messages.create.side_effect = _auth_error()

        with self.assertRaises(PermanentRunFailureError):
            generate_comment_with_metadata("PDF全文", max_retries=3)
        self.assertEqual(mock_client.messages.create.call_count, 1)
        mock_sleep.assert_not_called()

    @patch("src.comment_generator._create_client")
    @patch("src.comment_generator.time.sleep")
    def test_request_specific_bad_request_still_raises_bad_request(
        self, mock_sleep, mock_create_client,
    ):
        # PDF 固有の 400 は従来通り BadRequestError のまま raise（per-PDF で
        # 呼び出し側が握りつぶす）。PermanentRunFailureError には変換しない。
        mock_client = MagicMock()
        mock_create_client.return_value = mock_client
        mock_client.messages.create.side_effect = _request_specific_bad_request()

        with self.assertRaises(anthropic.BadRequestError) as ctx:
            generate_comment_with_metadata("PDF全文", max_retries=2)
        self.assertNotIsInstance(ctx.exception, PermanentRunFailureError)
        # 恒久ではない 400 もリトライしない（即 raise）→ create は 1 回
        self.assertEqual(mock_client.messages.create.call_count, 1)


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


class TestBatchApiRetriesOnTransientErrors(unittest.TestCase):
    """Batch 系 3 関数（``submit_batch`` / ``get_batch_status`` /
    ``get_batch_results``）の一過性エラーリトライ回帰防止。

    本番 GitHub Actions ラン（run_id=26811653746, branch=main, 3h22m）で、
    ``get_batch_status`` 内の ``client.messages.batches.retrieve(batch_id)`` が
    503 ``overloaded_error`` を 1 回返しただけで、リトライされずに即送出され、
    ``step3_wait_and_get_results`` の ``while`` ループを抜けて Traceback。
    3 時間ぶんのポーリング待機が水の泡になった。

    Batch 系 3 関数はいずれも ``generate_comment_with_metadata`` と同じ
    ``_RETRYABLE_API_ERRORS`` 集合を用いた指数バックオフリトライを持たねばならない。
    """

    # ── submit_batch ──

    @patch("src.comment_generator._create_client")
    @patch("src.comment_generator.time.sleep")
    def test_submit_batch_retries_on_503_then_succeeds(
        self, mock_sleep, mock_create_client,
    ):
        """503 InternalServerError は指数バックオフでリトライして成功する。"""
        mock_client = MagicMock()
        mock_create_client.return_value = mock_client
        success = MagicMock(id="batch_ok", processing_status="in_progress")
        mock_client.messages.batches.create.side_effect = [
            _internal_server_error_503(),
            _internal_server_error_503(),
            success,
        ]
        items = [{
            "custom_id": "item_0001",
            "pdf_text": "本文",
            "pdf_file_name": "x.pdf",
        }]
        batch_id = submit_batch(items)
        self.assertEqual(batch_id, "batch_ok")
        self.assertEqual(mock_client.messages.batches.create.call_count, 3)
        self.assertEqual(mock_sleep.call_count, 2)

    @patch("src.comment_generator._create_client")
    @patch("src.comment_generator.time.sleep")
    def test_submit_batch_retries_on_overloaded_529_then_succeeds(
        self, mock_sleep, mock_create_client,
    ):
        """529 Overloaded もリトライ対象に含まれること（既存 generate 系と同じ）。"""
        mock_client = MagicMock()
        mock_create_client.return_value = mock_client
        success = MagicMock(id="batch_ok2", processing_status="in_progress")
        mock_client.messages.batches.create.side_effect = [
            _overloaded_error(),
            success,
        ]
        items = [{"custom_id": "i1", "pdf_text": "t", "pdf_file_name": "n.pdf"}]
        batch_id = submit_batch(items)
        self.assertEqual(batch_id, "batch_ok2")
        self.assertEqual(mock_sleep.call_count, 1)

    @patch("src.comment_generator._create_client")
    @patch("src.comment_generator.time.sleep")
    def test_submit_batch_raises_when_retries_exhausted(
        self, mock_sleep, mock_create_client,
    ):
        """一過性エラーがリトライ上限を超えたら原例外を再送出（PermanentRunFailure には変換しない）。"""
        mock_client = MagicMock()
        mock_create_client.return_value = mock_client
        mock_client.messages.batches.create.side_effect = _internal_server_error_503()
        items = [{"custom_id": "i1", "pdf_text": "t", "pdf_file_name": "n.pdf"}]
        with self.assertRaises(anthropic.InternalServerError) as ctx:
            submit_batch(items)
        self.assertNotIsInstance(ctx.exception, PermanentRunFailureError)
        # 既定 max_retries=5 → 5 回 sleep, 6 回 create
        self.assertEqual(mock_client.messages.batches.create.call_count, 6)
        self.assertEqual(mock_sleep.call_count, 5)

    @patch("src.comment_generator._create_client")
    @patch("src.comment_generator.time.sleep")
    def test_submit_batch_credit_balance_raises_permanent_without_retry(
        self, mock_sleep, mock_create_client,
    ):
        """既存挙動：残高不足は即 PermanentRunFailureError、リトライしない。"""
        mock_client = MagicMock()
        mock_create_client.return_value = mock_client
        mock_client.messages.batches.create.side_effect = _credit_balance_error()
        items = [{"custom_id": "i1", "pdf_text": "t", "pdf_file_name": "n.pdf"}]
        with self.assertRaises(PermanentRunFailureError):
            submit_batch(items)
        self.assertEqual(mock_client.messages.batches.create.call_count, 1)
        mock_sleep.assert_not_called()

    # ── get_batch_status ──

    @patch("src.comment_generator._create_client")
    @patch("src.comment_generator.time.sleep")
    def test_get_batch_status_retries_on_503_then_succeeds(
        self, mock_sleep, mock_create_client,
    ):
        """本番事象の中核：retrieve() の 503 がリトライ吸収されて返る。"""
        mock_client = MagicMock()
        mock_create_client.return_value = mock_client
        rc = MagicMock(
            processing=10, succeeded=57, errored=0, canceled=0, expired=0,
        )
        ok = MagicMock(id="batch_ok", processing_status="ended", request_counts=rc)
        mock_client.messages.batches.retrieve.side_effect = [
            _internal_server_error_503(),
            ok,
        ]
        status = get_batch_status("batch_xx")
        self.assertEqual(status["status"], "ended")
        self.assertEqual(status["request_counts"]["succeeded"], 57)
        self.assertEqual(mock_client.messages.batches.retrieve.call_count, 2)
        self.assertEqual(mock_sleep.call_count, 1)

    @patch("src.comment_generator._create_client")
    @patch("src.comment_generator.time.sleep")
    def test_get_batch_status_raises_when_retries_exhausted(
        self, mock_sleep, mock_create_client,
    ):
        """リトライ上限超過時は原例外を再送出（上位のポーリングループが受ける）。"""
        mock_client = MagicMock()
        mock_create_client.return_value = mock_client
        mock_client.messages.batches.retrieve.side_effect = _internal_server_error_503()
        with self.assertRaises(anthropic.InternalServerError):
            get_batch_status("batch_xx")
        # max_retries=5 既定 → 6 attempts, 5 sleeps
        self.assertEqual(mock_client.messages.batches.retrieve.call_count, 6)
        self.assertEqual(mock_sleep.call_count, 5)

    @patch("src.comment_generator._create_client")
    @patch("src.comment_generator.time.sleep")
    def test_get_batch_status_authentication_raises_permanent(
        self, mock_sleep, mock_create_client,
    ):
        """既存挙動の保持: 401 は PermanentRunFailureError に変換、リトライなし。"""
        mock_client = MagicMock()
        mock_create_client.return_value = mock_client
        mock_client.messages.batches.retrieve.side_effect = _auth_error()
        with self.assertRaises(PermanentRunFailureError):
            get_batch_status("batch_xx")
        self.assertEqual(mock_client.messages.batches.retrieve.call_count, 1)
        mock_sleep.assert_not_called()

    # ── get_batch_results ──

    @patch("src.comment_generator._create_client")
    @patch("src.comment_generator.time.sleep")
    def test_get_batch_results_retries_on_503_then_succeeds(
        self, mock_sleep, mock_create_client,
    ):
        """results() の 503 もリトライで吸収される。"""
        mock_client = MagicMock()
        mock_create_client.return_value = mock_client

        # 成功時は結果ストリームを返す（空イテラブルで十分：実体パースは別テスト責務）。
        mock_client.messages.batches.results.side_effect = [
            _internal_server_error_503(),
            iter([]),
        ]
        results, failed_ids = get_batch_results("batch_xx")
        self.assertEqual(results, {})
        self.assertEqual(failed_ids, [])
        self.assertEqual(mock_client.messages.batches.results.call_count, 2)
        self.assertEqual(mock_sleep.call_count, 1)

    @patch("src.comment_generator._create_client")
    @patch("src.comment_generator.time.sleep")
    def test_get_batch_results_raises_when_retries_exhausted(
        self, mock_sleep, mock_create_client,
    ):
        mock_client = MagicMock()
        mock_create_client.return_value = mock_client
        mock_client.messages.batches.results.side_effect = _internal_server_error_503()
        with self.assertRaises(anthropic.InternalServerError):
            get_batch_results("batch_xx")
        self.assertEqual(mock_client.messages.batches.results.call_count, 6)
        self.assertEqual(mock_sleep.call_count, 5)

    @patch("src.comment_generator._create_client")
    @patch("src.comment_generator.time.sleep")
    def test_get_batch_results_credit_balance_raises_permanent(
        self, mock_sleep, mock_create_client,
    ):
        mock_client = MagicMock()
        mock_create_client.return_value = mock_client
        mock_client.messages.batches.results.side_effect = _credit_balance_error()
        with self.assertRaises(PermanentRunFailureError):
            get_batch_results("batch_xx")
        self.assertEqual(mock_client.messages.batches.results.call_count, 1)
        mock_sleep.assert_not_called()

    @patch("src.comment_generator._create_client")
    @patch("src.comment_generator.time.sleep")
    def test_submit_batch_apiconnection_is_retried(
        self, mock_sleep, mock_create_client,
    ):
        """接続エラーも一過性扱いで吸収できる（generate と同じ集合）。"""
        mock_client = MagicMock()
        mock_create_client.return_value = mock_client
        success = MagicMock(id="batch_ok3", processing_status="in_progress")
        mock_client.messages.batches.create.side_effect = [
            anthropic.APIConnectionError(request=MagicMock()),
            success,
        ]
        items = [{"custom_id": "i1", "pdf_text": "t", "pdf_file_name": "n.pdf"}]
        batch_id = submit_batch(items)
        self.assertEqual(batch_id, "batch_ok3")
        self.assertEqual(mock_sleep.call_count, 1)


if __name__ == "__main__":
    unittest.main()
