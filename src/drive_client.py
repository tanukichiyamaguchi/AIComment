"""Google Drive APIクライアント。フォルダ内PDF一覧取得＆ダウンロード。"""

from __future__ import annotations

import json
import logging
from typing import Any

from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
import io

from src.config import DRIVE_FOLDER_ID, GOOGLE_CREDENTIALS_JSON, GOOGLE_SCOPES

logger = logging.getLogger("jissen_comment")


def _get_credentials() -> Any:
    """環境に応じた認証情報を取得する。Colab優先、次にサービスアカウント。"""
    # Colab認証を試行
    try:
        import google.colab.auth  # type: ignore
        google.colab.auth.authenticate_user()
        from google.auth import default
        creds, _ = default(scopes=GOOGLE_SCOPES)
        logger.info("Google認証: Colab認証を使用")
        return creds
    except ImportError:
        pass

    # サービスアカウント認証（JSON直接指定）
    if GOOGLE_CREDENTIALS_JSON:
        from google.oauth2 import service_account
        info = json.loads(GOOGLE_CREDENTIALS_JSON)
        creds = service_account.Credentials.from_service_account_info(
            info, scopes=GOOGLE_SCOPES
        )
        logger.info("Google認証: サービスアカウントを使用")
        return creds

    # ADC（Workload Identity 連携 / gcloud auth）
    try:
        from google.auth import default
        creds, _ = default(scopes=GOOGLE_SCOPES)
        logger.info("Google認証: Application Default Credentialsを使用")
        return creds
    except Exception:
        pass

    raise RuntimeError(
        "Google認証情報が見つかりません。"
        "Colab環境、GOOGLE_CREDENTIALS_JSON環境変数、"
        "またはApplication Default Credentialsを設定してください。"
    )


def get_drive_service() -> Any:
    """Drive APIサービスを構築する。"""
    creds = _get_credentials()
    return build("drive", "v3", credentials=creds)


def list_pdfs(folder_id: str | None = None) -> list[dict]:
    """指定フォルダ内のPDFファイル一覧を取得する。

    Args:
        folder_id: Google DriveフォルダID（Noneの場合は設定値を使用）

    Returns:
        [{"id": str, "name": str}, ...]
    """
    folder_id = folder_id or DRIVE_FOLDER_ID
    if not folder_id:
        raise ValueError("DRIVE_FOLDER_IDが設定されていません")

    service = get_drive_service()
    query = f"'{folder_id}' in parents and mimeType='application/pdf' and trashed=false"

    files = []
    page_token = None

    while True:
        response = service.files().list(
            q=query,
            fields="nextPageToken, files(id, name)",
            pageToken=page_token,
            pageSize=100,
        ).execute()

        files.extend(response.get("files", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            break

    logger.info(f"Drive: {len(files)}件のPDFを検出 (フォルダ: {folder_id})")
    return files


def download_pdf(file_id: str) -> bytes:
    """PDFファイルをダウンロードする。

    Args:
        file_id: Google DriveファイルID

    Returns:
        PDFのバイナリデータ
    """
    service = get_drive_service()
    request = service.files().get_media(fileId=file_id)

    buffer = io.BytesIO()
    downloader = MediaIoBaseDownload(buffer, request)

    done = False
    while not done:
        _, done = downloader.next_chunk()

    data = buffer.getvalue()
    logger.info(f"Drive: ダウンロード完了 (ID: {file_id}, {len(data)} bytes)")
    return data
