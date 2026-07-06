"""Gmail APIクライアント。コメント付きPDFを添付した下書きを作成する。"""

from __future__ import annotations

import base64
import json
import logging
import time
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any

from googleapiclient.discovery import build

from src.config import (
    GOOGLE_API_NUM_RETRIES,
    GOOGLE_CREDENTIALS_JSON,
    GOOGLE_OAUTH_TOKEN_JSON,
    GOOGLE_SCOPES,
)
from src.utils import mask_email
from src.utils import mask_name

logger = logging.getLogger("jissen_comment")

# ── メールテンプレート ──
SUBJECT_TEMPLATE = "【実践事例】じっせん君コメント ─ {person_name}様"

BODY_TEMPLATE = """\
{person_name}様

いつもお世話になっております。
歯科医院地域一番実践会事務局です。

実践事例報告シートへのコメントをお送りいたします。
添付PDFをご確認ください。

引き続きよろしくお願いいたします。
"""


def _get_gmail_credentials() -> Any:
    """Gmail API用の認証情報を取得する。"""
    # Colab認証を試行
    try:
        import google.colab.auth  # type: ignore
        google.colab.auth.authenticate_user()
        from google.auth import default
        creds, _ = default(scopes=GOOGLE_SCOPES)
        logger.info("Gmail認証: Colab認証を使用")
        return creds
    except ImportError:
        pass

    # OAuthトークン（GitHub Actions用 / GOOGLE_OAUTH_TOKEN_JSON または旧 GMAIL_TOKEN_JSON）
    if GOOGLE_OAUTH_TOKEN_JSON:
        from google.oauth2.credentials import Credentials
        info = json.loads(GOOGLE_OAUTH_TOKEN_JSON)
        creds = Credentials.from_authorized_user_info(info)
        logger.info("Gmail認証: OAuthトークンを使用")
        return creds

    # サービスアカウント（ドメイン全体の委任が必要）
    if GOOGLE_CREDENTIALS_JSON:
        from google.oauth2 import service_account
        info = json.loads(GOOGLE_CREDENTIALS_JSON)
        creds = service_account.Credentials.from_service_account_info(
            info, scopes=GOOGLE_SCOPES
        )
        logger.info("Gmail認証: サービスアカウントを使用")
        return creds

    # ADC（Workload Identity 連携 / gcloud auth）
    try:
        from google.auth import default
        creds, _ = default(scopes=GOOGLE_SCOPES)
        logger.info("Gmail認証: Application Default Credentialsを使用")
        return creds
    except Exception:
        pass

    raise RuntimeError("Gmail認証情報が見つかりません")


def get_gmail_service() -> Any:
    """Gmail APIサービスを構築する。"""
    creds = _get_gmail_credentials()
    return build("gmail", "v1", credentials=creds)


def create_draft(
    to_email: str,
    person_name: str,
    pdf_paths: list[Path] | list[str] | str | Path,
    cc_email: str | None = None,
    max_retries: int = 4,
) -> str:
    """Gmail下書きを作成する。PDF添付付き。

    リトライ設計:
        呼び出し側（``run_common.create_grouped_drafts``）はランの **最後に**
        まとめて下書きを作成するため、ここで失敗した下書きはそのラン内で二度と
        再試行されず恒久ロストになる（PDF 処理自体は完了しているので再ランでも
        スキップされ、下書きだけが欠ける）。一過性エラー（429/5xx）で失わない
        よう、指数バックオフ付きの手動リトライ（既定 4 回）に加えて、
        ``execute(num_retries=...)`` の googleapiclient 内蔵リトライも重ねる
        （多層防御、step3 ポーリングと同じ方針）。

    Args:
        to_email: 送信先メールアドレス（TO）。空文字なら宛先空で作成（手動補完用）。
        person_name: 氏名（件名・本文に使用）
        pdf_paths: 添付する PDF のパス。複数 PDF を 1 通にまとめる場合はリスト
            で渡す。後方互換のため単一の ``str`` / ``Path`` も受け付ける。
        cc_email: CC のメールアドレス。``None`` または空文字列なら CC ヘッダー
            を付けない。現在は CC を使っていない（参加者マスター統合後の運用は
            TO のみ）。将来必要になったら呼び出し側で値を渡す。
        max_retries: 手動リトライ回数（指数バックオフ付き）

    Returns:
        作成された下書きのID
    """
    # 単一パスを渡された場合はリストに正規化（後方互換）
    if isinstance(pdf_paths, (str, Path)):
        path_list: list[Path] = [Path(pdf_paths)]
    else:
        path_list = [Path(p) for p in pdf_paths]
    if not path_list:
        raise ValueError("pdf_paths が空です")

    service = get_gmail_service()

    # メール構築
    message = MIMEMultipart()
    message["to"] = to_email
    if cc_email:
        message["cc"] = cc_email
    message["subject"] = SUBJECT_TEMPLATE.format(person_name=person_name)

    body = BODY_TEMPLATE.format(person_name=person_name)
    message.attach(MIMEText(body, "plain", "utf-8"))

    # PDF添付（複数対応）
    for pdf_path in path_list:
        with open(pdf_path, "rb") as f:
            attachment = MIMEApplication(f.read(), _subtype="pdf")
            attachment.add_header(
                "Content-Disposition",
                "attachment",
                filename=pdf_path.name,
            )
            message.attach(attachment)

    # Base64エンコード
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")

    # 下書き作成（指数バックオフ付きリトライ + googleapiclient 内蔵リトライ）
    last_error = None
    for attempt in range(max_retries + 1):
        try:
            draft = service.users().drafts().create(
                userId="me",
                body={"message": {"raw": raw}},
            ).execute(num_retries=GOOGLE_API_NUM_RETRIES)

            draft_id = draft["id"]
            cc_log = f", CC={mask_email(cc_email)}" if cc_email else ""
            attach_log = (
                f", 添付{len(path_list)}件" if len(path_list) > 1 else ""
            )
            logger.info(
                f"Gmail下書き作成: {mask_name(person_name)}様 "
                f"(TO={mask_email(to_email)}{cc_log}{attach_log}) "
                f"→ draft_id={draft_id}"
            )
            return draft_id

        except Exception as e:
            last_error = e
            if attempt < max_retries:
                wait = min(2.0 ** (attempt + 1), 30.0)
                logger.warning(
                    f"Gmail下書き作成失敗 (試行{attempt + 1}/{max_retries + 1}): "
                    f"{e}. {wait:.0f}秒後にリトライ..."
                )
                time.sleep(wait)
            else:
                logger.error(f"Gmail下書き作成失敗（リトライ上限）: {e}")

    raise last_error  # type: ignore
