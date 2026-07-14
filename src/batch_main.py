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
import hashlib
import json
import re
import shutil
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import anthropic
from google.auth.exceptions import RefreshError

from src.utils import (
    setup_logging,
    ensure_fonts,
    extract_clinic_number,
    extract_management_number,
    is_team_filename,
    mask_name,
)
from src import config
from src.config import LOGS_DIR
from src import discover, drive_client, gmail_client, run_common, sheets_client
from src import pdf_reader, comment_generator, pdf_creator, pdf_merger
from src.comment_generator import PermanentRunFailureError
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


# Anthropic Batch の custom_id 制約（英数・``_``・``-`` のみ / 最大 64 文字）。
_CUSTOM_ID_SAFE = re.compile(r"^[A-Za-z0-9_-]+$")


def _custom_id_for_file(file_id: str) -> str:
    """Drive file id から決定的な custom_id を生成する（CB-4 / 回収の要）。

    位置依存の連番（``item_0001``…）だと、回収時の Drive 再走査で並び順・
    重複除外がずれて Anthropic 結果（``batch_results.json`` の key）と突合できない。
    file id 由来の安定 ID にすることで、同一 Drive ファイルは投入時と再走査時で
    必ず同じ custom_id になり、結果と確実に紐づく。

    Anthropic の custom_id 制約（``^[A-Za-z0-9_-]{1,64}$``）に収まらない場合
    （file id が想定外に長い / 異文字を含む）は、SHA-256 ハッシュにフォール
    バックする（同じ file id なら必ず同じ値＝決定的）。
    """
    candidate = f"file-{file_id}"
    if len(candidate) <= 64 and _CUSTOM_ID_SAFE.match(candidate):
        return candidate
    digest = hashlib.sha256(file_id.encode("utf-8")).hexdigest()[:48]
    return f"file-{digest}"


def reconstruct_items_from_drive(
    profile: ProfileConfig | RunConfig,
) -> list[dict]:
    """Drive を再走査して step4 用の items を再構築する（CB-4 / 回収）。

    ``batch_prep.json`` が無い回収シナリオ（別 GHA ジョブでバッチ投入し、
    結果回収を新しいジョブで行う＝logs/ アーティファクトが復元されない）向け。

    step4 が必要とするのは ``custom_id`` / ``pdf_data_id`` / ``pdf_file_name``
    のみ（``pdf_text`` は step2 のプロンプト用で step4 では不要）。よって本文の
    ダウンロード・テキスト抽出は一切行わず、Drive のファイル一覧＋ファイル名分類
    だけで軽量に items を再構築する。``custom_id`` は ``_custom_id_for_file`` で
    安定生成するため、投入時と同じ ID を再現でき ``batch_results.json`` と突合できる。

    重複判定（処理済みスキップ）はここでは行わない。メイン PDF の重複は step4 の
    CB-3 が管理番号で弾き、添付資料はメイン処理で構築する ``case_map`` で
    glue されるため、回収時は「Drive にある全ファイル」を素直に再構築すればよい
    （冪等性は step4 側が担保）。添付資料情報は step4 の ``_process_attachments``
    が disk から読むため、``batch_attachments.json`` もここで再構築する。
    """
    logger = setup_logging()
    logger.info("=== Drive 再走査による items 再構築（回収）===")

    pdf_files = drive_client.list_pdfs(folder_id=profile.input_folder_id)
    main_files, attachment_files = run_common.split_main_and_attachments(pdf_files)

    items = [
        {
            "custom_id": _custom_id_for_file(f["id"]),
            "pdf_data_id": f["id"],
            "pdf_file_name": f["name"],
        }
        for f in main_files
    ]

    # 添付資料は管理番号を持つものだけ（``_process_attachments`` が管理番号で
    # case_map を引くため）。重複判定はかけない（step4 が冪等に処理する）。
    attachment_records = [
        {
            "file_id": f["id"],
            "file_name": f["name"],
            "management_number": extract_management_number(f["name"]),
        }
        for f in attachment_files
        if extract_management_number(f["name"])
    ]
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    _atomic_write_json(LOGS_DIR / _BATCH_ATTACHMENTS_FILE, attachment_records)

    logger.info(
        f"Drive 再走査で再構築: メイン {len(items)}件 / "
        f"添付資料 {len(attachment_records)}件（custom_id は file id 由来で安定・"
        f"本文 DL なし）"
    )
    return items


def _resolve_items_for_step4(
    profile: ProfileConfig | RunConfig,
) -> list[dict]:
    """step4 用の items を解決する（disk 優先 → Drive 再走査フォールバック）。

    ``batch_prep.json`` が非空ならそれを使う（step1 が作った厳密な items。旧
    positional custom_id のバッチもこれで復元可能）。無い / 空のときは Drive を
    再走査して再構築する（``reconstruct_items_from_drive``、CB-4 回収シナリオ）。
    """
    logger = setup_logging()
    prep_file = LOGS_DIR / "batch_prep.json"
    if prep_file.exists():
        items = json.loads(prep_file.read_text())
        if items:
            logger.info(f"items を batch_prep.json からロード ({len(items)}件)")
            return items
        logger.warning(
            "batch_prep.json が空のため Drive 再走査で items を再構築します（回収）"
        )
    else:
        logger.info(
            "batch_prep.json が無いため Drive 再走査で items を再構築します（回収）"
        )
    return reconstruct_items_from_drive(profile)


def _prepare_one_target(pdf_file: dict) -> dict:
    """メイン PDF 1 件のダウンロード + テキスト抽出を行い、結果タグ付きで返す。

    並列実行（``ThreadPoolExecutor``）のワーカー。共有状態（カウンタ・
    ``items`` / ``skipped_records``）には触れず、判定に必要な情報をすべて
    戻り値に載せる（集計はメインスレッドの消費ループが行う）。

    スレッド安全性: ``drive_client.download_pdf`` は呼び出しごとに
    thread-local のサービスを使い（PR-2b）、``pdf_reader.extract_text`` は
    bytes 入力のステートレス関数。共有可変状態は無い。

    Returns:
        ``{"status": "ok", "file_name", "item"}`` /
        ``{"status": "empty_text", "file_id", "file_name"}`` /
        ``{"status": "error", "file_id", "file_name", "error"}``

    Raises:
        RefreshError: Google 認証の恒久失敗。fail-fast 契約のため握らず
            伝播させる（呼び出し側がランを即停止する）。
    """
    file_id = pdf_file["id"]
    file_name = pdf_file["name"]
    try:
        pdf_data = drive_client.download_pdf(file_id)
        pdf_text = pdf_reader.extract_text(pdf_data)
    except RefreshError:
        raise
    except Exception as e:
        return {
            "status": "error",
            "file_id": file_id,
            "file_name": file_name,
            "error": str(e),
        }
    if not pdf_text:
        return {
            "status": "empty_text",
            "file_id": file_id,
            "file_name": file_name,
        }
    return {
        "status": "ok",
        "file_name": file_name,
        "item": {
            # custom_id は Drive file id 由来の安定 ID。回収時の Drive 再走査
            # でも同じ ID を再現でき batch_results.json と確実に突合できる
            # （CB-4）。
            "custom_id": _custom_id_for_file(file_id),
            "pdf_data_id": file_id,  # 後でダウンロードし直す用
            "pdf_file_name": file_name,
            "pdf_text": pdf_text,
        },
    }


def _consume_prepare_outcome(
    outcome: dict,
    items: list[dict],
    skipped_records: list[dict],
    logger: Any,
) -> None:
    """``_prepare_one_target`` の結果を items / skips に振り分ける（メインスレッド専用）。"""
    if outcome["status"] == "ok":
        items.append(outcome["item"])
    elif outcome["status"] == "empty_text":
        logger.warning(
            f"テキスト抽出失敗（空テキスト）: {outcome['file_name']}"
        )
        skipped_records.append({
            "file_id": outcome["file_id"],
            "file_name": outcome["file_name"],
            "reason": "empty_text_extraction",
        })
    else:  # "error"
        logger.warning(
            f"PDF処理失敗: {outcome['file_name']} - {outcome['error']}"
        )
        skipped_records.append({
            "file_id": outcome["file_id"],
            "file_name": outcome["file_name"],
            "reason": "download_or_parse_error",
            "error": outcome["error"],
        })


def step1_prepare(
    profile: ProfileConfig | RunConfig,
    test_count: int = 0,
    download_workers: int | None = None,
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
        download_workers: ダウンロード + テキスト抽出の並列度。None なら
            ``config.STEP1_DOWNLOAD_WORKERS``（既定 1 = 従来の逐次実行）。
            2 以上で ``ThreadPoolExecutor`` により並列化する（items の順序は
            targets 順で決定的）。8 以下推奨（Drive quota への配慮）。

    Returns:
        Batch APIに投入可能なアイテムのリスト
    """
    logger = setup_logging()
    logger.info("=== Step 1: 準備 ===")

    # target_folder モード（``master_sheet_strict=True``）では、参加者マスター
    # タブの不在 / 0 件を Anthropic Batch API への投入前に検知して即停止する。
    # 投入後に Step4 で気付いた場合、submit 済み分の Anthropic 利用料金は無駄
    # になるため、可能な限り早期に弾く。``ProfileConfig`` は ``master_sheet_strict``
    # を持たないので getattr で fallback（後方互換）。
    if getattr(profile, "master_sheet_strict", False):
        master_records = sheets_client.read_master_records(
            sheet_name=profile.master_sheet_name,
        )
        try:
            run_common.require_non_empty_master(
                master_records, profile.master_sheet_name, True, logger,
            )
        except run_common.MasterSheetEmptyError:
            run_common.append_completion_marker_safe(
                sheets_client, profile.output_sheet_name,
                f"中止（参加者マスタータブ未準備: '{profile.master_sheet_name}'）",
                logger,
            )
            raise

    pdf_files = drive_client.list_pdfs(folder_id=profile.input_folder_id)
    logger.info(f"PDF一覧: {len(pdf_files)}件")

    # 添付資料は Claude API に投げず、Step4 で出力へコピーするだけなので、
    # メインのバッチ投入経路（items）には含めない。
    main_files, attachment_files = run_common.split_main_and_attachments(
        pdf_files, logger,
    )

    # 重複判定（増分処理）— Batch API への投入前に実施する。
    # 管理番号はファイル名から取れるため、download / Claude 投入の前に分類できる。
    # 重複スキップは無条件（bypass なし）。再処理が必要なら出力シートの行を手動削除する。
    processed = sheets_client.get_processed_management_numbers(
        sheet_name=profile.output_sheet_name,
    )

    skipped_records: list[dict] = []  # M-2: manifest 出力用
    targets, skip_no_number, skip_processed = run_common.select_new_targets(
        main_files, processed, logger, skipped_records,
    )

    if test_count > 0:
        targets = targets[:test_count]
        logger.info(f"テストモード: 新規対象を{test_count}件に制限")
    logger.info(
        f"処理対象: {len(targets)}件のPDF（新規） / "
        f"管理番号なしスキップ {skip_no_number}件, "
        f"処理済みスキップ {skip_processed}件"
    )

    # 添付資料の情報を収集し batch_attachments.json に保存（Step4 で使用）。
    # 重複判定は「その添付資料自体が出力一覧シートに記録済みか」で行う
    # （``【添付資料】<元名>`` マーカーの存在チェック）。以前の「メインの管理
    # 番号が処理済みなら添付もスキップ」方式は、(1) メイン処理後に添付だけ
    # 後から Drive に追加されたケース、(2) メイン記録後・添付コピー前の
    # クラッシュ再実行、のどちらでも添付資料が **恒久ロスト** する欠陥があった。
    # 管理番号なしスキップは従来通り（出力先を解決できないため）。
    recorded_attachments = sheets_client.get_recorded_attachment_names(
        sheet_name=profile.output_sheet_name,
    )
    attachment_records: list[dict] = []
    for pdf_file in attachment_files:
        file_name = pdf_file["name"]
        mgmt_num = extract_management_number(file_name)
        if not mgmt_num:
            logger.warning(
                f"管理番号をファイル名から抽出できないため添付資料をスキップ"
                f"（先頭が NNN-NN-N / NNN-NN 形式でない）: {file_name}"
            )
            continue
        if f"【添付資料】{file_name}" in recorded_attachments:
            logger.info(
                f"記録済みのため添付資料をスキップ: {mgmt_num} ({file_name})"
            )
            continue
        attachment_records.append({
            "file_id": pdf_file["id"],
            "file_name": file_name,
            "management_number": mgmt_num,
        })

    items: list[dict] = []
    skip_extract_fail = 0  # M-2: テキスト抽出失敗を可視化
    skip_download_error = 0

    workers = (
        download_workers if download_workers is not None
        else config.STEP1_DOWNLOAD_WORKERS
    )
    if workers > 1 and len(targets) > 1:
        # 並列パス: ダウンロード + 抽出は I/O バウンドで、逐次だと 1000 件で
        # 15〜50 分かかる。``executor.map`` は **投入順に** 結果を yield する
        # ため items の順序は targets 順で決定的（``plan_batch_chunks`` の
        # チャンク境界が再走でも安定する）。カウンタ・skipped_records・items
        # への追記はこの消費ループ（メインスレッド）でのみ行う（ロック不要）。
        logger.info(f"Step 1: {workers} 並列でダウンロード + テキスト抽出")
        executor = ThreadPoolExecutor(max_workers=workers)
        try:
            for i, outcome in enumerate(
                executor.map(_prepare_one_target, targets), start=1,
            ):
                logger.info(f"[{i}/{len(targets)}] {outcome['file_name']}")
                _consume_prepare_outcome(
                    outcome, items, skipped_records, logger,
                )
                if outcome["status"] == "empty_text":
                    skip_extract_fail += 1
                elif outcome["status"] == "error":
                    skip_download_error += 1
        except RefreshError:
            # Google 認証の恒久失敗（トークン失効/取り消し）。以降の全 item も
            # 必ず同じエラーになるため即停止する（fail-fast、P-024 と同方針）。
            # 残りの未実行タスクはキャンセルして速やかに抜ける。
            executor.shutdown(wait=False, cancel_futures=True)
            logger.error(
                "Google 認証エラー（OAuth トークン失効の可能性）を検知。"
                "GOOGLE_OAUTH_TOKEN_JSON を再発行して再実行してください。"
            )
            raise
        finally:
            executor.shutdown(wait=True)
    else:
        # 逐次パス（既定）: 従来と同一の挙動・同一のログ形式。
        for i, pdf_file in enumerate(targets, start=1):
            logger.info(f"[{i}/{len(targets)}] {pdf_file['name']}")
            try:
                outcome = _prepare_one_target(pdf_file)
            except RefreshError:
                logger.error(
                    "Google 認証エラー（OAuth トークン失効の可能性）を検知。"
                    "GOOGLE_OAUTH_TOKEN_JSON を再発行して再実行してください。"
                )
                raise
            _consume_prepare_outcome(outcome, items, skipped_records, logger)
            if outcome["status"] == "empty_text":
                skip_extract_fail += 1
            elif outcome["status"] == "error":
                skip_download_error += 1

    # 新規対象があるのに 1 件も準備できなかった場合は異常（ネットワーク断・
    # 権限剥奪・全 PDF 破損など系統的な障害の兆候）。「0 件投入 → 成功 0 件で
    # 緑終了」という無言の空振りを防ぎ、loud に停止する（P-001）。
    if targets and not items:
        raise RuntimeError(
            f"Step 1: 新規対象 {len(targets)} 件すべての準備に失敗しました"
            f"（取得エラー {skip_download_error} / 抽出失敗 {skip_extract_fail}）。"
            f"ネットワーク・権限・PDF の破損を確認してください"
            f"（詳細: logs/batch_step1_skips.json）。"
        )

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


def _persist_batch_ids(batch_ids: list[str]) -> None:
    """``logs/batch_id.txt`` にバッチ ID を 1 行 1 ID で atomic に書き出す。

    書き込みは ``.tmp`` → ``rename`` の atomic write で、書き込み途中
    クラッシュで空ファイル / 半端な内容を残さない（P-023 と同じ方針）。
    """
    batch_id_file = LOGS_DIR / "batch_id.txt"
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    tmp = batch_id_file.with_suffix(batch_id_file.suffix + ".tmp")
    tmp.write_text("\n".join(batch_ids) + "\n")
    tmp.replace(batch_id_file)


def _load_batch_ids_from_disk() -> list[str]:
    """``logs/batch_id.txt`` からバッチ ID リストを読む（1 行 1 ID）。"""
    batch_id_file = LOGS_DIR / "batch_id.txt"
    if not batch_id_file.exists():
        raise FileNotFoundError(
            f"{batch_id_file} がありません。--batch-id で回収対象のバッチ ID を"
            f"明示指定するか、--step all で最初から実行してください"
            f"（未回収バッチはスプレッドシートの "
            f"'{sheets_client.BATCH_STATE_SHEET_NAME}' タブでも確認できます）。"
        )
    return [
        line.strip()
        for line in batch_id_file.read_text().splitlines()
        if line.strip()
    ]


def step2_submit_batch(
    items: list[dict],
    state_target: str | None = None,
) -> list[str]:
    """Step 2: Batch API送信（サイズ上限に応じて複数バッチへ自動分割）。

    チャンク分割:
        Anthropic Batch API の上限（100k リクエスト / 256MB per バッチ）に
        収まるよう ``comment_generator.plan_batch_chunks`` で分割し、チャンク
        ごとに 1 バッチとして送信する（通常規模では 1 バッチのまま）。

    バッチID永続化（CB-2 / 再開可能性）:
        Anthropic Batch API は **29 日間** 結果を保持する。``step3`` 以降で
        一過性エラー / GHA タイムアウト / プロセス強制終了などで落ちても、
        ``logs/batch_id.txt``（1 行 1 ID）を読み戻して
        ``python -m src.batch_main --step results --batch-id <id[,id2...]>`` で
        結果取得フェーズから再開できる（既にバッチは Anthropic 側で課金確定済み）。
        GHA は ``logs/`` を ``actions/upload-artifact@v4`` で 30 日保管するため、
        Job が失敗してもアーティファクトから batch_id を取り出せる。

    バッチ状態のスプレッドシート記録（二重課金防止）:
        GHA ランナーは ephemeral のため、``batch_id.txt`` はジョブ kill 後の
        再実行では存在しない。``state_target``（出力一覧シート名）を渡すと、
        投入したバッチ ID をスプレッドシートの ``_バッチ管理`` タブにも記録し、
        次回 ``step=all`` 実行が未回収バッチを検知して **再投入せず回収から
        再開** できるようにする。記録失敗はランを止めない（fail-soft、
        ``batch_id.txt`` + アーティファクトの復旧経路は残る）。

    Args:
        items: step1_prepareの戻り値
        state_target: バッチ状態記録用の出力一覧シート名（None なら記録しない）

    Returns:
        バッチIDのリスト（分割なしなら 1 要素）
    """
    logger = setup_logging()
    logger.info("=== Step 2: Batch API送信 ===")

    chunks = comment_generator.plan_batch_chunks(items)
    batch_ids: list[str] = []
    for idx, chunk in enumerate(chunks, start=1):
        if len(chunks) > 1:
            logger.info(f"--- バッチ {idx}/{len(chunks)} ({len(chunk)}件) ---")
        batch_id = comment_generator.submit_batch(chunk)
        batch_ids.append(batch_id)

        # 送信のたびに永続化する（後続チャンクの送信失敗時も、送信済み分の
        # ID は disk / Sheets に残り回収可能にする）。
        _persist_batch_ids(batch_ids)
        if state_target:
            try:
                sheets_client.append_batch_record(
                    state_target, batch_id, sheets_client.BATCH_STATE_SUBMITTED,
                )
            except Exception as e:
                logger.warning(
                    f"バッチ状態のスプレッドシート記録に失敗"
                    f"（batch_id.txt とアーティファクトから復旧可能）: {e}"
                )

    logger.info(
        f"バッチID保存: {LOGS_DIR / 'batch_id.txt'} → {batch_ids} "
        f"（29日間 Anthropic 側で保持。--step results --batch-id "
        f"{','.join(batch_ids)} で再開可能）"
    )
    return batch_ids


# step3 の既定最大待機（秒）。Anthropic Batch の処理窓は最大 24h だが、GHA の
# ジョブ上限は 6h（timeout-minutes: 360）のため、24h 待つ設定はジョブ kill
# （ログ・アーティファクト保全はされるが exit 経路を通らない）で終わるだけ。
# 5h で自発的にタイムアウトさせ、明確なメッセージと共に非ゼロ終了する。
# 投入済みバッチは ``_バッチ管理`` タブに記録済みなので、次の ``step=all``
# 再実行が自動で回収から再開する（再投入・二重課金なし）。
_DEFAULT_POLL_MAX_WAIT_SECONDS = 5 * 60 * 60

# ポーリング残量計算の最小フロア（秒）。deadline を過ぎていても 0 を渡すと
# ポーリングが一度も試行されずに即タイムアウトしてしまうため、最低 60 秒は
# 確保する（step3_wait_and_get_results / _resume_open_batches の per-batch
# 計算と同じフロア）。
_MIN_POLL_SECONDS = 60


def _remaining_seconds(deadline: float) -> int:
    """``deadline``（``time.monotonic()`` 基準）までの残り秒数を返す。

    ``run()`` は ``_resume_open_batches`` と ``_collect_results_for_batches``
    の両方にポーリング予算を渡すが、両者に ``poll_max_seconds`` を独立に
    満額渡すと合計待機が GHA の 6h ジョブ上限を超えてジョブ kill される
    （resume の回収と新規バッチの回収は同じランの中で直列に起きるため、
    本来は 1 つの予算を分け合うべき）。``run()`` が 1 本の deadline を持ち、
    本関数経由で「その時点で残っている予算」を都度計算して渡す。
    """
    return max(int(deadline - time.monotonic()), _MIN_POLL_SECONDS)


def step3_wait_and_get_results(
    batch_id: str,
    poll_interval: int = 60,
    max_wait: int = _DEFAULT_POLL_MAX_WAIT_SECONDS,
) -> dict[str, dict[str, str]]:
    """Step 3: バッチ結果をポーリングで取得する（構造化出力をパース済み）。

    多層防御の例外耐性:
        本番 GHA ラン（run_id=26811653746, 3h22m）で、3 時間ポーリングの最後に
        ``get_batch_status`` 内の ``client.messages.batches.retrieve(batch_id)`` が
        503 ``overloaded_error`` を返し、リトライされず即 Traceback。3 時間の
        待機が水の泡になった。対策は二段構え：
            1. ``comment_generator.get_batch_status`` 内で一過性エラーを
               指数バックオフリトライ（既定 5 回）。
            2. **更にその上**、本ループでも一過性例外を捕捉して
               ``poll_interval`` 待って continue する。1 関数のリトライ上限を
               超えても、ポーリングそのものは継続できるようにする。
               ``PermanentRunFailureError`` だけは即 raise（fail-fast、PR #46 と
               同じ方針）。``max_wait`` の制限は維持。

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
        try:
            status = comment_generator.get_batch_status(batch_id)
        except PermanentRunFailureError:
            # 恒久エラー（残高不足/認証/権限）はリトライ無意味。即 raise して
            # 呼び出し側 (`run`) がランを停止する（PR #46 / lessons P-024）。
            raise
        except Exception as e:
            # 一過性エラーが内側のリトライ上限を超えて漏れてきたケース。
            # ループそのものは続行する（多層防御）。warning ログを出し、
            # ``poll_interval`` 待ってから次イテレーションへ。
            logger.warning(
                f"バッチ状態取得が一過性失敗（リトライ済み）。"
                f"{poll_interval}秒後に再試行します: {e}"
            )
            time.sleep(poll_interval)
            elapsed += poll_interval
            continue

        logger.info(f"バッチステータス: {status['status']} ({status['request_counts']})")

        if status["status"] == "ended":
            break

        time.sleep(poll_interval)
        elapsed += poll_interval

    if elapsed >= max_wait:
        logger.error(
            f"バッチ結果待機タイムアウト ({max_wait}秒)。バッチは Anthropic 側で"
            f"処理継続中です（29日間保持・追加課金なし）。次回の step=all 実行が"
            f"未回収バッチ ({batch_id}) を自動検知して回収から再開します"
            f"（または --step results --batch-id {batch_id} で手動回収）。"
        )
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


def _collect_results_for_batches(
    batch_ids: list[str],
    poll_max_seconds: int = _DEFAULT_POLL_MAX_WAIT_SECONDS,
) -> dict[str, dict[str, str]]:
    """複数バッチの結果を順に回収し、マージした results を返す。

    チャンク分割送信（``step2_submit_batch``）に対応する回収側。custom_id は
    Drive file id 由来でバッチ間で衝突しないため、単純マージでよい。マージ後の
    全体を ``batch_results.json`` に保存し直す（step4 の disk 復元用）。

    ``poll_max_seconds`` は **全体** の残り時間として扱う（バッチごとに満額
    待つと GHA の 6h を超えるため）。
    """
    logger = setup_logging()
    merged: dict[str, dict[str, str]] = {}
    deadline = time.monotonic() + poll_max_seconds
    for idx, batch_id in enumerate(batch_ids, start=1):
        if len(batch_ids) > 1:
            logger.info(f"--- バッチ回収 {idx}/{len(batch_ids)}: {batch_id} ---")
        remaining = max(int(deadline - time.monotonic()), 60)
        merged.update(step3_wait_and_get_results(batch_id, max_wait=remaining))
    if len(batch_ids) > 1:
        _save_results_to_disk(merged)
        logger.info(
            f"複数バッチの結果をマージして保存: {len(merged)}件 "
            f"({len(batch_ids)}バッチ)"
        )
    return merged


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
        items = _resolve_items_for_step4(profile)

    # CB-4: バッチ結果があるのに処理対象 items を 1 件も解決できないのは異常
    # （別ジョブで死んだ回収で batch_prep.json が無く Drive 再走査も 0 件、
    # プロファイル / 対象フォルダの取り違え等）。黙って「成功 0 件」を書くと回収
    # 失敗が隠れてしまうため、ここで loud に停止する（無言の no-op を撲滅）。
    if results and not items:
        raise RuntimeError(
            "step4: バッチ結果は存在しますが処理対象 items を 1 件も解決できません"
            "（batch_prep.json も Drive 再走査も 0 件）。プロファイル / 対象フォルダが"
            "バッチ投入時と一致しているか確認してください（無言の成功 0 件を防止）。"
        )

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

    stats = {
        "success": 0, "error": 0, "missing": 0,
        "skip_already_processed": 0, "skip_unsubmitted": 0,
    }

    # 添付資料パススルー用の対応表。メイン PDF の処理ループで構築し、
    # ループ後に添付資料をこの表で引いて同じ出力フォルダへコピーする。
    # (医院番号, 医院名, 個人名) — 医院番号と医院名（AI 抽出の生の値）を別々
    # に保持し、添付資料アップロード時にメインと同じ
    # ``find_or_create_clinic_folder`` を通じて同じ医院フォルダ（医院番号で
    # 識別）に合流させる（P-019）。
    case_map: dict[str, tuple[str, str, str]] = {}

    # 医院フォルダURLシート（``<出力シート名>_医院``）への記録は
    # ClinicFolderRecorder が担う（記録済みスナップショット + 実行内重複防止）。
    clinic_recorder = run_common.ClinicFolderRecorder(
        sheets_client, profile.output_sheet_name,
    )

    # 参加者マスターシートを Step4 開始時に 1 回だけ読み込む。医院名の標準化
    # （フォルダ命名・各種シート列）と Gmail 下書きの TO ルックアップを兼ねる
    # スナップショット。シート未作成なら自動作成（ヘッダーのみ）+ 空リストを
    # 返す。メイン経路と添付資料経路（``_process_attachments``）で共有する
    # （毎回読まない）。
    master_records: list[MasterRecord] = sheets_client.read_master_records(
        sheet_name=profile.master_sheet_name,
    )
    # target_folder モード（``master_sheet_strict=True``）で空マスターなら
    # HARD FAIL（``--step results``/``pdfs`` だけ実行する resume パスで
    # Step1 の事前チェックを通っていないケースの保険）。
    if getattr(profile, "master_sheet_strict", False):
        try:
            run_common.require_non_empty_master(
                master_records, profile.master_sheet_name, True, logger,
            )
        except run_common.MasterSheetEmptyError:
            run_common.append_completion_marker_safe(
                sheets_client, profile.output_sheet_name,
                f"中止（参加者マスタータブ未準備: '{profile.master_sheet_name}'）",
                logger,
            )
            raise

    # Gmail 下書きは Step4 末尾でまとめて作成する（同じメールアドレスを持つ
    # 複数 PDF を 1 通に集約するため）。ループ中は draft_items に蓄積するだけ。
    # PDF ファイルは集約まで生存させる必要があるので session_outputs_dir に書く。
    session_outputs_dir = Path(tempfile.mkdtemp(prefix="aicomment_batch_outputs_"))
    draft_items: list[dict[str, Any]] = []

    # Gmail 下書きが OFF のときは、出力 PDF を集約下書きまで保持する必要が
    # 無い。アップロード成功のたびにサブディレクトリを即削除し、1000 件 ×
    # 数 MB の結合 PDF が /tmp に滞留してディスクを圧迫するのを防ぐ
    # （長時間・大量ラン対策）。ON のときは従来通り集約添付まで保持する。
    keep_outputs = config.ENABLE_GMAIL_DRAFTS

    # 一時ディレクトリの掃除は mkdtemp 直後からの try/finally で保証する。
    # 以前は末尾のドラフト作成呼び出しだけを finally が包んでおり、メイン
    # ループ・添付経路の per-item try に包まれていない箇所（disk 由来 JSON の
    # 不正・results のフィールド欠落など）で例外が出ると rmtree に到達せず、
    # ローカル/Codespaces の反復実行で一時ディレクトリが蓄積した。
    try:

        for item in items:
            custom_id = item["custom_id"]
            pdf_file_name = item.get("pdf_file_name", "")
            # mgmt_num はどちらの分岐（results 有無）でも必要なため先に抽出する。
            mgmt_num = extract_management_number(pdf_file_name)

            if custom_id not in results:
                # results に無い item は 3 通りに分類する。回収実行
                # （``--step results --batch-id`` / 自動レジューム）では items が
                # ``reconstruct_items_from_drive`` による Drive 全件再走査で作られる
                # ため、「今回のバッチには含まれない過去処理済み PDF」や「そもそも
                # 投入されなかった PDF」が items に混入する。これらを無条件で
                # missing 扱いすると、1000 件規模の回収実行で無関係な PDF が大量に
                # 「コメント未取得」警告 + missing 集計され、完了マーカーの
                # 「未取得 N 件」が実態と乖離する（過去に処理済みなのに「未取得」に
                # 見える）。
                if mgmt_num and mgmt_num in step4_processed:
                    # 過去ラン（または今回の別バッチ）で処理済み。CB-3 と同じ判定
                    # だが結果を伴わないため per-item ログは出さない（回収実行での
                    # ログ洪水を防ぐ）。
                    stats["skip_already_processed"] += 1
                elif not mgmt_num:
                    # 管理番号を抽出できないファイル（Drive 再走査でのみ発生し得る。
                    # step1 の通常経路では select_new_targets が事前に除外済み）。
                    logger.info(
                        f"未投入ファイルをスキップ（管理番号抽出不可のため投入対象"
                        f"外だった可能性）: {pdf_file_name}"
                    )
                    stats["skip_unsubmitted"] += 1
                else:
                    # 投入されたはずなのに結果が無い＝真の missing。
                    logger.warning(f"コメント未取得: {custom_id} ({pdf_file_name})")
                    stats["missing"] += 1
                continue

            # results は通常 ``_parse_extraction`` が全フィールドを保証するが、
            # 回収経路では disk（``batch_results.json``）から読むため、旧形式・
            # 手編集のファイルだとフィールドが欠落し得る。KeyError で per-item
            # try の外から抜けてラン全体が死なないよう .get で防御する。
            meta = results[custom_id]
            clinic_name_from_ai = meta.get("clinic_name", "") or "unknown_clinic"
            person_name = meta.get("person_name", "") or "unknown_person"
            sample_title = (
                meta.get("sample_title", "") or Path(pdf_file_name).stem or "untitled"
            )
            comment = meta.get("comment", "")
            if not comment:
                # comment 欠落/空はこの item の恒久異常（コメントページを空で
                # 作らない）。per-item エラーとして計上し続行する。
                logger.error(
                    f"バッチ結果に comment がありません（disk 上の "
                    f"batch_results.json が不正な可能性）: {custom_id} "
                    f"({pdf_file_name})"
                )
                stats["error"] += 1
                continue

            # 医院番号（管理番号の先頭セグメント）を抽出する。医院フォルダの識別
            # は医院番号のみで行うため（P-019）、医院番号と医院名を別々に
            # ``upload_pdf_to_clinic_person`` に渡す。医院番号が空の場合は
            # ``find_or_create_clinic_folder`` 側で旧来の名前ベース照合に
            # フォールバックする。
            clinic_number = extract_clinic_number(pdf_file_name)
            if not mgmt_num:
                logger.warning(
                    f"管理番号をファイル名から抽出できません"
                    f"（先頭が NNN-NN-N / NNN-NN 形式でない）: {pdf_file_name}"
                )

            # CB-3: Step4 開始時のスナップショットに mgmt_num が含まれていれば
            # 前回 Step4 で処理済み（Drive アップロード + Sheets 追記 + 下書き想定）。
            # 再処理すると Drive / Sheets / Gmail で重複が出るためスキップ。
            # ただし case_map（添付資料パススルー用の対応表）には **登録する**。
            # 前回ランが「メイン記録後・添付コピー前」でクラッシュした場合、この
            # 登録が無いと添付資料が対応表を引けず恒久ロストになる（AI 抽出結果
            # ``meta`` はバッチ結果に残っているため、アップロードなしで医院名解決
            # だけ行える）。
            if mgmt_num and mgmt_num in step4_processed:
                logger.info(
                    f"Step4 スキップ（処理済み再実行検知）: {mgmt_num} "
                    f"({pdf_file_name})"
                )
                resolved_name, _ = run_common.resolve_clinic_name(
                    sheets_client, master_records,
                    extract_clinic_number(pdf_file_name), clinic_name_from_ai,
                    logger,
                )
                case_map[mgmt_num] = (
                    extract_clinic_number(pdf_file_name), resolved_name, person_name
                )
                stats["skip_already_processed"] += 1
                continue

            # 医院名は参加者マスターの標準表記を最優先、未登録なら AI 抽出値で
            # 代用（警告ログは resolve_clinic_name 内）。以後すべての医院名
            # 用途で ``clinic_name`` 変数を使う。
            clinic_name, clinic_name_is_authoritative = (
                run_common.resolve_clinic_name(
                    sheets_client, master_records,
                    clinic_number, clinic_name_from_ai, logger,
                )
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
                        clinic_name_authoritative=clinic_name_is_authoritative,
                    )

                    # チーム事例（チーム実践_ / チームMTG_）は、同じチームの
                    # 全メンバーのフォルダにも同じ PDF を配布する。シート記録
                    # より **前** に行う（配布失敗 → 記録なし → 再実行で配布
                    # からやり直し。逆順だと配布漏れが恒久化する、P-031）。
                    if is_team_filename(pdf_file_name):
                        team_members = run_common.distribute_team_copies(
                            drive_client, sheets_client, logger,
                            master_records=master_records,
                            reporter_mgmt_num=mgmt_num,
                            file_path=output_path,
                            file_name=output_filename,
                            output_folder_id=profile.output_folder_id,
                        )
                        # メンバー分も出力一覧シートに記録する(報告者と同じ
                        # 管理番号・sample_name に【チーム配布】マーカーを付与し、
                        # 添付資料の【添付資料】マーカーと同じ見分け方にする)。
                        for member in team_members:
                            sheets_client.append_output_record(
                                management_number=mgmt_num,
                                clinic_name=member["clinic_name"],
                                person_name=member["person_name"],
                                sample_name=f"【チーム配布】{sample_title}",
                                drive_url=member["drive_url"],
                                sheet_name=profile.output_sheet_name,
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
                    clinic_recorder.record(
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
                    f"完了: {mgmt_num} / {mask_name(clinic_name)} / "
                    f"{mask_name(person_name)} / {sample_title}"
                )
                stats["success"] += 1

                # ドラフト OFF なら出力 PDF はもう不要（Drive 保存済み）。
                # 逐次削除してディスク滞留を防ぐ。
                if not keep_outputs:
                    shutil.rmtree(pdf_subdir, ignore_errors=True)

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
            keep_outputs=keep_outputs,
        )

        # ── 集約下書き作成 ──
        # メイン + 添付資料経路で蓄積した draft_items をメールアドレスでグルーピング
        # し、グループごとに 1 通の Gmail 下書きを作成する。Gmail 下書きの ON/OFF
        # 判定は ``_create_grouped_drafts_for_batch`` 内で行う。一時ファイル削除は必ず行う。
        _create_grouped_drafts_for_batch(draft_items)
    finally:
        shutil.rmtree(session_outputs_dir, ignore_errors=True)

    total = stats["success"] + stats["error"] + stats["missing"]
    logger.info(
        f"Step 4完了: 成功 {stats['success']}/{total}件, "
        f"エラー {stats['error']}/{total}件, "
        f"未取得 {stats['missing']}/{total}件 "
        f"(処理済みスキップ {stats['skip_already_processed']}件, "
        f"未投入スキップ {stats['skip_unsubmitted']}件)"
    )

    # 出力一覧シートの最終行に「完了」マーカーを 1 行追加（fail-soft）。
    run_common.append_completion_marker_safe(
        sheets_client,
        profile.output_sheet_name,
        (
            f"成功 {stats['success']}件 / "
            f"エラー {stats['error']}件 / "
            f"未取得 {stats['missing']}件"
        ),
        logger,
    )


def _collect_draft_item_batch(
    draft_items: list[dict[str, Any]],
    master_records: list[MasterRecord],
    clinic_number: str,
    person_name: str,
    pdf_path: Path,
) -> None:
    """Gmail 下書き用の (メール, 個人名, PDF) を ``draft_items`` に蓄積する。

    実装は ``run_common.collect_draft_item`` に委譲する（通常モードと共通、
    fail-soft）。
    """
    run_common.collect_draft_item(
        draft_items, master_records, sheets_client,
        clinic_number=clinic_number,
        person_name=person_name,
        pdf_path=pdf_path,
    )


def _create_grouped_drafts_for_batch(
    draft_items: list[dict[str, Any]],
) -> None:
    """``draft_items`` をメールアドレスでグループ化して下書きを作成する。

    実装は ``run_common.create_grouped_drafts`` に委譲する（通常モードと
    共通、P-023）。``gmail_client`` は呼び出し時のモジュール属性を渡す
    （テストのモジュールパッチを生かすため）。
    """
    run_common.create_grouped_drafts(draft_items, gmail_client)


def _process_attachments(
    profile: ProfileConfig | RunConfig,
    case_map: dict[str, tuple[str, str, str]],
    stats: dict[str, int],
    master_records: list[MasterRecord],
    session_outputs_dir: Path,
    draft_items: list[dict[str, Any]],
    keep_outputs: bool = True,
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

    try:
        attachment_records: list[dict] = json.loads(attachments_file.read_text())
    except json.JSONDecodeError as e:
        # atomic write（P-023）により通常は起きないが、手編集・部分コピー等で
        # 壊れた場合に生の Traceback で終わらせず、復旧手順つきで loud に停止
        # する（P-001）。
        raise RuntimeError(
            f"添付資料データが壊れています: {attachments_file}。"
            f"このファイルを削除するか、--step prepare を再実行して"
            f"作り直してください（元エラー: {e}）"
        ) from e
    if not attachment_records:
        logger.info("添付資料 0 件")
        return

    def _collect(clinic_number: str, person_name: str, pdf_path: Path) -> None:
        """添付資料経路の下書き蓄積（``draft_items`` / ``master_records`` を束縛）。"""
        _collect_draft_item_batch(
            draft_items=draft_items,
            master_records=master_records,
            clinic_number=clinic_number,
            person_name=person_name,
            pdf_path=pdf_path,
        )

    logger.info(f"--- 添付資料パススルー: {len(attachment_records)}件 ---")
    for record in attachment_records:
        file_id = record["file_id"]
        file_name = record["file_name"]
        mgmt_num = record["management_number"]

        case = case_map.get(mgmt_num)
        if case is None:
            # メインが同一ラン内で処理されていない（過去ラン処理済み +
            # 添付だけ後から追加、等）。参加者マスターだけで出力先を解決する
            # フォールバック（メイン非依存・恒久ロスト防止）。
            case = run_common.resolve_case_via_master(
                sheets_client, master_records, mgmt_num, logger,
            )
        if case is None:
            logger.warning(
                f"対応するメイン実践事例 PDF がこの実行で処理されておらず、"
                f"参加者マスターにも管理番号 {mgmt_num} が未登録のため"
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
        run_common.passthrough_attachment(
            drive_module=drive_client,
            sheets_module=sheets_client,
            logger=logger,
            stats=stats,
            collect_draft=_collect,
            file_id=file_id,
            file_name=file_name,
            mgmt_num=mgmt_num,
            clinic_number=clinic_number,
            clinic_name=clinic_name,
            person_name=person_name,
            output_folder_id=profile.output_folder_id,
            output_sheet_name=profile.output_sheet_name,
            session_outputs_dir=session_outputs_dir,
            clinic_name_authoritative=clinic_name_authoritative,
        )
        # ドラフト OFF なら添付コピーの一時ファイルも逐次削除（ディスク滞留防止）。
        if not keep_outputs:
            shutil.rmtree(
                session_outputs_dir / f"attach_{file_id}", ignore_errors=True,
            )


def _mark_batches_done(target_sheet_name: str, batch_ids: list[str]) -> None:
    """回収が完了したバッチを ``_バッチ管理`` タブに記録する（fail-soft）。

    記録に失敗しても、次回 ``step=all`` が同じバッチを再回収するだけで、
    Step4 の CB-3 重複ガード（処理済み管理番号スキップ）により出力の重複は
    起きない（安全側に倒れる）。
    """
    logger = setup_logging()
    for bid in batch_ids:
        try:
            sheets_client.append_batch_record(
                target_sheet_name, bid, sheets_client.BATCH_STATE_DONE,
            )
        except Exception as e:
            logger.warning(
                f"バッチ回収完了の記録に失敗（次回実行で再回収されるが、"
                f"CB-3 重複ガードにより出力は重複しない）: {bid}: {e}"
            )


def _resume_open_batches(
    profile: ProfileConfig | RunConfig,
    poll_max_seconds: int = _DEFAULT_POLL_MAX_WAIT_SECONDS,
) -> None:
    """未回収バッチ（投入済み・回収未完了）を検知し、回収から再開する。

    ``step=all`` の冒頭で呼ぶ。GHA ランが step3/step4 の途中で kill された後の
    再実行は、GHA ランナーが ephemeral のため ``batch_id.txt`` を持たず、従来は
    全件を **再投入**（＝Anthropic への二重課金）していた。スプレッドシートの
    ``_バッチ管理`` タブから未回収バッチを検知し、再投入せず結果回収 → PDF 生成
    を先に完走させる。回収後は通常フロー（step1〜）に続き、回収分は出力一覧
    シート記録済みのため重複判定で自然にスキップされる。

    - バッチがまだ処理中 → ``step3`` のポーリングで待つ（タイムアウト時は
      非ゼロ終了し、次回実行が再び回収から再開する）
    - バッチが Anthropic 側に存在しない（29 日保持期限切れ等）→ ``期限切れ``
      として記録し、ブロックせず先へ進む（該当分は再投入対象に戻る）
    - ``_バッチ管理`` タブの読み取り失敗 → 警告して新規実行として続行
      （fail-soft。復旧経路より新規処理の継続を優先）
    """
    logger = setup_logging()
    try:
        open_ids = sheets_client.get_open_batch_ids(profile.output_sheet_name)
    except Exception as e:
        logger.warning(f"未回収バッチの確認に失敗（新規実行として続行）: {e}")
        return
    if not open_ids:
        return

    logger.warning(
        f"未回収バッチ {len(open_ids)} 件を検出: {open_ids}。"
        f"再投入せず結果回収から再開します（二重課金防止）。"
    )
    recovered: dict[str, dict[str, str]] = {}
    consumed: list[str] = []
    deadline = time.monotonic() + poll_max_seconds
    for bid in open_ids:
        remaining = max(int(deadline - time.monotonic()), 60)
        try:
            recovered.update(
                step3_wait_and_get_results(bid, max_wait=remaining)
            )
            consumed.append(bid)
        except anthropic.NotFoundError:
            logger.error(
                f"バッチ {bid} は Anthropic 側に存在しません（29 日の保持期限"
                f"切れ等・結果は回収不能）。期限切れとして記録し先へ進みます。"
                f"未処理分は出力一覧シート未記録のため、この後の通常フローで"
                f"再投入されます。"
            )
            try:
                sheets_client.append_batch_record(
                    profile.output_sheet_name, bid,
                    sheets_client.BATCH_STATE_EXPIRED,
                )
            except Exception as e:
                logger.warning(f"期限切れバッチの記録に失敗: {bid}: {e}")

    if recovered:
        step4_generate_pdfs(profile, recovered, items=None)
    _mark_batches_done(profile.output_sheet_name, consumed)
    logger.info(
        f"未回収バッチの回収が完了 ({len(consumed)}/{len(open_ids)}件)。"
        f"通常フローに続きます。"
    )


def run(
    batch_mode: bool = True,
    test_count: int = 0,
    batch_id: str | None = None,
    step: str = "all",
    profile_name: str | None = None,
    target_folder: str | None = None,
    poll_max_minutes: int = 300,
) -> None:
    """Batchモードのメイン処理。

    Args:
        batch_mode: Batch API使用（Falseなら通常モードにフォールバック）
        test_count: 新規 PDF を N 件まで処理（0=全件）。重複・管理番号なしを
            除外した「処理対象の新規 PDF」に対して適用される。
        batch_id: 既存のバッチID（Step 3から再開する場合）。チャンク分割で
            複数バッチになった場合はカンマ区切りで複数指定できる。
        step: 実行するステップ ("all", "prepare", "submit", "results", "pdfs")
        profile_name: プロファイル名（``profiles/<name>.yaml``）。
            省略時かつ ``target_folder`` も無指定なら ``jissen_default``。
        target_folder: フォルダ自動検出モードのフォルダ名。
            ``__list__`` 指定時は候補列挙のみ行い即 return。
        poll_max_minutes: step3 ポーリングの最大待機（分）。GHA の 6h 上限内で
            自発的にタイムアウトさせるための値（既定 300 分 = 5h）。
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
    batch_ids: list[str] = (
        [b.strip() for b in batch_id.split(",") if b.strip()]
        if batch_id else []
    )
    poll_max_seconds = poll_max_minutes * 60
    # run() 全体で 1 本の deadline を持つ。resume（回収）フェーズと通常フェーズの
    # 結果待ちが両方走ると合計待機時間が GHA の 6h ジョブ上限を超え得るため、
    # 「このラン全体でポーリングに使ってよい予算」を 1 つに固定し、各フェーズは
    # ``_remaining_seconds`` でその時点の残量を取得する（P-030 と同じ思想:
    # 予算枯渇時は安全にタイムアウトし、次回実行の自動レジュームに引き継ぐ）。
    poll_deadline = time.monotonic() + poll_max_seconds

    # 回収判定: 明示的に ``--batch-id`` を渡した ``results`` 実行だけを「回収」と
    # みなし、step4 まで完走させる。``batch_id`` を ``batch_id.txt`` から補完する
    # discrete な ``results``（batch-orchestrator の 1000 件分割運用）では step4 を
    # 走らせない＝長時間ポーリング直後に PDF 生成を始めて GHA 6h を超える事故を
    # 防ぐ。``batch_ids`` はこの後の results ブロックで補完され得るため、補完前の
    # 「明示指定だったか」をここで確定させる。
    is_recovery = step == "results" and bool(batch_ids)

    # 未回収バッチの自動レジューム（``step=all`` のみ）。ジョブ kill 後の
    # 再実行で全件再投入 → 二重課金になるのを防ぐ。
    if step == "all":
        _resume_open_batches(cfg, _remaining_seconds(poll_deadline))

    if step in ("all", "prepare"):
        items = step1_prepare(cfg, test_count=test_count)

    # 新規 0 件（全 PDF 処理済み）は増分運用の定常状態。従来はこの後の
    # results ブロックが ``batch_id.txt``（存在しない）を読もうとして
    # FileNotFoundError で赤ランになっていた。正常終了する。
    if step == "all" and items is not None and not items:
        logger.info(
            "新規処理対象が 0 件のため、以降のステップをスキップして正常終了"
            "します（入力フォルダの全 PDF が処理済み）"
        )
        return

    if step in ("all", "submit"):
        # CB-2: step=submit を別 GHA 実行から呼ぶケース。in-memory items が
        # 無ければ batch_prep.json から復元する（CB-1 で pdf_text 込みで保存）。
        if items is None:
            items = _load_items_from_disk()
            logger.info(f"items を batch_prep.json からロード ({len(items)}件)")
        if not items:
            logger.info("処理対象が0件のため、バッチ送信をスキップします")
        else:
            batch_ids = step2_submit_batch(
                items, state_target=cfg.output_sheet_name,
            )

    if step in ("all", "results"):
        if not batch_ids:
            batch_ids = _load_batch_ids_from_disk()
        results = _collect_results_for_batches(
            batch_ids, _remaining_seconds(poll_deadline),
        )

    if step in ("all", "pdfs") or is_recovery:
        # CB-2: step=pdfs / 回収を別 GHA 実行から呼ぶケース。in-memory results が
        # 無ければ disk（batch_results.json、step3 が永続化）から復元する。
        # CB-4: items の解決は step4 に委譲する（batch_prep.json があればそれを
        # 優先、無ければ Drive 再走査で再構築）。``--step results --batch-id`` で
        # 死んだジョブから回収する場合、batch_prep.json は別ジョブにしか無いため、
        # ここでは in-memory items（=None）をそのまま渡し step4 で解決させる。
        if results is None:
            results = _load_results_from_disk()
            logger.info(f"results を batch_results.json からロード ({len(results)}件)")
        step4_generate_pdfs(cfg, results, items=items)
        # 回収完了をバッチ状態管理タブに記録（fail-soft）。step=pdfs 単独実行
        # では batch_ids が空のため no-op。
        _mark_batches_done(cfg.output_sheet_name, batch_ids)

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
        help="既存バッチID（結果取得から再開）。複数はカンマ区切り",
    )
    parser.add_argument(
        "--step", type=str, default="all",
        choices=["all", "prepare", "submit", "results", "pdfs"],
        help="実行するステップ",
    )
    parser.add_argument(
        "--poll-max-minutes", type=int, default=300,
        help=(
            "step3 ポーリングの最大待機（分）。GHA の 6h 上限内で自発的に"
            "タイムアウトさせる（既定 300 分）。タイムアウトしても投入済み"
            "バッチは次回 step=all 実行が自動回収する"
        ),
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
        poll_max_minutes=args.poll_max_minutes,
    )


if __name__ == "__main__":
    main()
