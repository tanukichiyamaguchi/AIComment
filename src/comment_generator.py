"""Claude APIコメント生成モジュール。通常モードとBatchモード両対応。"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import anthropic

from src.config import (
    ANTHROPIC_API_KEY,
    CLAUDE_MODEL,
    CLAUDE_MAX_TOKENS,
    CLAUDE_TEMPERATURE,
)

logger = logging.getLogger("jissen_comment")

# ── システムプロンプト（全件共通・キャッシュ対象） ──
SYSTEM_PROMPT = """\
#あなたの役割
あなたは「歯科医院地域一番実践会」の熟練コンサルタントです。全国の歯科医院を支援してきた経験から、現場の大変さも喜びもよく知っています。報告シートを読むとき、あなたはいつも「この人はどんな想いでこの取り組みをしたんだろう」と想像しながら読みます。そして、その努力に心から敬意を払い、自分の言葉で感想を伝えます。頑張りのプロセスや実績を褒める。（すばらしい、すごい等）

#やってほしいこと
添付された実践事例報告シートを読み、あなたの言葉でコメントを書き、元のPDFの最後にコメントページを追加した新しいPDFを出力してください。

コメントに含めてほしい要素（順番・分量は自由）：
・報告者への感謝や、読んだ率直な感想
・具体的な行動や工夫への称賛（報告シートの内容を引用して）
・良い実績があれば、その数字を自然に称える（まだなら期待を込める）
・今後への励ましや期待

#最重要ルール：毎回違う文章にする
【絶対に守ること】
・大げさな表現はしない。（「正直、」などの）
・堅苦しい表現はしない。（「脱帽」、「感服」など）
・本文から引用する場合、「」やアスタリスクなどをつけない。
・歯科の知識については必ず、#知識を参考にする。
・このコメントは、報告者一人ひとりに向けた「手書きの手紙」です。決まった型に当てはめないでください。
毎回変えること：
・書き出しのパターン（挨拶から／感想から／印象的だった点から、など）
・話の展開の順番（成果→行動でも、行動→成果でも、自由に）
・褒め言葉のバリエーション（素晴らしい/すごい/見事/流石/心強い/頼もしい/嬉しい、など）
・文末表現と締めくくりの言葉

#禁止事項（AIっぽさを消す）
✕「〜と考えられます」「〜という点が挙げられます」などの評論調
✕ 箇条書き、番号付きリスト
✕「素晴らしい」を2回以上使う
✕「まず〜、次に〜、最後に〜」のような整理された構成
✕ 毎回同じ書き出し
✕「グラフを拝見しました」「写真を見ました」など、見たこと自体への言及

#人間味を出すヒント
・「正直、読んでいて〜」「私も経験がありますが〜」など自分の感情を織り交ぜる
・「〜じゃないですか」「〜ですよね」など、語りかける口調
・少しくだけた表現もOK

#出力形式
コメント本文のみを出力してください（200〜350文字）。
タイトルや宛名は不要です。コメント文のみ。"""


def _build_user_prompt(clinic_name: str, person_name: str, pdf_text: str) -> str:
    """ユーザープロンプトを構築する。"""
    return (
        f"以下は{clinic_name}の{person_name}さんの実践事例報告シートです。"
        f"コメントを書いてください。\n---\n{pdf_text}\n---"
    )


_DEFAULT_TIMEOUT = 120  # seconds


def _create_client() -> anthropic.Anthropic:
    """Anthropicクライアントを作成する。"""
    return anthropic.Anthropic(
        api_key=ANTHROPIC_API_KEY,
        timeout=_DEFAULT_TIMEOUT,
    )


def generate_comment(
    clinic_name: str,
    person_name: str,
    pdf_text: str,
    max_retries: int = 3,
) -> str:
    """通常モードでコメントを1件生成する。指数バックオフリトライ付き。

    Args:
        clinic_name: 医院名
        person_name: 氏名
        pdf_text: PDFから抽出したテキスト全文
        max_retries: 最大リトライ回数

    Returns:
        生成されたコメントテキスト（200〜350文字）
    """
    client = _create_client()
    user_prompt = _build_user_prompt(clinic_name, person_name, pdf_text)

    for attempt in range(max_retries + 1):
        try:
            response = client.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=CLAUDE_MAX_TOKENS,
                temperature=CLAUDE_TEMPERATURE,
                system=[
                    {
                        "type": "text",
                        "text": SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[{"role": "user", "content": user_prompt}],
            )
            if not response.content or not response.content[0].text.strip():
                logger.warning(
                    f"API応答が空です: {clinic_name} {person_name} "
                    f"(試行{attempt + 1}/{max_retries + 1})"
                )
                if attempt < max_retries:
                    wait = 2 ** (attempt + 1)
                    time.sleep(wait)
                    continue
                raise ValueError(
                    f"API応答が空です: {clinic_name} {person_name}"
                )

            comment = response.content[0].text.strip()
            logger.info(
                f"コメント生成完了: {clinic_name} {person_name} "
                f"({len(comment)}文字)"
            )
            return comment

        except (
            anthropic.RateLimitError,
            anthropic.InternalServerError,
            anthropic.APITimeoutError,
            anthropic.APIConnectionError,
        ) as e:
            if attempt < max_retries:
                wait = 2 ** (attempt + 1)
                logger.warning(
                    f"API エラー (試行{attempt + 1}/{max_retries + 1}): {e}. "
                    f"{wait}秒後にリトライ..."
                )
                time.sleep(wait)
            else:
                logger.error(f"API エラー: リトライ上限到達: {e}")
                raise


def create_batch_requests(
    items: list[dict],
) -> list[dict]:
    """Batch API用のリクエストリストを作成する。

    Args:
        items: [{"custom_id": str, "clinic_name": str,
                 "person_name": str, "pdf_text": str}, ...]

    Returns:
        Batch API送信用のリクエストリスト
    """
    requests = []
    for item in items:
        user_prompt = _build_user_prompt(
            item["clinic_name"], item["person_name"], item["pdf_text"]
        )
        requests.append(
            {
                "custom_id": item["custom_id"],
                "params": {
                    "model": CLAUDE_MODEL,
                    "max_tokens": CLAUDE_MAX_TOKENS,
                    "temperature": CLAUDE_TEMPERATURE,
                    "system": [
                        {
                            "type": "text",
                            "text": SYSTEM_PROMPT,
                            "cache_control": {"type": "ephemeral"},
                        }
                    ],
                    "messages": [{"role": "user", "content": user_prompt}],
                },
            }
        )
    return requests


def submit_batch(items: list[dict]) -> str:
    """Batch APIにリクエストを一括送信する。

    Args:
        items: create_batch_requestsと同じ形式のリスト

    Returns:
        バッチID
    """
    client = _create_client()
    requests = create_batch_requests(items)

    logger.info(f"Batch API送信: {len(requests)}件")

    batch = client.messages.batches.create(requests=requests)

    logger.info(f"Batch作成完了: ID={batch.id}, ステータス={batch.processing_status}")
    return batch.id


def get_batch_status(batch_id: str) -> dict[str, Any]:
    """バッチの処理ステータスを取得する。

    Returns:
        {"status": str, "results_url": str | None, ...}
    """
    client = _create_client()
    batch = client.messages.batches.retrieve(batch_id)

    return {
        "id": batch.id,
        "status": batch.processing_status,
        "request_counts": {
            "processing": batch.request_counts.processing,
            "succeeded": batch.request_counts.succeeded,
            "errored": batch.request_counts.errored,
            "canceled": batch.request_counts.canceled,
            "expired": batch.request_counts.expired,
        },
    }


def get_batch_results(batch_id: str) -> tuple[dict[str, str], list[str]]:
    """バッチの結果を取得する。

    Returns:
        (results, failed_ids) のタプル。
        results: {custom_id: comment_text, ...}
        failed_ids: 失敗したcustom_idのリスト
    """
    client = _create_client()
    results = {}
    failed_ids: list[str] = []

    for result in client.messages.batches.results(batch_id):
        custom_id = result.custom_id
        if result.result.type == "succeeded":
            comment = result.result.message.content[0].text.strip()
            if comment:
                results[custom_id] = comment
                logger.info(f"Batch結果取得: {custom_id} ({len(comment)}文字)")
            else:
                logger.warning(f"Batch結果が空: {custom_id}")
                failed_ids.append(custom_id)
        else:
            logger.error(f"Batch失敗: {custom_id} - {result.result.type}")
            failed_ids.append(custom_id)

    logger.info(
        f"Batch結果取得完了: {len(results)}件成功, {len(failed_ids)}件失敗"
    )
    return results, failed_ids
