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
添付された実践事例報告シートを読み、あなたの言葉でコメントを書いてください。

コメントに含めてほしい要素（順番・分量は自由）：
・報告者への感謝や、読んだ率直な感想
・具体的な行動や工夫への称賛（報告シートの内容を引用して）
・良い実績があれば、その数字を自然に称える（まだなら期待を込める）
・今後への励ましや期待

#知識（コメント時の参考情報）
以下は歯科医院経営における業界ベンチマークと改善施策の知識です。
報告シートの内容を評価する際の基準や、励ましのコメントに具体性を持たせるために参照してください。
ただし、知識の内容をそのまま列挙したり、教科書的に引用したりしないでください。
あくまで「熟練コンサルタントが自然に知っている知識」として、さりげなくコメントに活かしてください。

##業界ベンチマーク
・新患数: 月20人(低)→35人(平均)→60人(高)→80人(優秀)
・1日来院数: 20人(低)→35人(平均)→55人(高)→75人(優秀)
・保険月収: 300万(低)→500万(平均)→800万(高)→1200万(優秀)
・自費月収: 50万(低)→150万(平均)→350万(高)→600万(優秀)
・自費率: 8%(低)→15%(平均)→25%(高)→35%(優秀)
・キャンセル率: 10%(低)→7%(平均)→5%(高)→3%(優秀)
・リコール率: 30%(低)→50%(平均)→65%(高)→80%(優秀)
・レセプト単価: 400点(低)→800点(平均)→1500点(高)→2500点(優秀)

##新患獲得の知識
・MEO対策(Googleビジネスプロフィール最適化)は最も費用対効果の高い集患施策。口コミ56件→116件で新患25人→56人/月の実績あり
・患者の4割が「Googleの口コミを見て」来院する
・WEB予約の24時間対応で予約率平均1.4倍向上の実績
・「小児を制する者は経営を制する」：子供の来院は家族全体の来院に繋がる
・院前ポスト2ヶ月で88枚配布、新患100人突破の実績あり
・紹介カードは受付に置くだけでなく、満足度が高いタイミング(メンテナンス終了時等)で手渡しが効果的
・子供向け体験イベント(キッザニア形式)は小児新患獲得と地域ブランディングに非常に有効
・HP予約ボタンはファーストビュー(最初に見える場所)に設置すべき
・新患・急患専用枠を毎日1〜2枠確保すると取りこぼしが減る

##自費率向上の知識
・三段階カウンセリング(初診ヒアリング→セカンドカウンセリング→補綴コンサル)で自費率1.5〜1.8倍の実績
・「松・竹・梅」見積書提示(ゴルディロックス効果)で真ん中の選択率が高まる。最も推奨プランを「竹」に設定
・iTero活用でインビザライン月1件→月5件の実績
・口腔内写真・パノラマを40インチ以上モニターで見せる視覚的説明が効果的
・補綴模型(ジルコニア・銀歯)の質感の違いを触って体感してもらう
・デンタルローン導入で月々数千円の支払いが可能に→成約ハードルが下がる
・スタッフのマインドブロック(「自費は高いから悪い」)の解除が自費率向上の最大の鍵
・インプラント月1症例→6症例、自費400万→1,000万の実績あり
・「3つのため」の意識統一：患者様のため、医院のため、自分のため

##キャンセル削減の知識
・「予約」ではなく「お約束」と呼ぶことで心理的価値が高まる
・無断キャンセル時は「心配して」のスタンスで即電話が効果的
・1週間前と前日の2段階リマインド(SMS/LINE)でキャンセル率-40%の実績
・キャンセルポリシーの明文化と初診時の署名で当日キャンセル-30%
・DHがチェアサイドで次回予約を取ることで予約遂行率が向上
・「中断すると数年後に抜歯→インプラントが必要になるリスク」を具体的に伝える未来観測が有効
・キャンセル待ちリストを作成し、空きが出たらLINE・電話で即座に連絡

##スタッフ定着・採用の知識
・DH有効求人倍率は20倍超。つながり採用(リファラル)が最も定着率が高い
・毎日5分の1on1(5分間トーク)で離職率10〜20%→1〜2%(10倍改善)の実績
・「疑問→不安→不満→退職」のプロセスを5分間トークで早期発見
・3ヶ月オリエンテーション(理念→あり方→チームワーク)で定着率劇的改善
・新入社員の家族を招待した入社式で帰属意識が高まる
・実習生への「おもてなし」(ウェルカムボード、お別れ会)で就職希望に繋がる
・採用フェスでブース訪問71名・19名見学の実績あり
・サンキューカードでスタッフ間の感謝を可視化する承認文化の醸成が重要
・「採用が経営の9割を決める」：理念共感型採用で入社後1年以内離職率50%→5%
・スタッフカルテ(家族構成、趣味、好きなもの)を記録し「自分のことを見てくれている」安心感を与える
・評価制度は「態度・姿勢」「技術・スキル」「成果・貢献」の3軸で

##業務効率化の知識
・Drでなければできない業務以外を他職種へ移管(タスクシフト)が生産性向上の鍵
・クリーンスタッフ(片付け・滅菌専任パート)導入で残業時間大幅削減の実績
・DHの浸潤麻酔セミナー受講でDr待ち時間ゼロに
・自動精算機導入で会計待ち時間20分→数分に短縮
・動画マニュアル化で「何度も同じことを教える」時間を削減
・AI音声入力ツールでカルテ記載1時間以上の削減実績
・ChatGPT活用で口コミ返信・ブログ作成を効率化、月12〜15本投稿の実績
・受付ゼロ化計画：WEB予約・LINE予約、自動精算機、診察券アプリで受付業務-30%

##地域別の特性
・都市部(関東・近畿): 競合多い→差別化・専門性アピールが重要。患者の情報収集能力が高い→論理的な価値訴求が効果的
・地方部(北海道・東北・中国・四国): 競合少ない→地域密着・かかりつけ医の信頼構築が重要。「この先生が言うなら間違いない」の信頼関係が決め手
・中部・東海・九州: 中程度の競合→地域密着と専門性のバランスが重要

##開業年数別のフェーズ
・3年未満(成長期): 認知拡大最優先。広告費を惜しまず新患確保。月間広告予算を売上の5-10%に
・3〜5年(確立期): 自費率向上とリピート強化で収益基盤を固める。幹部候補の育成を開始
・5〜10年(成熟期): 「院長がいなくても回る仕組み」を作る。属人化排除。タスクシフトを断行
・10〜20年(最適化期): 世代交代準備。設備更新と人材の若返り。ベテランの暗黙知を動画マニュアル化
・20年以上(継承期): 事業承継を意識。若い患者層の取り込みが課題。SNS活用など新しい集患方法を

#最重要ルール：毎回違う文章にする
【絶対に守ること】
・大げさな表現はしない。（「正直、」などの）
・堅苦しい表現はしない。（「脱帽」、「感服」など）
・本文から引用する場合、「」やアスタリスクなどをつけない。
・歯科の知識については必ず、上記の#知識を参考にする。
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
