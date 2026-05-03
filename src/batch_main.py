"""Batchモードのエントリポイント。400件一括処理用。"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path

from src.utils import setup_logging, mask_email, ensure_fonts
from src.config import DRIVE_OUTPUT_FOLDER_ID, LOGS_DIR
from src import drive_client, sheets_client, gmail_client
from src import pdf_reader, comment_generator, pdf_creator, pdf_merger
from src.matcher import match_record


def step1_prepare() -> list[dict]:
    """Step 1: 準備 — PDF取得・テキスト抽出・マッチング。

    Returns:
        処理可能なアイテムのリスト
    """
    logger = setup_logging()
    logger.info("=== Step 1: 準備 ===")

    records = sheets_client.get_unprocessed_records()
    pdf_files = drive_client.list_pdfs()

    logger.info(f"PDF {len(pdf_files)}件, 未処理レコード {len(records)}件")

    items = []
    for i, pdf_file in enumerate(pdf_files, start=1):
        file_id = pdf_file["id"]
        file_name = pdf_file["name"]
        logger.info(f"[{i}/{len(pdf_files)}] {file_name}")

        try:
            pdf_data = drive_client.download_pdf(file_id)
            pdf_text = pdf_reader.extract_text(pdf_data)
            if not pdf_text:
                logger.warning(f"テキスト抽出失敗（空テキスト）: {file_name}")
                continue
        except Exception as e:
            logger.warning(f"PDF処理失敗: {file_name} - {e}")
            continue

        record = match_record(pdf_text, records, pdf_filename=file_name)
        if not record:
            logger.warning(f"マッチング失敗: {file_name}")
            sheets_client.update_status(
                record.row_number if record else 0,
                f"スキップ: マッチング失敗",
            ) if record else None
            continue

        items.append({
            "custom_id": f"item_{i:04d}",
            "clinic_name": record.clinic_name,
            "person_name": record.person_name,
            "email": record.email,
            "row_number": record.row_number,
            "pdf_text": pdf_text,
            "pdf_data_id": file_id,  # 後でダウンロードし直す用
            "pdf_file_name": file_name,  # Drive保存時のファイル名
        })
        sheets_client.update_status(record.row_number, "処理中")

    logger.info(f"Step 1完了: {len(items)}件が処理可能")

    # 準備データをJSONに保存（Step 3/4で使用）
    prep_file = LOGS_DIR / "batch_prep.json"
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    # pdf_textは大きいので保存しない（Step2用にはバッチリクエスト作成時に使う）
    save_items = [{k: v for k, v in item.items() if k != "pdf_text"} for item in items]
    prep_file.write_text(json.dumps(save_items, ensure_ascii=False, indent=2))
    logger.info(f"準備データ保存: {prep_file}")

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

    # バッチIDを保存
    batch_id_file = LOGS_DIR / "batch_id.txt"
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    batch_id_file.write_text(batch_id)
    logger.info(f"バッチID保存: {batch_id_file} → {batch_id}")

    return batch_id


def step3_wait_and_get_results(
    batch_id: str,
    items: list[dict] | None = None,
    poll_interval: int = 60,
    max_wait: int = 86400,
    max_retries: int = 2,
) -> dict[str, str]:
    """Step 3: バッチ結果をポーリングで取得する。失敗アイテムは個別リトライ。

    Args:
        batch_id: バッチID
        items: 準備データ（リトライ用。Noneの場合はファイルから読み込み）
        poll_interval: ポーリング間隔（秒）
        max_wait: 最大待機時間（秒）
        max_retries: 失敗アイテムのリトライ回数

    Returns:
        {custom_id: comment_text, ...}
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

    # 失敗アイテムを個別リトライ
    if failed_ids and max_retries > 0:
        logger.info(f"失敗アイテム {len(failed_ids)}件を個別リトライします (最大{max_retries}回)")

        # items lookup用のマップを構築
        if items is None:
            prep_file = LOGS_DIR / "batch_prep.json"
            if prep_file.exists():
                items = json.loads(prep_file.read_text())

        items_by_id = {item["custom_id"]: item for item in (items or [])}

        for custom_id in failed_ids:
            item = items_by_id.get(custom_id)
            if not item:
                logger.warning(f"リトライ用データが見つかりません: {custom_id}")
                continue

            # pdf_textが必要 — prep_fileには保存されていないためスキップ
            if "pdf_text" not in item:
                logger.warning(f"リトライにはpdf_textが必要です（スキップ）: {custom_id}")
                continue

            for attempt in range(1, max_retries + 1):
                try:
                    logger.info(f"リトライ {attempt}/{max_retries}: {custom_id}")
                    comment = comment_generator.generate_comment(
                        clinic_name=item["clinic_name"],
                        person_name=item["person_name"],
                        pdf_text=item["pdf_text"],
                    )
                    results[custom_id] = comment
                    logger.info(f"リトライ成功: {custom_id}")
                    break
                except Exception as e:
                    logger.warning(f"リトライ失敗 {attempt}/{max_retries}: {custom_id} - {e}")

    # 最終サマリー
    final_failed = [cid for cid in failed_ids if cid not in results]
    logger.info(
        f"Step 3完了: {len(results)}件成功"
        + (f", {len(final_failed)}件失敗 ({', '.join(final_failed)})" if final_failed else "")
    )
    return results


def step4_generate_pdfs_and_drafts(
    results: dict[str, str],
    items: list[dict] | None = None,
) -> None:
    """Step 4: PDF生成 & Gmail下書き作成。

    Args:
        results: {custom_id: comment_text, ...}
        items: 準備データ（Noneの場合はファイルから読み込み）
    """
    logger = setup_logging()
    logger.info("=== Step 4: PDF生成 & メール下書き ===")

    ensure_fonts()

    # 準備データの読み込み
    if items is None:
        prep_file = LOGS_DIR / "batch_prep.json"
        items = json.loads(prep_file.read_text())

    stats = {"success": 0, "error": 0}

    for item in items:
        custom_id = item["custom_id"]
        if custom_id not in results:
            logger.warning(f"コメント未取得: {custom_id}")
            stats["error"] += 1
            continue

        comment = results[custom_id]
        clinic_name = item["clinic_name"]
        person_name = item["person_name"]
        email = item["email"]

        try:
            # PDFダウンロード
            pdf_data = drive_client.download_pdf(item["pdf_data_id"])

            with tempfile.TemporaryDirectory() as tmpdir:
                # コメントページ生成
                comment_page_path = Path(tmpdir) / "comment_page.pdf"
                pdf_creator.create_comment_page(
                    comment=comment,
                    clinic_name=clinic_name,
                    person_name=person_name,
                    output_path=comment_page_path,
                )

                # PDF結合
                output_filename = pdf_merger.make_output_filename(
                    clinic_name, person_name
                )
                output_path = Path(tmpdir) / output_filename
                pdf_merger.merge_pdfs(
                    original_pdf_data=pdf_data,
                    comment_page_path=comment_page_path,
                    output_path=output_path,
                )

                # Drive 階層保存 + 出力一覧シート追記
                #     （DRIVE_OUTPUT_FOLDER_ID 未設定時はスキップ）
                pdf_file_name = item.get("pdf_file_name", output_filename)
                if DRIVE_OUTPUT_FOLDER_ID:
                    try:
                        upload_result = drive_client.upload_pdf_to_clinic_person(
                            file_path=output_path,
                            output_root_folder_id=DRIVE_OUTPUT_FOLDER_ID,
                            clinic_name=clinic_name,
                            person_name=person_name,
                            file_name=pdf_file_name,
                        )
                        sheets_client.append_output_record(
                            clinic_name=clinic_name,
                            person_name=person_name,
                            sample_name=pdf_file_name,
                            drive_url=upload_result["webViewLink"],
                        )
                    except Exception as drive_err:
                        logger.warning(
                            f"Drive保存/出力一覧追記スキップ: {drive_err}"
                        )

                # Gmail下書き
                if email:
                    gmail_client.create_draft(
                        to_email=email,
                        person_name=person_name,
                        pdf_path=output_path,
                    )

            # ステータス更新
            if "row_number" in item:
                sheets_client.update_status(item["row_number"], "完了")
            stats["success"] += 1

        except Exception as e:
            logger.error(f"処理エラー: {clinic_name} {person_name} - {e}")
            stats["error"] += 1
            if "row_number" in item:
                try:
                    sheets_client.update_status(
                        item["row_number"], f"エラー: {str(e)[:50]}"
                    )
                except Exception:
                    pass

    total = stats["success"] + stats["error"]
    logger.info(
        f"Step 4完了: 成功 {stats['success']}/{total}件, エラー {stats['error']}/{total}件"
    )
    if stats["error"] > 0:
        logger.warning(f"一部アイテムでエラーが発生しました（{stats['error']}件）。ログを確認してください。")


def run(
    batch_mode: bool = True,
    test_count: int = 0,
    batch_id: str | None = None,
    step: str = "all",
) -> None:
    """Batchモードのメイン処理。

    Args:
        batch_mode: Batch API使用（Falseなら通常モードにフォールバック）
        test_count: テスト件数（0=全件）
        batch_id: 既存のバッチID（Step 3から再開する場合）
        step: 実行するステップ ("all", "prepare", "submit", "results", "pdfs")
    """
    logger = setup_logging()
    logger.info("=== じっせん君コメントシステム（Batchモード）開始 ===")

    if not batch_mode:
        from src.main import run as run_normal
        run_normal(test_count=test_count)
        return

    items = None
    results = None

    if step in ("all", "prepare"):
        items = step1_prepare()
        if test_count > 0:
            items = items[:test_count]

    if step in ("all", "submit"):
        if items is None:
            prep_file = LOGS_DIR / "batch_prep.json"
            items_data = json.loads(prep_file.read_text())
            # pdf_textが必要なので再取得が必要 → step1からやり直し
            raise RuntimeError("submit単独実行にはprepareが必要です")
        if not items:
            logger.info("処理対象が0件のため、バッチ送信をスキップします")
        else:
            bid = step2_submit_batch(items)
            batch_id = bid

    if step in ("all", "results"):
        if batch_id is None:
            batch_id_file = LOGS_DIR / "batch_id.txt"
            batch_id = batch_id_file.read_text().strip()
        results = step3_wait_and_get_results(batch_id, items=items)

    if step in ("all", "pdfs"):
        if results is None:
            raise RuntimeError("results が未取得です。step=results を先に実行してください")
        step4_generate_pdfs_and_drafts(results)

    logger.info("=== Batchモード処理完了 ===")


def main() -> None:
    parser = argparse.ArgumentParser(description="じっせん君コメントシステム（Batchモード）")
    parser.add_argument(
        "--no-batch", action="store_true",
        help="通常モードで実行",
    )
    parser.add_argument(
        "--test-count", type=int, default=0,
        help="テスト件数（0=全件処理）",
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
    args = parser.parse_args()

    run(
        batch_mode=not args.no_batch,
        test_count=args.test_count,
        batch_id=args.batch_id,
        step=args.step,
    )


if __name__ == "__main__":
    main()
