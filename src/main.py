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
from typing import Any

from src.utils import (
    setup_logging,
    ensure_fonts,
    extract_clinic_number,
    extract_management_number,
    is_attachment_filename,
)
from src import discover, drive_client, gmail_client, sheets_client
from src import pdf_reader, comment_generator, pdf_creator, pdf_merger
from src.sheets_client import MasterRecord


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

    # 参加者マスターシートをループ開始前に 1 回だけ読み込む。医院名の標準化
    # （フォルダ命名・各種シート列）と Gmail 下書きの TO ルックアップを兼ねる
    # スナップショット。シート未作成なら自動作成（ヘッダーのみ）+ 空リストを
    # 返す。メイン経路と添付資料経路で共有する（毎回読まない）。
    master_records: list[MasterRecord] = sheets_client.read_master_records(
        sheet_name=cfg.master_sheet_name,
    )

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

    # メイン処理ループで構築する管理番号 → (医院番号, 医院名, 個人名) の
    # 対応表。添付資料はこの表を引いて、同じ管理番号のメインと同じ出力フォルダ
    # へコピーする。医院番号と医院名（AI 抽出の生の値）を別々に保持し、添付
    # 資料アップロード時にメインと同じ ``find_or_create_clinic_folder`` を
    # 通じて同じ医院フォルダ（医院番号で識別）に合流させる（P-019）。
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

    # Gmail 下書きは PDF 処理ループの最後にまとめて作成する。同じメール
    # アドレスを持つ複数 PDF を 1 通の下書きに集約するため、ループ中は
    # ``(email, person_name, pdf_path)`` を ``draft_items`` に蓄積するだけ。
    # PDF ファイルはループ終了まで生存させる必要があるので、セッション
    # スコープの ``session_outputs_dir``（後述）に書き出す。
    draft_items: list[dict[str, Any]] = []

    def _collect_draft_item(
        clinic_number: str,
        person_name: str,
        pdf_path: Path,
    ) -> None:
        """PDF アップロード成功後、Gmail 下書き用の (メール, 個人名, PDF) を
        ``draft_items`` に追加する。マスター lookup の例外は警告ログを出して
        握りつぶす（fail-soft、PDF 処理は止めない）。
        """
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

    # セッションスコープの出力ディレクトリ。各 PDF の最終出力をここに書き出し、
    # ループ終了後の集約下書き作成まで生存させる（メールアドレスで集約するため
    # 全 PDF が処理し終わるまで一時ファイルを保持する必要がある）。コメント
    # ページ等の中間ファイルは従来通り iteration スコープの tempdir に置く。
    # ``with`` ブロックで囲むと既存ループ全体に追加インデントが入るため、
    # ``mkdtemp`` + 関数末尾の ``shutil.rmtree`` でクリーンアップする。
    import shutil
    session_outputs_dir = Path(tempfile.mkdtemp(prefix="aicomment_outputs_"))

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
            clinic_name_from_ai = metadata["clinic_name"] or "unknown_clinic"
            person_name = metadata["person_name"] or "unknown_person"
            sample_title = metadata["sample_title"] or Path(file_name).stem
            comment = metadata["comment"]

            # 医院番号（管理番号の先頭セグメント）を抽出する。医院フォルダの
            # 識別は医院番号のみで行うため（P-019）、医院番号と医院名を別々に
            # ``upload_pdf_to_clinic_person`` に渡す。医院番号が空（管理番号
            # なし）の場合は ``find_or_create_clinic_folder`` 側で旧来の名前
            # ベース照合にフォールバックする。
            clinic_number = extract_clinic_number(file_name)
            mgmt_num = extract_management_number(file_name)

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

            # 中間ファイル（コメントページ）は iteration スコープの tempdir、
            # 最終出力 PDF は session_outputs_dir に書き出してループ終了後
            # まで生存させる。同じ個人の複数 PDF を 1 通の下書きに集約する
            # ため。
            pdf_subdir = session_outputs_dir / f"main_{i:05d}"
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
                    output_root_folder_id=cfg.output_folder_id,
                    clinic_number=clinic_number,
                    clinic_name=clinic_name,
                    person_name=person_name,
                    file_name=output_filename,
                    # マスター由来の確定医院名なら既存フォルダ名も同期させる
                    clinic_name_authoritative=bool(clinic_name_from_master),
                )

                # 管理番号は処理対象選定時に抽出・検証済み（空でないことが保証される）。
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
                    clinic_number, clinic_name,
                    upload_result["clinic_folder_id"],
                )

            # Gmail 下書きはここで作らず、 ``draft_items`` に蓄積する。
            # ループ終了後にメールアドレスでグルーピングして 1 通にまとめる。
            _collect_draft_item(
                clinic_number=clinic_number,
                person_name=person_name,
                pdf_path=output_path,
            )

            # 添付資料パススルー用の対応表を構築。同じ管理番号の添付資料を
            # このメインと同じ出力フォルダへコピーするために使う。医院名は
            # 既にマスター標準化済みの値が入っているため、添付資料経路は
            # この表をそのまま再利用すれば同じフォルダ・同じ列値になる。
            case_map[mgmt_num] = (clinic_number, clinic_name, person_name)

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

        # case_map は (医院番号, 医院名, 個人名)。医院名はメイン処理ループで
        # 既に参加者マスター標準化済み（未登録時は AI 抽出値）。添付資料は
        # メインと同じ管理番号 = 同じ医院番号なので、``find_or_create_clinic_folder``
        # 経由でメインと同じ医院フォルダへコピーされる。
        clinic_number, clinic_name, person_name = case
        try:
            # 元 PDF のバイト列をそのまま再アップロード（マージ・コメント
            # ページ生成はしない）。出力ファイル名は元のまま（make_output_filename
            # は使わない＝「そのまま反映」）。添付資料 PDF は session_outputs_dir
            # に書いてループ終了後の集約下書き作成まで生存させる。
            pdf_data = drive_client.download_pdf(file_id)
            att_subdir = session_outputs_dir / f"attach_{file_id}"
            att_subdir.mkdir(parents=True, exist_ok=True)
            attachment_path = att_subdir / file_name
            attachment_path.write_bytes(pdf_data)
            upload_result = drive_client.upload_pdf_to_clinic_person(
                file_path=attachment_path,
                output_root_folder_id=cfg.output_folder_id,
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
                sheet_name=cfg.output_sheet_name,
            )

            # 添付資料経路も Gmail 下書き用に蓄積する。同じ個人宛のメイン
            # PDF と同じグループに集約され、1 通の下書きに複数添付される。
            _collect_draft_item(
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

    # ── 集約下書き作成 ──
    # メイン + 添付資料ループで蓄積した ``draft_items`` をメールアドレスで
    # グルーピングし、グループごとに 1 通の Gmail 下書きを作成する。同じ
    # 個人の複数 PDF が 1 通の下書きに集約される（複数添付）。メールが
    # 空の項目は集約せず、PDF ごとに宛先空の下書きを作る（手動補完前提）。
    try:
        _create_grouped_drafts_for_run(draft_items, gmail_client)
    finally:
        # PDF 一時ファイルは下書き作成が終わったので削除
        shutil.rmtree(session_outputs_dir, ignore_errors=True)

    logger.info("=== 処理完了 ===")
    logger.info(
        f"成功: {stats['success']}件（メイン + 添付資料）, "
        f"テキスト抽出失敗: {stats['skip']}件, "
        f"管理番号なしスキップ: {stats['skip_no_number']}件, "
        f"処理済みスキップ: {stats['skip_processed']}件, "
        f"添付資料メイン不在スキップ: {stats['skip_attachment_orphan']}件, "
        f"エラー: {stats['error']}件"
    )


def _create_grouped_drafts_for_run(
    draft_items: list[dict[str, Any]],
    gmail_module: Any,
) -> None:
    """``draft_items`` をメールアドレスでグループ化して下書きを作成する。

    - メールアドレスが空でない項目: アドレスごとに 1 通の下書きにまとめる
      （複数 PDF はそのまま複数添付になる）
    - メールアドレスが空の項目: グループ化キーがないため項目ごとに 1 通の
      宛先空の下書きを作る（手動で補完してもらう運用）

    例外は警告ログを出して握りつぶし、他グループの下書き作成は続行する
    （fail-soft、Gmail API の一過性エラーで全滅しないよう）。
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
