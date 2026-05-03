# じっせん君コメントシステム

歯科医院の実践事例PDF（400件以上）を読み取り、Claude Sonnet 4.6 APIでコンサルタント風コメントを生成し、元PDFの末尾にコメントページを追加した新PDFを作成、Gmail下書きとして保存するシステム。

## セットアップ

### 1. Python環境

```bash
pip install -r requirements.txt
```

### 2. 環境変数 / Secrets

| 変数名 | 内容 |
|--------|------|
| `ANTHROPIC_API_KEY` | Claude APIキー |
| `GOOGLE_CREDENTIALS_JSON` | GCPサービスアカウントJSON |
| `DRIVE_FOLDER_ID` | 実践事例PDFのGoogle DriveフォルダID（入力） |
| `DRIVE_OUTPUT_FOLDER_ID` | コメント付きPDFの出力先Google DriveフォルダID（任意。未設定時はDrive保存をスキップ） |
| `SPREADSHEET_ID` | 医院名・氏名・メールのスプレッドシートID |
| `GMAIL_TOKEN_JSON` | Gmail OAuth認証トークン |

### 3. アセットファイル

- `assets/jissen_kun.png` — じっせん君キャラクター画像（社内から取得）
- フォント（NotoSansJP）は初回実行時に自動ダウンロードされます

### 4. スプレッドシート構成

**Sheet1（医院マスター）** — 入力用、1人1行

| 列 | ヘッダー | 内容 |
|----|---------|------|
| A | 医院名 | 必須 |
| B | 氏名 | 必須 |
| C | メールアドレス | 必須 |
| D | ステータス | システムが自動更新 |

**出力一覧**シート — `DRIVE_OUTPUT_FOLDER_ID` 設定時に自動作成、1ファイル1行

| 列 | ヘッダー | 内容 |
|----|---------|------|
| A | 医院名 | システムが書き込み |
| B | 個人名 | システムが書き込み |
| C | 実践事例名 | 元PDFのファイル名 |
| D | Drive URL | 出力PDFの閲覧URL |
| E | 処理日時 | システムが書き込み |

### 5. Drive 出力構造（`DRIVE_OUTPUT_FOLDER_ID` 設定時）

```
<DRIVE_OUTPUT_FOLDER_ID>/
├── 三浦歯科医院/
│   └── 白川 蓮/
│       ├── 新患獲得.pdf       ← 元PDF＋コメントページ
│       └── 自費率向上.pdf
└── 山本歯科医院/
    └── 田中 太郎/
        └── キャンセル削減.pdf
```

## 使い方

### Google Colab（開発・テスト）

`notebooks/jissen_comment.ipynb` を開き、セルを上から順に実行してください。
Colab Secretsに環境変数を設定しておけば自動で読み込まれます。

### コマンドライン

```bash
# 通常モード（1件ずつ処理）
python -m src.main --test-count 5

# Batchモード（50%割引・400件一括）
python -m src.batch_main

# Batchモード（テスト）
python -m src.batch_main --test-count 10

# バッチ結果取得から再開
python -m src.batch_main --batch-id msgbatch_xxx --step results
```

### GitHub Actions（本番運用）

1. リポジトリの Settings → Secrets に環境変数を登録
2. Actions タブ → 「Generate Jissen Comments」→ 「Run workflow」
3. 2〜3時間後、Gmail下書きフォルダを確認
4. コメント内容を確認後、手動で送信

## コスト目安（400件）

| 構成 | 合計 | 日本円概算 |
|------|------|----------|
| 通常 | $25.20 | 約3,800円 |
| Batch API（50%オフ） | $12.60 | 約1,900円 |
| Batch + キャッシュ | 約$9.70 | 約1,500円 |

## トラブルシューティング

| 問題 | 対処 |
|------|------|
| フォントダウンロード失敗 | 手動で `assets/` にNotoSansJP-Regular.ttf / Bold.ttfを配置 |
| PDF文字化けない | フォントファイルが正しくダウンロードされているか確認 |
| マッチング失敗が多い | スプレッドシートの医院名がPDF内の表記と一致しているか確認 |
| Claude API 429エラー | 自動リトライあり。頻発する場合はBatchモード推奨 |
| Gmail下書き作成失敗 | OAuth認証トークンの期限を確認。再認証が必要な場合あり |
| GitHub Actions タイムアウト | Batchモードを使用して処理時間を短縮 |

## ディレクトリ構成

```
├── src/
│   ├── main.py              # 通常モード エントリポイント
│   ├── batch_main.py         # Batchモード エントリポイント
│   ├── config.py             # 設定値管理
│   ├── utils.py              # ログ設定・フォントダウンロード
│   ├── pdf_reader.py         # PDFテキスト抽出
│   ├── comment_generator.py  # Claude APIコメント生成
│   ├── pdf_creator.py        # コメントページPDF生成
│   ├── pdf_merger.py         # PDF結合
│   ├── drive_client.py       # Google Drive API
│   ├── sheets_client.py      # Google Sheets API
│   ├── gmail_client.py       # Gmail API
│   └── matcher.py            # PDF↔スプレッドシート マッチング
├── tests/                    # テストコード
├── notebooks/                # Google Colabノートブック
├── assets/                   # フォント・画像
├── logs/                     # 実行ログ
└── .github/workflows/        # GitHub Actions
```
