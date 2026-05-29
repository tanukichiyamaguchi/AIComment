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
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

from src.utils import (
    setup_logging,
    ensure_fonts,
    extract_clinic_number,
    extract_management_number,
    is_attachment_filename,
)
from src.config import LOGS_DIR
from src import discover, drive_client, gmail_client, sheets_client
from src import pdf_reader, comment_generator, pdf_creator, pdf_merger
from src.discover import RunConfig
from src.profile import ProfileConfig
from src.sheets_client import MasterRecord


# 添付資料 PDF の情報（file id / file name / 管理番号）を Step1→Step4 で
# 受け渡すための JSON ファイル。``batch_prep.json``（メイン PDF の準備データ）
# とは別ファイルにして、既存の prep 形式を壊さずに passthrough 情報を運ぶ。
_BATCH_ATTACHMENTS_FILE = "batch_attachments.json"
_BATCH_RESULTS_FILE = "batch_results.json"


def _atomic_write_json(target: Path, payload: Any) -> None:
    """JSON を atomic に書き出す（write → fsync → rename）。

    途中クラッシュ時に半端な内容のファイルが残るのを防ぐ（P-023）。
    1000 PDF 規模の連続実行で batch_prep.json が読み取り不可能な状態で
    残ると Step2/Step4 が立ち上がれなくなる事故への対策。
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    tmp.replace(target)


def _load_items_from_disk() -> list[dict]:
    """``batch_prep.json`` を読み込んで items を復元する（CB-1 / CB-2）。

    別 GHA 実行から ``--step submit`` / ``--step pdfs`` で再開する際、
    in-memory の items が無いケースでこの関数で永続化済みの items を
    ロードする。``pdf_text`` が含まれている前提（CB-1）。
    """
    prep_file = LOGS_DIR / "batch_prep.json"
    if not prep_file.exists():
        raise FileNotFoundError(
            f"{prep_file} がありません。--step prepare を先に実行してください"
        )
    return json.loads(prep_file.read_text())


def _save_results_to_disk(results: dict[str, dict[str, str]]) -> None:
    """``step3`` の結果を JSON に永続化する（CB-2）。"""
    _atomic_write_json(LOGS_DIR / _BATCH_RESULTS_FILE, results)


def _load_results_from_disk() -> dict[str, dict[str, str]]:
    """``batch_results.json`` から step3 の結果をロードする（CB-2）。"""
    results_file = LOGS_DIR / _BATCH_RESULTS_FILE
    if not results_file.exists():
        raise FileNotFoundError(
            f"{results_file} がありません。--step results を先に実行してください"
        )
    return json.loads(results_file.read_text())


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
    skipped_records: list[dict] = []  # M-2: manifest 出力用
    for pdf_file in main_files:
        file_name = pdf_file["name"]
        mgmt_num = extract_management_number(file_name)
        if not mgmt_num:
            logger.warning(
                f"管理番号をファイル名から抽出できないためスキップ"
                f"（先頭が NNN-NN-N 形式でない / 重複検知不可）: {file_name}"
            )
            skip_no_number += 1
            skipped_records.append({
                "file_id": pdf_file["id"], "file_name": file_name,
                "reason": "no_management_number",
            })
            continue
        if mgmt_num in processed:
            logger.info(f"処理済みのためスキップ: {mgmt_num} ({file_name})")
            skip_processed += 1
            skipped_records.append({
                "file_id": pdf_file["id"], "file_name": file_name,
                "reason": "already_processed",
                "management_number": mgmt_num,
            })
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
    skip_extract_fail = 0  # M-2: テキスト抽出失敗を可視化
    skip_download_error = 0
    for i, pdf_file in enumerate(targets, start=1):
        file_id = pdf_file["id"]
        file_name = pdf_file["name"]
        logger.info(f"[{i}/{len(targets)}] {file_name}")

        try:
            pdf_data = drive_client.download_pdf(file_id)
            pdf_text = pdf_reader.extract_text(pdf_data)
            if not pdf_text:
                logger.warning(f"テキスト抽出失敗（空テキスト）: {file_name}")
                skip_extract_fail += 1
                skipped_records.append({
                    "file_id": file_id, "file_name": file_name,
                    "reason": "empty_text_extraction",
                })
                continue
        except Exception as e:
            logger.warning(f"PDF処理失敗: {file_name} - {e}")
            skip_download_error += 1
            skipped_records.append({
                "file_id": file_id, "file_name": file_name,
                "reason": "download_or_parse_error",
                "error": str(e),
            })
            continue

        items.append({
            "custom_id": f"item_{i:04d}",
            "pdf_data_id": file_id,  # 後でダウンロードし直す用
            "pdf_file_name": file_name,
            "pdf_text": pdf_text,
        })

    logger.info(
        f"Step 1完了: メイン {len(items)}件が処理可能 / "
        f"添付資料 {len(attachment_records)}件をパススルー予約 / "
        f"スキップ内訳 (管理番号なし {skip_no_number}, 処理済み {skip_processed}, "
        f"抽出失敗 {skip_extract_fail}, 取得エラー {skip_download_error})"
    )

    # 準備データをJSONに保存（Step 4でも使用）
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    prep_file = LOGS_DIR / "batch_prep.json"
    # CB-1 対策: pdf_text を **保存する**。これがないと step=submit を別 GHA
    # 実行で行うとき Batch API に空 prompt を投げてしまう（過去バグ）。
    # 1000 PDF × 30KB ≒ 30MB。許容範囲。
    _atomic_write_json(prep_file, items)
    logger.info(f"準備データ保存: {prep_file} ({len(items)} items, pdf_text 含む)")

    # 添付資料の情報を別ファイルに保存（既存の prep 形式を壊さない）。
    # Step4 がこのファイルを読み、メインと同じフォルダへコピーする。
    attachments_file = LOGS_DIR / _BATCH_ATTACHMENTS_FILE
    _atomic_write_json(attachments_file, attachment_records)
    logger.info(f"添付資料データ保存: {attachments_file}")

    # M-2: スキップされた PDF の manifest を別ファイルに出力。1000 PDF 規模で
    # 何件がどの理由で消えたかを後追いできるようにする。
    skips_file = LOGS_DIR / "batch_step1_skips.json"
    _atomic_write_json(skips_file, skipped_records)
    logger.info(f"スキップ記録: {skips_file} ({len(skipped_records)} 件)")

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
    # CB-2: step4 を別 GHA 実行から呼ぶケースに備えて結果を永続化する。
    _save_results_to_disk(results)
    logger.info(f"バッチ結果保存: {LOGS_DIR / _BATCH_RESULTS_FILE}")
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
        items = _load_items_from_disk()

    # CB-3: Step4 開始時に **再度** 処理済み管理番号をスナップショットする。
    # Step1 のスナップショット (`processed`) は items の決定に使ったが、
    # Step4 が途中クラッシュして再実行されたとき、前回 Step4 で書き込んだ行が
    # 加わって `processed` セットが拡大している可能性がある。Step1 を再実行
    # しない経路（--step pdfs 単独）でも、ここで再取得することで Drive/Sheets
    # の重複書き込みを防ぐ（P-023）。
    step4_processed = sheets_client.get_processed_management_numbers(
        sheet_name=profile.output_sheet_name,
    )
    if step4_processed:
        logger.info(
            f"Step4 開始時の処理済みスナップショット: "
            f"{len(step4_processed)} 件（重複処理を防ぐため）"
        )

    stats = {"success": 0, "error": 0, "missing": 0, "skip_already_processed": 0}

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

    # 参加者マスターシートを Step4 開始時に 1 回だけ読み込む。医院名の標準化
    # （フォルダ命名・各種シート列）と Gmail 下書きの TO ルックアップを兼ねる
    # スナップショット。シート未作成なら自動作成（ヘッダーのみ）+ 空リストを
    # 返す。メイン経路と添付資料経路（``_process_attachments``）で共有する
    # （毎回読まない）。
    master_records: list[MasterRecord] = sheets_client.read_master_records(
        sheet_name=profile.master_sheet_name,
    )

    # Gmail 下書きは Step4 末尾でまとめて作成する（同じメールアドレスを持つ
    # 複数 PDF を 1 通に集約するため）。ループ中は draft_items に蓄積するだけ。
    # PDF ファイルは集約まで生存させる必要があるので session_outputs_dir に書く。
    session_outputs_dir = Path(tempfile.mkdtemp(prefix="aicomment_batch_outputs_"))
    draft_items: list[dict[str, Any]] = []

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
        clinic_name_from_ai = meta["clinic_name"] or "unknown_clinic"
        person_name = meta["person_name"] or "unknown_person"
        sample_title = meta["sample_title"] or Path(pdf_file_name).stem or "untitled"
        comment = meta["comment"]

        # 医院番号（管理番号の先頭セグメント）を抽出する。医院フォルダの識別
        # は医院番号のみで行うため（P-019）、医院番号と医院名を別々に
        # ``upload_pdf_to_clinic_person`` に渡す。医院番号が空の場合は
        # ``find_or_create_clinic_folder`` 側で旧来の名前ベース照合に
        # フォールバックする。
        clinic_number = extract_clinic_number(pdf_file_name)
        mgmt_num = extract_management_number(pdf_file_name)
        if not mgmt_num:
            logger.warning(
                f"管理番号をファイル名から抽出できません"
                f"（先頭が NNN-NN-N 形式でない）: {pdf_file_name}"
            )

        # CB-3: Step4 開始時のスナップショットに mgmt_num が含まれていれば
        # 前回 Step4 で処理済み（Drive アップロード + Sheets 追記 + 下書き想定）。
        # 再処理すると Drive / Sheets / Gmail で重複が出るためスキップ。
        # case_map（添付資料パススルー）の構築のため、メイン PDF の処理を
        # スキップしても管理番号 → (医院番号, 医院名, 個人名) のマッピングは
        # 残す必要があるが、Step4 単独再実行で添付資料の glue が無くなるのは
        # 仕様上許容（添付資料は同じ Step4 内のメイン処理に依存する）。
        if mgmt_num and mgmt_num in step4_processed:
            logger.info(
                f"Step4 スキップ（処理済み再実行検知）: {mgmt_num} "
                f"({pdf_file_name})"
            )
            stats["skip_already_processed"] += 1
            continue

        # 医院名は参加者マスターから引いた標準表記を最優先（表記統一 +
        # 「開業準備中」等の固定文字列対応）。未登録なら AI 抽出値で代用
        # （現状維持の挙動）。代用時は必ず警告ログを出す。以後すべての
        # 医院名用途で ``clinic_name`` 変数を使う。
        clinic_name_from_master = sheets_client.lookup_clinic_name(
            master_records, clinic_number,
        )
        if clinic_name_from_master:
            clinic_name = clinic_name_from_master
            logger.info(
                f"医院名をマスターシートから取得: "
                f"{clinic_number} → {clinic_name}"
            )
        else:
            clinic_name = clinic_name_from_ai
            logger.warning(
                f"参加者マスター未登録、AI 抽出値で代用: "
                f"医院番号={clinic_number}, AI 抽出={clinic_name_from_ai}"
            )

        output_filename = pdf_merger.make_output_filename(
            clinic_name, person_name, sample_title
        )

        try:
            pdf_data = drive_client.download_pdf(item["pdf_data_id"])

            # 中間ファイルは iteration スコープ、最終出力は session_outputs_dir
            # に書いてループ終了後の集約下書き作成まで生存させる。
            pdf_subdir = session_outputs_dir / f"main_{custom_id}"
            pdf_subdir.mkdir(parents=True, exist_ok=True)
            output_path = pdf_subdir / output_filename

            with tempfile.TemporaryDirectory() as tmpdir:
                comment_page_path = Path(tmpdir) / "comment_page.pdf"
                pdf_creator.create_comment_page(
                    comment=comment,
                    clinic_name=clinic_name,
                    person_name=person_name,
                    output_path=comment_page_path,
                )

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
                    # マスター由来の確定医院名なら既存フォルダ名も同期させる
                    clinic_name_authoritative=bool(clinic_name_from_master),
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
                    clinic_number, clinic_name,
                    upload_result["clinic_folder_id"],
                )

            # Gmail 下書きはここで作らず draft_items に蓄積する。ループ終了後
            # にメールアドレスでグルーピングして 1 通にまとめる。
            _collect_draft_item_batch(
                draft_items=draft_items,
                master_records=master_records,
                clinic_number=clinic_number,
                person_name=person_name,
                pdf_path=output_path,
            )

            # 添付資料パススルー用の対応表を構築。医院名は既にマスター標準化
            # 済みの値が入っているため、添付資料経路はこの表をそのまま再利用
            # すれば同じフォルダ・同じ列値になる。
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
    # 存在しない（添付資料ゼロ）場合は何もしない。添付資料 PDF も同じ
    # session_outputs_dir / draft_items に蓄積される（同じ個人宛グループに合流）。
    _process_attachments(
        profile, case_map, stats, master_records,
        session_outputs_dir, draft_items,
    )

    # ── 集約下書き作成 ──
    # メイン + 添付資料経路で蓄積した draft_items をメールアドレスでグルーピング
    # し、グループごとに 1 通の Gmail 下書きを作成する。
    try:
        _create_grouped_drafts_for_batch(draft_items)
    finally:
        shutil.rmtree(session_outputs_dir, ignore_errors=True)

    total = stats["success"] + stats["error"] + stats["missing"]
    logger.info(
        f"Step 4完了: 成功 {stats['success']}/{total}件, "
        f"エラー {stats['error']}/{total}件, "
        f"未取得 {stats['missing']}/{total}件"
    )

    # 出力一覧シートの最終行に「完了」マーカーを 1 行追加（運用者がシート上で
    # 一目で完了を把握できるように）。書き込み自体は fail-soft（Step4 本体は
    # 既に終わっているため、マーカー失敗で例外を上げない）。
    try:
        sheets_client.append_completion_marker(
            sheet_name=profile.output_sheet_name,
            summary=(
                f"成功 {stats['success']}件 / "
                f"エラー {stats['error']}件 / "
                f"未取得 {stats['missing']}件"
            ),
        )
    except Exception as e:
        logger.warning(f"完了マーカーの追記に失敗（処理自体は完了済み）: {e}")


def _collect_draft_item_batch(
    draft_items: list[dict[str, Any]],
    master_records: list[MasterRecord],
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
        email = sheets_client.lookup_email_by_clinic_and_person(
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


def _create_grouped_drafts_for_batch(
    draft_items: list[dict[str, Any]],
) -> None:
    """``draft_items`` をメールアドレスでグループ化して下書きを作成する。

    通常モード (``main._create_grouped_drafts_for_run``) と同じロジック:
    メールアドレスごとに 1 通の下書きにまとめ、空メールは PDF ごとに個別の
    宛先空下書きを作る。例外は警告ログを出して握りつぶす（fail-soft）。
    """
    logger = setup_logging()
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
            gmail_client.create_draft(
                to_email=email,
                person_name=person_name,
                pdf_paths=pdf_paths,
                cc_email=None,
            )
        except Exception as e:
            logger.error(
                f"Gmail下書き作成失敗（処理は続行）: {e}", exc_info=True
            )

    for item in empty_email_items:
        try:
            gmail_client.create_draft(
                to_email="",
                person_name=item["person_name"],
                pdf_paths=[item["pdf_path"]],
                cc_email=None,
            )
        except Exception as e:
            logger.error(
                f"Gmail下書き作成失敗（処理は続行）: {e}", exc_info=True
            )


def _process_attachments(
    profile: ProfileConfig | RunConfig,
    case_map: dict[str, tuple[str, str, str]],
    stats: dict[str, int],
    master_records: list[MasterRecord],
    session_outputs_dir: Path,
    draft_items: list[dict[str, Any]],
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
        master_records: Step4 開始時に読み込んだ参加者マスタースナップショット。
            メイン経路と共有する（毎回読まない）。
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

        # case_map は (医院番号, 医院名, 個人名)。医院名はメイン処理ループで
        # 既に参加者マスター標準化済み（未登録時は AI 抽出値）。添付資料は
        # ``find_or_create_clinic_folder`` 経由でメインと同じ医院フォルダへ
        # コピーされる。
        clinic_number, clinic_name, person_name = case
        # 添付資料も同じ医院フォルダへ入る。医院名がマスター由来（確定名）なら
        # 既存フォルダ名を確定名へ同期する。メイン PDF 処理で同期済みでも、
        # Step4 単独再実行等の順序差異に備えてここでも再判定する。
        clinic_name_authoritative = bool(
            sheets_client.lookup_clinic_name(master_records, clinic_number)
        )
        try:
            # 元 PDF のバイト列をそのまま再アップロード（マージ・コメント
            # ページ生成はしない）。出力ファイル名は元のまま。session_outputs_dir
            # に書いてループ終了後の集約下書き作成まで生存させる。
            pdf_data = drive_client.download_pdf(file_id)
            att_subdir = session_outputs_dir / f"attach_{file_id}"
            att_subdir.mkdir(parents=True, exist_ok=True)
            attachment_path = att_subdir / file_name
            attachment_path.write_bytes(pdf_data)
            upload_result = drive_client.upload_pdf_to_clinic_person(
                file_path=attachment_path,
                output_root_folder_id=profile.output_folder_id,
                clinic_number=clinic_number,
                clinic_name=clinic_name,
                person_name=person_name,
                file_name=file_name,
                clinic_name_authoritative=clinic_name_authoritative,
            )

            sheets_client.append_output_record(
                management_number=mgmt_num,
                clinic_name=clinic_name,
                person_name=person_name,
                sample_name=f"【添付資料】{file_name}",
                drive_url=upload_result["webViewLink"],
                sheet_name=profile.output_sheet_name,
            )

            # 添付資料経路も Gmail 下書き用に蓄積する。同じ個人宛のメイン PDF
            # と同じグループに集約され、1 通の下書きに複数添付される。
            _collect_draft_item_batch(
                draft_items=draft_items,
                master_records=master_records,
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
        # CB-2: step=submit を別 GHA 実行から呼ぶケース。in-memory items が
        # 無ければ batch_prep.json から復元する（CB-1 で pdf_text 込みで保存）。
        if items is None:
            items = _load_items_from_disk()
            logger.info(f"items を batch_prep.json からロード ({len(items)}件)")
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
        # CB-2: step=pdfs を別 GHA 実行から呼ぶケース。in-memory results /
        # items が無ければ disk から復元する（step3 が batch_results.json を
        # 永続化、step1 が batch_prep.json を永続化）。
        if results is None:
            results = _load_results_from_disk()
            logger.info(f"results を batch_results.json からロード ({len(results)}件)")
        if items is None:
            items = _load_items_from_disk()
            logger.info(f"items を batch_prep.json からロード ({len(items)}件)")
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
