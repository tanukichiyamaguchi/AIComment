# じっせん君コメントシステム - Task Tracker

## Phase 23: 処理速度 × 出力品質・正確性の改善（2026-07-02〜）

### ゴール
Phase 22（耐障害系）の次段。ユーザー優先領域「処理速度」「出力品質・正確性」に絞った
修正を優先度順の複数 PR で実施（Gmail 関連・コスト予算ゲート・定期実行/通知はスコープ外）。
6 レンズ監査 + 広域スイープ（Explore）→ 実装設計（Plan）で計画策定。
計画ファイル: `/root/.claude/plans/swift-strolling-crystal.md`

### PR-6: 長時間ハードニング（完了 2026-07-06）
- [x] 6a: 一時ディレクトリ掃除の全域化（mkdtemp直後からtry/finally。未ガード区間の
      例外でrmtree不達→リークする問題、P-034）
- [x] 6b: disk由来 batch_results/attachments の防御的パース（meta.get化 + 空comment
      の per-item エラー化 + JSONDecodeError の復旧手順つき RuntimeError）
- [x] 6c: ENABLE_GMAIL_DRAFTS=false 時のディスク逐次解放（アップロード直後に
      サブディレクトリ削除。ON は従来通り、回帰テストで固定）
- [x] PR #61 マージ済み（2026-07-06）
- [x] pytest 719 → 725 件 pass / mypy clean
- 検証済み・対処不要: ログローテ(100MB×5世代) / logs/上書き型 / キャッシュ有界 /
  メモリ(1000件で~115MB)

### PR-1: 正確性バグ修正（完了）
- [x] **1a: 回収時 missing 誤報** — `--step results --batch-id`回収/自動レジュームで
      items が Drive 全件再走査から作られるため、過去処理済み PDF が「コメント未取得」
      誤報される問題を修正（3分岐: 処理済み/管理番号抽出不可/真のmissing、P-032）
- [x] **1b: 時間バジェット二重消費** — resume フェーズと通常フェーズが各々満額
      poll_max_seconds を消費し合計で GHA 6h 超過し得た問題を、run() 単一 deadline +
      `_remaining_seconds()` で解消（P-033）
- [x] **1c: get_open_batch_ids の O(n²)** — list 線形探索を挿入順保持 dict に置換
- [x] pytest 704 → 712 件 pass / mypy clean
- [x] PR #59 マージ済み（2026-07-04）

### PR-2: 処理速度（完了 2026-07-06）
- [x] 2a: step1 ダウンロード並列化（ThreadPoolExecutor + executor.map で順序決定論、
      既定 workers=1 = 従来逐次パス温存、本番 GHA は env `STEP1_DOWNLOAD_WORKERS: 4`。
      RefreshError は fail-fast 伝播 + cancel_futures）
- [x] 2b: Drive/Sheets サービスの thread-local キャッシュ（build+認証+リフレッシュを
      呼び出しごと → プロセス/スレッドごと 1 回に。conftest autouse でテスト間リセット）
- [x] 2c: `read_master_records` のプロセス内メモ化（step1 strict ガード + step4 の
      二重読みを 1 読みに。空結果も同一オブジェクトでキャッシュ）
- [x] config._get_int 新設（不正値は既定へ fail-soft）
- [x] pytest 725 → 740 件 pass / mypy clean
- [x] PR #62 マージ済み（2026-07-06）

### PR-3: 出力品質（完了）
- [x] PR #60 マージ済み（2026-07-06）
- [x] 3a: コメント品質ガード（`_MIN_COMMENT_CHARS=100`、warning-only。batch失敗化は
      恒久再投入ループのリスクがあるため見送り）。scrub 短縮の INFO ログ + context 追跡付き
- [x] 3b: pdf_creator の本文/じっせん君画像の重なり回避（画像ジオメトリを定数昇格し
      本文下限を導出: box_y+40mm → 画像帯上端+5mm クリアランス）
- [x] pytest 712 → 719 件 pass / mypy clean

### PR-4: workflow入力拡張（完了 2026-07-06）
- [x] step choice に prepare/submit/pdfs を追加（batch-orchestrator の分割実行を
      dispatch から実行可能に。logs/ 手動復元が前提の上級操作と明記）
- [x] poll_max_minutes 入力（既定300）を追加し --poll-max-minutes に配線
      （Batchモードのみ。step=all の既存経路は不変）

### PR-5: PIIログマスク（完了 2026-07-06）
- [x] `utils.mask_name`（先頭1文字+＊）を追加
- [x] 氏名・医院名を平文出力する全ログを棚卸ししてマスク:
      comment_generator 抽出/Batch結果ログ / main・batch_main 完了ログ /
      run_common（医院名解決・下書き蓄積・分割下書き・添付コピー・マスター解決）/
      gmail_client 下書き作成ログ / sheets_client（出力一覧追記・個人名lookup系）
- [x] 対象はログのみ（シート・ファイル名・Drive階層は業務データなので平文のまま）
- [x] pytest 740 件 pass（mask_name 単体5 + 既存2件をマスク期待値に更新）/ mypy clean

### バックログ（見送り・記録のみ）
- OCR フォールバック（スキャンPDF救済、工数L）/ コスト予算ゲート / 定期実行cron /
  GITHUB_STEP_SUMMARY / 失敗通知 / mypy strict化 / batch_main分割リファクタ 等

## Phase 22: 大量PDF運用の耐障害監査と修復（2026-07-02）

### ゴール
「1000 件規模の本番運用で止まらない・二重課金しない・成果物を失わない」
（ユーザー最重要要件）。エージェントチーム（6 レンズ並列監査 + 敵対的検証）で
リスクを洗い出し、コード精読で確認できたものを全修復。

### 監査で確認された問題と修復
- [x] **A: 新規 0 件クラッシュ** — 増分運用の定常ケース（新規なし）で
      `batch_id.txt` FileNotFoundError → 赤ラン。0 件なら正常終了に
- [x] **B: 256MB 無ガード** — `submit_batch` は全件 1 バッチ送信。日本語は
      JSON エスケープで約 6 倍に膨張し 2000〜3000 件で実際に上限到達。
      `plan_batch_chunks` で自動分割（複数バッチ・1 行 1 ID・カンマ区切り回収）
- [x] **C: pdf_text 無上限** — 巨大 PDF がコンテキスト超過 → 毎ラン再投入・
      再失敗の永久ループ。10 万文字で警告付き切り詰め
- [x] **D: 二重課金（最重要）** — ジョブ kill 後の再実行が投入済みバッチを
      再投入。バッチ状態を Sheets `_バッチ管理` タブに永続化し自動レジューム
      （P-030）。期限切れバッチも記録してブロックしない
- [x] **E: Gmail リトライ無し** — バックオフ無し即時 1 回のみ → 恒久ロスト。
      指数バックオフ 5 試行 + num_retries の多層防御
- [x] **F: 25MB 添付上限** — 超過グループの下書きが恒久失敗。17MB で自動分割、
      単独超過は ERROR ログ + 除外（Drive には保存済み）
- [x] **G: 添付資料の恒久ロスト** — dedup がメインの管理番号を借りていたため
      (1) 後から追加された添付 (2) クラッシュ再実行、で永遠にスキップ。
      マーカーベース dedup + マスター単独ルーティング + CB-3 スキップ時の
      case_map 補完（P-031）
- [x] **H: 通常モード 6h 超過** — 1000 件で GHA ジョブ kill → 下書き全損。
      時間バジェット（既定 320 分）で安全に打ち切り、tail 処理まで完走
- [x] **I: read quota 429 ストーム** — 1 行追記ごとに ensure が read×2 発行
      （1000 行で 2000 回超・throttle 対象外）。プロセス内キャッシュで排除
- [x] **J: Drive 冗長全走査** — 1 件ごとに医院/個人フォルダをフル走査
      （1000 件で数千回）。ラン内キャッシュ
- [x] **K: 2 ラン同時の write quota 超過** — throttle 50→28/分
      （合算 56 < 上限 60）
- [x] **L: 認証失効の無駄ループ** — RefreshError を fail-fast + step1 全件
      失敗の loud 停止（無言の緑終了防止）
- [x] **M: ポーリング 24h vs GHA 6h** — 既定 5h に変更（`--poll-max-minutes`）。
      タイムアウトしても D の自動レジュームで次回回収
- [x] **N: re-run で artifact 409** — artifact 名に run_attempt を付与
- [x] **R: 名寄せの表記揺れ** — resolve_master_sheet_name に NFKC 正規化
      （全角/半角差での意図しない HARD FAIL 防止）
- [x] **S: PDF 切り捨ての無言欠落** — 描画領域超過時に loud 警告（P-001）

### 見送り（理由付き）
- master_records の step1/step4 二重読み: 1 ランで +1 read のみ。キャッシュの
  staleness リスクに見合わない
- step4 のディスク滞留（1000 件分の結合 PDF を下書き作成まで保持）: 平均
  2-5MB × 1000 = 2-5GB は runner の 14GB 内。20MB 級が大半を占める運用に
  なったら再検討（その場合は Gmail 添付を Drive 再取得方式に変える）

### 検証
- [x] pytest 633 → 704 件 pass（+71: バッチ状態 7 / レジューム 5 / チャンク
      11 / 添付復旧 6 / Gmail 7 / キャッシュ 7 / 時間バジェット 2 /
      fail-fast 3 / 名寄せ正規化 4 / 切り捨て警告 2 ほか）
- [x] mypy clean
- [ ] PR 作成 → ユーザーレビュー → マージ

### 結果サマリ
（マージ後に記入）

## Phase 21: 参加者マスタータブの「セミナー名 substring」名寄せ（2026-06-26）

### ゴール
target_folder モードで「**入力フォルダ名がセミナー名を含む**全フォルダ」が
同じ参加者マスタータブを参照するようにする。年度・期ごとにフォルダを
分けつつ、マスタータブはセミナー単位 1 枚で運用したい（ユーザー要望）。

例:
- タブ ``参加者マスター(新人育成塾)`` を 1 枚作っておく
- フォルダ ``新人育成塾`` / ``新人育成塾_2026_Q1`` / ``2026_新人育成塾_実践事例``
  のいずれからの実行でも同じタブを参照

### 設計判断
- **複数マッチ時**: 最長一致を採用（より具体的なセミナー名を優先）
- **マッチなし時**: ``参加者マスター(<folder>)`` を fallback として返し、
  HARD FAIL (Phase 18 / ``master_sheet_strict=True``) で停止
- **タブ列挙失敗時**: 致命扱いせず fallback に倒す（後段の HARD FAIL に任せる）
- **純関数化**: 名寄せロジック ``resolve_master_sheet_name`` は API 非依存
  の純関数として実装し、ユニットテスト容易性を確保

### タスク
- [x] baseline: 617 tests pass, mypy clean
- [x] ``sheets_client.list_master_sheet_tabs`` を新設
- [x] ``discover.resolve_master_sheet_name`` (純関数) を新設、
      部分一致 + 最長一致のロジック
- [x] ``DiscoveredContext.master_sheet_name`` を追加
- [x] ``resolve_context`` で ``list_master_sheet_tabs`` を呼び、
      解決結果を ``master_sheet_name`` に格納
- [x] ``RunConfig.from_discovered`` を ``ctx.master_sheet_name`` 採用に
- [x] tests: 名寄せロジック 8 件 / resolve_context 結合 4 件 /
      list_master_sheet_tabs 4 件
- [x] README §4-4 と「命名規約と派生ルール」を新仕様に更新
- [x] pytest 633 件 pass / mypy clean
- [ ] PR 作成 → ドラフト

### 結果サマリ
（マージ後に記入）

## Phase 20: コメント PDF の改行を「文脈の切れ目」で入れる（2026-06-26）

### ゴール
コメントが 1 段落のべた書きになっている問題を解消し、**文脈の切れ目**で
自然に改行されるようにする。同時に PDF 描画側の **行頭禁則**（句読点・閉じ
括弧が行頭に来ない）も入れる。

### 設計判断
- **文脈判定は生成側（Claude）でやる**: 描画ロジックでは「話題の転換」を
  判定できないので、SYSTEM_PROMPT に「文脈の切れ目で `\n` を挿入」を指示
- PDF 描画側は元々 ``text.split("\n")`` で段落を独立処理する仕組みなので、
  プロンプトの追加だけで段落分けが反映される（描画ロジックの大改造は不要）
- 補助として ``_wrap_text`` に最低限の行頭禁則処理を入れる（「、」「。」
  「！」「？」「)」等が次行頭になりそうなら前行末尾にぶら下げる簡易禁則）

### タスク
- [x] baseline: 610 tests pass, mypy clean
- [x] ``SYSTEM_PROMPT`` の「【comment フィールド】」セクションに改行ルールを追加
  - 文脈が切り替わるところで ``\n`` を挿入
  - 機械的に句点ごとには改行しない（1〜2 文/段落 が目安）
  - 段落間に空行は入れない（``\n\n`` ではなく ``\n`` 1 つ）
- [x] ``pdf_creator._wrap_text`` に行頭禁則文字 set ``_GYOTOU_KINSOKU`` を追加、
      改行直後に禁則文字が来そうなら前行末尾にぶら下げる
- [x] tests: 行頭禁則 5 件 / 段落区切り 1 件 / 連続改行 1 件 /
      プロンプトに改行指示が含まれることの検証 1 件
- [x] pytest 617 件 pass / mypy clean
- [ ] PR 作成 → ドラフト

### 結果サマリ
（マージ後に記入）

## Phase 19: テーマ別プロンプト分岐の撤廃（2026-06-26）

### ゴール
ファイル名のテーマ（読書 / LIGレポート / パートナー / チームMTG 等）による
プロンプト振り分けを廃止し、**じっせん実践事例プロンプト（``SYSTEM_PROMPT``）に
全件統一**する（ユーザー方針）。

### タスク
- [x] baseline: 638 tests pass, mypy clean
- [x] comment_generator から ``extract_theme`` / ``get_system_prompt`` /
      テーマ別プロンプト定数（READING / LIG / PARTNER / TEAM_MTG /
      _PRACTICE_PRAISE_HEAD・TAIL / 各 _EXAMPLES）/ ``_KNOWN_THEMES`` /
      ``_THEME_PROMPTS`` を削除
- [x] 通常モード（``generate_comment_with_metadata``）と Batch モード
      （``create_batch_requests``）の system を ``SYSTEM_PROMPT`` 固定
- [x] 「テーマ判定」ログも削除（不要な情報）
- [x] テスト: 6 つのテーマ関連クラスを削除し、「常に SYSTEM_PROMPT が
      使われる」検証クラス（2 ケース）に置換
- [x] pytest 610 件 pass / mypy clean
- [ ] PR 作成 → ドラフト

### 結果サマリ
（マージ後に記入）

## Phase 18: 参加者マスタータブをセミナーごとに分離 + 空タブ HARD FAIL（2026-06-24）

### ゴール
セミナー（= 入力フォルダ名）ごとに参加者マスタータブを独立させる。
target_folder モード（フォルダ自動検出）で `参加者マスター(<フォルダ名>)`
形式のタブを使い、タブ不在 / 0 件のときは PDF 処理に入る前に HARD FAIL で
即停止する（F-09 撤回の理由だった「ユーザー運用と食い違う」は、HARD FAIL で
「気づけない事故」を排除することで解消）。

### 設計判断
- タブ名は **フォルダ名をそのまま `()` 内に使う**（例: `新人育成塾` →
  `参加者マスター(新人育成塾)`）
- HARD FAIL は **target_folder モードのみ**（`master_sheet_strict=True`）。
  プロファイルモードは共有タブを 1 枚使い回す既存運用を維持
  （`master_sheet_strict=False`、後方互換）
- Batch モードでは **Anthropic API 投入前**（Step1）にガード（料金の無駄を防ぐ）
- Resume パス（`--step results`/`pdfs`）にも保険として Step4 で重ねる（多層防御）
- HARD FAIL 時は「中止」マーカーを fail-soft で 1 行追記してから例外送出

### タスク
- [x] baseline: 629 tests pass, mypy clean
- [x] discover.RunConfig: `master_sheet_name = f"参加者マスター({target})"` + `master_sheet_strict=True`
- [x] run_common: `MasterSheetEmptyError` + `require_non_empty_master`
- [x] main.run: read_master_records 後に HARD FAIL を組み込む（中止マーカー追記 → raise）
- [x] batch_main.step1_prepare: Step1 開始時に HARD FAIL（Anthropic 投入前のガード）
- [x] batch_main._process_results_and_create_pdfs: Step4 にも保険を入れる
- [x] tests: test_discover の派生規則、test_main の HARD FAIL 経路、test_batch_main の Step1/Step4 ガード、run_common 単体（合計 +9 件）
- [x] README §4-4 と「フォルダ自動検出モード」を新仕様に更新
- [x] lessons.md P-028 + Session Log 2026-06-24 を追記
- [x] pytest 638 件 pass / mypy clean
- [ ] PR 作成 → ドラフト

### 結果サマリ
（マージ後に記入）

## Phase 17: 蓄積した無駄の一掃リファクタリング（2026-06-11, A〜C 完了）

### ゴール
長年蓄積したデッドコード・二重実装・腐敗ドキュメントを段階的に除去する。
**全フェーズで外部挙動は不変**（出力 PDF / Drive / Sheets / Gmail 下書きの内容・
冪等性・エラー分類は一切変えない）。各フェーズ完了ごとに
pytest 全パス + mypy clean を確認してから個別コミットする。

### ベースライン（着手前に確定済み）
- pytest: **652 passed**（+22 subtests）
- mypy: **Success: no issues found in 15 source files**
- 本番経路: GitHub Actions `generate_comments.yml` → `python -m src.batch_main` / `src.main`

### 調査で確定した「無駄」の全量
1. **TypeScript スキャフォールド一式（完全デッドコード）**
   - `src/index.ts` / `src/ai/*.ts` / `src/workflow/*.ts` / `src/types/workflow.ts`
     / `tests/*.test.ts` / `tsconfig.json` / `package.json` / `package-lock.json`
   - 実装はインターフェース＋モックのみ（実 API 呼び出しゼロ）。
     2026-03-16 の初期スキャフォールド以降、一度も変更されていない。
     本番ワークフローからの参照ゼロ。CI の `typescript-tests` ジョブだけが延命装置。
2. **`src/matcher.py`（156 行、完全デッドコード）**
   - 本体コード（src/ scripts/）から呼び出しゼロ。`tests/test_matcher.py` のみが参照。
     AI 抽出（comment_generator）への置き換えで不要化した旧マッチングロジック。
3. **main.py / batch_main.py の二重実装（約 395 行の重複）**
   - 医院フォルダ URL 記録（完全一致 25 行 ×2）、PDF 分類、管理番号デデュープ、
     Gmail 下書き蓄積/集約（`_create_grouped_drafts_for_run` vs `_for_batch`）、
     完了マーカー追記、添付資料パススルー（約 80% 重複）
4. **ドキュメント腐敗**
   - CLAUDE.md の Project Overview / Build & Test が TS 用（npm run build 等）のまま
   - package.json の description「AI-powered code comment generation」も初期スキャフォールドの名残
5. **（低優先・要相談）設定 3 系統の整理 / プロンプト共通化**
   - ProfileConfig / RunConfig の重複フィールド（約 80 行）
   - テーマ別プロンプトの共通括り出し（約 60 行、ただし出力品質に直結）

### フェーズ計画
- **Phase 17-A: TS スキャフォールド全削除（純削除・低リスク）**
  - [x] TS ソース・テスト・tsconfig・package.json・package-lock.json を削除
  - [x] `.github/workflows/ci.yml` から `typescript-tests` ジョブを削除
  - [x] CLAUDE.md の Project Overview / Build & Test を実態（pytest / mypy）に更新
  - [x] `.gitignore` の node 系エントリ整理
  - [x] pytest + mypy 全パス確認 → コミット
- **Phase 17-B: matcher.py 削除（純削除・低リスク）**
  - [x] `src/matcher.py` + `tests/test_matcher.py` を削除
  - [x] 横断 grep で参照ゼロを最終確認（README 構成図の 1 行のみ → 更新）
  - [x] pytest + mypy 全パス確認 → コミット
- **Phase 17-C: main / batch_main の共通処理抽出（挙動不変リファクタ）**
  - [x] 共有モジュール（`src/run_common.py`）を新設し、共通ブロックを移管:
        PDF 分類 / デデュープ / 医院名標準化 / 医院フォルダ記録（Recorder 化）
        / 下書き蓄積・集約 / 完了マーカー / 添付資料パススルー
  - [x] 微差（run_halted の「中止」分岐、skip manifest、authoritative 再判定、
        テスト注入用 gmail_module 引数）は引数・呼び出し側に残す（挙動不変）
  - [x] 依存モジュール（sheets/drive/gmail）は注入方式にして既存テストの
        モジュール属性パッチ（`patch("src.main.sheets_client")` 等）を維持
  - [x] main / batch_main の統合はしない（通常 / Batch の設計思想が異なるため）
  - [x] pytest + mypy 全パス確認 → コミット
- **Phase 17-D（未実施・要承認）: 設定層の整理** — ユーザー判断で今回スコープ外
- **Phase 17-E（未実施・推奨保留）: プロンプト共通括り出し** — 同上

### 結果サマリ
- スコープ: ユーザー承認により A〜C を実施（D/E は見送り）
- コミット: 3 件（17-A: 55e64eb / 17-B: b9a40e9 / 17-C: d7c5a5f）
- 削減量:
  - TS 一式: 13 ファイル・約 1,150 行 + CI 1 ジョブ（npm install 含む延命コスト）
  - matcher.py + テスト: 332 行
  - main.py 652 → 475 行 / batch_main.py 1165 → 994 行（重複 476 行を削除、
    新規 run_common.py 393 行に単一実装として集約）
- 検証: pytest 629 passed（652 から減少した 23 件は削除した matcher テストのみ、
  既存テストは無修正で全パス）/ mypy Success
- 外部挙動: 不変（ログ文言・統計カウンタ・Sheets/Drive/Gmail 呼び出し順を維持）

## Phase 12: 参加者マスターシート統合リファクタ（2026-05-26）

### ゴール
PR #35 で導入した「メールアドレス一覧」シート方式を廃止し、
ユーザーが既に運用している「管理シート」（5列: 管理番号 / 医院名 / 参加者名 /
申し込み会場 / メールアドレス）を **唯一の lookup ソース** にする。
医院名表記の統一とメール lookup の両方を 1 シートで完結させる。

### 設計判断
- シート名はプロファイル YAML で per-seminar 指定可能。デフォルトは `参加者マスター`
- 医院名 lookup ミス時 → AI 抽出値で代用（現状維持の挙動）
- メール lookup ミス時 → Gmail 下書きだけスキップ、PDF 処理は続行（fail-soft）
- 「開業準備中」など B 列の固定文字列もそのまま標準医院名として扱う

### タスク
- [x] baseline: 436 tests pass
- [x] commit 1: sheets_client / config 旧 EmailRecord 系を削除 + MasterRecord 系を追加
- [x] commit 2: profile / discover で email_sheet_name → master_sheet_name にリネーム
- [x] commit 3: main.py / batch_main.py で master_records を使う統合
- [x] commit 4: テスト全面置き換え + README + lessons.md + YAML 雛形
- [x] mypy エラー 0（既存の yaml stub error 1 件のみ・無関係）
- [x] 全テスト pass（436 → 440 件）

### 結果サマリ
- テスト件数: 436 → 440 件（差分 +4）
    - sheets_client: 16 件入替（旧 EmailRecord 系 16 件削除 → MasterRecord 系 16 件追加）
    - main: 6 件 → 8 件（TestRunGmailDraftIntegration → TestRunMasterSheetIntegration）
    - batch_main: 5 件 → 7 件（TestStep4GmailDraftIntegration → TestStep4MasterSheetIntegration）
    - integration smoke: 2 件 → 2 件（TestGmailDraftIntegrationE2E → TestMasterSheetIntegrationE2E）
- 変更ファイル: src/config.py, src/sheets_client.py, src/gmail_client.py,
  src/profile.py, src/discover.py, src/main.py, src/batch_main.py,
  profiles/jissen_2024_q1.yaml, README.md, tasks/lessons.md, tasks/todo.md
- 4 つのコミットに分けてプッシュ

## Phase 11: Gmail 下書きの本処理組み込み（2026-05-25, PR #35）

### ゴール（参考: 後続 Phase 12 で参加者マスターに統合）

## Phase 9: フォルダ自動検出システム（2026-05-17）

### ゴール
INPUT_ROOT 配下のサブフォルダを auto-discover し、出力フォルダ・シートタブ・
管理番号 prefix を自動派生するアーキテクチャを追加する。
Convention over Configuration を優先し、Secret/YAML 追加なしで新セミナーに対応する。

### 設計コンセプト
- 必要な Secret 3 つ: `DRIVE_INPUT_ROOT` / `DRIVE_OUTPUT_ROOT` / `SPREADSHEET_ID`
- ユーザー作業: Drive サブフォルダ作成 + PDF アップロード + `target_folder` 名を入力
- システム: フォルダ検索（表記揺れ吸収）→ 出力フォルダ自動作成 → シートタブ自動作成
  → 管理番号 prefix を `<folder_name>-` で派生 → 既存パイプライン実行
- 後方互換: `--profile` モードは完全維持

### タスク

- [x] baseline: 265 tests pass, mypy 0 errors
- [x] `src/discover.py` 新規: 3 関数（list_input_subfolders / resolve_context / list_target_folder_names）
- [x] `src/config.py` 修正: `DRIVE_INPUT_ROOT` `DRIVE_OUTPUT_ROOT` 読み込み追加
- [x] `src/main.py` 修正: `--target-folder` 引数 / `__list__` モード / run() 分岐
- [x] `src/batch_main.py` 修正: `--target-folder` 引数 / step1 / step4 への context 受け渡し
- [x] `tests/test_discover.py` 新規: 上記 3 関数の単体テスト
- [x] `tests/test_main.py` 拡張: --target-folder 関連テスト
- [x] `tests/test_batch_main.py` 拡張: --target-folder 関連テスト
- [x] `tests/test_integration_smoke.py` 拡張: target_folder E2E + profile リグレッション
- [x] `.github/workflows/generate_comments.yml` 修正: target_folder input + 2 Secret env
- [x] `docs/google_form_setup.md` 修正: 対象フォルダ名質問追加
- [x] `README.md` 修正: フォルダ自動検出セクション
- [x] `tasks/lessons.md` 修正: P-013 Convention over Configuration
- [x] pytest 全件 pass / mypy 0 errors
- [x] 論理単位でコミット → push → ドラフト PR 作成

### 受け入れ条件
- [x] `pytest tests/` 全件 pass（既存 265 + 新規）
- [x] `mypy src/ --ignore-missing-imports` エラー 0
- [x] `src/discover.py` 関数 3 つ実装
- [x] `--target-folder` 引数が main.py / batch_main.py で動作
- [x] `--target-folder __list__` で候補列挙
- [x] 既存 `--profile` モードのリグレッションテスト追加
- [x] workflow YAML に `target_folder` input + 2 Secret env 追加
- [x] docs/google_form_setup.md に対象フォルダ名質問の追加手順
- [x] README にフォルダ自動検出セクション追加
- [x] tasks/lessons.md に P-013 追記
- [x] ドラフト PR 作成完了

---

## 実装フェーズ

- [x] Phase 1: 基盤構築（ディレクトリ構成・requirements.txt・config.py・utils.py）
- [x] Phase 2: コア機能（pdf_reader・comment_generator・pdf_creator・pdf_merger）
- [x] Phase 3: Google連携（drive_client・sheets_client・gmail_client・matcher）
- [x] Phase 4: 統合（main.py・batch_main.py・notebooks/jissen_comment.ipynb）
- [x] Phase 5: デプロイ（GitHub Actions・README.md・テストコード）

## テスト結果
- 全26テスト合格（2026-03-16）
- フォント自動ダウンロード: 動作確認済み
- PDF生成・結合: 日本語テキスト正常
- マッチング: 完全一致・部分一致・ファイル名マッチング動作確認済み

## 残タスク（手動作業）
- [ ] assets/jissen_kun.png を配置
- [ ] Google Cloud APIの有効化とサービスアカウント作成
- [ ] GitHub Secretsの設定
- [ ] サンプルPDFでの動作確認

## Phase 6: 1000+ PDFスケール対応エージェントチーム（2026-05-01）

### 設計目標
400件 → 1000件以上に投入規模が拡大した際、5つの本質的失敗モードを完全に防ぐ。

### 失敗モードと対応エージェント
| # | 失敗モード | 既存コードの問題箇所 | 担当エージェント |
|---|---|---|---|
| 1 | スキャン/暗号化/破損PDFのサイレントスキップ | `pdf_reader.extract_text` line 67-69 が空文字列を返し、`main.py` line 53-56 が `stats["skip"]` をインクリメントするだけ | pdf-triage-officer |
| 2 | Anthropic Batch API 256MB/100k req上限・GitHub Actions 6h timeout | `batch_main.py` 全体が単一バッチ前提、`step3` の polling が無制限 | batch-orchestrator |
| 3 | リラン時のGmail下書き重複・「処理中」永久滞留 | `sheets_client.get_unprocessed_records` line 127 が「処理中」を未処理扱いしない、`gmail_client.create_draft` にdedupeキー無し | idempotency-guardian |
| 4 | API レート制限・コスト青天井 | `comment_generator` line 187-237 にトータル制限なし、Gmail/Sheetsクオータ追跡なし | resource-cost-sentinel |
| 5 | 結合PDFのコメント欠落・フォント失敗・破損 | `pdf_creator._wrap_text` line 139 が黙ってコメントを切り捨て、`pdf_merger.merge_pdfs` が出力検証なし | output-verifier |

### 実装
- [x] `.claude/agents/pdf-triage-officer.md` — トリアージ責務のエージェント定義
- [x] `.claude/agents/batch-orchestrator.md` — バッチ調整責務のエージェント定義
- [x] `.claude/agents/idempotency-guardian.md` — 冪等性保証責務のエージェント定義
- [x] `.claude/agents/resource-cost-sentinel.md` — リソース監視責務のエージェント定義
- [x] `.claude/agents/output-verifier.md` — 出力検証責務のエージェント定義
- [x] `scripts/triage_pdfs.py` — 決定論的PDF分類（healthy/scanned/encrypted/corrupted/oversized/duplicate）
- [x] `scripts/audit_idempotency.py` — Sheets×Gmail Draftsの状態監査
- [x] `scripts/verify_output.py` — 5%サンプル検証（ページ数・テキスト一致・フォント検証）
- [x] テスト: `test_triage_pdfs.py`, `test_audit_idempotency.py`, `test_verify_output.py`

### 標準オペレーション（1000件投入時）
```
1. pdf-triage-officer        → logs/triage_manifest.json (PROCEED/HALT判定)
2. idempotency-guardian      → logs/idempotency_audit.json (Safe to proceed判定)
3. batch-orchestrator        → step1 prepare
4. resource-cost-sentinel    → コスト見積 / バッジ承認
5. batch-orchestrator        → step2 submit
6. resource-cost-sentinel    → polling中の継続監視
7. batch-orchestrator        → step3 results, step4 pdfs
8. output-verifier           → APPROVE_SEND/BLOCK_SEND判定
9. (人間)                    → 下書きを目視後送信
```

### 残課題（次回以降）
- [ ] `batch_main.py` line 75 の `pdf_text` 削除を修正（リトライ不能の温床）
- [ ] `idempotency-guardian` を `batch_main.run()` の冒頭で自動呼び出し
- [ ] `pdf-triage-officer` を `step1_prepare` の前段に統合
- [ ] Anthropic Batch APIの実上限（256MB/100k req）に基づくシャーディング実装

## Phase 7: Drive階層出力 + Sheets出力一覧（2026-05-01）

### ゴール
コメント付きPDFをDriveに「医院名/個人名/」階層で保存し、Sheetsの「出力一覧」シートに1ファイル1行で履歴を記録する。

### 仕様
- 出力Drive構造: `<DRIVE_OUTPUT_FOLDER_ID>/<医院名>/<個人名>/<元PDFファイル名>`
- 出力ファイル名: 元PDFと同じ（複数事例対応のため）
- 出力一覧シート: `医院名 | 個人名 | 実践事例名 | Drive URL | 処理日時`
- 既存Sheet1（医院マスター）は変更なし

### タスク
- [ ] `src/config.py` に `DRIVE_OUTPUT_FOLDER_ID` を追加 / Driveスコープを書き込み可能に拡張
- [ ] `src/drive_client.py` に `find_or_create_folder()` と `upload_pdf()` を追加
- [ ] `src/sheets_client.py` に `append_output_record()` を追加
- [ ] `src/main.py` に Drive アップロード + Sheets 追記を統合
- [ ] `src/batch_main.py` step4 に同上の統合
- [ ] テスト追加（`test_drive_client.py`, `test_sheets_client.py`）
- [ ] mypy / pytest をパス
- [ ] README に新Secret `DRIVE_OUTPUT_FOLDER_ID` の取得手順を追記

## Phase 8: Drive書き込みのOAuthユーザー委任（2026-05-03）

### 背景
PR #8 で `supportsAllDrives=True` を追加したが、本番実行で `storageQuotaExceeded` 403 が発生。原因はサービスアカウントが My Drive 配下のファイル所有者になれないこと（クォータ 0GB）。詳細は `lessons.md` P-007。

### 仕様
- `GOOGLE_OAUTH_TOKEN_JSON` をDrive/Gmail認証で優先使用
- 旧 `GMAIL_TOKEN_JSON` は後方互換でフォールバック
- Sheets はサービスアカウントのまま（クォータ問題なし）

### タスク
- [x] `src/config.py` に `GOOGLE_OAUTH_TOKEN_JSON` 追加（`GMAIL_TOKEN_JSON` フォールバック付き）
- [x] `src/drive_client._get_credentials()` でOAuthユーザートークン優先
- [x] `src/gmail_client.py` を `GOOGLE_OAUTH_TOKEN_JSON` 経由に切替
- [x] `.github/workflows/generate_comments.yml` で新Secretをパス
- [x] `tests/test_drive_client.py` に認証優先順位の検証を追加
- [x] README に `GOOGLE_OAUTH_TOKEN_JSON` の役割・取得手順を追記
- [x] pytest / mypy / npm test をパス（Python 124件 / TypeScript 11件 全合格）

### 受け入れ条件（次回本番実行時）
- [ ] `GOOGLE_OAUTH_TOKEN_JSON` Secret に `drive` `spreadsheets` `gmail.compose` 3スコープを含むOAuthトークンを設定
- [ ] PDFが `<DRIVE_OUTPUT_FOLDER_ID>/<医院名>/<個人名>/<タイトル>.pdf` でアップロードされる
- [ ] 出力一覧シートに行が追記される
- [ ] Driveログ：`Drive認証: OAuthユーザートークンを使用` が出ていること

## Phase 15: マスター列構造修正 + 同一人物の PDF を 1 通の下書きに集約（2026-05-26）

### 背景
PR #37 マージ後、本番実行で「下書きは作られるが宛先が空」になっていた。
原因はマスターシート A 列の構造想定違い:
- コードが期待: A 列 = 医院コード `001`（医院管理番号として）
- ユーザーの実装: A 列 = 管理番号 `101-01` のまま、別途 A 列に医院コードを
  入れようとして空にしていた

ユーザー要件の整理:
1. **A 列は管理番号 (`xxx-yy`) のまま運用したい**（管理シートを変えたくない）
2. **コード側で先頭セグメントから医院コードを派生** させる
3. 加えて、**同一人物の複数 PDF は 1 通の下書きに集約** したい
   （現状は PDF ごとに別下書き）

### 実装
- `MasterRecord.management_number` を A 列とし、`clinic_number` は property
  として先頭セグメント（`-` の前）を返す（``101-01`` → ``101``、``101``
  単独もそのまま医院コードとして扱う）
- ヘッダーを元の 5 列に戻す: `["管理番号", "医院名", "参加者名", "申し込み会場", "メールアドレス"]`
- `lookup_clinic_name` / `lookup_email_by_clinic_and_person` を派生
  `clinic_number` プロパティ基準に変更
- `gmail_client.create_draft`: `pdf_path` → `pdf_paths` (list 対応)。
  単一パスも後方互換で受け付ける
- `main.py` / `batch_main.py`: ループ中は `_collect_draft_item` で
  `(email, person_name, pdf_path)` を蓄積し、ループ終了後に
  `_create_grouped_drafts_*` でメールアドレスごとに 1 通の下書きに集約。
  PDF はセッションスコープの tempdir に保持し集約完了後に削除
- メール空の項目は集約せず PDF ごとに個別の宛先空下書きを作成

### 集約方針（ユーザー確定）
- グループキー: メールアドレスのみ
- 件名・本文テンプレート: 現行のまま（変更しない）
- 同一メール / 異なる個人名: 警告ログ + 先頭の個人名を採用
- メール空: 集約せず個別下書き（手動補完用）

### タスク
- [x] `MasterRecord` を `management_number` 中心に戻し `clinic_number` を property 化
- [x] `_MASTER_SHEET_HEADER` を 5 列に戻す（管理番号 / 医院名 / 参加者名 / 申し込み会場 / メールアドレス）
- [x] `read_master_records` の読み取り範囲を A:E に
- [x] `lookup_clinic_name` / `lookup_email_by_clinic_and_person` を派生 clinic_number 基準に
- [x] `gmail_client.create_draft` に `pdf_paths` (list) 対応を追加（後方互換あり）
- [x] `main.py` でセッションスコープ tempdir + 集約下書き作成に切り替え
- [x] `batch_main.py` でも同様の集約フローに切り替え
- [x] `_create_gmail_draft_safe` を削除（`_collect_draft_item_batch` + `_create_grouped_drafts_for_batch` に分解）
- [x] テスト更新（管理番号 `xxx-yy` 形式に統一、集約後の挙動を検証）
- [x] 全 457 件パス
- [ ] commit + push + PR

## Phase 14: メールアドレス突合を「医院管理番号 + 個人名」方式に変更（2026-05-26）

### 背景
- PDF ファイル名: `xxx-yy-z`（3 セグメント、例 `001-01-0`）
- これまでの参加者マスター A 列: `xxx-yy`（2 セグメント、例 `001-01`）
- 両者は別概念であり、文字列マッチングが原理的に成立しない
- 結果として `lookup_email_by_management_number()` がほぼ常に空文字を返し、Gmail 下書きが「メール未登録」でスキップされていた

### 新仕様（ユーザー確定）
- 参加者マスター A 列を **医院管理番号 (= 医院コード `xxx` のみ)** に変更
- 突合キーは **A 列 (医院管理番号) + C 列 (参加者名)**
- 個人名は強めの正規化（NFKC + 全空白除去 + カナ統一）
- Levenshtein 距離 ≤ 1 のファジー一致も「同一人物」とみなす
- 同姓同名複数ヒット → 警告ログを出して先頭を採用
- ヒットなし → **宛先空のまま Gmail 下書きを作成**（手動補完してもらう）

### マスターシート新スキーマ
| 列 | 名称 | 例 | 用途 |
|---|---|---|---|
| A | 医院管理番号 | `001` | 突合キー + 医院名 lookup |
| B | 医院名 | `〇〇歯科` | 医院名標準化 |
| C | 参加者名 | `山田太郎` | 突合キー |
| D | 申し込み会場 | `東京会場` | 参照用 |
| E | メールアドレス | `xxx@example.com` | Gmail 下書き宛先 |

### タスク
- [x] baseline: 全テストパス確認（440 件）
- [x] sheets_client: `MasterRecord.management_number` → `clinic_number` にリネーム
- [x] sheets_client: `_MASTER_SHEET_HEADER` の A 列を「医院管理番号」に変更
- [x] sheets_client: `_normalize_person_name()` 追加（NFKC + 全空白除去 + カナ統一）
- [x] sheets_client: `_levenshtein_distance()` 追加（純 Python 実装、1 文字差判定用）
- [x] sheets_client: `lookup_email_by_clinic_and_person()` 追加（完全一致 → ファジー一致 → 不一致の3段階）
- [x] sheets_client: `lookup_clinic_name()` を新 A 列形式に対応（完全一致）
- [x] sheets_client: `lookup_email_by_management_number()` を削除
- [x] gmail_client: 既存 `to_email` 空文字対応で動く（mask_email も空文字対応済）
- [x] main / batch_main: `_create_gmail_draft` を新 lookup 関数に置換し、不一致時も下書き作成
- [x] tests/test_sheets_client: 完全一致 / 空白差 / カナ差 / 1 文字差 / 同姓同名 / 不一致 / 医院違いの 7 ケース追加 + 正規化 5 ケース + Levenshtein 6 ケース
- [x] pytest 全件パス（440 → 456 件）
- [x] tasks/lessons.md に P-022 を追記
- [ ] commit + push + draft PR

### 受け入れ条件（次回本番実行時）
- [ ] 参加者マスター A 列に **医院管理番号（医院コードのみ）** を入れる
- [ ] PDF ファイル名 `xxx-yy-z` から抽出した医院コード `xxx` がマスター A 列に存在すること
- [ ] AI 抽出した個人名（または 1 文字違い以内）がマスター C 列に存在すること
- [ ] ヒットすればその E 列のメールが Gmail 下書きの TO に設定される
- [ ] ヒットしなければ宛先空で下書きが作成され、ログに「マスター未ヒット」が出る

## Phase 13: 既存医院フォルダをマスター医院名へ同期（2026-05-29）

### ゴール
「PDF ファイル名先頭の医院番号 → 参加者マスター照合 → 医院名をフォルダ名に反映」。
新規フォルダは P-021 で既にマスター医院名を最優先使用済み。残る gap は
**同じ医院番号の既存フォルダが P-019 仕様で再利用時にリネームされず旧名のまま**
残る点。マスター由来の確定名のときだけ既存フォルダもリネームして反映する。

### 設計
- `find_or_create_clinic_folder` / `upload_pdf_to_clinic_person` に
  `clinic_name_authoritative: bool = False` を追加。
- 既存フォルダ再利用時、`authoritative=True` かつ既存名 ≠ `<医院番号>_<確定名>`
  のときだけ `files().update` でリネーム。AI 抽出値（`False`）は従来通り非リネーム
  （P-019 の churn 防止意図を維持）。
- リネーム失敗は WARNING のみで続行（フォルダ ID で処理継続）。
- フォルダ ID・URL 不変 → 医院フォルダ URL シートの既存リンクは保持。

### タスク
- [x] baseline: 全テストパス確認（507 件）
- [x] drive_client: `clinic_name_authoritative` 追加 + リネームロジック + docstring 更新
- [x] main.py: `clinic_name_authoritative=bool(clinic_name_from_master)` を渡す
- [x] batch_main.py 本体: 同上
- [x] batch_main.py 添付資料: `master_records` から確定判定を再導出して渡す
- [x] tests/test_drive_client: rename 実行 / 一致時無動作 / 非確定時非リネーム / 失敗非致命 の 4 件追加
- [x] pytest 全件パス（507 → 511 件）
- [x] mypy `--ignore-missing-imports` Success（CI 同条件）
- [x] tasks/lessons.md に追記（P-019 緩和の経緯）
- [ ] commit + push（既存ブランチ claude/resume-oauth-setup-KHMv9 → PR #40）

### Review（結果）
- 新規フォルダ命名は無変更（既にマスター名）。既存フォルダのみ挙動追加。
- P-019 の意図（AI 抽出名での往復 churn 防止）は `authoritative` ガードで維持。
- 次回本番で WARNING 級の差分（旧名 → 確定名へ同期）のログが出るか観測予定。

## Phase 16: Batch 回収機能の正しい実装（2026-06-02, draft PR）

### 背景 / 根本原因
`--step results --batch-id <id>` 回収が出力 0 件の no-op。3 層の根本原因（ルーティングで
step4 不実行 / items を `batch_prep.json` からしか読めず回収ランに不在 / `custom_id` 位置依存で
Drive 再走査と突合不可）。詳細は lessons.md P-026。

### 設計判断
- D1: `custom_id` を Drive file id 由来の安定 ID 化（`_custom_id_for_file`）。
- D2: `reconstruct_items_from_drive`（list + ファイル名分類のみ、本文 DL なし、添付資料も再構築）。
- D3: `_resolve_items_for_step4`（`batch_prep.json` 優先 → Drive 再走査）。
- D4: `is_recovery = step=="results" and batch_id is not None` のときだけ step4 完走
  （discrete results / GHA 6h を保護）。
- D5: results 非空 & items 空 → `RuntimeError`（無言 0 件撲滅）。

### タスク
- [x] step1 `custom_id` を安定 ID 化
- [x] `reconstruct_items_from_drive` 追加
- [x] `_resolve_items_for_step4` 追加
- [x] `run()` ルーティング（`is_recovery`）+ step4 へ items 解決を委譲
- [x] step4 loud guard（results 非空 & items 空 → 例外）
- [x] README / workflow `step` 説明を完走仕様へ更新
- [x] tests/test_batch_main.py に 12 件追加
- [x] E2E smoke 6 件を file id 由来 key に更新
- [x] pytest 652 件 green / mypy 既存 stub 警告のみ

### Review（結果）
- 回収コマンドは不変（`--step results --batch-id X`）だが、結果取得だけでなく
  Drive 再走査 → items 再構築 → step4 まで完走するようになった。`batch_prep.json` が
  無い別ジョブからの回収でも完走する。
- 後方互換: `batch_prep.json` があれば優先利用（旧 positional バッチも復元可）。
  discrete `--step results`（batch_id なし）/ `--step pdfs` / `--step all` は挙動維持。
- マージは強制しない（draft PR）。
