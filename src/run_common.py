"""通常モード（main）と Batch モード（batch_main）の共通処理。

両エントリポイントは「PDF 分類 → 重複判定 → 医院名標準化 → Drive/Sheets 出力
→ Gmail 下書き集約 → 完了マーカー」という同じ後段処理を持つ。ここに共通実装を
置き、エントリポイント側は実行モード固有の流れ（1 件ずつ処理 / Batch API の
step 分割）だけを持つ。

依存モジュール（sheets_client / drive_client / gmail_client）は import せず、
呼び出し側から引数で受け取る（依存注入）。既存テストが
``patch("src.main.sheets_client")`` のようにエントリポイントのモジュール属性を
パッチする前提のため、共通側が直接 import すると差し替えが効かなくなる
（lessons.md P-024 の「except 節とモックの相互作用」と同種の罠）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable
import logging

from src import config
from src.utils import extract_management_number, is_attachment_filename, setup_logging


def split_main_and_attachments(
    pdf_files: list[dict],
    logger: logging.Logger | None = None,
) -> tuple[list[dict], list[dict]]:
    """入力 PDF を「メイン実践事例」と「添付資料」に早期分類する（P-016）。

    添付資料は AI 処理せず passthrough 経路で出力へコピーするだけなので、
    メインループ内の if 分岐ではなく、安価なファイル名判定で別経路に分ける。

    Args:
        pdf_files: ``drive_client.list_pdfs`` の戻り値
        logger: 渡された場合のみ分類結果の件数ログを出す
            （回収時の Drive 再走査など、独自ログを持つ呼び出し元は省略する）
    """
    main_files = [f for f in pdf_files if not is_attachment_filename(f["name"])]
    attachment_files = [f for f in pdf_files if is_attachment_filename(f["name"])]
    if logger is not None:
        logger.info(
            f"入力分類: メイン実践事例 {len(main_files)}件 / "
            f"添付資料 {len(attachment_files)}件"
        )
    return main_files, attachment_files


def select_new_targets(
    main_files: list[dict],
    processed: set[str],
    logger: logging.Logger,
    skipped_records: list[dict] | None = None,
) -> tuple[list[dict], int, int]:
    """メイン PDF から処理対象（新規）を選定する（増分処理 / P-015）。

    管理番号（ファイル名先頭の ``NNN-NN-N``）をキーに、出力一覧シートに既存の
    PDF と管理番号なし PDF をスキップする。重複スキップは無条件（bypass なし）。
    再処理が必要なら出力シートの該当行を手動削除する。

    Args:
        main_files: メイン実践事例 PDF のリスト
        processed: 出力一覧シートの処理済み管理番号スナップショット
        logger: スキップ理由のログ出力先
        skipped_records: 渡された場合、スキップした PDF の manifest レコードを
            追記する（Batch モードの ``batch_step1_skips.json`` 用、M-2）

    Returns:
        ``(targets, skip_no_number 件数, skip_processed 件数)``
    """
    targets: list[dict] = []
    skip_no_number = 0
    skip_processed = 0
    for pdf_file in main_files:
        file_name = pdf_file["name"]
        mgmt_num = extract_management_number(file_name)
        if not mgmt_num:
            logger.warning(
                f"管理番号をファイル名から抽出できないためスキップ"
                f"（先頭が NNN-NN-N 形式でない / 重複検知不可）: {file_name}"
            )
            skip_no_number += 1
            if skipped_records is not None:
                skipped_records.append({
                    "file_id": pdf_file["id"], "file_name": file_name,
                    "reason": "no_management_number",
                })
            continue
        if mgmt_num in processed:
            logger.info(f"処理済みのためスキップ: {mgmt_num} ({file_name})")
            skip_processed += 1
            if skipped_records is not None:
                skipped_records.append({
                    "file_id": pdf_file["id"], "file_name": file_name,
                    "reason": "already_processed",
                    "management_number": mgmt_num,
                })
            continue
        targets.append(pdf_file)
    return targets, skip_no_number, skip_processed


def resolve_clinic_name(
    sheets_module: Any,
    master_records: list[Any],
    clinic_number: str,
    clinic_name_from_ai: str,
    logger: logging.Logger,
) -> tuple[str, bool]:
    """医院名を参加者マスターの標準表記で解決する。

    マスターから引いた標準表記を最優先（表記統一 + 「開業準備中」等の固定
    文字列対応）。未登録なら AI 抽出値で代用し、必ず警告ログを出す
    （サイレント代用は事故の元、P-021）。

    Returns:
        ``(医院名, マスター由来の確定名か)``。確定名のときだけ既存フォルダの
        リネーム同期（``clinic_name_authoritative``）を許可する（P-019 の限定緩和)。
    """
    clinic_name_from_master = sheets_module.lookup_clinic_name(
        master_records, clinic_number,
    )
    if clinic_name_from_master:
        logger.info(
            f"医院名をマスターシートから取得: "
            f"{clinic_number} → {clinic_name_from_master}"
        )
        return clinic_name_from_master, True
    logger.warning(
        f"参加者マスター未登録、AI 抽出値で代用: "
        f"医院番号={clinic_number}, AI 抽出={clinic_name_from_ai}"
    )
    return clinic_name_from_ai, False


def collect_draft_item(
    draft_items: list[dict[str, Any]],
    master_records: list[Any],
    sheets_module: Any,
    clinic_number: str,
    person_name: str,
    pdf_path: Path,
) -> None:
    """PDF アップロード成功後、Gmail 下書き用の (メール, 個人名, PDF) を
    ``draft_items`` に追加する。マスター lookup の例外は警告ログを出して
    握りつぶす（fail-soft、PDF 処理は止めない）。
    """
    logger = setup_logging()
    try:
        email = sheets_module.lookup_email_by_clinic_and_person(
            master_records, clinic_number, person_name,
        )
        if not email:
            logger.warning(
                f"メール未ヒット → 宛先空で下書き予定 "
                f"(医院管理番号={clinic_number}, 個人名={person_name!r})"
            )
        draft_items.append({
            "email": email,
            "person_name": person_name,
            "pdf_path": pdf_path,
            "clinic_number": clinic_number,
        })
    except Exception as e:
        logger.error(
            f"メール lookup 失敗（PDF 処理は続行、下書きは作らない）: {e}",
            exc_info=True,
        )


def create_grouped_drafts(
    draft_items: list[dict[str, Any]],
    gmail_module: Any,
) -> None:
    """``draft_items`` をメールアドレスでグループ化して下書きを作成する（P-023）。

    - メールアドレスが空でない項目: アドレスごとに 1 通の下書きにまとめる
      （複数 PDF はそのまま複数添付になる）
    - メールアドレスが空の項目: グループ化キーがないため項目ごとに 1 通の
      宛先空の下書きを作る（手動で補完してもらう運用）

    例外は警告ログを出して握りつぶし、他グループの下書き作成は続行する
    （fail-soft、Gmail API の一過性エラーで全滅しないよう）。

    ``config.ENABLE_GMAIL_DRAFTS=false`` のときは下書きを 1 通も作らずに戻る。
    """
    logger = setup_logging()
    if not config.ENABLE_GMAIL_DRAFTS:
        logger.info(
            f"Gmail下書きはOFF（ENABLE_GMAIL_DRAFTS=false）のため作成をスキップ"
            f"（蓄積 {len(draft_items)}件は破棄）"
        )
        return
    if not draft_items:
        return

    groups: dict[str, list[dict[str, Any]]] = {}
    empty_email_items: list[dict[str, Any]] = []
    for item in draft_items:
        email = item["email"]
        if email:
            groups.setdefault(email, []).append(item)
        else:
            empty_email_items.append(item)

    logger.info(
        f"Gmail下書き集約: メールあり{len(groups)}グループ "
        f"({sum(len(v) for v in groups.values())}件分), "
        f"メール空{len(empty_email_items)}件 → 計{len(groups) + len(empty_email_items)}通の下書きを作成"
    )

    # メールあり: グループごとに 1 通
    for email, items in groups.items():
        pdf_paths = [item["pdf_path"] for item in items]
        unique_names = sorted({item["person_name"] for item in items})
        if len(unique_names) == 1:
            person_name = unique_names[0]
        else:
            # 同一メールに別人が紐づく場合（家族メール、医院共有メール等）、
            # 件名・本文に複数名が含まれていることを示す形式に切り替える
            # （F-06: 先頭1名だけ書くと受信者が混乱するため）
            person_name = f"{unique_names[0]} ほか{len(unique_names) - 1}名"
            logger.warning(
                f"同一メール {email!r} に異なる個人名が紐づいています: "
                f"{unique_names} → '{person_name}' で下書き作成"
            )
        try:
            gmail_module.create_draft(
                to_email=email,
                person_name=person_name,
                pdf_paths=pdf_paths,
                cc_email=None,
            )
        except Exception as e:
            logger.error(
                f"Gmail下書き作成失敗（処理は続行）: {e}", exc_info=True
            )

    # メール空: 項目ごとに 1 通（集約キーがないため）
    for item in empty_email_items:
        try:
            gmail_module.create_draft(
                to_email="",
                person_name=item["person_name"],
                pdf_paths=[item["pdf_path"]],
                cc_email=None,
            )
        except Exception as e:
            logger.error(
                f"Gmail下書き作成失敗（処理は続行）: {e}", exc_info=True
            )


class ClinicFolderRecorder:
    """医院フォルダURLシート（``<出力シート名>_医院``）への記録（重複防止込み）。

    記録済み医院番号を実行開始時（コンストラクタ）に 1 回スナップショットし、
    同一実行内で記録した医院番号も追跡して、両方に無い医院だけ追記する
    （同一医院をシートに重複追加しない）。
    """

    def __init__(self, sheets_module: Any, output_sheet_name: str) -> None:
        self._sheets = sheets_module
        self.clinic_sheet_name = f"{output_sheet_name}_医院"
        self._recorded: set[str] = sheets_module.get_recorded_clinic_numbers(
            sheet_name=self.clinic_sheet_name,
        )
        self._recorded_this_run: set[str] = set()

    def record(
        self, clinic_number: str, clinic_name: str, clinic_folder_id: str
    ) -> None:
        """医院を 1 行記録する。医院番号が空（管理番号抽出不能）なら記録しない。"""
        if not clinic_number:
            return
        if clinic_number in self._recorded:
            return
        if clinic_number in self._recorded_this_run:
            return
        clinic_folder_url = (
            f"https://drive.google.com/drive/folders/{clinic_folder_id}"
        )
        self._sheets.append_clinic_folder_record(
            clinic_number=clinic_number,
            clinic_name=clinic_name,
            clinic_folder_url=clinic_folder_url,
            sheet_name=self.clinic_sheet_name,
        )
        self._recorded_this_run.add(clinic_number)


def append_completion_marker_safe(
    sheets_module: Any,
    sheet_name: str,
    summary: str,
    logger: logging.Logger,
) -> None:
    """出力一覧シートの最終行に完了/中止マーカーを 1 行追加する（fail-soft）。

    運用者がシート上で一目で完了を把握できるようにする。本処理は既に終わって
    いるため、マーカー追記の失敗で例外は上げない（警告ログのみ）。
    """
    try:
        sheets_module.append_completion_marker(
            sheet_name=sheet_name,
            summary=summary,
        )
    except Exception as e:
        logger.warning(f"完了マーカーの追記に失敗（処理自体は完了済み）: {e}")


def passthrough_attachment(
    *,
    drive_module: Any,
    sheets_module: Any,
    logger: logging.Logger,
    stats: dict[str, int],
    collect_draft: Callable[..., None],
    file_id: str,
    file_name: str,
    mgmt_num: str,
    clinic_number: str,
    clinic_name: str,
    person_name: str,
    output_folder_id: str,
    output_sheet_name: str,
    session_outputs_dir: Path,
    clinic_name_authoritative: bool = False,
) -> None:
    """添付資料 PDF 1 件をメインと同じ出力先へコピーする（P-016 passthrough）。

    AI 処理（テキスト抽出 / Claude API / コメントページ生成 / 結合）は一切
    せず、元 PDF のバイト列をそのまま再アップロードする。出力ファイル名は
    元のまま（``make_output_filename`` は使わない）。添付資料はメインと同じ
    管理番号 = 同じ医院番号なので、``find_or_create_clinic_folder`` 経由で
    メインと同じ医院フォルダへコピーされる（P-019）。出力一覧シートには
    「【添付資料】<元名>」で記録する。

    成功 / 失敗は ``stats``（``success`` / ``error``）にインプレース反映する。
    例外はログを出して握りつぶす（per-item fail-soft）。

    Args:
        collect_draft: Gmail 下書き蓄積コールバック
            （``clinic_number`` / ``person_name`` / ``pdf_path`` キーワードで呼ぶ）。
            添付資料も同じ個人宛のメイン PDF と同じグループに集約され、
            1 通の下書きに複数添付される。
        clinic_name_authoritative: 医院名がマスター由来の確定名のときだけ
            ``True``（既存フォルダ名の同期リネームを許可）。
    """
    try:
        # 添付資料 PDF は session_outputs_dir に書いてループ終了後の
        # 集約下書き作成まで生存させる。
        pdf_data = drive_module.download_pdf(file_id)
        att_subdir = session_outputs_dir / f"attach_{file_id}"
        att_subdir.mkdir(parents=True, exist_ok=True)
        attachment_path = att_subdir / file_name
        attachment_path.write_bytes(pdf_data)
        upload_result = drive_module.upload_pdf_to_clinic_person(
            file_path=attachment_path,
            output_root_folder_id=output_folder_id,
            clinic_number=clinic_number,
            clinic_name=clinic_name,
            person_name=person_name,
            file_name=file_name,
            clinic_name_authoritative=clinic_name_authoritative,
        )

        sheets_module.append_output_record(
            management_number=mgmt_num,
            clinic_name=clinic_name,
            person_name=person_name,
            sample_name=f"【添付資料】{file_name}",
            drive_url=upload_result["webViewLink"],
            sheet_name=output_sheet_name,
        )

        collect_draft(
            clinic_number=clinic_number,
            person_name=person_name,
            pdf_path=attachment_path,
        )

        logger.info(
            f"添付資料コピー完了: {mgmt_num} / {clinic_name} / "
            f"{person_name} / {file_name}"
        )
        stats["success"] += 1

    except Exception as e:
        logger.error(
            f"添付資料処理エラー: {file_name} - {e}", exc_info=True
        )
        stats["error"] += 1
