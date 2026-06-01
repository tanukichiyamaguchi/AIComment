"""Claude APIコメント生成モジュール。通常モードとBatchモード両対応。"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

import anthropic
from anthropic.types import TextBlock
from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
from anthropic.types.messages.batch_create_params import Request

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

#出力タスク
報告シートを読み、以下の4要素を構造化出力（JSON）で返してください：

- `clinic_name` : PDF本文（タイトル・冒頭・最終ページなど）に明記されている歯科医院名（最も完全な表記を選ぶ）。判別不能なら空文字列。
- `person_name` : 報告者の氏名（「報告者」「氏名」「Dr」などのラベル付近）。役職や肩書きは含めない。判別不能なら空文字列。
- `sample_title` : この実践事例のタイトル / テーマ（「タイトル」「テーマ」「取り組み名」「冒頭の見出し」など）。判別不能なら空文字列。
- `comment` : 報告者一人ひとりに向けた手書き調コメント本文（200〜350文字）。タイトルや宛名は不要。コメント文のみ。

【comment フィールドの絶対禁止事項】
- 医院名（「○○歯科」「○○クリニック」「○○医院」など、抽出された clinic_name に相当する固有名詞）を本文中に一切含めない。
- 報告者氏名（姓・名・フルネーム、抽出された person_name に相当する固有名詞）を本文中に一切含めない。
- 「○○様」「○○先生」「○○院長」など、氏名付きの呼びかけ・敬称をしない（氏名を伴わない「先生」「皆様」も使わない）。
- 「貴院」「御院」「貴クリニック」など医院を直接指す代名詞は使わない。
- 冒頭・末尾の宛名行（「○○歯科医院 ○○様」等）は一切付けない。
コメントは個人名・医院名に依存しない、内容そのものに向けた感想・提案として書いてください。"""


# ── テーマ別プロンプト ──
# ファイル名（例: "101-01-02【B1】読書_35歳までに...pdf"）の 】 直後〜最初の _ に
# 書かれたテーマ名で振り分ける。当てはまらないものは ``SYSTEM_PROMPT``（既存
# プロンプト＝じっせん実践事例）にフォールバック。各プロンプトは構造化出力
# （EXTRACTION_SCHEMA: clinic_name / person_name / sample_title / comment）の
# 4 フィールドを返す前提。本文内に医院名・氏名を入れない方針は ``_scrub_names_from_comment``
# が抽出後に保険として除去するので、テーマ別プロンプトで個別に重複明記しなくてもよい。

# 読書感想文用プロンプト（テーマ「読書」）。
# 構造化出力のため、sample_title は書籍名を意味する。
READING_SYSTEM_PROMPT = """\
あなたは歯科医院のスタッフ育成やマネジメントをサポートするメンターです。

アップロードされた読書感想文（ファイル）の内容を読み取り、提出者のモチベーションが上がるようなフィードバックコメントを作成してください。
以下の【条件】を厳守して出力してください。

【条件】
1. 情報の自動抽出: アップロードされたファイルから「書籍名」「具体的な気づきや実践内容」を読み取り、コメント内に自然に組み込んでください。
2. 文字数: 100文字〜250文字程度に収めること。
3. 個別化: 定型文や一般的な感想は避け、提出者が書いた「具体的な気づき」や「実践した行動・そこから得た結果」を必ずピックアップして称賛すること。
4. 重複回避の工夫: 同一人物から複数の感想文が提出されることを想定し、コメントの構成や言い回しがパターン化しないようにすること。毎回、その書籍の「特有のテーマ（例：チーム連携、プレゼン技術、マインドセット、マネジメント等）」にフォーカスして切り口を変えること。
5. トーン: 提出者の努力や成長を認め、未来の活躍への期待を込めた、温かくポジティブなトーンで作成すること。
6. 提出者の名前は入れない。
7. 過度にカギ括弧を使用しない。
8. アスタリスクは絶対に使用しない。

#出力タスク
読書感想文を読み、以下の4要素を構造化出力（JSON）で返してください：

- `clinic_name` : 感想文に書かれた歯科医院名。判別不能なら空文字列。
- `person_name` : 提出者の氏名。判別不能なら空文字列。
- `sample_title` : 書籍名。判別不能なら空文字列。
- `comment` : 上記【条件】に従ったコメント本文。タイトルや宛名は不要。コメント文のみ。
"""


# ── 実践レポート系（SNS活用・チーム運営・パートナーシップ等）共通プロンプト ──
# LIGレポート / パートナー / チームMTG の3テーマは「称賛＋改善提案」という同一構造
# のため、指示・構成ルール・出力タスクを共有し、文例だけテーマ別に差し替える
# （prompt diff 最小化）。元のユーザー指示にあった末尾の「【条件】1〜8」は読書
# プロンプトの転記（書籍名への言及・文字数100〜250の矛盾）だったため不採用とし、
# 本体の指示（100〜200字）を採用している。
_PRACTICE_PRAISE_HEAD = """\
あなたは歯科医院の実践をサポートするメンターです。
以下の歯科医院による実践事例（SNS活用・チーム運営・パートナーシップ等）を読み、条件に従って称賛と改善提案のコメントを作成してください。

【書き方の条件】
- 文章形式で書く。称賛ポイント・改善ポイントを項目で分けず、一連の文章にする。
- 文量は100文字〜200文字程度。
- 特にポイントとなる褒めどころには「!」「!!」を活用し、特徴あるコメントにする。
- 文章の締めは「〇〇ですね。」のような話し言葉調にする。
- アスタリスク（*）は使用しない。
- 文を締める際に「ですよ」は使用しない。

【コメントの構成ルール】
1. 承認の言葉: 「すごいですね」「素晴らしい」「さすがですね」などをランダムに、1コメントにつき1〜2回程度含めて承認する。
2. 具体的な褒めポイント: 実践内容（登録者数の増加、業務効率化、患者満足度向上、システム連携など）の中から、特に優れた具体的な取り組みを特定して褒める。取り組み自体ではなく、取り組みの中でポイントとなる点について褒める。
3. 更なる改善への提案: 今後の成果をさらに高めるための建設的な提案を1つ以上する。具体的すぎない抽象的な内容でよい。本文に「アドバイス」という言葉は使わない。
"""

_PRACTICE_PRAISE_TAIL = """\
#出力タスク
以下の4要素を構造化出力（JSON）で返してください：
- `clinic_name` : 報告に書かれた歯科医院名。判別不能なら空文字列。
- `person_name` : 提出者の氏名。判別不能なら空文字列。
- `sample_title` : 実践事例のタイトル / 取り組み名 / テーマ。判別不能なら空文字列。
- `comment` : 上記の条件・構成ルールに従ったコメント本文（100〜200文字程度）。宛名やタイトルは不要、コメント文のみ。医院名・提出者名は本文に含めない。
"""

_LIG_REPORT_EXAMPLES = """\
【コメント例（LIGレポート）】
わずか3ヶ月で登録者数を1,500人まで伸ばし、電話対応の削減という目に見える成果を出されているのは本当にすごいですね！ 特に、矯正やインプラントの相談窓口をLINEに集約し、患者様の利便性を大きく高めた点が素晴らしいです。院内全体に「電話ゼロ」の文化を広げたプロセスもさすがですね。成果を出す他院をベンチマークし、自院に最適化して取り入れる進め方がとても論理的ですね！
"""

_PARTNER_EXAMPLES = """\
【コメント例（パートナー）】
メンバー一人ひとりの背景を理解しようと1on1面談を即断された点、素晴らしいですね。脱退というピンチでも感情をコントロールし、誠実さを貫いた姿勢はさすがですね。数字だけでなくマインドの変革こそ重要だと本質に気づかれた点もすごいですね！ 本音でぶつかり合って絆を深め、相互フィードバックの仕組み化まで繋げた点は見事です。この本音の対話の文化を院内にも広げられると、さらに強い組織づくりが加速しますね！
"""

_TEAM_MTG_EXAMPLES = """\
【コメント例（チームMTG）】
全メンバーと1on1MTGを実施されたとのこと、素晴らしいですね！ 信頼関係の構築を最優先にした姿勢が、個々の悩みの早期発見に繋がっています。予定を見える化し、互いの動きを意識できる環境を作った点もすごいですね。次は開催時間や場所の工夫を続けられると、さらに一体感が高まりますね。役割分担を明確にして全員が主体的に動ける体制づくりもさすがですね！
"""

LIG_REPORT_SYSTEM_PROMPT = (
    _PRACTICE_PRAISE_HEAD + "\n" + _LIG_REPORT_EXAMPLES + "\n" + _PRACTICE_PRAISE_TAIL
)
PARTNER_SYSTEM_PROMPT = (
    _PRACTICE_PRAISE_HEAD + "\n" + _PARTNER_EXAMPLES + "\n" + _PRACTICE_PRAISE_TAIL
)
TEAM_MTG_SYSTEM_PROMPT = (
    _PRACTICE_PRAISE_HEAD + "\n" + _TEAM_MTG_EXAMPLES + "\n" + _PRACTICE_PRAISE_TAIL
)


# テーマ判定で認識する 5 テーマ。ファイル名の 】 直後〜最初の _ までの文字列を
# 半角/全角スペース除去後にこの集合と完全一致比較する。順序は判定に影響しない。
_KNOWN_THEMES: tuple[str, ...] = (
    "読書",
    "LIGレポート",
    "パートナー",
    "チームMTG",
    "チーム実践",
)

# テーマ → システムプロンプト の対応表。
# プロンプト未提供のテーマは ``get_system_prompt`` のフォールバックで
# 既存プロンプト（SYSTEM_PROMPT）が使われる。
_THEME_PROMPTS: dict[str, str] = {
    "読書": READING_SYSTEM_PROMPT,
    "LIGレポート": LIG_REPORT_SYSTEM_PROMPT,
    "パートナー": PARTNER_SYSTEM_PROMPT,
    "チームMTG": TEAM_MTG_SYSTEM_PROMPT,
    # "チーム実践" は別途追加予定。提供されるまでは fallback（既存プロンプト）で動作。
}


def extract_theme(filename: str) -> str:
    """PDF ファイル名からテーマ名を抽出する。

    ファイル名フォーマット（例）:
        ``"101-01-02【B1】読書_35歳までに必ずやるべきこと_吉野浩史.pdf"``
        ``"112-16【B1】LIGレポート _医療法人志結会おざき歯科医院.pdf"`` (空白入りもあり)

    ロジック:
        1. ``】`` の直後の文字列を取り出す（無ければ空）。
        2. 最初の ``_`` で切り、先頭/末尾の空白と半角・全角スペースを除去。
        3. 既知 5 テーマと完全一致比較。一致しなければ空文字列を返す（＝既存プロンプト適用）。

    Returns:
        マッチしたテーマ名（``"読書"`` など）。マッチしないなら ``""``。
    """
    if not filename:
        return ""
    after_bracket = filename.split("】", 1)
    if len(after_bracket) < 2:
        return ""
    segment = after_bracket[1].split("_", 1)[0].strip()
    # ファイル名内の見栄え用空白を除去（"LIGレポート " のような末尾空白対策）。
    segment_normalized = segment.replace(" ", "").replace("　", "")
    for theme in _KNOWN_THEMES:
        if segment_normalized == theme:
            return theme
    return ""


def get_system_prompt(theme: str) -> str:
    """テーマ名に対応するシステムプロンプトを返す。

    未知テーマ / 空文字列 / プロンプト未提供のテーマは既存プロンプト
    （じっせん実践事例用 SYSTEM_PROMPT）にフォールバックする。
    """
    return _THEME_PROMPTS.get(theme, SYSTEM_PROMPT)


def _build_user_prompt(pdf_text: str, pdf_filename: str = "") -> str:
    """ユーザープロンプトを構築する（テーマ非依存・抽出フィールドは全テーマ共通）。

    ファイルが ``実践事例`` か ``読書感想文`` かによらず読めるよう、ファイル種別の
    断定は避けて中立的に書く。各テーマの書き方の違いは system プロンプト側で
    制御する（``get_system_prompt`` 参照）。
    """
    file_note = f"\nファイル名: {pdf_filename}\n" if pdf_filename else ""
    return (
        "以下は歯科医院スタッフから提出された報告ファイルです。"
        "本文から医院名・報告者氏名・タイトル（実践事例名 / 書籍名 / 取り組み名 等）を抽出し、"
        "本文に対する手書き調コメントを生成してください。"
        "コメントの書き方（文字数・トーン・禁止事項など）は system プロンプトに従ってください。"
        f"{file_note}\n---\n{pdf_text}\n---"
    )


_DEFAULT_TIMEOUT = 120  # seconds


_EXTRACTED_FIELDS = ("clinic_name", "person_name", "sample_title", "comment")

EXTRACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "clinic_name": {
            "type": "string",
            "description": (
                "歯科医院名。報告シート本文から抽出。"
                "判別不能なら空文字列を返す。"
            ),
        },
        "person_name": {
            "type": "string",
            "description": (
                "報告者の氏名。役職・肩書きは含めない。"
                "判別不能なら空文字列を返す。"
            ),
        },
        "sample_title": {
            "type": "string",
            "description": (
                "この実践事例のタイトル / テーマ。"
                "判別不能なら空文字列を返す。"
            ),
        },
        "comment": {
            "type": "string",
            "description": (
                "報告者一人ひとりに向けた手書き調コメント本文（200〜350文字）。"
                "医院名（clinic_name に相当する固有名詞）と報告者氏名"
                "（person_name に相当する固有名詞）は本文中に一切含めない。"
                "「○○様」「○○先生」「貴院」等の宛名・呼びかけ・敬称も使わない。"
            ),
        },
    },
    "required": ["clinic_name", "person_name", "sample_title", "comment"],
    "additionalProperties": False,
}


def _create_client() -> anthropic.Anthropic:
    """Anthropicクライアントを作成する。"""
    return anthropic.Anthropic(
        api_key=ANTHROPIC_API_KEY,
        timeout=_DEFAULT_TIMEOUT,
    )


def _build_extraction_request_params(
    system_prompt: str | None = None,
) -> dict[str, Any]:
    """構造化出力リクエスト用の共通パラメータを生成する。

    Args:
        system_prompt: 使用するシステムプロンプト。``None`` のときは既存
            プロンプト（SYSTEM_PROMPT）にフォールバック（後方互換）。
    """
    return {
        "model": CLAUDE_MODEL,
        "max_tokens": CLAUDE_MAX_TOKENS,
        "temperature": CLAUDE_TEMPERATURE,
        "system": [
            {
                "type": "text",
                "text": system_prompt if system_prompt is not None else SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        "output_config": {
            "format": {"type": "json_schema", "schema": EXTRACTION_SCHEMA},
        },
    }


def _parse_extraction(text: str) -> dict[str, str]:
    """JSONテキストをパースし、必須フィールドを文字列として補完する。"""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(
            f"Claude応答のJSONパースに失敗: {e}: {text[:200]}"
        ) from e
    if not isinstance(data, dict):
        raise ValueError(f"Claude応答がオブジェクトではありません: {text[:200]}")
    extracted = {field: str(data.get(field, "") or "").strip() for field in _EXTRACTED_FIELDS}
    extracted["comment"] = _scrub_names_from_comment(
        comment=extracted["comment"],
        clinic_name=extracted["clinic_name"],
        person_name=extracted["person_name"],
    )
    return extracted


# 敬称（呼びかけ）のサフィックス。氏名・医院名と組み合わさったときに丸ごと除去する。
_HONORIFIC_SUFFIXES = ("様", "さん", "さま", "先生", "院長", "ドクター", "Dr.", "Dr")
# 名前トークンの最小長（短いと普通の語と衝突しやすいので除去スキップ）。
_NAME_SCRUB_MIN_LEN = 2


def _scrub_names_from_comment(
    comment: str,
    clinic_name: str,
    person_name: str,
) -> str:
    """コメント本文から医院名・報告者氏名と関連する敬称を除去する。

    プロンプトで禁止しているが、AI が偶発的に混入させた場合の保険として、
    抽出後にプログラム的に取り除く。clinic_name / person_name そのものと、
    それらに敬称が付いた「氏名+様」「氏名+先生」「医院名+様」等を消す。
    名前トークンが極端に短い（1文字）場合は普通の語と衝突するため除去しない。

    削除後に残る不要な空白・連続スペース・先頭末尾の改行をならして返す。
    """
    if not comment:
        return comment
    result = comment
    names = [n for n in (clinic_name, person_name) if n and len(n) >= _NAME_SCRUB_MIN_LEN]
    # 長い名前から先に消す（短い別名が長い名前の一部を切り崩さないように）。
    for name in sorted(set(names), key=len, reverse=True):
        for suffix in _HONORIFIC_SUFFIXES:
            result = result.replace(f"{name}{suffix}", "")
        result = result.replace(name, "")
    # 連続スペースを 1 つに、前後の空白・空行をならす。
    result = re.sub(r"[ \t　]+", " ", result)
    result = re.sub(r"\n{3,}", "\n\n", result)
    return result.strip()


def generate_comment_with_metadata(
    pdf_text: str,
    pdf_filename: str = "",
    max_retries: int = 3,
) -> dict[str, str]:
    """通常モードで医院名・氏名・実践事例タイトル・コメントを一括抽出/生成する。

    Args:
        pdf_text: PDFから抽出したテキスト全文
        pdf_filename: 元PDFのファイル名（プロンプトの補助情報として使用）
        max_retries: APIエラー時の最大リトライ回数

    Returns:
        ``{"clinic_name", "person_name", "sample_title", "comment"}`` を必ず含む辞書。
        AIが判別できなかったフィールドは空文字列で返る。``comment`` のみ
        空文字列の場合は ``ValueError`` を送出する。
    """
    client = _create_client()
    user_prompt = _build_user_prompt(pdf_text, pdf_filename)
    theme = extract_theme(pdf_filename)
    system_prompt = get_system_prompt(theme)
    base_params = _build_extraction_request_params(system_prompt=system_prompt)
    logger.info(
        f"テーマ判定: filename='{pdf_filename}' → theme='{theme or '(該当なし→既定)'}'"
    )

    for attempt in range(max_retries + 1):
        try:
            response = client.messages.create(
                **base_params,
                messages=[{"role": "user", "content": user_prompt}],
            )
            text_blocks = [
                b for b in response.content if isinstance(b, TextBlock)
            ]
            if not text_blocks or not text_blocks[0].text.strip():
                logger.warning(
                    f"API応答が空です (試行{attempt + 1}/{max_retries + 1})"
                )
                if attempt < max_retries:
                    time.sleep(2 ** (attempt + 1))
                    continue
                raise ValueError("API応答が空です")

            data = _parse_extraction(text_blocks[0].text)
            if not data["comment"]:
                logger.warning(
                    f"コメントが空です (試行{attempt + 1}/{max_retries + 1})"
                )
                if attempt < max_retries:
                    time.sleep(2 ** (attempt + 1))
                    continue
                raise ValueError("コメントが空です")

            logger.info(
                f"抽出完了: clinic='{data['clinic_name']}' "
                f"person='{data['person_name']}' "
                f"title='{data['sample_title']}' "
                f"comment={len(data['comment'])}文字"
            )
            return data

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

    raise RuntimeError("unreachable: retry loop exited without return or raise")


def create_batch_requests(
    items: list[dict],
) -> list[Request]:
    """Batch API用のリクエストリストを作成する（構造化出力）。

    Args:
        items: [{"custom_id": str, "pdf_text": str,
                 "pdf_file_name": str (任意)}, ...]

    Returns:
        Batch API送信用のリクエストリスト
    """
    requests: list[Request] = []
    for item in items:
        pdf_filename = item.get("pdf_file_name", "")
        user_prompt = _build_user_prompt(
            pdf_text=item["pdf_text"],
            pdf_filename=pdf_filename,
        )
        # Batch モードはアイテム単位でテーマを判定する。同一バッチに混在する
        # 異なるテーマでも、それぞれ専用プロンプトでリクエストできる。
        theme = extract_theme(pdf_filename)
        system_prompt = get_system_prompt(theme)
        params = MessageCreateParamsNonStreaming(
            model=CLAUDE_MODEL,
            max_tokens=CLAUDE_MAX_TOKENS,
            temperature=CLAUDE_TEMPERATURE,
            system=[
                {
                    "type": "text",
                    "text": system_prompt,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            output_config={
                "format": {"type": "json_schema", "schema": EXTRACTION_SCHEMA},
            },
            messages=[{"role": "user", "content": user_prompt}],
        )
        requests.append(Request(custom_id=item["custom_id"], params=params))
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


def get_batch_results(
    batch_id: str,
) -> tuple[dict[str, dict[str, str]], list[str]]:
    """バッチの結果を取得し、構造化出力をパースする。

    Returns:
        (results, failed_ids) のタプル。
        results: ``{custom_id: {clinic_name, person_name, sample_title, comment}}``
        failed_ids: 失敗または空コメントのcustom_idのリスト
    """
    client = _create_client()
    results: dict[str, dict[str, str]] = {}
    failed_ids: list[str] = []

    for result in client.messages.batches.results(batch_id):
        custom_id = result.custom_id
        if result.result.type != "succeeded":
            logger.error(f"Batch失敗: {custom_id} - {result.result.type}")
            failed_ids.append(custom_id)
            continue

        text_blocks = [
            b
            for b in result.result.message.content
            if isinstance(b, TextBlock)
        ]
        if not text_blocks or not text_blocks[0].text.strip():
            logger.warning(f"Batch結果が空: {custom_id}")
            failed_ids.append(custom_id)
            continue

        try:
            data = _parse_extraction(text_blocks[0].text)
        except ValueError as e:
            logger.error(f"Batch結果のJSONパース失敗: {custom_id} - {e}")
            failed_ids.append(custom_id)
            continue

        if not data["comment"]:
            logger.warning(f"Batch結果のコメントが空: {custom_id}")
            failed_ids.append(custom_id)
            continue

        results[custom_id] = data
        logger.info(
            f"Batch結果取得: {custom_id} "
            f"clinic='{data['clinic_name']}' person='{data['person_name']}' "
            f"title='{data['sample_title']}'"
        )

    logger.info(
        f"Batch結果取得完了: {len(results)}件成功, {len(failed_ids)}件失敗"
    )
    return results, failed_ids
