"""Google Drive APIクライアント。フォルダ内PDF一覧取得・ダウンロード・アップロード。"""

from __future__ import annotations

import io
import json
import logging
from pathlib import Path
from typing import Any

from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload

from src.config import (
    DRIVE_FOLDER_ID,
    GOOGLE_CREDENTIALS_JSON,
    GOOGLE_OAUTH_TOKEN_JSON,
    GOOGLE_SCOPES,
)
from src.utils import normalize_name_for_match

logger = logging.getLogger("jissen_comment")

# Drive API ``files().list()`` の最大 pageSize。
# Google の公式ドキュメント上の上限値であり、これを超えると API が 400 を返す。
# 1 ページあたりの取得件数を最大化することで pageToken ループの回数を最小化する。
DRIVE_PAGE_SIZE = 1000


def _get_credentials() -> Any:
    """Drive API用の認証情報を取得する。

    優先順位：
    1. Colab認証
    2. OAuthユーザートークン — サービスアカウントは My Drive 配下に
       アップロードすると storageQuotaExceeded になるため、ユーザー認可で
       実行することでファイル所有者をユーザーに固定する。
    3. サービスアカウント（共有ドライブ宛なら有効）
    4. ADC（Workload Identity 連携 / gcloud auth）
    """
    # Colab認証を試行
    try:
        import google.colab.auth  # type: ignore
        google.colab.auth.authenticate_user()
        from google.auth import default
        creds, _ = default(scopes=GOOGLE_SCOPES)
        logger.info("Drive認証: Colab認証を使用")
        return creds
    except ImportError:
        pass

    # OAuthユーザートークン（My Drive へのアップロードに必須）
    if GOOGLE_OAUTH_TOKEN_JSON:
        from google.oauth2.credentials import Credentials
        info = json.loads(GOOGLE_OAUTH_TOKEN_JSON)
        creds = Credentials.from_authorized_user_info(info)
        logger.info("Drive認証: OAuthユーザートークンを使用")
        return creds

    # サービスアカウント認証（JSON直接指定）
    if GOOGLE_CREDENTIALS_JSON:
        from google.oauth2 import service_account
        info = json.loads(GOOGLE_CREDENTIALS_JSON)
        creds = service_account.Credentials.from_service_account_info(
            info, scopes=GOOGLE_SCOPES
        )
        logger.info("Drive認証: サービスアカウントを使用")
        return creds

    # ADC（Workload Identity 連携 / gcloud auth）
    try:
        from google.auth import default
        creds, _ = default(scopes=GOOGLE_SCOPES)
        logger.info("Drive認証: Application Default Credentialsを使用")
        return creds
    except Exception:
        pass

    raise RuntimeError(
        "Google認証情報が見つかりません。"
        "Colab環境、GOOGLE_OAUTH_TOKEN_JSON / GMAIL_TOKEN_JSON、"
        "GOOGLE_CREDENTIALS_JSON、"
        "またはApplication Default Credentialsを設定してください。"
    )


def get_drive_service() -> Any:
    """Drive APIサービスを構築する。"""
    creds = _get_credentials()
    return build("drive", "v3", credentials=creds)


def list_pdfs(folder_id: str | None = None) -> list[dict]:
    """指定フォルダ内のPDFファイル一覧を取得する。共有ドライブ対応。

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
            pageSize=DRIVE_PAGE_SIZE,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()

        files.extend(response.get("files", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            break

    logger.info(f"Drive: {len(files)}件のPDFを検出 (フォルダ: {folder_id})")
    return files


def download_pdf(file_id: str) -> bytes:
    """PDFファイルをダウンロードする。共有ドライブ対応。

    Args:
        file_id: Google DriveファイルID

    Returns:
        PDFのバイナリデータ
    """
    service = get_drive_service()
    request = service.files().get_media(
        fileId=file_id,
        supportsAllDrives=True,
    )

    buffer = io.BytesIO()
    downloader = MediaIoBaseDownload(buffer, request)

    done = False
    while not done:
        _, done = downloader.next_chunk()

    data = buffer.getvalue()
    logger.info(f"Drive: ダウンロード完了 (ID: {file_id}, {len(data)} bytes)")
    return data


def find_or_create_folder(
    folder_name: str,
    parent_id: str,
    service: Any | None = None,
) -> str:
    """指定された親フォルダ配下に同名フォルダがあれば再利用、無ければ新規作成する。
    共有ドライブ対応。

    Args:
        folder_name: フォルダ名
        parent_id: 親フォルダのDrive ID
        service: 既存のDrive APIサービス（指定されない場合は新規構築）

    Returns:
        フォルダのDrive ID
    """
    if not folder_name:
        raise ValueError("folder_name が空です")
    if not parent_id:
        raise ValueError("parent_id が空です")

    service = service or get_drive_service()

    # 親フォルダ配下のフォルダを全件取得し、正規化形で一致するものを再利用する。
    # 完全一致検索だと「医療法人 かがやき」と「医療法人かがやき」のような表記揺れで
    # 重複フォルダが作られてしまうため、Drive側のフィルタは緩めて Python 側で
    # normalize_name_for_match() の結果を比較する。
    #
    # NOTE: Drive API は 1 ページ最大 1000 件しか返さないため、必ず
    # nextPageToken をループで辿る。1 ページしか見ないと 1001 件目以降にある
    # 既存フォルダを見落として重複作成してしまう（lessons.md P-010）。
    query = (
        f"'{parent_id}' in parents "
        f"and mimeType='application/vnd.google-apps.folder' "
        f"and trashed=false"
    )

    target_normalized = normalize_name_for_match(folder_name)
    page_token: str | None = None
    while True:
        response = service.files().list(
            q=query,
            fields="nextPageToken, files(id, name)",
            pageSize=DRIVE_PAGE_SIZE,
            pageToken=page_token,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()

        for existing_file in response.get("files", []):
            existing_name = existing_file["name"]
            if normalize_name_for_match(existing_name) == target_normalized:
                folder_id: str = existing_file["id"]
                if existing_name == folder_name:
                    logger.info(
                        f"Drive: 既存フォルダを再利用 ({folder_name}, ID: {folder_id})"
                    )
                else:
                    logger.info(
                        f"Drive: 表記揺れを検知して既存フォルダを再利用 "
                        f"(要求='{folder_name}', 既存='{existing_name}', ID: {folder_id})"
                    )
                return folder_id

        page_token = response.get("nextPageToken")
        if not page_token:
            break

    metadata = {
        "name": folder_name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [parent_id],
    }
    created = service.files().create(
        body=metadata,
        fields="id",
        supportsAllDrives=True,
    ).execute()
    new_id: str = created["id"]
    logger.info(f"Drive: フォルダ新規作成 ({folder_name}, ID: {new_id})")
    return new_id


def upload_pdf(
    file_path: str | Path,
    folder_id: str,
    file_name: str | None = None,
    service: Any | None = None,
) -> dict[str, str]:
    """PDFをDriveにアップロードし、ファイルIDと閲覧URLを返す。共有ドライブ対応。

    Args:
        file_path: アップロード元のローカルPDFパス
        folder_id: アップロード先フォルダのDrive ID
        file_name: Drive上のファイル名（省略時はローカルファイル名を使用）
        service: 既存のDrive APIサービス（指定されない場合は新規構築）

    Returns:
        {"id": "<file_id>", "webViewLink": "<url>"}
    """
    file_path = Path(file_path)
    if not file_path.exists():
        raise FileNotFoundError(f"アップロード元ファイルが存在しません: {file_path}")
    if not folder_id:
        raise ValueError("folder_id が空です")

    service = service or get_drive_service()

    metadata = {
        "name": file_name or file_path.name,
        "parents": [folder_id],
    }

    with file_path.open("rb") as fh:
        media = MediaIoBaseUpload(fh, mimetype="application/pdf", resumable=False)
        created = service.files().create(
            body=metadata,
            media_body=media,
            fields="id, webViewLink",
            supportsAllDrives=True,
        ).execute()

    file_id: str = created["id"]
    web_view_link: str = created.get("webViewLink", "")
    logger.info(
        f"Drive: アップロード完了 ({metadata['name']}, ID: {file_id})"
    )
    return {"id": file_id, "webViewLink": web_view_link}


def upload_pdf_to_clinic_person(
    file_path: str | Path,
    output_root_folder_id: str,
    clinic_name: str,
    person_name: str,
    file_name: str | None = None,
) -> dict[str, str]:
    """医院名/個人名 階層を作成（または再利用）し、その配下にPDFをアップロードする。

    Args:
        file_path: アップロード元PDFパス
        output_root_folder_id: 出力ルートフォルダのDrive ID
        clinic_name: 医院名（フォルダ名になる）
        person_name: 個人名（サブフォルダ名になる）
        file_name: Drive上のファイル名（省略時はローカルファイル名を使用）

    Returns:
        {"id": "<file_id>", "webViewLink": "<url>"}
    """
    service = get_drive_service()
    clinic_folder_id = find_or_create_folder(
        clinic_name, output_root_folder_id, service=service
    )
    person_folder_id = find_or_create_folder(
        person_name, clinic_folder_id, service=service
    )
    return upload_pdf(
        file_path=file_path,
        folder_id=person_folder_id,
        file_name=file_name,
        service=service,
    )
