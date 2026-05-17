# じっせん君コメントシステム

歯科医院の実践事例PDF（数百件）を読み取り、Claude Sonnet 4.6 API で **医院名・氏名・実践事例タイトル** を本文から自動抽出し、コンサルタント風コメントを生成、コメントページを末尾に追加した新PDFを **Google Drive に「医院名/個人名/」階層** で保存し、**スプレッドシート「出力一覧」** に履歴を記録するシステム。

事前のスプレッドシート入力は不要。Drive にPDFを置けば自動で処理されます。

## セットアップ

### 1. Python環境

```bash
pip install -r requirements.txt
```

### 2. 環境変数 / Secrets

| 変数名 | 内容 | 必須 |
|--------|------|------|
| `ANTHROPIC_API_KEY` | Claude APIキー | ✅ |
| `GOOGLE_CREDENTIALS_JSON` | GCPサービスアカウントJSON（読み取り・Sheets書き込み用） | ✅ |
| `GOOGLE_OAUTH_TOKEN_JSON` | OAuthユーザートークン（Drive書き込み・Gmail下書き作成用） | ✅ |
| `DRIVE_FOLDER_ID` | 入力PDFが格納されたGoogle DriveフォルダID | ✅ |
| `DRIVE_OUTPUT_FOLDER_ID` | 出力PDFを保存するGoogle DriveフォルダID | ✅ |
| `SPREADSHEET_ID` | 出力一覧を書き込むスプレッドシートID | ✅ |
| `GMAIL_TOKEN_JSON` | （旧称）`GOOGLE_OAUTH_TOKEN_JSON` の後方互換エイリアス | 旧Secret再利用可 |

#### `GOOGLE_OAUTH_TOKEN_JSON` がなぜ必要か

サービスアカウントは Google Drive の **マイドライブ** 配下に新規ファイルをアップロードできません（`storageQuotaExceeded` エラー）。これは Google の仕様で、サービスアカウントには 0GB の容量しか割り当てられていないためです。

回避策は次の2つ：

1. **OAuthユーザートークンを使う**（本実装の方針） — Driveアップロード時はユーザー認可で実行し、ファイル所有者をユーザー本人にする。本人の Drive 容量（個人/Workspace）を消費する
2. 共有ドライブを使う（Google Workspace Business Standard 以上が必要）

#### `GOOGLE_OAUTH_TOKEN_JSON` の取得手順

OAuthユーザートークンは下記スコープを含む必要があります：

```
https://www.googleapis.com/auth/drive
https://www.googleapis.com/auth/spreadsheets
https://www.googleapis.com/auth/gmail.compose
```

トークンJSON生成手順（**Codespacesで完結**、ローカルPCは不要）：

1. GCPコンソール → APIとサービス → OAuth同意画面：User Type を **External**、テストユーザーに自分のGmailを追加、スコープに上記3つを追加
2. GCPコンソール → APIとサービス → 認証情報 → 「OAuth 2.0 クライアント ID」を作成（種類：**デスクトップアプリ**）
3. JSONをダウンロード → Codespaces の `AIComment/` 直下にドラッグ&ドロップでアップロード → ファイル名を **`client_secret.json`** にリネーム（`.gitignore` で保護済み、誤コミットされません）
4. Codespacesターミナルで実行:
   ```bash
   pip install -r requirements.txt
   python scripts/generate_oauth_token.py
   ```
5. スクリプトが表示する認証URLをブラウザで開き、Google認証 → 3スコープすべてを許可
6. 認証後、ブラウザは「このサイトにアクセスできません」エラーになる（**正常**）。アドレスバーのURLを全部コピー → スクリプトに貼り付けて Enter
7. 生成された `token.json` の内容（`refresh_token` を含む）を `GOOGLE_OAUTH_TOKEN_JSON` Secret に貼り付ける

> ⚠️ `client_secret.json` は **絶対に Git コミットしない**こと。Public リポジトリの場合は瞬時に漏洩します。`.gitignore` で保護されていますが、念のため `git status` でステージされていないことを確認してください。

### 3. アセットファイル

- `assets/jissen_kun.png` — じっせん君キャラクター画像（社内から取得）
- フォント（NotoSansJP）は初回実行時に自動ダウンロードされます

### 4. スプレッドシート「出力一覧」シート

`DRIVE_OUTPUT_FOLDER_ID` 設定済みかつ初回実行時に自動作成されます。1ファイル1行で記録：

| 列 | ヘッダー | 内容 |
|----|---------|------|
| A | 医院名 | AIがPDFから自動抽出 |
| B | 個人名 | AIがPDFから自動抽出 |
| C | 実践事例タイトル | AIがPDFから自動抽出 |
| D | Drive URL | 出力PDFの閲覧URL |
| E | 処理日時 | システムが書き込み |

> 事前の入力（医院名や氏名のリスト）は **不要**。AIがPDF本文から判別できなかった場合は `unknown_clinic` / `unknown_person` として処理を続行します。

### 5. Drive 出力構造

```
<DRIVE_OUTPUT_FOLDER_ID>/
├── 三浦歯科医院/
│   └── 白川 蓮/
│       ├── 三浦歯科医院＿白川 蓮＿AI活用インプラント新患獲得.pdf  ← 元PDF＋コメントページ
│       └── 三浦歯科医院＿白川 蓮＿自費率向上の取り組み.pdf
├── 山本歯科医院/
│   └── 田中 太郎/
│       └── 山本歯科医院＿田中 太郎＿夏祭りイベント開催.pdf
└── unknown_clinic/         ← 医院名抽出失敗時のフォールバック
    └── unknown_person/
        └── unknown_clinic＿unknown_person＿名称不明事例.pdf
```

ファイル名は ``<医院名>＿<個人名>＿<実践事例タイトル>.pdf`` 形式（区切りは全角アンダースコア ``＿``）。AI が抽出した値から特殊文字を除いて生成。

## プロファイル

### なぜ必要か

「ドキュメントタイプ × 提出期間」の組み合わせごとに **入力フォルダ / 出力フォルダ / 出力一覧シート / 管理番号 prefix** を切り替えるための仕組み。同一ワークフローを 1 本だけ保ったまま、四半期ごとの実践事例を別フォルダ・別シートに整理して処理できる。

```
[現状] 1 ワークフロー → 固定の {入力/出力/シート/プロンプト} を直接参照
[新規] 1 ワークフロー → --profile 引数 → プロファイル定義 → 各種設定を解決
```

### 利用可能なプロファイル

`profiles/*.yaml` に定義。コマンドラインからは `python -c "from src.profile import list_available_profiles; print(list_available_profiles())"` で一覧取得可能。

| プロファイル名 | 入力 Secret | 出力 Secret | 出力シート | 管理番号 prefix |
|---------------|------------|------------|-----------|----------------|
| `jissen_default` | `DRIVE_FOLDER_ID` | `DRIVE_OUTPUT_FOLDER_ID` | `出力一覧` | （なし） |
| `jissen_2024_q1` | `DRIVE_FOLDER_JISSEN_2024_Q1` | `DRIVE_OUTPUT_JISSEN_2024_Q1` | `実践事例_2024Q1_出力一覧` | `J24Q1-` |
| `jissen_2024_q2` | `DRIVE_FOLDER_JISSEN_2024_Q2` | `DRIVE_OUTPUT_JISSEN_2024_Q2` | `実践事例_2024Q2_出力一覧` | `J24Q2-` |
| `jissen_2024_q3` | `DRIVE_FOLDER_JISSEN_2024_Q3` | `DRIVE_OUTPUT_JISSEN_2024_Q3` | `実践事例_2024Q3_出力一覧` | `J24Q3-` |
| `jissen_2024_q4` | `DRIVE_FOLDER_JISSEN_2024_Q4` | `DRIVE_OUTPUT_JISSEN_2024_Q4` | `実践事例_2024Q4_出力一覧` | `J24Q4-` |

`jissen_default` は **既存挙動を完全維持する後方互換プロファイル**。`--profile` 引数を省略した場合の既定値でもあり、過去の Secret / シート / 管理番号採番ロジックがそのまま動く。

### 新しいプロファイルを追加する手順

1. `profiles/<新プロファイル名>.yaml` を作成。schema は下記を参照
2. GitHub Secrets に `input_folder_id_secret` / `output_folder_id_secret` で指定したキー名で Drive フォルダ ID を登録
3. `.github/workflows/generate_comments.yml` の `env:` セクションに `<KEY>: ${{ secrets.<KEY> }}` を追記
4. （オプション）Google フォームのプルダウン選択肢と Apps Script の `ALLOWED_PROFILES` 配列に新プロファイル名を追加

YAML スキーマ（必須フィールド）：

```yaml
display_name: "（ログ表示用の人間可読名）"
document_type: jissen_practice_case          # 文書タイプ識別子
period: "2024_q1"                             # 期間識別子
input_folder_id_secret: DRIVE_FOLDER_XXX     # 環境変数名（解決はランタイム）
output_folder_id_secret: DRIVE_OUTPUT_XXX    # 環境変数名（解決はランタイム）
output_sheet_name: "実践事例_2024Q1_出力一覧" # 同一スプレッドシート内のシート名
management_number_prefix: "J24Q1-"           # 管理番号の prefix（空文字で prefix なし）
prompt_template: jissen_practice_case        # コメント生成プロンプト識別子
```

## 使い方

### コマンドライン

```bash
# 通常モード（1件ずつ処理、まずは少量で動作確認）
python -m src.main --test-count 5

# Batchモード（50%割引・大量一括処理、デフォルトプロファイル）
python -m src.batch_main --test-count 5      # まずテスト
python -m src.batch_main                      # 本番（全件）

# プロファイル指定（例: 2024 年度 Q1）
python -m src.batch_main --profile jissen_2024_q1 --test-count 5
python -m src.main --profile jissen_2024_q3

# バッチ結果取得から再開
python -m src.batch_main --batch-id msgbatch_xxx --step results
```

### GitHub Actions（本番運用）

1. リポジトリの Settings → Secrets に環境変数を登録
2. Actions タブ → 「Generate Jissen Comments」→ 「Run workflow」
3. **`test_count: 5` でまずテスト実行** → Drive と出力一覧シートを目視確認
4. 問題なければ `test_count: 0` で全件実行
5. 完了後、Drive のフォルダ階層と出力一覧シートを確認

## 処理フロー（v2）

```
1. Drive入力フォルダから全PDFを取得
2. 各PDFについて：
   a. テキスト抽出
   b. Claude API（構造化出力）で 医院名・氏名・実践事例タイトル・コメントを一括取得
   c. コメントページPDFを生成
   d. 元PDF + コメントページを結合
   e. Drive `<出力root>/<医院名>/<個人名>/<タイトル>.pdf` にアップロード
   f. 出力一覧シートに 1 行追記
```

## コスト目安（400件）

| 構成 | 合計 | 日本円概算 |
|------|------|----------|
| 通常 | $25.20 | 約3,800円 |
| Batch API（50%オフ） | $12.60 | 約1,900円 |
| Batch + キャッシュ | 約$9.70 | 約1,500円 |

## トラブルシューティング

| 問題 | 対処 |
|------|------|
| 出力一覧シートが空のまま | `DRIVE_OUTPUT_FOLDER_ID` がSecretsに設定されているか確認 |
| `Service Accounts do not have storage quota` 403エラー | `GOOGLE_OAUTH_TOKEN_JSON` が未設定。OAuthユーザートークンを設定すること（手順は「`GOOGLE_OAUTH_TOKEN_JSON` の取得手順」を参照） |
| Driveアップロードで `insufficient permissions` / `invalid_scope` エラー | OAuthトークンに `drive` スコープが含まれていない。スコープ3点を含めて再生成 |
| `unknown_clinic` / `unknown_person` フォルダに大量に入る | PDF本文からAIが判別できていない。PDFのフォーマットや書式を見直す |
| フォントダウンロード失敗 | 手動で `assets/` にNotoSansJP-Regular.ttf / Bold.ttfを配置 |
| PDFの文字が□ □ □ になる | フォントファイルが正しくダウンロードされているか確認 |
| Claude API 429エラー | 自動リトライあり。頻発する場合はBatchモード推奨 |
| GitHub Actions タイムアウト | Batchモードを使用して処理時間を短縮 |
| 同じPDFを再実行すると重複出力される | v1時点で重複検知は未実装。再実行前にDrive出力フォルダと出力一覧シートを手動で整理してください |

## ディレクトリ構成

```
├── src/
│   ├── main.py              # 通常モード エントリポイント
│   ├── batch_main.py         # Batchモード エントリポイント
│   ├── config.py             # 設定値管理
│   ├── profile.py            # プロファイル loader / resolver
│   ├── utils.py              # ログ設定・フォントダウンロード・ファイル名整形
│   ├── pdf_reader.py         # PDFテキスト抽出
│   ├── comment_generator.py  # Claude API（構造化出力で医院名/氏名/タイトル/コメント取得）
│   ├── pdf_creator.py        # コメントページPDF生成
│   ├── pdf_merger.py         # PDF結合
│   ├── drive_client.py       # Google Drive API（階層フォルダ作成 + アップロード）
│   ├── sheets_client.py      # Google Sheets API（出力一覧シート追記）
│   ├── gmail_client.py       # Gmail API（v2では未使用、将来Gmail送付時に再利用）
│   └── matcher.py            # 旧マッチング（v2では未使用）
├── profiles/                 # プロファイル YAML（document_type × period の組み合わせ）
├── tests/                    # テストコード
├── notebooks/                # Google Colabノートブック
├── assets/                   # フォント・画像
├── logs/                     # 実行ログ
└── .github/workflows/        # GitHub Actions
```

## 非開発者向けの実行方法

GitHub アカウントを持たない事務スタッフ等にも `Generate Jissen Comments` ワークフローを起動してもらえます。Google フォーム → Apps Script → GitHub Actions の連携で、フォーム送信だけで実行できる仕組みです。セットアップ手順（PAT 作成、フォーム作成、Apps Script コード、テスト方法、共有時のセキュリティ注意点）は [`docs/google_form_setup.md`](docs/google_form_setup.md) を参照してください。
