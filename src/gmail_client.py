"""Gmail APIクライアント。コメント付きPDFを添付した下書きを作成する。"""

from __future__ import annotations

import base64
import json
import logging
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any

from googleapiclient.discovery import build

from src.config import (
    GOOGLE_CREDENTIALS_JSON,
    GOOGLE_OAUTH_TOKEN_JSON,
    GOOGLE_SCOPES,
)
from src.utils import mask_email

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
    pdf_path: str | Path,
    cc_email: str | None = None,
    max_retries: int = 1,
) -> str:
    """Gmail下書きを作成する。PDF添付付き。

    Args:
        to_email: 送信先メールアドレス（TO）
        person_name: 氏名（件名・本文に使用）
        pdf_path: 添付するPDFのパス
        cc_email: CC のメールアドレス。``None`` または空文字列なら CC ヘッダー
            を付けない（個人メール宛のときに医院メールを CC する用途）
        max_retries: リトライ回数

    Returns:
        作成された下書きのID
    """
    pdf_path = Path(pdf_path)
    service = get_gmail_service()

    # メール構築
    message = MIMEMultipart()
    message["to"] = to_email
    if cc_email:
        message["cc"] = cc_email
    message["subject"] = SUBJECT_TEMPLATE.format(person_name=person_name)

    body = BODY_TEMPLATE.format(person_name=person_name)
    message.attach(MIMEText(body, "plain", "utf-8"))

    # PDF添付
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

    # 下書き作成（リトライ付き）
    last_error = None
    for attempt in range(max_retries + 1):
        try:
            draft = service.users().drafts().create(
                userId="me",
                body={"message": {"raw": raw}},
            ).execute()

            draft_id = draft["id"]
            cc_log = f", CC={mask_email(cc_email)}" if cc_email else ""
            logger.info(
                f"Gmail下書き作成: {person_name}様 "
                f"(TO={mask_email(to_email)}{cc_log}) → draft_id={draft_id}"
            )
            return draft_id

        except Exception as e:
            last_error = e
            if attempt < max_retries:
                logger.warning(f"Gmail下書き作成失敗 (試行{attempt + 1}): {e}")
            else:
                logger.error(f"Gmail下書き作成失敗（リトライ上限）: {e}")

    raise last_error  # type: ignore
