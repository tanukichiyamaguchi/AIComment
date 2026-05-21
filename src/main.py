"""通常モードのエントリポイント。

PDFを読み取り、Claude APIで医院名・氏名・実践事例タイトル・コメントを抽出/生成し、
コメント付きPDFをDriveに「医院名/個人名/」階層で保存し、出力一覧シートに記録する。
事前のスプレッドシート入力（Sheet1）は不要。

管理番号は実践事例 PDF のファイル名先頭（``NNN-NN-N`` 形式）から抽出する。

実行モード:
    1. プロファイル（``--profile <name>``）— ``profiles/<name>.yaml`` を読み込み、
       入力フォルダ・出力フォルダ・出力シート名を切り替える。
       既存挙動を完全維持する後方互換モード。
    2. フォルダ自動検出（``--target-folder <name>``）— ``DRIVE_INPUT_ROOT``
       配下の同名サブフォルダを auto-discover し、出力フォルダ・シートタブを
       自動派生する。Secret/YAML 追加なしで新セミナーに対応。
    3. ``--target-folder __list__`` — 候補名を列挙して即終了（ユーザー向け補助）。

    両方省略時は ``--profile jissen_default``（既存挙動）。
"""

from __future__ import annotations

import argparse
import tempfile
from pathlib import Path

from src.utils import setup_logging, ensure_fonts, extract_management_number
from src import discover, drive_client, sheets_client
from src import pdf_reader, comment_generator, pdf_creator, pdf_merger


def run(
    test_count: int = 0,
    profile_name: str | None = None,
    target_folder: str | None = None,
) -> None:
    """通常モードのメイン処理。

    Args:
        test_count: テスト件数（0=全件処理）
        profile_name: プロファイル名（``profiles/<name>.yaml``）。
            省略時かつ ``target_folder`` も無指定なら ``jissen_default``。
        target_folder: フォルダ自動検出モードのフォルダ名。
            ``__list__`` 指定時は候補列挙のみ行い即 return。
    """
    logger = setup_logging()
    logger.info("=== じっせん君コメントシステム（通常モード）開始 ===")

    if target_folder == "__list__":
        discover.handle_list_mode(logger)
        return

    cfg = discover.resolve_run_config(profile_name, target_folder)
    logger.info(cfg.display_name)

    ensure_fonts()

    logger.info("Step 1: PDF一覧取得")
    pdf_files = drive_client.list_pdfs(folder_id=cfg.input_folder_id)

    if test_count > 0:
        pdf_files = pdf_files[:test_count]
        logger.info(f"テストモード: {test_count}件に制限")

    logger.info(f"処理対象: {len(pdf_files)}件のPDF")

    stats = {"success": 0, "skip": 0, "error": 0}

    for i, pdf_file in enumerate(pdf_files, start=1):
        file_id = pdf_file["id"]
        file_name = pdf_file["name"]
        logger.info(f"--- [{i}/{len(pdf_files)}] {file_name} ---")

        try:
            pdf_data = drive_client.download_pdf(file_id)
            pdf_text = pdf_reader.extract_text(pdf_data)
            if not pdf_text:
                logger.warning(f"テキスト抽出失敗: {file_name}")
                stats["skip"] += 1
                continue

            metadata = comment_generator.generate_comment_with_metadata(
                pdf_text=pdf_text,
                pdf_filename=file_name,
            )
            clinic_name = metadata["clinic_name"] or "unknown_clinic"
            person_name = metadata["person_name"] or "unknown_person"
            sample_title = metadata["sample_title"] or Path(file_name).stem
            comment = metadata["comment"]

            output_filename = pdf_merger.make_output_filename(
                clinic_name, person_name, sample_title
            )

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
                    output_root_folder_id=cfg.output_folder_id,
                    clinic_name=clinic_name,
                    person_name=person_name,
                    file_name=output_filename,
                )

            mgmt_num = extract_management_number(file_name)
            if not mgmt_num:
                logger.warning(
                    f"管理番号をファイル名から抽出できません"
                    f"（先頭が NNN-NN-N 形式でない）: {file_name}"
                )
            sheets_client.append_output_record(
                management_number=mgmt_num,
                clinic_name=clinic_name,
                person_name=person_name,
                sample_name=sample_title,
                drive_url=upload_result["webViewLink"],
                sheet_name=cfg.output_sheet_name,
            )

            logger.info(
                f"完了: {mgmt_num} / {clinic_name} / {person_name} / {sample_title}"
            )
            stats["success"] += 1

        except Exception as e:
            logger.error(f"処理エラー: {file_name} - {e}", exc_info=True)
            stats["error"] += 1

    logger.info("=== 処理完了 ===")
    logger.info(
        f"成功: {stats['success']}件, "
        f"スキップ: {stats['skip']}件, "
        f"エラー: {stats['error']}件"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="じっせん君コメントシステム（通常モード）")
    parser.add_argument(
        "--test-count", type=int, default=0,
        help="テスト件数（0=全件処理）",
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
        test_count=args.test_count,
        profile_name=args.profile,
        target_folder=args.target_folder,
    )


if __name__ == "__main__":
    main()
