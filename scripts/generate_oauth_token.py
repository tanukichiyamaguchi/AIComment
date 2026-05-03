"""GOOGLE_OAUTH_TOKEN_JSON 用の OAuth ユーザートークンを生成する一回限りのセットアップスクリプト。

クラウド開発環境（GitHub Codespaces / Cloud Shell など）でも動作する手動URL貼付け方式。
ローカル環境でブラウザの localhost コールバックを使えない場合のフォールバックとして、
ユーザーがブラウザのURL欄から認証コードを手でコピペする形を取る。

事前準備:
  1. GCPコンソールで OAuth クライアント ID（種類: デスクトップアプリ）を作成
  2. JSON をダウンロードして本リポジトリのルートに `client_secret.json` として保存

実行:
  python scripts/generate_oauth_token.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from google_auth_oauthlib.flow import Flow

SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/gmail.compose",
]

CLIENT_SECRET_PATH = Path(__file__).resolve().parent.parent / "client_secret.json"
TOKEN_OUTPUT_PATH = Path(__file__).resolve().parent.parent / "token.json"
REDIRECT_URI = "http://localhost:8080/"


def main() -> None:
    if not CLIENT_SECRET_PATH.exists():
        sys.exit(
            f"client_secret.json が見つかりません: {CLIENT_SECRET_PATH}\n"
            "GCPコンソール → クライアント → デスクトップアプリのクライアントIDを作成し、"
            "JSONをダウンロードして上記パスに配置してください。"
        )

    flow = Flow.from_client_secrets_file(
        str(CLIENT_SECRET_PATH),
        scopes=SCOPES,
        redirect_uri=REDIRECT_URI,
    )
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        include_granted_scopes="true",
    )

    print("\n" + "=" * 70)
    print("STEP 1: 下のURL（auth_url）をコピーし、自分のPCのブラウザで開く")
    print("=" * 70)
    print(auth_url)
    print()
    print("STEP 2: Googleでログイン（Drive保存先と同じ仕事用アカウント）")
    print("        → Drive / Sheets / Gmail の3つすべて『許可』")
    print()
    print("STEP 3: 認証完了後、ブラウザは『このサイトにアクセスできません』に")
    print("        なるが正常。ブラウザのアドレスバーに表示された")
    print("        http://localhost:8080/?code=4/0AVMBsJj...&scope=...")
    print("        という長いURL全体をコピー（state、scope、codeすべて含む）")
    print()
    print("=" * 70)

    redirect_url = input(
        "STEP 4: コピーしたURL全体を貼り付けて Enter:\n> "
    ).strip()

    parsed = urlparse(redirect_url)
    query = parse_qs(parsed.query)
    code_values = query.get("code", [])
    if not code_values:
        sys.exit(
            "URL に認証コード（code=...）が含まれていません。"
            "STEP 3 のURL全体を貼り付けたか確認してください。"
        )
    code = code_values[0]

    flow.fetch_token(code=code)
    creds = flow.credentials

    token_data = {
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": creds.scopes,
    }
    TOKEN_OUTPUT_PATH.write_text(
        json.dumps(token_data, ensure_ascii=False, indent=2)
    )

    print()
    print("=" * 70)
    print(f"成功！token.json を生成しました: {TOKEN_OUTPUT_PATH}")
    print("=" * 70)
    print()
    print("以下の内容をすべてコピーし、GitHub Secret『GOOGLE_OAUTH_TOKEN_JSON』に")
    print("貼り付けてください（Settings → Secrets and variables → Actions）:")
    print()
    print(TOKEN_OUTPUT_PATH.read_text())


if __name__ == "__main__":
    main()
