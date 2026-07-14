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
from src.utils import mask_name


class MasterSheetEmptyError(RuntimeError):
    """target_folder モードで参加者マスタータブが不在 / 0 件のとき送出する。

    自動検出モード（``RunConfig.master_sheet_strict=True``）は「セミナーごとに
    1 タブ」を前提とするため、対象セミナーのマスタータブが未準備のままだと
    Gmail 下書きが全件宛先空・医院名フォルダが AI 抽出値で作成、といった
    リカバリ困難な事故になる（2026-05-29 撤回の F-09 と同じ症状）。空タブの
    まま処理を進めないよう、PDF 処理ループに入る前に即停止する。

    呼び出し側（main.run / batch_main.step1_prepare_items /
    batch_main._process_results_and_create_pdfs）は本例外を捕捉して
    「中止」マーカーを追記してから再送出し、GHA ジョブを非ゼロ終了させる。
    """


def require_non_empty_master(
    records: list[Any],
    sheet_name: str,
    strict: bool,
    logger: logging.Logger,
) -> None:
    """target_folder モードでマスターが空ならログ + 例外。それ以外は何もしない。

    ``strict`` が False（プロファイルモード）のときは、過去通り空でも処理を
    続行する（WARNING は ``sheets_client._ensure_master_sheet`` が出す）。
    """
    if not strict:
        return
    if records:
        return
    logger.error(
        f"参加者マスタータブ '{sheet_name}' が不在または 0 件です。"
        f"セミナー名（= 入力フォルダ名）と一致するタブを準備して再実行して"
        f"ください（例: 入力フォルダ '新人育成塾' なら "
        f"タブ '参加者マスター(新人育成塾)' に参加者行を投入）。"
        f"準備しないまま実行すると Gmail 下書きが全件宛先空になり、"
        f"医院名フォルダも AI 抽出値で作成されてしまうため即停止します。"
    )
    raise MasterSheetEmptyError(sheet_name)


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
                f"（先頭が NNN-NN-N / NNN-NN 形式でない / 重複検知不可）: {file_name}"
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
            f"{clinic_number} → {mask_name(clinic_name_from_master)}"
        )
        return clinic_name_from_master, True
    logger.warning(
        f"参加者マスター未登録、AI 抽出値で代用: "
        f"医院番号={clinic_number}, AI 抽出={mask_name(clinic_name_from_ai)}"
    )
    return clinic_name_from_ai, False


def resolve_case_via_master(
    sheets_module: Any,
    master_records: list[Any],
    mgmt_num: str,
    logger: logging.Logger,
) -> tuple[str, str, str] | None:
    """添付資料の出力先 ``(医院番号, 医院名, 個人名)`` を参加者マスターだけで解決する。

    添付資料は通常、同一ラン内で処理されたメイン PDF の ``case_map`` から
    出力先を引く。しかし (1) メインが過去ランで処理済みで、添付資料だけ後から
    Drive に追加されたケース、(2) メイン記録後・添付コピー前にクラッシュして
    再実行したケース、では ``case_map`` に対応エントリが無い。従来はここで
    警告スキップ = **添付資料の恒久ロスト** になっていた。

    参加者マスターは管理番号（個人単位 ``NNN-NN``）→ 医院名・参加者名を持つ
    ため、メイン PDF の AI 抽出結果に依存せずマスターだけで出力先を解決できる。

    Returns:
        ``(医院番号, 医院名（マスター標準表記）, 個人名)``。マスターに管理番号
        が未登録なら None（呼び出し側が従来通り警告スキップする）。
    """
    record = sheets_module.lookup_participant_by_management_number(
        master_records, mgmt_num,
    )
    if record is None or not record.clinic_name:
        return None
    person_name = record.participant_name or "unknown_person"
    logger.info(
        f"添付資料の出力先を参加者マスターから解決: {mgmt_num} → "
        f"({record.clinic_number}, {mask_name(record.clinic_name)}, "
        f"{mask_name(person_name)})"
    )
    return (record.clinic_number, record.clinic_name, person_name)


def distribute_team_copies(
    drive_module: Any,
    sheets_module: Any,
    logger: logging.Logger,
    *,
    master_records: list[Any],
    reporter_mgmt_num: str,
    file_path: Path,
    file_name: str,
    output_folder_id: str,
) -> list[dict[str, str]]:
    """チーム事例のコメント入り PDF を、報告者と同じチームの全メンバーの
    フォルダへ配布する。

    ファイル名に ``チーム実践_`` / ``チームMTG_`` を含む PDF（``is_team_filename``
    判定）の呼び出し専用。報告者の管理番号 → 参加者マスター行 → F 列
    「所属チーム」→ 同チームの全行、の順に配布先を解決し、各メンバーの
    ``<医院番号>_<医院名>/<参加者名>/`` へ同じファイル名でアップロードする。

    設計上の要点:
        - **報告者自身は除外**する（報告者分は呼び出し元の既存フローが直前に
          アップロード済み。二重アップロードは ``upload_pdf`` の同名スキップで
          無害だが、無駄な Drive クエリを避ける）。
        - **メンバーへのアップロード失敗は raise で伝播**する（fail-soft に
          しない）。呼び出し元の per-item try が捕捉して error 計上 → 出力
          一覧シート未記録 → 再実行で配布からやり直しになる（アップロードは
          冪等なので安全）。fail-soft にすると「一部メンバーだけ欠けたまま
          シート記録済み = 恒久欠落」になる（P-031 の教訓）。
        - 呼び出し元は本関数を ``append_output_record`` より **前** に呼ぶこと
          （配布 → 記録の順序。逆にすると記録後クラッシュで配布漏れが恒久化）。
        - 報告者がマスター未登録 / F 列が空の場合は WARNING を出して空リストで
          戻る（報告者のみ = 従来動作への自然なフォールバック。raise しない）。

    Args:
        drive_module: drive_client モジュール（依存注入、テストパッチ互換）
        sheets_module: sheets_client モジュール
        logger: ログ出力先
        master_records: ``read_master_records`` のスナップショット
        reporter_mgmt_num: 報告者 PDF の管理番号（ファイル名冒頭 ``NNN-NN-N``）
        file_path: コメント結合済み PDF のローカルパス
        file_name: Drive 上のファイル名（全メンバー共通・報告者分と同名）
        output_folder_id: 出力ルートフォルダの Drive ID

    Returns:
        配布した各メンバー（報告者を除く）の
        ``{"clinic_name", "person_name", "drive_url"}`` 辞書のリスト。
        呼び出し側はこれを使って出力一覧シートにもメンバー分の行を追記できる。
        フォールバック時は空リスト。
    """
    reporter = sheets_module.lookup_participant_by_management_number(
        master_records, reporter_mgmt_num,
    )
    if reporter is None or not reporter.team:
        logger.warning(
            f"チーム事例ですが配布先を解決できません（参加者マスターに"
            f"管理番号 {reporter_mgmt_num} の行が無いか、F 列「所属チーム」が"
            f"空）: {file_name} → 報告者のフォルダのみに保存します"
        )
        return []

    members = sheets_module.find_team_members(master_records, reporter.team)
    reporter_key = (
        reporter.clinic_number,
        # 報告者自身の行を除外する（find_team_members と同じ同一人物判定）
        reporter.participant_name,
    )
    distributed: list[dict[str, str]] = []
    for member in members:
        if (member.clinic_number, member.participant_name) == reporter_key:
            continue
        if not member.clinic_name:
            logger.warning(
                f"チーム配布先の医院名が空のためスキップ "
                f"(管理番号={member.management_number})"
            )
            continue
        member_person_name = member.participant_name or "unknown_person"
        upload_result = drive_module.upload_pdf_to_clinic_person(
            file_path=file_path,
            output_root_folder_id=output_folder_id,
            clinic_number=member.clinic_number,
            clinic_name=member.clinic_name,
            person_name=member_person_name,
            file_name=file_name,
            # マスター由来の確定医院名なので既存フォルダ名の同期を許可
            clinic_name_authoritative=True,
        )
        distributed.append({
            "clinic_name": member.clinic_name,
            "person_name": member_person_name,
            "drive_url": upload_result["webViewLink"],
        })

    logger.info(
        f"チーム事例を配布: チーム={mask_name(reporter.team)} / "
        f"報告者を除く {len(distributed)} 名のフォルダへコピー ({file_name})"
    )
    return distributed


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
                f"(医院管理番号={clinic_number}, 個人名={mask_name(person_name)})"
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


# Gmail 添付合計サイズの上限（1 通あたり、生バイト）。
# Gmail のメッセージ上限は 25MB だが、添付は base64 で約 4/3 倍に膨らむため、
# 生 PDF の合計を 17MB に抑える（17MB × 1.37 ≒ 23.3MB < 25MB）。超過グループは
# 複数通に分割する。1 ファイル単独で超過する PDF は添付不能（分割できない）
# ため、ERROR ログを出して添付から除外する（PDF 自体は Drive 保存済みで、
# 運用者が Drive から手動添付できる）。
_GMAIL_ATTACH_TOTAL_LIMIT_BYTES = 17 * 1024 * 1024


def _chunk_paths_by_size(
    pdf_paths: list[Any],
    limit_bytes: int,
    logger: logging.Logger,
) -> tuple[list[list[Any]], list[Any]]:
    """添付 PDF を合計サイズが ``limit_bytes`` 以下のチャンク列に分割する。

    要素は元のオブジェクト（str / Path）をそのまま保持する（``create_draft``
    がどちらも受けるため、変換して呼び出し契約を変えない）。

    Returns:
        ``(chunks, oversized)``。``oversized`` は単独で上限超過し添付不能な
        ファイル（チャンクには含まれない）。
    """
    chunks: list[list[Any]] = []
    current: list[Any] = []
    current_size = 0
    oversized: list[Any] = []
    for path in pdf_paths:
        try:
            size = Path(path).stat().st_size
        except OSError:
            size = 0
        if size > limit_bytes:
            oversized.append(path)
            continue
        if current and current_size + size > limit_bytes:
            chunks.append(current)
            current = []
            current_size = 0
        current.append(path)
        current_size += size
    if current:
        chunks.append(current)
    return chunks, oversized


def _create_draft_with_size_guard(
    gmail_module: Any,
    to_email: str,
    person_name: str,
    pdf_paths: list[Any],
    logger: logging.Logger,
) -> None:
    """添付合計 25MB 上限を守って下書きを作成する（必要なら複数通に分割）。

    Gmail はメッセージ全体 25MB が上限で、超過すると下書き作成が **恒久失敗**
    する（リトライしても直らない → その個人宛の下書きが 1 通も作られない）。
    合計サイズでチャンクに分割し、チャンクごとに 1 通作成する。例外は呼び出し
    側で握る（本関数は raise し得る）。
    """
    chunks, oversized = _chunk_paths_by_size(
        pdf_paths, _GMAIL_ATTACH_TOTAL_LIMIT_BYTES, logger,
    )
    for path in oversized:
        logger.error(
            f"添付 PDF が単独で Gmail 上限を超えるため下書きに添付できません"
            f"（Drive には保存済み・手動添付してください）: {Path(path).name} "
            f"({Path(path).stat().st_size // (1024 * 1024)}MB)"
        )
    if not chunks:
        if oversized:
            logger.error(
                f"全添付がサイズ超過のため下書きを作成できません "
                f"(宛先={mask_name(person_name)}様)。Drive から手動送付してください。"
            )
        return
    if len(chunks) > 1:
        logger.warning(
            f"添付合計が Gmail 上限を超えるため {len(chunks)} 通に分割 "
            f"(宛先={mask_name(person_name)}様, 計{len(pdf_paths)}ファイル)"
        )
    for chunk in chunks:
        gmail_module.create_draft(
            to_email=to_email,
            person_name=person_name,
            pdf_paths=chunk,
            cc_email=None,
        )


def create_grouped_drafts(
    draft_items: list[dict[str, Any]],
    gmail_module: Any,
) -> None:
    """``draft_items`` をメールアドレスでグループ化して下書きを作成する（P-023）。

    - メールアドレスが空でない項目: アドレスごとに 1 通の下書きにまとめる
      （複数 PDF はそのまま複数添付になる）。添付合計が Gmail 上限
      （25MB/メッセージ）を超えるグループはサイズで複数通に分割する。
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

    # メールあり: グループごとに 1 通（サイズ超過時は複数通に分割）
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
                f"{[mask_name(n) for n in unique_names]} → "
                f"'{mask_name(person_name)}' で下書き作成"
            )
        try:
            _create_draft_with_size_guard(
                gmail_module,
                to_email=email,
                person_name=person_name,
                pdf_paths=pdf_paths,
                logger=logger,
            )
        except Exception as e:
            logger.error(
                f"Gmail下書き作成失敗（処理は続行）: {e}", exc_info=True
            )

    # メール空: 項目ごとに 1 通（集約キーがないため）
    for item in empty_email_items:
        try:
            _create_draft_with_size_guard(
                gmail_module,
                to_email="",
                person_name=item["person_name"],
                pdf_paths=[item["pdf_path"]],
                logger=logger,
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
            f"添付資料コピー完了: {mgmt_num} / {mask_name(clinic_name)} / "
            f"{mask_name(person_name)} / {file_name}"
        )
        stats["success"] += 1

    except Exception as e:
        logger.error(
            f"添付資料処理エラー: {file_name} - {e}", exc_info=True
        )
        stats["error"] += 1
