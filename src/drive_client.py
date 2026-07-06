"""Google Drive APIクライアント。フォルダ内PDF一覧取得・ダウンロード・アップロード。"""

from __future__ import annotations

import io
import json
import logging
import threading
import unicodedata
from pathlib import Path
from typing import Any

from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload

from src.config import (
    DRIVE_FOLDER_ID,
    GOOGLE_API_NUM_RETRIES,
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


# ── サービスの thread-local キャッシュ ──
# ``build()`` + 認証情報生成 + 初回トークンリフレッシュは 1 回あたり数百 ms の
# コストがあり、従来は API 呼び出しのたびに実行していた（1000 件ランで
# download + upload の 2000 回超）。プロセス内でキャッシュして排除する。
# thread-local にする理由: googleapiclient のサービス（下層 httplib2）は
# 非スレッドセーフのため、step1 の並列ダウンロード（PR-2a）ではスレッドごとに
# 独立したサービスが必要。creds の期限切れは google-auth が呼び出し時に自動
# リフレッシュするため、長時間ラン（5h ポーリング後の step4 等）でも安全。
_SERVICE_TLS = threading.local()


def _cached_drive_service() -> Any:
    """thread-local にキャッシュした Drive サービスを返す（初回のみ構築）。"""
    service = getattr(_SERVICE_TLS, "service", None)
    if service is None:
        service = get_drive_service()
        _SERVICE_TLS.service = service
    return service


def reset_service_cache() -> None:
    """サービスキャッシュをクリアする（テスト用）。"""
    global _SERVICE_TLS
    _SERVICE_TLS = threading.local()


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

    service = _cached_drive_service()
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
        ).execute(num_retries=GOOGLE_API_NUM_RETRIES)

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
    service = _cached_drive_service()
    request = service.files().get_media(
        fileId=file_id,
        supportsAllDrives=True,
    )

    buffer = io.BytesIO()
    downloader = MediaIoBaseDownload(buffer, request)

    done = False
    while not done:
        _, done = downloader.next_chunk(num_retries=GOOGLE_API_NUM_RETRIES)

    data = buffer.getvalue()
    logger.info(f"Drive: ダウンロード完了 (ID: {file_id}, {len(data)} bytes)")
    return data


# ── フォルダ解決のプロセス内キャッシュ ──
# ``find_or_create_folder`` / ``find_or_create_clinic_folder`` は 1 回の解決
# ごとに親フォルダ配下の **全サブフォルダを走査** する（表記揺れ照合のため
# Drive クエリで絞り込めない）。1000 PDF 規模では 1 件ごとに医院フォルダ +
# 個人フォルダの 2 走査 = 数千回の冗長 list API 呼び出しになり、実行時間を
# 数十分〜時間単位で浪費する。同一ラン内で解決済みのフォルダ ID をキャッシュ
# して再走査を省く（フォルダ ID はリネームでも不変なので、authoritative
# リネーム後もキャッシュは有効）。
_FOLDER_CACHE: dict[tuple[str, str], str] = {}
_CLINIC_FOLDER_CACHE: dict[tuple[str, str], str] = {}


def reset_folder_caches() -> None:
    """フォルダ解決キャッシュをクリアする（テスト用 / 長寿命プロセス用）。"""
    _FOLDER_CACHE.clear()
    _CLINIC_FOLDER_CACHE.clear()


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

    cache_key = (parent_id, normalize_name_for_match(folder_name))
    cached = _FOLDER_CACHE.get(cache_key)
    if cached is not None:
        return cached

    service = service or _cached_drive_service()

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
        ).execute(num_retries=GOOGLE_API_NUM_RETRIES)

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
                _FOLDER_CACHE[cache_key] = folder_id
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
    ).execute(num_retries=GOOGLE_API_NUM_RETRIES)
    new_id: str = created["id"]
    logger.info(f"Drive: フォルダ新規作成 ({folder_name}, ID: {new_id})")
    _FOLDER_CACHE[cache_key] = new_id
    return new_id


def _find_file_in_folder(
    file_name: str,
    folder_id: str,
    service: Any,
) -> dict[str, str] | None:
    """指定フォルダ内に同名のファイルが既に存在するか調べる。

    1000 PDF 規模の再実行で同じファイル名が **重複アップロード** されるのを
    防ぐ（P-023）。同名・同所のファイル ID と webViewLink を返す。存在しない
    場合は None。複数該当（過去の重複の名残）の場合は決定論的に ID 昇順の
    最初を採用する。
    """
    safe_name = file_name.replace("'", "\\'")
    query = (
        f"name = '{safe_name}' "
        f"and '{folder_id}' in parents "
        f"and mimeType = 'application/pdf' "
        f"and trashed = false"
    )
    response = service.files().list(
        q=query,
        fields="files(id, name, webViewLink)",
        pageSize=10,
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
    ).execute(num_retries=GOOGLE_API_NUM_RETRIES)
    files = response.get("files", [])
    if not files:
        return None
    if len(files) > 1:
        logger.warning(
            f"Drive: 同名ファイルが {len(files)} 件存在（重複の名残）: "
            f"{file_name}, IDs={sorted(f['id'] for f in files)} → 先頭採用"
        )
    chosen = sorted(files, key=lambda f: f["id"])[0]
    return {"id": chosen["id"], "webViewLink": chosen.get("webViewLink", "")}


def upload_pdf(
    file_path: str | Path,
    folder_id: str,
    file_name: str | None = None,
    service: Any | None = None,
) -> dict[str, str]:
    """PDFをDriveにアップロードし、ファイルIDと閲覧URLを返す。共有ドライブ対応。

    冪等性: 同名ファイルがすでにアップロード先フォルダに存在する場合は
    **再アップロードせず** 既存のファイル ID / URL を返す（P-023）。
    Step4 が途中クラッシュして再実行した際に Drive 側でファイルが重複する
    （Drive は同名・同所のファイル重複を許容する）のを防ぐ。

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

    service = service or _cached_drive_service()
    upload_name = file_name or file_path.name

    # 重複アップロード防止（P-023）。同名ファイルが既存なら再アップロードしない。
    existing = _find_file_in_folder(upload_name, folder_id, service)
    if existing is not None:
        logger.info(
            f"Drive: 同名ファイル既存のためアップロードをスキップ "
            f"({upload_name}, ID: {existing['id']})"
        )
        return existing

    metadata = {
        "name": upload_name,
        "parents": [folder_id],
    }

    with file_path.open("rb") as fh:
        media = MediaIoBaseUpload(fh, mimetype="application/pdf", resumable=False)
        created = service.files().create(
            body=metadata,
            media_body=media,
            fields="id, webViewLink",
            supportsAllDrives=True,
        ).execute(num_retries=GOOGLE_API_NUM_RETRIES)

    file_id: str = created["id"]
    web_view_link: str = created.get("webViewLink", "")
    logger.info(
        f"Drive: アップロード完了 ({metadata['name']}, ID: {file_id})"
    )
    return {"id": file_id, "webViewLink": web_view_link}


def find_or_create_clinic_folder(
    clinic_number: str,
    clinic_name: str,
    parent_id: str,
    service: Any | None = None,
    clinic_name_authoritative: bool = False,
) -> str:
    """医院フォルダを医院番号で識別して find_or_create する。

    医院フォルダ名は ``<医院番号>_<医院名>`` 形式だが、医院名部分は
    AI 抽出で揺れる（``三浦歯科医院`` / ``三浦歯科`` / ``医療法人三浦歯科``）
    ため、**識別は医院番号（フォルダ名の ``<医院番号>_`` プレフィックス）
    のみ** で行う。同じ医院番号のフォルダが既にあれば医院名部分が今回の抽出と
    違っても再利用する（既定ではリネームしない）。ただし
    ``clinic_name_authoritative=True``（参加者マスター由来の確定医院名）の
    ときは、既存フォルダ名が確定名と異なれば確定名へリネームして反映する。

    P-009 の正規化（NFKC＋空白除去）は語そのものの違い（``三浦歯科医院`` と
    ``三浦歯科`` のように接尾語が違う）を吸収できないため、識別子（医院番号）
    部分のみを使った前方一致で判定する（P-019）。

    Args:
        clinic_number: 医院番号（3〜5桁の数字。空文字列の場合は
            ``find_or_create_folder(clinic_name, parent_id)`` にフォールバック）
        clinic_name: 医院名（AI 抽出値）。新規作成時のフォルダ名末尾に使う
        parent_id: 親フォルダ ID
        service: Drive API サービス（省略時は新規構築）
        clinic_name_authoritative: ``clinic_name`` が参加者マスター由来の確定名
            のとき ``True``。既存フォルダ名が確定名と異なる場合に確定名へ
            リネームする。AI 抽出値のとき ``False``（既定）でリネームしない。

    Returns:
        医院フォルダの Drive ID
    """
    if not parent_id:
        raise ValueError("parent_id が空です")

    # 医院番号が空 → 旧来の名前ベース照合へフォールバック（defensive、
    # 実運用上は管理番号なし PDF はループの前段でスキップされるためほぼ発生
    # しないが、呼び出し側に余計な分岐を強要しないためにここで吸収する）。
    if not clinic_number:
        if not clinic_name:
            raise ValueError("clinic_number と clinic_name の両方が空です")
        return find_or_create_folder(clinic_name, parent_id, service=service)

    # 同一ラン内で解決済みの医院フォルダは再走査しない。authoritative リネームは
    # 初回解決時に実施済み（同一ラン内はマスタースナップショットが不変のため、
    # 2 回目以降の再判定は同じ結果になる）。
    clinic_cache_key = (parent_id, clinic_number)
    cached_clinic = _CLINIC_FOLDER_CACHE.get(clinic_cache_key)
    if cached_clinic is not None:
        return cached_clinic

    service = service or _cached_drive_service()

    # 親フォルダ配下のサブフォルダ一覧を取得（P-010、pageToken ループ必須。
    # 親フォルダに 1001 件以上のサブフォルダがあるとき、2 ページ目以降に存在
    # する同じ医院番号のフォルダを見落として重複作成しないように）。
    query = (
        f"'{parent_id}' in parents "
        f"and mimeType='application/vnd.google-apps.folder' "
        f"and trashed=false"
    )

    # 識別子部分は ``<医院番号>_``（アンダースコア込み）で前方一致判定する。
    # ``_`` 込みなら ``001_`` は ``0011_別医院`` に誤マッチしない
    # （``0011_`` は ``001_`` で始まらない）。
    prefix = f"{clinic_number}_"

    matched: list[dict] = []
    page_token: str | None = None
    while True:
        response = service.files().list(
            q=query,
            fields="nextPageToken, files(id, name)",
            pageSize=DRIVE_PAGE_SIZE,
            pageToken=page_token,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute(num_retries=GOOGLE_API_NUM_RETRIES)

        for existing_file in response.get("files", []):
            existing_name = existing_file["name"]
            # 既存フォルダ名は NFKC 正規化してプレフィックス判定する
            # （全角数字で作られたフォルダがあっても拾えるように。空白除去は
            # 医院番号には不要なため、normalize_name_for_match は使わない）。
            normalized = unicodedata.normalize("NFKC", existing_name)
            if normalized.startswith(prefix):
                matched.append(existing_file)

        page_token = response.get("nextPageToken")
        if not page_token:
            break

    if matched:
        # フォルダ ID 昇順で決定論的に 1 つ選ぶ。1 件ならそのまま、複数件なら
        # 「本修正前にできた重複」を意味するため警告 + 手動統合推奨ログを出す。
        matched.sort(key=lambda f: f["id"])
        chosen = matched[0]
        folder_id: str = chosen["id"]
        if len(matched) == 1:
            logger.info(
                f"Drive: 医院番号で既存フォルダを再利用 "
                f"(医院番号={clinic_number}, 既存名='{chosen['name']}', "
                f"今回の医院名='{clinic_name}', ID: {folder_id})"
            )
        else:
            duplicate_ids = [f["id"] for f in matched]
            logger.warning(
                f"Drive: 同じ医院番号({clinic_number})のフォルダが"
                f"{len(matched)}個見つかりました。フォルダ ID 昇順で先頭を"
                f"決定論的に再利用します（手動統合を推奨）。"
                f"重複 ID 一覧: {duplicate_ids}, "
                f"選択 ID: {folder_id}（名前='{chosen['name']}'）"
            )

        # clinic_name_authoritative（参加者マスター由来の確定医院名）なら、
        # 既存フォルダ名が確定名と違うとき確定名へリネームして反映する。AI 抽出値
        # （authoritative=False）はリネームしない（``三浦歯科`` ↔ ``三浦歯科医院``
        # の往復 churn を防ぐ P-019 の意図を維持）。リネームはフォルダ ID・URL を
        # 変えないため、医院フォルダ URL シートの既存リンクは保持される。
        if clinic_name_authoritative and clinic_name:
            desired_name = f"{clinic_number}_{clinic_name}"
            if chosen["name"] != desired_name:
                try:
                    service.files().update(
                        fileId=folder_id,
                        body={"name": desired_name},
                        fields="id",
                        supportsAllDrives=True,
                    ).execute(num_retries=GOOGLE_API_NUM_RETRIES)
                    logger.info(
                        f"Drive: 医院フォルダ名をマスター医院名に同期 "
                        f"(医院番号={clinic_number}, "
                        f"'{chosen['name']}' → '{desired_name}', ID: {folder_id})"
                    )
                except Exception as e:
                    # リネーム失敗は再利用を妨げない（ID で続行、次回再試行）。
                    logger.warning(
                        f"Drive: 医院フォルダ名の同期に失敗 "
                        f"(医院番号={clinic_number}, 目標名='{desired_name}', "
                        f"ID: {folder_id}): {e}"
                    )
        _CLINIC_FOLDER_CACHE[clinic_cache_key] = folder_id
        return folder_id

    # マッチ無し → 新規作成。フォルダ名は今回の AI 抽出値を使った
    # ``<医院番号>_<医院名>`` 形式。
    new_folder_name = f"{clinic_number}_{clinic_name}" if clinic_name else clinic_number
    metadata = {
        "name": new_folder_name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [parent_id],
    }
    created = service.files().create(
        body=metadata,
        fields="id",
        supportsAllDrives=True,
    ).execute(num_retries=GOOGLE_API_NUM_RETRIES)
    new_id: str = created["id"]
    logger.info(
        f"Drive: 医院フォルダ新規作成 ({new_folder_name}, ID: {new_id})"
    )
    _CLINIC_FOLDER_CACHE[clinic_cache_key] = new_id
    return new_id


def upload_pdf_to_clinic_person(
    file_path: str | Path,
    output_root_folder_id: str,
    clinic_number: str,
    clinic_name: str,
    person_name: str,
    file_name: str | None = None,
    service: Any | None = None,
    clinic_name_authoritative: bool = False,
) -> dict[str, str]:
    """医院/個人名 階層を作成（または再利用）し、その配下にPDFをアップロードする。

    医院フォルダの識別は **医院番号のみ** で行う（``find_or_create_clinic_folder``、
    P-019）。AI が同じ医院を違う医院名で抽出しても、同じ医院番号のフォルダが
    既にあれば再利用する。``clinic_name_authoritative=True``（参加者マスター由来
    の確定名）のときのみ、既存フォルダ名を確定名へリネームして反映する。

    Args:
        file_path: アップロード元PDFパス
        output_root_folder_id: 出力ルートフォルダのDrive ID
        clinic_number: 医院番号（3〜5桁の数字。空文字列の場合は ``clinic_name``
            だけでフォルダを照合する旧来挙動にフォールバック）
        clinic_name: 医院名（AI 抽出の生の値、医院番号プレフィックスは含まない）。
            新規作成される医院フォルダ名は ``<医院番号>_<医院名>``。
        person_name: 個人名（サブフォルダ名になる）
        file_name: Drive上のファイル名（省略時はローカルファイル名を使用）
        service: 既存のDrive APIサービス（指定されない場合は新規構築）
        clinic_name_authoritative: ``clinic_name`` が参加者マスター由来の確定名
            のとき ``True``。既存医院フォルダ名が確定名と異なれば確定名へ
            リネームする（``find_or_create_clinic_folder`` に委譲）。

    Returns:
        ``{"id": "<file_id>", "webViewLink": "<url>", "clinic_folder_id": "<id>"}``。
        ``clinic_folder_id`` は医院フォルダの Drive ID で、呼び出し側が
        医院フォルダ URL を構築する（医院フォルダ URL シート記録に使う）。
    """
    service = service or _cached_drive_service()
    clinic_folder_id = find_or_create_clinic_folder(
        clinic_number=clinic_number,
        clinic_name=clinic_name,
        parent_id=output_root_folder_id,
        service=service,
        clinic_name_authoritative=clinic_name_authoritative,
    )
    person_folder_id = find_or_create_folder(
        person_name, clinic_folder_id, service=service
    )
    result = upload_pdf(
        file_path=file_path,
        folder_id=person_folder_id,
        file_name=file_name,
        service=service,
    )
    result["clinic_folder_id"] = clinic_folder_id
    return result
