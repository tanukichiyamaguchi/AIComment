"""GOOGLE_OAUTH_TOKEN_JSON 用の OAuth ユーザートークンを生成する一回限りのセットアップスクリプト。

事前準備:
  1. GCPコンソールで OAuth クライアント ID（種類: デスクトップアプリ）を作成
  2. JSON をダウンロードして本スクリプトと同じディレクトリに `client_secret.json` として保存

実行:
  python scripts/generate_oauth_token.py

成功時の挙動:
  - ブラウザが自動で開き Google 認証を要求
  - 同意後、`token.json` が生成され、内容が標準出力に表示される
  - その内容を GitHub Secret `GOOGLE_OAUTH_TOKEN_JSON` に貼り付ける
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/gmail.compose",
]

CLIENT_SECRET_PATH = Path(__file__).resolve().parent.parent / "client_secret.json"
TOKEN_OUTPUT_PATH = Path(__file__).resolve().parent.parent / "token.json"


def main() -> None:
    if not CLIENT_SECRET_PATH.exists():
        sys.exit(
            f"client_secret.json が見つかりません: {CLIENT_SECRET_PATH}\n"
            "GCPコンソール → APIとサービス → 認証情報 → OAuthクライアントIDを作成し、"
            "JSONをダウンロードして上記パスに配置してください。"
        )

    flow = InstalledAppFlow.from_client_secrets_file(
        str(CLIENT_SECRET_PATH), scopes=SCOPES
    )
    creds = flow.run_local_server(port=0)

    token_data = {
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": creds.scopes,
    }

    TOKEN_OUTPUT_PATH.write_text(json.dumps(token_data, ensure_ascii=False, indent=2))
    print(f"\n=== token.json を生成しました: {TOKEN_OUTPUT_PATH} ===\n")
    print("以下の内容をすべてコピーし、GitHub Secret『GOOGLE_OAUTH_TOKEN_JSON』に貼り付けてください:\n")
    print(TOKEN_OUTPUT_PATH.read_text())


if __name__ == "__main__":
    main()
