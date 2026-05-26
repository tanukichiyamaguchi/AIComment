# じっせん君コメントシステム - Task Tracker

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
