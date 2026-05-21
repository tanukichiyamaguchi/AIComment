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

from src.utils import setup_logging, ensure_fonts, extract_management_number
from src.config import LOGS_DIR
from src import discover, drive_client, sheets_client
from src import pdf_reader, comment_generator, pdf_creator, pdf_merger
from src.discover import RunConfig
from src.profile import ProfileConfig


def step1_prepare(
    profile: ProfileConfig | RunConfig,
    test_count: int = 0,
) -> list[dict]:
    """Step 1: 準備 — PDF取得・テキスト抽出。

    Args:
        profile: 実行時設定（``ProfileConfig`` または ``RunConfig``）。
            後方互換のため ``ProfileConfig`` を受けるが、内部では ``RunConfig``
            と同じフィールドのみ参照する。
        test_count: テスト件数（0=全件処理）

    Returns:
        Batch APIに投入可能なアイテムのリスト
    """
    logger = setup_logging()
    logger.info("=== Step 1: 準備 ===")

    pdf_files = drive_client.list_pdfs(folder_id=profile.input_folder_id)
    if test_count > 0:
        pdf_files = pdf_files[:test_count]
        logger.info(f"テストモード: {test_count}件に制限")
    logger.info(f"処理対象: {len(pdf_files)}件のPDF")

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

        items.append({
            "custom_id": f"item_{i:04d}",
            "pdf_data_id": file_id,  # 後でダウンロードし直す用
            "pdf_file_name": file_name,
            "pdf_text": pdf_text,
        })

    logger.info(f"Step 1完了: {len(items)}件が処理可能")

    # 準備データをJSONに保存（Step 4でも使用）
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

    total = stats["success"] + stats["error"] + stats["missing"]
    logger.info(
        f"Step 4完了: 成功 {stats['success']}/{total}件, "
        f"エラー {stats['error']}/{total}件, "
        f"未取得 {stats['missing']}/{total}件"
    )


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
        test_count: テスト件数（0=全件）
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
