"""Batchモードのエントリポイント。50%割引で大量PDFを一括処理する。

事前のスプレッドシート入力（Sheet1）は不要。Drive上の全PDFを取得し、
Claude APIで医院名・氏名・実践事例タイトル・コメントを抽出/生成、
コメント付きPDFをDriveに「医院名/個人名/」階層で保存し、出力一覧シートに記録する。

管理番号は実践事例 PDF のファイル名先頭（``NNN-NN-N`` 形式）から抽出する。

実行モード:
    1. プロファイル（``--profile <name>``）— ``profiles/<name>.yaml`` を読み込み、
       入力フォルダ・出力フォルダ・出力シート名を切り替える。
       既存挙動を完全維持する後方互換モード。
    2. フォルダ自動検出（``--target-folder <name>``）— ``DRIVE_INPUT_ROOT``
       配下の同名サブフォルダを auto-discover し、出力フォルダ・シートタブを
       自動派生する。Secret/YAML 追加なしで新セミナーに対応。
    3. ``--target-folder __list__`` — 候補名を列挙して即終了。

    両方省略時は ``--profile jissen_default``（既存挙動）。
"""

from __future__ import annotations

import argparse
import json
import tempfile
import time
from pathlib import Path

from src.utils import (
    setup_logging,
    ensure_fonts,
    extract_clinic_number,
    extract_management_number,
    is_attachment_filename,
)
from src.config import LOGS_DIR
from src import discover, drive_client, sheets_client
from src import pdf_reader, comment_generator, pdf_creator, pdf_merger
from src.discover import RunConfig
from src.profile import ProfileConfig


# 添付資料 PDF の情報（file id / file name / 管理番号）を Step1→Step4 で
# 受け渡すための JSON ファイル。``batch_prep.json``（メイン PDF の準備データ）
# とは別ファイルにして、既存の prep 形式を壊さずに passthrough 情報を運ぶ。
_BATCH_ATTACHMENTS_FILE = "batch_attachments.json"


def step1_prepare(
    profile: ProfileConfig | RunConfig,
    test_count: int = 0,
) -> list[dict]:
    """Step 1: 準備 — PDF取得・重複判定・テキスト抽出。

    増分処理（重複検知）対応:
        管理番号（ファイル名先頭の ``NNN-NN-N``）をキーに、出力一覧シートに
        既存の PDF はスキップ対象に分類し ``items`` に含めない（無条件、bypass
        なし）。Batch API に投げない＝コスト削減になる。管理番号を持たない
        PDF も同様にスキップする（重複検知が原理的に不可能なため）。
        既に処理済みの PDF を再処理したい場合は、出力一覧シートの該当行を
        手動で削除すれば、その管理番号は「未処理」扱いに戻り次回実行で
        再処理される。

    添付資料パススルー対応:
        ファイル名に「【添付資料】」を含む PDF は実践事例の補足資料であり、
        Claude API に投げない。メイン PDF のみを ``items`` に入れて従来通り
        バッチ投入し、添付資料の情報（file id / file name / 管理番号）は
        ``batch_attachments.json`` に別途保存して Step4 に引き継ぐ
        （lessons.md P-016）。添付資料にも重複判定（処理済みスキップ・
        管理番号なしスキップ）をこの時点で適用し、スキップ対象は保存しない。

    Args:
        profile: 実行時設定（``ProfileConfig`` または ``RunConfig``）。
            後方互換のため ``ProfileConfig`` を受けるが、内部では ``RunConfig``
            と同じフィールドのみ参照する。
        test_count: 新規 PDF を N 件まで処理（0=全件処理）。重複・管理番号
            なしを除外した「処理対象の新規 PDF」に対して適用される。

    Returns:
        Batch APIに投入可能なアイテムのリスト
    """
    logger = setup_logging()
    logger.info("=== Step 1: 準備 ===")

    pdf_files = drive_client.list_pdfs(folder_id=profile.input_folder_id)
    logger.info(f"PDF一覧: {len(pdf_files)}件")

    # 入力 PDF を「メイン実践事例」と「添付資料」に早期分類する（P-016）。
    # 添付資料は Claude API に投げず、Step4 で出力へコピーするだけなので、
    # メインのバッチ投入経路（items）には含めない。
    main_files = [f for f in pdf_files if not is_attachment_filename(f["name"])]
    attachment_files = [f for f in pdf_files if is_attachment_filename(f["name"])]
    logger.info(
        f"入力分類: メイン実践事例 {len(main_files)}件 / "
        f"添付資料 {len(attachment_files)}件"
    )

    # 重複判定（増分処理）— Batch API への投入前に実施する。
    # 管理番号はファイル名から取れるため、download / Claude 投入の前に分類できる。
    # 重複スキップは無条件（bypass なし）。再処理が必要なら出力シートの行を手動削除する。
    processed = sheets_client.get_processed_management_numbers(
        sheet_name=profile.output_sheet_name,
    )

    skip_no_number = 0
    skip_processed = 0
    targets: list[dict] = []
    for pdf_file in main_files:
        file_name = pdf_file["name"]
        mgmt_num = extract_management_number(file_name)
        if not mgmt_num:
            logger.warning(
                f"管理番号をファイル名から抽出できないためスキップ"
                f"（先頭が NNN-NN-N 形式でない / 重複検知不可）: {file_name}"
            )
            skip_no_number += 1
            continue
        if mgmt_num in processed:
            logger.info(f"処理済みのためスキップ: {mgmt_num} ({file_name})")
            skip_processed += 1
            continue
        targets.append(pdf_file)

    if test_count > 0:
        targets = targets[:test_count]
        logger.info(f"テストモード: 新規対象を{test_count}件に制限")
    logger.info(
        f"処理対象: {len(targets)}件のPDF（新規） / "
        f"管理番号なしスキップ {skip_no_number}件, "
        f"処理済みスキップ {skip_processed}件"
    )

    # 添付資料の情報を収集し batch_attachments.json に保存（Step4 で使用）。
    # 重複判定（処理済みスキップ・管理番号なしスキップ）は添付資料にもこの
    # 時点で適用し、スキップ対象は保存しない。
    attachment_records: list[dict] = []
    for pdf_file in attachment_files:
        file_name = pdf_file["name"]
        mgmt_num = extract_management_number(file_name)
        if not mgmt_num:
            logger.warning(
                f"管理番号をファイル名から抽出できないため添付資料をスキップ"
                f"（先頭が NNN-NN-N 形式でない）: {file_name}"
            )
            continue
        if mgmt_num in processed:
            logger.info(
                f"処理済みのため添付資料をスキップ: {mgmt_num} ({file_name})"
            )
            continue
        attachment_records.append({
            "file_id": pdf_file["id"],
            "file_name": file_name,
            "management_number": mgmt_num,
        })

    items = []
    for i, pdf_file in enumerate(targets, start=1):
        file_id = pdf_file["id"]
        file_name = pdf_file["name"]
        logger.info(f"[{i}/{len(targets)}] {file_name}")

        try:
            pdf_data = drive_client.download_pdf(file_id)
            pdf_text = pdf_reader.extract_text(pdf_data)
            if not pdf_text:
                logger.warning(f"テキスト抽出失敗（空テキスト）: {file_name}")
                continue
        except Exception as e:
            logger.warning(f"PDF処理失敗: {file_name} - {e}")
            continue

        items.append({
            "custom_id": f"item_{i:04d}",
            "pdf_data_id": file_id,  # 後でダウンロードし直す用
            "pdf_file_name": file_name,
            "pdf_text": pdf_text,
        })

    logger.info(
        f"Step 1完了: メイン {len(items)}件が処理可能 / "
        f"添付資料 {len(attachment_records)}件をパススルー予約"
    )

    # 準備データをJSONに保存（Step 4でも使用）
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    prep_file = LOGS_DIR / "batch_prep.json"
    # pdf_textは大きいので保存しない（Step2用にはバッチリクエスト作成時に使う）
    save_items = [{k: v for k, v in item.items() if k != "pdf_text"} for item in items]
    prep_file.write_text(json.dumps(save_items, ensure_ascii=False, indent=2))
    logger.info(f"準備データ保存: {prep_file}")

    # 添付資料の情報を別ファイルに保存（既存の prep 形式を壊さない）。
    # Step4 がこのファイルを読み、メインと同じフォルダへコピーする。
    attachments_file = LOGS_DIR / _BATCH_ATTACHMENTS_FILE
    attachments_file.write_text(
        json.dumps(attachment_records, ensure_ascii=False, indent=2)
    )
    logger.info(f"添付資料データ保存: {attachments_file}")

    return items


def step2_submit_batch(items: list[dict]) -> str:
    """Step 2: Batch API送信。

    Args:
        items: step1_prepareの戻り値

    Returns:
        バッチID
    """
    logger = setup_logging()
    logger.info("=== Step 2: Batch API送信 ===")

    batch_id = comment_generator.submit_batch(items)

    batch_id_file = LOGS_DIR / "batch_id.txt"
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    batch_id_file.write_text(batch_id)
    logger.info(f"バッチID保存: {batch_id_file} → {batch_id}")

    return batch_id


def step3_wait_and_get_results(
    batch_id: str,
    poll_interval: int = 60,
    max_wait: int = 86400,
) -> dict[str, dict[str, str]]:
    """Step 3: バッチ結果をポーリングで取得する（構造化出力をパース済み）。

    Args:
        batch_id: バッチID
        poll_interval: ポーリング間隔（秒）
        max_wait: 最大待機時間（秒）

    Returns:
        ``{custom_id: {clinic_name, person_name, sample_title, comment}}``
    """
    logger = setup_logging()
    logger.info(f"=== Step 3: バッチ結果取得 (ID: {batch_id}) ===")

    elapsed = 0
    while elapsed < max_wait:
        status = comment_generator.get_batch_status(batch_id)
        logger.info(f"バッチステータス: {status['status']} ({status['request_counts']})")

        if status["status"] == "ended":
            break

        time.sleep(poll_interval)
        elapsed += poll_interval

    if elapsed >= max_wait:
        logger.error(f"バッチ結果待機タイムアウト ({max_wait}秒)")
        raise TimeoutError("Batch API結果の取得がタイムアウトしました")

    results, failed_ids = comment_generator.get_batch_results(batch_id)
    logger.info(
        f"Step 3完了: {len(results)}件成功"
        + (f", {len(failed_ids)}件失敗 ({', '.join(failed_ids)})" if failed_ids else "")
    )
    return results


def step4_generate_pdfs(
    profile: ProfileConfig | RunConfig,
    results: dict[str, dict[str, str]],
    items: list[dict] | None = None,
) -> None:
    """Step 4: PDF生成 → Drive保存 → 出力一覧シート追記。

    添付資料パススルー対応:
        メイン PDF の結果処理ループで管理番号 → ``(医院名, 個人名)`` の対応表
        を構築する。ループ後、``batch_attachments.json`` を読み、各添付資料を
        対応表で引いて、同じ管理番号のメインと同じ ``<医院名>/<個人名>/``
        フォルダへ元ファイル名のままコピーし、出力一覧シートに記録する
        （lessons.md P-016）。対応表に無い添付資料は警告つきでスキップする。
        ``batch_attachments.json`` が存在しない（添付資料ゼロ）場合は何も
        しない。

    Args:
        profile: 実行時設定（``ProfileConfig`` または ``RunConfig``）
        results: ``{custom_id: {clinic_name, person_name, sample_title, comment}}``
        items: 準備データ（Noneの場合はファイルから読み込み）
    """
    logger = setup_logging()
    logger.info("=== Step 4: PDF生成 & Drive保存 ===")

    ensure_fonts()

    if items is None:
        prep_file = LOGS_DIR / "batch_prep.json"
        items = json.loads(prep_file.read_text())

    stats = {"success": 0, "error": 0, "missing": 0}

    # 添付資料パススルー用の対応表。メイン PDF の処理ループで構築し、
    # ループ後に添付資料をこの表で引いて同じ出力フォルダへコピーする。
    # (医院番号, 医院名, 個人名) — 医院番号と医院名（AI 抽出の生の値）を別々
    # に保持し、添付資料アップロード時にメインと同じ
    # ``find_or_create_clinic_folder`` を通じて同じ医院フォルダ（医院番号で
    # 識別）に合流させる（P-019）。
    case_map: dict[str, tuple[str, str, str]] = {}

    # 医院フォルダURLシート（``<出力シート名>_医院``）の記録済み医院番号を
    # 実行開始時に 1 回スナップショットする。ループ中に記録した医院番号は
    # ``clinics_recorded_this_run`` で追跡し、両方に無い医院だけ追記する。
    clinic_sheet_name = f"{profile.output_sheet_name}_医院"
    recorded_clinics = sheets_client.get_recorded_clinic_numbers(
        sheet_name=clinic_sheet_name,
    )
    clinics_recorded_this_run: set[str] = set()

    def _record_clinic_folder(
        clinic_number: str, clinic_name: str, clinic_folder_id: str
    ) -> None:
        """医院フォルダURLシートに医院を 1 行記録する（重複防止込み）。

        医院番号が実行開始時のスナップショットにも同一実行内で記録済みの
        集合にも無いときだけ追記する。医院番号が空の場合は記録しない。
        """
        if not clinic_number:
            return
        if clinic_number in recorded_clinics:
            return
        if clinic_number in clinics_recorded_this_run:
            return
        clinic_folder_url = (
            f"https://drive.google.com/drive/folders/{clinic_folder_id}"
        )
        sheets_client.append_clinic_folder_record(
            clinic_number=clinic_number,
            clinic_name=clinic_name,
            clinic_folder_url=clinic_folder_url,
            sheet_name=clinic_sheet_name,
        )
        clinics_recorded_this_run.add(clinic_number)

    for item in items:
        custom_id = item["custom_id"]
        if custom_id not in results:
            logger.warning(f"コメント未取得: {custom_id}")
            stats["missing"] += 1
            continue

        meta = results[custom_id]
        pdf_file_name = item.get("pdf_file_name", "")
        clinic_name = meta["clinic_name"] or "unknown_clinic"
        person_name = meta["person_name"] or "unknown_person"
        sample_title = meta["sample_title"] or Path(pdf_file_name).stem or "untitled"
        comment = meta["comment"]

        # 医院番号（管理番号の先頭セグメント）を抽出する。医院フォルダの識別
        # は医院番号のみで行うため（P-019）、医院番号と医院名を別々に
        # ``upload_pdf_to_clinic_person`` に渡す。医院番号が空の場合は
        # ``find_or_create_clinic_folder`` 側で旧来の名前ベース照合に
        # フォールバックする。
        clinic_number = extract_clinic_number(pdf_file_name)

        output_filename = pdf_merger.make_output_filename(
            clinic_name, person_name, sample_title
        )

        try:
            pdf_data = drive_client.download_pdf(item["pdf_data_id"])

            with tempfile.TemporaryDirectory() as tmpdir:
                comment_page_path = Path(tmpdir) / "comment_page.pdf"
                pdf_creator.create_comment_page(
                    comment=comment,
                    clinic_name=clinic_name,
                    person_name=person_name,
                    output_path=comment_page_path,
                )

                output_path = Path(tmpdir) / output_filename
                pdf_merger.merge_pdfs(
                    original_pdf_data=pdf_data,
                    comment_page_path=comment_page_path,
                    output_path=output_path,
                )

                upload_result = drive_client.upload_pdf_to_clinic_person(
                    file_path=output_path,
                    output_root_folder_id=profile.output_folder_id,
                    clinic_number=clinic_number,
                    clinic_name=clinic_name,
                    person_name=person_name,
                    file_name=output_filename,
                )

            mgmt_num = extract_management_number(pdf_file_name)
            if not mgmt_num:
                logger.warning(
                    f"管理番号をファイル名から抽出できません"
                    f"（先頭が NNN-NN-N 形式でない）: {pdf_file_name}"
                )
            sheets_client.append_output_record(
                management_number=mgmt_num,
                clinic_name=clinic_name,
                person_name=person_name,
                sample_name=sample_title,
                drive_url=upload_result["webViewLink"],
                sheet_name=profile.output_sheet_name,
            )
            # 医院フォルダURLシートに医院を記録（同一医院は 1 行のみ）。
            _record_clinic_folder(
                clinic_number, clinic_name, upload_result["clinic_folder_id"]
            )
            # 添付資料パススルー用の対応表を構築。同じ管理番号の添付資料を
            # このメインと同じ出力フォルダへコピーするために使う。
            if mgmt_num:
                case_map[mgmt_num] = (
                    clinic_number, clinic_name, person_name
                )
            logger.info(
                f"完了: {mgmt_num} / {clinic_name} / {person_name} / {sample_title}"
            )
            stats["success"] += 1

        except Exception as e:
            logger.error(
                f"処理エラー: {custom_id} ({pdf_file_name}) - {e}",
                exc_info=True,
            )
            stats["error"] += 1

    # ── 添付資料パススルー経路 ──
    # batch_attachments.json を読み、各添付資料を case_map で引いて、同じ管理
    # 番号のメインと同じ出力フォルダへ元ファイル名のままコピーする。AI 処理
    # （Claude API / コメントページ生成 / 結合）は一切しない。ファイルが
    # 存在しない（添付資料ゼロ）場合は何もしない。
    _process_attachments(profile, case_map, stats)

    total = stats["success"] + stats["error"] + stats["missing"]
    logger.info(
        f"Step 4完了: 成功 {stats['success']}/{total}件, "
        f"エラー {stats['error']}/{total}件, "
        f"未取得 {stats['missing']}/{total}件"
    )


def _process_attachments(
    profile: ProfileConfig | RunConfig,
    case_map: dict[str, tuple[str, str, str]],
    stats: dict[str, int],
) -> None:
    """``batch_attachments.json`` を読み、添付資料をメインと同じ出力先へコピーする。

    AI 処理（Claude API / コメントページ生成 / 結合）は一切しない。元 PDF の
    バイト列をそのまま再アップロードし、出力一覧シートに「【添付資料】<元名>」
    で記録する。``case_map`` に対応するメインが無い添付資料は警告スキップ。
    添付資料はメインと同じ管理番号（= 同じ医院番号）なので、
    ``find_or_create_clinic_folder`` 経由でメインと同じ医院フォルダへ
    コピーされる（P-019）。

    Args:
        profile: 実行時設定（出力フォルダ ID / シート名を参照）
        case_map: メイン処理で構築した管理番号 →
            ``(医院番号, 医院名, 個人名)`` の対応表
        stats: Step4 の統計 dict（``success`` / ``error`` をインプレース更新）
    """
    logger = setup_logging()
    attachments_file = LOGS_DIR / _BATCH_ATTACHMENTS_FILE
    if not attachments_file.exists():
        logger.info("添付資料データなし（batch_attachments.json 不在）")
        return

    attachment_records: list[dict] = json.loads(attachments_file.read_text())
    if not attachment_records:
        logger.info("添付資料 0 件")
        return

    logger.info(f"--- 添付資料パススルー: {len(attachment_records)}件 ---")
    for record in attachment_records:
        file_id = record["file_id"]
        file_name = record["file_name"]
        mgmt_num = record["management_number"]

        case = case_map.get(mgmt_num)
        if case is None:
            logger.warning(
                f"対応するメイン実践事例 PDF がこの実行で処理されていないため"
                f"スキップ: {file_name}"
            )
            stats["error"] += 1
            continue

        # case_map は (医院番号, 医院名, 個人名)。添付資料は
        # ``find_or_create_clinic_folder`` 経由でメインと同じ医院フォルダへ
        # コピーされ、出力一覧シートには AI 抽出の医院名（医院番号なし）を
        # 記録する。
        clinic_number, clinic_name, person_name = case
        try:
            # 元 PDF のバイト列をそのまま再アップロード（マージ・コメント
            # ページ生成はしない）。出力ファイル名は元のまま。
            pdf_data = drive_client.download_pdf(file_id)
            with tempfile.TemporaryDirectory() as tmpdir:
                attachment_path = Path(tmpdir) / file_name
                attachment_path.write_bytes(pdf_data)
                upload_result = drive_client.upload_pdf_to_clinic_person(
                    file_path=attachment_path,
                    output_root_folder_id=profile.output_folder_id,
                    clinic_number=clinic_number,
                    clinic_name=clinic_name,
                    person_name=person_name,
                    file_name=file_name,
                )

            sheets_client.append_output_record(
                management_number=mgmt_num,
                clinic_name=clinic_name,
                person_name=person_name,
                sample_name=f"【添付資料】{file_name}",
                drive_url=upload_result["webViewLink"],
                sheet_name=profile.output_sheet_name,
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


def run(
    batch_mode: bool = True,
    test_count: int = 0,
    batch_id: str | None = None,
    step: str = "all",
    profile_name: str | None = None,
    target_folder: str | None = None,
) -> None:
    """Batchモードのメイン処理。

    Args:
        batch_mode: Batch API使用（Falseなら通常モードにフォールバック）
        test_count: 新規 PDF を N 件まで処理（0=全件）。重複・管理番号なしを
            除外した「処理対象の新規 PDF」に対して適用される。
        batch_id: 既存のバッチID（Step 3から再開する場合）
        step: 実行するステップ ("all", "prepare", "submit", "results", "pdfs")
        profile_name: プロファイル名（``profiles/<name>.yaml``）。
            省略時かつ ``target_folder`` も無指定なら ``jissen_default``。
        target_folder: フォルダ自動検出モードのフォルダ名。
            ``__list__`` 指定時は候補列挙のみ行い即 return。
    """
    logger = setup_logging()
    logger.info("=== じっせん君コメントシステム（Batchモード）開始 ===")

    if target_folder == "__list__":
        discover.handle_list_mode(logger)
        return

    if not batch_mode:
        from src.main import run as run_normal
        run_normal(
            test_count=test_count,
            profile_name=profile_name,
            target_folder=target_folder,
        )
        return

    cfg = discover.resolve_run_config(profile_name, target_folder)
    logger.info(cfg.display_name)

    items: list[dict] | None = None
    results: dict[str, dict[str, str]] | None = None

    if step in ("all", "prepare"):
        items = step1_prepare(cfg, test_count=test_count)

    if step in ("all", "submit"):
        if items is None:
            raise RuntimeError("submit単独実行にはprepareが必要です")
        if not items:
            logger.info("処理対象が0件のため、バッチ送信をスキップします")
        else:
            batch_id = step2_submit_batch(items)

    if step in ("all", "results"):
        if batch_id is None:
            batch_id_file = LOGS_DIR / "batch_id.txt"
            batch_id = batch_id_file.read_text().strip()
        results = step3_wait_and_get_results(batch_id)

    if step in ("all", "pdfs"):
        if results is None:
            raise RuntimeError("results が未取得です。step=results を先に実行してください")
        step4_generate_pdfs(cfg, results, items=items)

    logger.info("=== Batchモード処理完了 ===")


def main() -> None:
    parser = argparse.ArgumentParser(description="じっせん君コメントシステム（Batchモード）")
    parser.add_argument(
        "--no-batch", action="store_true",
        help="通常モードで実行",
    )
    parser.add_argument(
        "--test-count", type=int, default=0,
        help="テスト件数（0=全件処理）。重複・管理番号なしを除外した新規 PDF に適用",
    )
    parser.add_argument(
        "--batch-id", type=str, default=None,
        help="既存バッチID（結果取得から再開）",
    )
    parser.add_argument(
        "--step", type=str, default="all",
        choices=["all", "prepare", "submit", "results", "pdfs"],
        help="実行するステップ",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--profile", type=str, default=None,
        help=(
            "プロファイル名（profiles/<name>.yaml）。"
            "省略時かつ --target-folder も無指定なら jissen_default（既存挙動）"
        ),
    )
    group.add_argument(
        "--target-folder", type=str, default=None,
        help=(
            "DRIVE_INPUT_ROOT 配下のサブフォルダ名（フォルダ自動検出モード）。"
            "'__list__' で候補を列挙して即終了"
        ),
    )
    args = parser.parse_args()

    run(
        batch_mode=not args.no_batch,
        test_count=args.test_count,
        batch_id=args.batch_id,
        step=args.step,
        profile_name=args.profile,
        target_folder=args.target_folder,
    )


if __name__ == "__main__":
    main()
