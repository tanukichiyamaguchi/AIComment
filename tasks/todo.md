# じっせん君コメントシステム - Task Tracker

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
