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

from src.utils import (
    setup_logging,
    ensure_fonts,
    extract_clinic_number,
    extract_management_number,
    is_attachment_filename,
)
from src import discover, drive_client, sheets_client
from src import pdf_reader, comment_generator, pdf_creator, pdf_merger


def run(
    test_count: int = 0,
    profile_name: str | None = None,
    target_folder: str | None = None,
) -> None:
    """通常モードのメイン処理。

    増分処理（重複検知）対応:
        入力フォルダに PDF を継続追加して再実行する運用で、出力先の重複を
        防ぐ。管理番号（ファイル名先頭の ``NNN-NN-N``）をキーに、出力一覧
        シートに既存の PDF は download / Claude API 呼び出しの前に無条件で
        スキップする。管理番号を持たない PDF は重複検知が原理的に不可能な
        ため、毎回再処理せず警告つきでスキップする（fail-loud）。
        既に処理済みの PDF を再処理したい場合は、出力一覧シートの該当行を
        手動で削除すれば、その管理番号は「未処理」扱いに戻り次回実行で
        再処理される。

    添付資料パススルー対応:
        ファイル名に「【添付資料】」を含む PDF は実践事例の補足資料であり、
        AI 処理（テキスト抽出 / Claude API / コメントページ生成 / 結合）を
        一切しない。入力をファイル名で「メイン」と「添付資料」に早期分類し、
        添付資料は別経路で処理する（lessons.md P-016）。メイン PDF を処理
        するループで管理番号 → ``(医院名, 個人名)`` の対応表を作り、添付資料
        は同じ管理番号のメインと同じ ``<医院名>/<個人名>/`` フォルダへ元
        ファイル名のままコピーし、出力一覧シートにも記録する。両経路が合流
        するのは出力（Drive / シート）だけ。

    Args:
        test_count: 新規 PDF を N 件まで処理（0=全件処理）。重複・管理番号
            なしを除外した「処理対象の新規 PDF」に対して適用される。
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
    logger.info(f"PDF一覧: {len(pdf_files)}件")

    stats = {
        "success": 0,
        "skip": 0,
        "skip_no_number": 0,
        "skip_processed": 0,
        "skip_attachment_orphan": 0,
        "error": 0,
    }

    # 入力 PDF を「メイン実践事例」と「添付資料」に早期分類する（P-016）。
    # 添付資料は AI 処理せず passthrough 経路で出力へコピーするだけなので、
    # メインループ内の if 分岐ではなく、安価なファイル名判定で別経路に分ける。
    main_files = [f for f in pdf_files if not is_attachment_filename(f["name"])]
    attachment_files = [f for f in pdf_files if is_attachment_filename(f["name"])]
    logger.info(
        f"入力分類: メイン実践事例 {len(main_files)}件 / "
        f"添付資料 {len(attachment_files)}件"
    )

    # 重複判定（増分処理）— download / Claude API 呼び出しの前に実施する。
    # 管理番号はファイル名から取れるため、コストのかかる処理に入る前に分類できる。
    # 重複スキップは無条件（bypass なし）。再処理が必要なら出力シートの行を手動削除する。
    # この集合は実行開始時の 1 スナップショット。メイン処理が同一実行内の添付資料
    # 判定に影響しないよう、ループ中は更新しない。
    processed = sheets_client.get_processed_management_numbers(
        sheet_name=cfg.output_sheet_name,
    )

    # 医院フォルダURLシート（``<出力シート名>_医院``）の記録済み医院番号を
    # 実行開始時に 1 回スナップショットする。ループ中に記録した医院番号は
    # ``clinics_recorded_this_run`` で追跡し、両方に無い医院だけ追記する
    # （同一医院をシートに重複追加しない）。
    clinic_sheet_name = f"{cfg.output_sheet_name}_医院"
    recorded_clinics = sheets_client.get_recorded_clinic_numbers(
        sheet_name=clinic_sheet_name,
    )
    clinics_recorded_this_run: set[str] = set()

    targets: list[dict] = []
    for pdf_file in main_files:
        file_name = pdf_file["name"]
        mgmt_num = extract_management_number(file_name)
        if not mgmt_num:
            logger.warning(
                f"管理番号をファイル名から抽出できないためスキップ"
                f"（先頭が NNN-NN-N 形式でない / 重複検知不可）: {file_name}"
            )
            stats["skip_no_number"] += 1
            continue
        if mgmt_num in processed:
            logger.info(f"処理済みのためスキップ: {mgmt_num} ({file_name})")
            stats["skip_processed"] += 1
            continue
        targets.append(pdf_file)

    if test_count > 0:
        targets = targets[:test_count]
        logger.info(f"テストモード: 新規対象を{test_count}件に制限")

    logger.info(f"処理対象: {len(targets)}件のPDF（新規）")

    # メイン処理ループで構築する管理番号 → (医院フォルダ名, 医院名, 個人名) の
    # 対応表。添付資料はこの表を引いて、同じ管理番号のメインと同じ出力フォルダ
    # へコピーする。医院フォルダ名は ``<医院番号>_<医院名>``（Drive のフォルダ
    # 階層用）、医院名は AI 抽出値そのまま（出力一覧シートの医院名列用）。
    case_map: dict[str, tuple[str, str, str]] = {}

    def _record_clinic_folder(
        clinic_number: str, clinic_name: str, clinic_folder_id: str
    ) -> None:
        """医院フォルダURLシートに医院を 1 行記録する（重複防止込み）。

        ある医院番号が実行開始時のスナップショット（``recorded_clinics``）にも
        同一実行内で記録済みの集合（``clinics_recorded_this_run``）にも無い
        ときだけ追記する。医院番号が空（管理番号抽出不能）の場合は記録しない。
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

    for i, pdf_file in enumerate(targets, start=1):
        file_id = pdf_file["id"]
        file_name = pdf_file["name"]
        logger.info(f"--- [{i}/{len(targets)}] {file_name} ---")

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

            # 医院番号（管理番号の先頭セグメント）を抽出し、医院フォルダ名を
            # ``<医院番号>_<医院名>`` で構築する。医院番号が抽出できない場合は
            # 医院名のみのフォルダ名にフォールバックする。
            clinic_number = extract_clinic_number(file_name)
            clinic_folder_name = (
                f"{clinic_number}_{clinic_name}"
                if clinic_number
                else clinic_name
            )

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
                    clinic_name=clinic_folder_name,
                    person_name=person_name,
                    file_name=output_filename,
                )

            # 管理番号は処理対象選定時に抽出・検証済み（空でないことが保証される）。
            mgmt_num = extract_management_number(file_name)
            sheets_client.append_output_record(
                management_number=mgmt_num,
                clinic_name=clinic_name,
                person_name=person_name,
                sample_name=sample_title,
                drive_url=upload_result["webViewLink"],
                sheet_name=cfg.output_sheet_name,
            )

            # 医院フォルダURLシートに医院を記録（同一医院は 1 行のみ）。
            _record_clinic_folder(
                clinic_number, clinic_name, upload_result["clinic_folder_id"]
            )

            # 添付資料パススルー用の対応表を構築。同じ管理番号の添付資料を
            # このメインと同じ出力フォルダへコピーするために使う。医院
            # フォルダ名（医院番号付き）と医院名（AI 抽出値）の両方を保持する。
            case_map[mgmt_num] = (clinic_folder_name, clinic_name, person_name)

            logger.info(
                f"完了: {mgmt_num} / {clinic_name} / {person_name} / {sample_title}"
            )
            stats["success"] += 1

        except Exception as e:
            logger.error(f"処理エラー: {file_name} - {e}", exc_info=True)
            stats["error"] += 1

    # ── 添付資料パススルー経路 ──
    # 添付資料 PDF は AI 処理（テキスト抽出 / Claude API / コメントページ生成
    # / 結合）を一切せず、同じ管理番号のメインと同じ出力フォルダへ元ファイル
    # のままコピーする。メイン処理が完了した後にまとめて処理する。
    if attachment_files:
        logger.info(f"--- 添付資料パススルー: {len(attachment_files)}件 ---")
    for pdf_file in attachment_files:
        file_id = pdf_file["id"]
        file_name = pdf_file["name"]
        mgmt_num = extract_management_number(file_name)

        if not mgmt_num:
            logger.warning(
                f"管理番号をファイル名から抽出できないため添付資料をスキップ"
                f"（先頭が NNN-NN-N 形式でない）: {file_name}"
            )
            stats["skip_no_number"] += 1
            continue

        # 重複判定は実行開始時のスナップショット（前回実行でコピー済みか）。
        if mgmt_num in processed:
            logger.info(f"処理済みのため添付資料をスキップ: {mgmt_num} ({file_name})")
            stats["skip_processed"] += 1
            continue

        case = case_map.get(mgmt_num)
        if case is None:
            logger.warning(
                f"対応するメイン実践事例 PDF がこの実行で処理されていないため"
                f"スキップ: {file_name}"
            )
            stats["skip_attachment_orphan"] += 1
            continue

        # case_map は (医院フォルダ名, 医院名, 個人名)。添付資料はメインと同じ
        # 管理番号 = 同じ医院番号なので、同じ医院フォルダ（医院番号付き）へ
        # コピーされる。出力一覧シートの医院名列は AI 抽出値（医院番号なし）。
        clinic_folder_name, clinic_name, person_name = case
        try:
            # 元 PDF のバイト列をそのまま再アップロード（マージ・コメント
            # ページ生成はしない）。出力ファイル名は元のまま（make_output_filename
            # は使わない＝「そのまま反映」）。
            pdf_data = drive_client.download_pdf(file_id)
            with tempfile.TemporaryDirectory() as tmpdir:
                attachment_path = Path(tmpdir) / file_name
                attachment_path.write_bytes(pdf_data)
                upload_result = drive_client.upload_pdf_to_clinic_person(
                    file_path=attachment_path,
                    output_root_folder_id=cfg.output_folder_id,
                    clinic_name=clinic_folder_name,
                    person_name=person_name,
                    file_name=file_name,
                )

            sheets_client.append_output_record(
                management_number=mgmt_num,
                clinic_name=clinic_name,
                person_name=person_name,
                sample_name=f"【添付資料】{file_name}",
                drive_url=upload_result["webViewLink"],
                sheet_name=cfg.output_sheet_name,
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

    logger.info("=== 処理完了 ===")
    logger.info(
        f"成功: {stats['success']}件（メイン + 添付資料）, "
        f"テキスト抽出失敗: {stats['skip']}件, "
        f"管理番号なしスキップ: {stats['skip_no_number']}件, "
        f"処理済みスキップ: {stats['skip_processed']}件, "
        f"添付資料メイン不在スキップ: {stats['skip_attachment_orphan']}件, "
        f"エラー: {stats['error']}件"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="じっせん君コメントシステム（通常モード）")
    parser.add_argument(
        "--test-count", type=int, default=0,
        help="テスト件数（0=全件処理）。重複・管理番号なしを除外した新規 PDF に適用",
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
