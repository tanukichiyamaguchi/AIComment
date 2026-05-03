"""GOOGLE_OAUTH_TOKEN_JSON 用の OAuth ユーザートークンを生成するセットアップスクリプト。

クラウド環境（Codespaces / Cloud Shell）で実行する **手動URL貼付フロー**。
ローカル前提の `flow.run_local_server` は使用しない（ヘッドレス環境で動かないため）。

事前準備:
  1. GCPコンソールで OAuth クライアント ID（種類: デスクトップアプリ）を作成
  2. JSON をダウンロードし、Codespacesの `AIComment/client_secret.json` にアップロード
     （`.gitignore` で保護されているため誤コミットの心配なし）

実行（Codespaces のターミナルで）:
  python scripts/generate_oauth_token.py

フロー:
  [1/3] スクリプトが認証URLを表示
  [2/3] そのURLをブラウザで開く → Google認証 → 3スコープ許可
  [3/3] Google が `http://localhost` にリダイレクト → ブラウザは「接続できません」
        エラー画面になる（正常）。その失敗画面のURLをアドレスバーから全部コピーして
        ターミナルに貼り付け
  → token.json が生成され、内容が表示される
  → 内容を GitHub Secret `GOOGLE_OAUTH_TOKEN_JSON` に貼り付け
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from google_auth_oauthlib.flow import Flow

SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/gmail.compose",
]

CLIENT_SECRET_PATH = Path(__file__).resolve().parent.parent / "client_secret.json"
TOKEN_OUTPUT_PATH = Path(__file__).resolve().parent.parent / "token.json"
REDIRECT_URI = "http://localhost"


def main() -> None:
    if not CLIENT_SECRET_PATH.exists():
        sys.exit(
            f"client_secret.json が見つかりません: {CLIENT_SECRET_PATH}\n"
            "GCPコンソール → APIとサービス → 認証情報 → "
            "OAuthクライアントID（デスクトップアプリ）を作成し、JSONをダウンロードして上記パスに配置してください。"
        )

    flow = Flow.from_client_secrets_file(
        str(CLIENT_SECRET_PATH),
        scopes=SCOPES,
        redirect_uri=REDIRECT_URI,
    )
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        prompt="consent",
    )

    print()
    print("========================================")
    print("[1/3] 以下のURLをブラウザで開いて、Googleで認証してください")
    print("========================================")
    print(auth_url)
    print()
    print("[2/3] 以下の3スコープすべてを『許可』してください:")
    for scope in SCOPES:
        print(f"      - {scope}")
    print()
    print("[3/3] 認証完了後、ブラウザは『このサイトにアクセスできません』")
    print("      または『接続できません』というエラー画面になります（正常です）。")
    print("      そのエラー画面のアドレスバーのURLを最初から最後まで全部コピーして、")
    print("      下に貼り付けてEnterを押してください。")
    print()
    redirected_url = input("リダイレクトURL: ").strip()

    try:
        flow.fetch_token(authorization_response=redirected_url)
    except Exception as exc:
        sys.exit(
            f"\nトークン交換に失敗しました: {exc}\n\n"
            "考えられる原因:\n"
            "  - URLが不完全（途中で切れている）\n"
            "  - URLに `code=...` パラメータが含まれていない\n"
            "    （認証画面そのもののURLを貼ってしまった等）\n"
            "  - 認可コードが既に消費済み（同じURLは1回しか使えません）\n"
            "→ もう一度このスクリプトを実行して、新しいURLからやり直してください。"
        )

    creds = flow.credentials

    if not creds.refresh_token:
        sys.exit(
            "\n警告: refresh_token が取得できませんでした。\n"
            "本番ワークフローでは refresh_token が必須です。\n"
            "→ Googleアカウントの『サードパーティアプリのアクセス』から本アプリを削除し、"
            "もう一度このスクリプトを実行してください。"
        )

    token_data = {
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": list(creds.scopes) if creds.scopes else SCOPES,
    }

    TOKEN_OUTPUT_PATH.write_text(json.dumps(token_data, ensure_ascii=False, indent=2))

    print()
    print("========================================")
    print(f"=== token.json を生成しました: {TOKEN_OUTPUT_PATH} ===")
    print("========================================")
    print()
    print("以下の内容をすべてコピーし、GitHub Secret『GOOGLE_OAUTH_TOKEN_JSON』に貼り付けてください:")
    print("（GitHub → Settings → Secrets and variables → Actions → New repository secret）")
    print()
    print("---BEGIN COPY---")
    print(TOKEN_OUTPUT_PATH.read_text())
    print("---END COPY---")


if __name__ == "__main__":
    main()
