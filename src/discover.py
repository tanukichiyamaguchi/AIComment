"""Drive フォルダ自動検出システム。

INPUT_ROOT 配下のサブフォルダから動的に処理対象を解決し、
出力フォルダ・スプレッドシートタブを自動派生する。

設計コンセプト（Convention over Configuration）:
    [現状] プロファイル YAML + Secret 追加が新セミナーごとに必要
    [新規] INPUT_ROOT 直下のサブフォルダを auto-discover、出力フォルダ・
           シートタブを自動派生

ユーザーの作業:
    1. Drive で ``DRIVE_INPUT_ROOT`` 配下にサブフォルダ作成
    2. そのフォルダに PDF をアップロード
    3. ワークフロー実行時に ``target_folder: <フォルダ名>`` と入力

システムが自動でやること:
    1. ``DRIVE_INPUT_ROOT`` 配下から ``target_folder`` 名のフォルダを検索
       （``normalize_name_for_match`` で表記揺れ吸収）
    2. ``DRIVE_OUTPUT_ROOT`` 配下に同名フォルダを作成（既存ならスキップ）
    3. ``SPREADSHEET_ID`` に同名タブを作成（既存ならスキップ）
    4. 既存処理パイプライン（PDF 取得 → Claude API → コメント PDF 生成
       → 結合 → Drive 保存 → Sheets 追記）を実行

管理番号は採番せず、実践事例 PDF のファイル名先頭（``NNN-NN-N`` 形式）から
抽出する（``src.utils.extract_management_number`` を参照）。

後方互換性:
    既存の ``--profile`` モード（``jissen_default`` / ``jissen_2024_q1`` 等）
    と既存の ``DRIVE_FOLDER_ID`` / ``DRIVE_OUTPUT_FOLDER_ID`` などの Secret は
    引き続き使用可能。本モジュールは追加機能であり、既存挙動を一切変更しない。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from src import drive_client
from src.config import GOOGLE_API_NUM_RETRIES, MASTER_SHEET_NAME
from src.profile import ProfileConfig, load_profile
from src.utils import normalize_name_for_match

logger = logging.getLogger("jissen_comment")


@dataclass(frozen=True)
class DiscoveredContext:
    """target_folder から解決された実行コンテキスト。

    Attributes:
        target_folder_name: 元の表記（ログ・表示用）。Drive 上のフォルダ名そのまま。
        input_folder_id: 入力サブフォルダの Drive ID。
        output_folder_id: 出力サブフォルダの Drive ID（自動作成済み）。
        output_sheet_name: スプレッドシートのタブ名。
    """
    target_folder_name: str
    input_folder_id: str
    output_folder_id: str
    output_sheet_name: str


@dataclass(frozen=True)
class RunConfig:
    """main / batch_main の実行時に使う、プロファイル / 自動検出のどちらでも
    統一的に扱える設定。``step1_prepare`` / ``step4_generate_pdfs`` がこの
    インターフェースだけに依存することで、分岐コードを最小化する。

    ``master_sheet_name`` は参加者マスターシート名（医院名標準化 + Gmail
    下書きの TO ルックアップ用）。自動検出モードでは Profile 由来ではないため
    ``MASTER_SHEET_NAME`` の既定値を使う。
    """
    display_name: str
    input_folder_id: str
    output_folder_id: str
    output_sheet_name: str
    master_sheet_name: str = MASTER_SHEET_NAME

    @classmethod
    def from_profile(cls, profile: ProfileConfig) -> "RunConfig":
        return cls(
            display_name=(
                f"プロファイル: {profile.display_name} ({profile.name}, "
                f"document_type={profile.document_type}, period={profile.period})"
            ),
            input_folder_id=profile.input_folder_id,
            output_folder_id=profile.output_folder_id,
            output_sheet_name=profile.output_sheet_name,
            master_sheet_name=profile.master_sheet_name,
        )

    @classmethod
    def from_discovered(cls, ctx: DiscoveredContext) -> "RunConfig":
        return cls(
            display_name=f"自動検出: {ctx.target_folder_name}",
            input_folder_id=ctx.input_folder_id,
            output_folder_id=ctx.output_folder_id,
            output_sheet_name=ctx.output_sheet_name,
        )


def resolve_run_config(
    profile_name: str | None,
    target_folder: str | None,
) -> RunConfig:
    """``profile_name`` / ``target_folder`` のどちらが指定されたかで設定を解決する。

    優先順位: target_folder > profile_name > "jissen_default"（後方互換）。
    両方指定はエントリポイントの argparse 排他で防止されている。
    """
    if target_folder:
        from src.config import DRIVE_INPUT_ROOT, DRIVE_OUTPUT_ROOT, SPREADSHEET_ID
        ctx = resolve_context(
            target_folder=target_folder,
            input_root_id=DRIVE_INPUT_ROOT,
            output_root_id=DRIVE_OUTPUT_ROOT,
            spreadsheet_id=SPREADSHEET_ID,
        )
        return RunConfig.from_discovered(ctx)

    return RunConfig.from_profile(load_profile(profile_name or "jissen_default"))


def handle_list_mode(logger_: logging.Logger | None = None) -> None:
    """``--target-folder __list__`` の処理。候補名を列挙してログ出力する。

    ``DRIVE_INPUT_ROOT`` が未設定なら早期 return（クラッシュさせない）。
    """
    log = logger_ or logger
    from src.config import DRIVE_INPUT_ROOT
    if not DRIVE_INPUT_ROOT:
        log.error(
            "DRIVE_INPUT_ROOT が未設定です。"
            "フォルダ自動検出モードを使うには Secret/環境変数 DRIVE_INPUT_ROOT を"
            "設定してください。"
        )
        return
    names = list_target_folder_names(DRIVE_INPUT_ROOT)
    log.info(f"利用可能な target_folder ({len(names)} 件): {names}")


def list_input_subfolders(
    input_root_id: str,
    service: Any | None = None,
) -> list[dict[str, str]]:
    """INPUT_ROOT 配下のサブフォルダを全件取得する（pageToken 対応）。

    Args:
        input_root_id: 親フォルダの Drive ID
        service: 既存の Drive API サービス（指定されない場合は新規構築）

    Returns:
        ``[{"id": "...", "name": "..."}, ...]`` 形式のリスト。順序は API 戻り順。

    Notes:
        - Drive API は 1 ページ最大 1000 件しか返さないため、``nextPageToken``
          を必ずループで辿る（lessons.md P-010）。
        - 共有ドライブ対応のため ``supportsAllDrives=True`` /
          ``includeItemsFromAllDrives=True`` を渡す。
    """
    if not input_root_id:
        raise ValueError("input_root_id が空です")

    service = service or drive_client.get_drive_service()
    query = (
        f"'{input_root_id}' in parents "
        f"and mimeType='application/vnd.google-apps.folder' "
        f"and trashed=false"
    )

    all_folders: list[dict[str, str]] = []
    page_token: str | None = None
    while True:
        response = service.files().list(
            q=query,
            fields="nextPageToken, files(id, name)",
            pageSize=drive_client.DRIVE_PAGE_SIZE,
            pageToken=page_token,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute(num_retries=GOOGLE_API_NUM_RETRIES)
        all_folders.extend(response.get("files", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            break

    logger.info(
        f"Drive: INPUT_ROOT 配下サブフォルダ {len(all_folders)} 件を検出 "
        f"(parent={input_root_id})"
    )
    return all_folders


def resolve_context(
    target_folder: str,
    input_root_id: str,
    output_root_id: str,
    spreadsheet_id: str,
    service_drive: Any | None = None,
    service_sheets: Any | None = None,
) -> DiscoveredContext:
    """``target_folder`` 名から実行コンテキストを解決する。

    手順:
        1. ``INPUT_ROOT`` 配下から ``target_folder`` 名のフォルダを find
           （``normalize_name_for_match`` マッチ）。
        2. ``OUTPUT_ROOT`` 配下に同名フォルダを find_or_create。
        3. ``SPREADSHEET_ID`` に同名タブを find_or_create（実際の create は
           ``sheets_client._ensure_output_sheet`` 経由で append 時に走る。
           ここでは sheet 名のみ決定する）。

    Args:
        target_folder: ユーザー指定のフォルダ名（表記揺れ許容）
        input_root_id: 入力 ROOT の Drive ID
        output_root_id: 出力 ROOT の Drive ID
        spreadsheet_id: 出力先スプレッドシートの ID（タブ名は actual_name と同じ）
        service_drive: 既存の Drive API サービス（テスト時の注入用）
        service_sheets: 既存の Sheets API サービス（テスト時の注入用、
            本関数では未使用だが API 一貫性のため受ける）

    Returns:
        ``DiscoveredContext``

    Raises:
        ValueError: ``target_folder`` が ``INPUT_ROOT`` 配下に見つからない、
            または各種 ID が空。
    """
    if not target_folder:
        raise ValueError("target_folder が空です")
    if not input_root_id:
        raise ValueError("input_root_id が空です（DRIVE_INPUT_ROOT 未設定）")
    if not output_root_id:
        raise ValueError("output_root_id が空です（DRIVE_OUTPUT_ROOT 未設定）")
    if not spreadsheet_id:
        raise ValueError("spreadsheet_id が空です（SPREADSHEET_ID 未設定）")

    # 1. INPUT_ROOT 配下のサブフォルダを取得し、normalize マッチで検索
    folders = list_input_subfolders(input_root_id, service_drive)
    target_normalized = normalize_name_for_match(target_folder)
    matched = next(
        (f for f in folders if normalize_name_for_match(f["name"]) == target_normalized),
        None,
    )
    if not matched:
        available = ", ".join(sorted(f["name"] for f in folders))
        raise ValueError(
            f"target_folder '{target_folder}' が DRIVE_INPUT_ROOT 配下に "
            f"見つかりません。利用可能: {available}"
        )

    actual_name = matched["name"]  # Drive 上の元表記を採用（表示優先）
    input_folder_id = matched["id"]

    if actual_name != target_folder:
        logger.info(
            f"Drive: target_folder 表記揺れを検知 "
            f"(要求='{target_folder}', 実体='{actual_name}')"
        )

    # 2. 出力フォルダを find_or_create（既存の drive_client.find_or_create_folder
    #    を流用し、同じ表記揺れ吸収ロジックで重複作成を防ぐ）
    output_folder_id = drive_client.find_or_create_folder(
        folder_name=actual_name,
        parent_id=output_root_id,
        service=service_drive,
    )

    # 3. シートタブ名 = フォルダ名（簡素優先）。実際の addSheet は
    #    sheets_client._ensure_output_sheet が append 時に冪等実行する。
    output_sheet_name = actual_name

    ctx = DiscoveredContext(
        target_folder_name=actual_name,
        input_folder_id=input_folder_id,
        output_folder_id=output_folder_id,
        output_sheet_name=output_sheet_name,
    )
    logger.info(
        f"フォルダ自動検出: target='{actual_name}' "
        f"input={input_folder_id} output={output_folder_id} "
        f"sheet='{output_sheet_name}'"
    )
    return ctx


def list_target_folder_names(
    input_root_id: str,
    service: Any | None = None,
) -> list[str]:
    """target_folder 候補名のリストを返す（ログ表示・``__list__`` モード用）。

    Args:
        input_root_id: 入力 ROOT の Drive ID
        service: 既存の Drive API サービス

    Returns:
        サブフォルダ名のソート済みリスト（重複は API 仕様上ないが念のため
        unique 化はしない — Drive 側で同名フォルダが存在する状態自体が運用上の
        問題なので、見えるままにする）。
    """
    return sorted(f["name"] for f in list_input_subfolders(input_root_id, service))
