"""通常モードのエントリポイント。1件ずつ処理する。"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

from src.utils import setup_logging, mask_email, ensure_fonts
from src.config import LOGS_DIR
from src import drive_client, sheets_client, gmail_client
from src import pdf_reader, comment_generator, pdf_creator, pdf_merger
from src.matcher import match_record


def run(test_count: int = 0) -> None:
    """通常モードのメイン処理。

    Args:
        test_count: テスト件数（0=全件処理）
    """
    logger = setup_logging()
    logger.info("=== じっせん君コメントシステム（通常モード）開始 ===")

    # フォント準備
    ensure_fonts()

    # 1. Google認証 & データ取得
    logger.info("Step 1: データ取得")
    records = sheets_client.get_unprocessed_records()
    pdf_files = drive_client.list_pdfs()

    if test_count > 0:
        pdf_files = pdf_files[:test_count]
        logger.info(f"テストモード: {test_count}件に制限")

    logger.info(f"処理対象: PDF {len(pdf_files)}件, 未処理レコード {len(records)}件")

    # 統計
    stats = {"success": 0, "skip": 0, "error": 0}

    # 2. 1件ずつ処理
    for i, pdf_file in enumerate(pdf_files, start=1):
        file_id = pdf_file["id"]
        file_name = pdf_file["name"]
        logger.info(f"--- [{i}/{len(pdf_files)}] {file_name} ---")

        try:
            # 2.1 PDFダウンロード & テキスト抽出
            pdf_data = drive_client.download_pdf(file_id)
            pdf_text = pdf_reader.extract_text(pdf_data)
            if not pdf_text:
                logger.warning(f"テキスト抽出失敗: {file_name}")
                stats["skip"] += 1
                continue

            # 2.2 スプレッドシートとマッチング
            record = match_record(pdf_text, records, pdf_filename=file_name)
            if not record:
                logger.warning(f"マッチング失敗: {file_name}")
                stats["skip"] += 1
                continue

            # ステータスを「処理中」に更新
            sheets_client.update_status(record.row_number, "処理中")

            # 2.3 Claude APIでコメント生成
            comment = comment_generator.generate_comment(
                clinic_name=record.clinic_name,
                person_name=record.person_name,
                pdf_text=pdf_text,
            )

            # 2.4 コメントページPDF生成
            with tempfile.TemporaryDirectory() as tmpdir:
                comment_page_path = Path(tmpdir) / "comment_page.pdf"
                pdf_creator.create_comment_page(
                    comment=comment,
                    clinic_name=record.clinic_name,
                    person_name=record.person_name,
                    output_path=comment_page_path,
                )

                # 2.5 PDF結合
                output_filename = pdf_merger.make_output_filename(
                    record.clinic_name, record.person_name
                )
                output_path = Path(tmpdir) / output_filename
                pdf_merger.merge_pdfs(
                    original_pdf_data=pdf_data,
                    comment_page_path=comment_page_path,
                    output_path=output_path,
                )

                # 2.6 Gmail下書き作成
                if record.email:
                    gmail_client.create_draft(
                        to_email=record.email,
                        person_name=record.person_name,
                        pdf_path=output_path,
                    )
                    logger.info(
                        f"下書き作成完了: {record.person_name}様 "
                        f"→ {mask_email(record.email)}"
                    )
                else:
                    logger.warning(
                        f"メールアドレスなし: {record.clinic_name} {record.person_name}"
                    )

            # 2.7 ステータス「完了」に更新
            sheets_client.update_status(record.row_number, "完了")
            stats["success"] += 1

        except Exception as e:
            logger.error(f"処理エラー: {file_name} - {e}", exc_info=True)
            stats["error"] += 1
            # ステータスを「エラー」に更新（recordが取得できている場合）
            try:
                if record:
                    sheets_client.update_status(
                        record.row_number, f"エラー: {str(e)[:50]}"
                    )
            except Exception:
                pass

    # 結果サマリ
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
    args = parser.parse_args()
    run(test_count=args.test_count)


if __name__ == "__main__":
    main()
