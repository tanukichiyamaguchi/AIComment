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
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

from google.auth.exceptions import RefreshError

from src.utils import (
    setup_logging,
    ensure_fonts,
    extract_clinic_number,
    extract_management_number,
)
from src import config
from src import discover, drive_client, gmail_client, run_common, sheets_client
from src import pdf_reader, comment_generator, pdf_creator, pdf_merger
from src.comment_generator import (
    PermanentRunFailureError,
    permanent_failure_message,
)
from src.sheets_client import MasterRecord


def run(
    test_count: int = 0,
    profile_name: str | None = None,
    target_folder: str | None = None,
    max_runtime_minutes: int = 320,
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
        max_runtime_minutes: 実行時間バジェット（分、0=無制限）。通常モードは
            1 件ずつ逐次処理のため、1000 件規模では GHA の 6h ジョブ上限
            （timeout-minutes: 360）を超えてジョブ kill され、**下書き作成前の
            成果物が失われる**。バジェット到達でループを安全に打ち切り、
            処理済み分の下書き作成・完了マーカーまで完走させる（未処理分は
            出力一覧シート未記録のため、再実行の増分処理で自然に続きから
            処理される）。既定 320 分 = GHA 上限に 40 分のマージン。
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

    main_files, attachment_files = run_common.split_main_and_attachments(
        pdf_files, logger,
    )

    # 重複判定（増分処理）— download / Claude API 呼び出しの前に実施する。
    # 管理番号はファイル名から取れるため、コストのかかる処理に入る前に分類できる。
    # 重複スキップは無条件（bypass なし）。再処理が必要なら出力シートの行を手動削除する。
    # この集合は実行開始時の 1 スナップショット。メイン処理が同一実行内の添付資料
    # 判定に影響しないよう、ループ中は更新しない。
    processed = sheets_client.get_processed_management_numbers(
        sheet_name=cfg.output_sheet_name,
    )

    # 医院フォルダURLシート（``<出力シート名>_医院``）への記録は
    # ClinicFolderRecorder が担う（記録済みスナップショット + 実行内重複防止）。
    clinic_recorder = run_common.ClinicFolderRecorder(
        sheets_client, cfg.output_sheet_name,
    )

    # 参加者マスターシートをループ開始前に 1 回だけ読み込む。医院名の標準化
    # （フォルダ命名・各種シート列）と Gmail 下書きの TO ルックアップを兼ねる
    # スナップショット。シート未作成なら自動作成（ヘッダーのみ）+ 空リストを
    # 返す。メイン経路と添付資料経路で共有する（毎回読まない）。
    master_records: list[MasterRecord] = sheets_client.read_master_records(
        sheet_name=cfg.master_sheet_name,
    )
    # target_folder モード（``master_sheet_strict=True``）でタブ不在 / 0 件なら
    # PDF 処理ループに入る前に HARD FAIL（``MasterSheetEmptyError``）。
    # 「中止」マーカーを追記して GHA を非ゼロ終了させる。
    try:
        run_common.require_non_empty_master(
            master_records, cfg.master_sheet_name,
            cfg.master_sheet_strict, logger,
        )
    except run_common.MasterSheetEmptyError as e:
        run_common.append_completion_marker_safe(
            sheets_client, cfg.output_sheet_name,
            f"中止（参加者マスタータブ未準備: '{cfg.master_sheet_name}'）",
            logger,
        )
        raise

    targets, skip_no_number, skip_processed = run_common.select_new_targets(
        main_files, processed, logger,
    )
    stats["skip_no_number"] += skip_no_number
    stats["skip_processed"] += skip_processed

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

    # ラン全体を停止する恒久エラー（残高不足 / 認証 / 権限）を検知したら、
    # この変数に例外を入れてループを break する。以降の添付資料パススルーも
    # スキップし、既に成功した分の下書き作成と一時ファイル削除だけ行う。
    # 例外クラスは直接 import する（comment_generator モジュールがテストで
    # モックされても except 節が壊れないよう、モジュール経由参照を避ける）。
    run_halted: PermanentRunFailureError | None = None

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
        """Gmail 下書き用の (メール, 個人名, PDF) を ``draft_items`` に蓄積する。

        実装は ``run_common.collect_draft_item`` に委譲する（Batch モードと
        共通、fail-soft）。
        """
        run_common.collect_draft_item(
            draft_items, master_records, sheets_client,
            clinic_number=clinic_number,
            person_name=person_name,
            pdf_path=pdf_path,
        )

    # セッションスコープの出力ディレクトリ。各 PDF の最終出力をここに書き出し、
    # ループ終了後の集約下書き作成まで生存させる（メールアドレスで集約するため
    # 全 PDF が処理し終わるまで一時ファイルを保持する必要がある）。コメント
    # ページ等の中間ファイルは従来通り iteration スコープの tempdir に置く。
    # ``with`` ブロックで囲むと既存ループ全体に追加インデントが入るため、
    # ``mkdtemp`` + 関数末尾の ``shutil.rmtree`` でクリーンアップする。
    session_outputs_dir = Path(tempfile.mkdtemp(prefix="aicomment_outputs_"))

    # Gmail 下書きが OFF のときは、出力 PDF を集約下書きまで保持する必要が
    # 無い。アップロード成功のたびにサブディレクトリを即削除し、大量ランで
    # /tmp が圧迫されるのを防ぐ（長時間・大量ラン対策）。
    keep_outputs = config.ENABLE_GMAIL_DRAFTS

    # 一時ディレクトリの掃除は mkdtemp 直後からの try/finally で保証する。
    # 以前は末尾のドラフト作成呼び出しだけを finally が包んでおり、メイン
    # ループ〜添付資料ループの未ガード箇所で例外が出ると rmtree に到達せず、
    # ローカル/Codespaces の反復実行で一時ディレクトリが蓄積した。
    try:

        # 実行時間バジェット（GHA 6h ジョブ kill による成果物ロスト防止）。
        deadline = (
            time.monotonic() + max_runtime_minutes * 60
            if max_runtime_minutes > 0 else None
        )
        time_budget_hit = False

        for i, pdf_file in enumerate(targets, start=1):
            if deadline is not None and time.monotonic() > deadline:
                time_budget_hit = True
                logger.warning(
                    f"実行時間バジェット（{max_runtime_minutes}分）に到達しました。"
                    f"残り {len(targets) - i + 1} 件はループを打ち切り、処理済み分の"
                    f"下書き作成・完了マーカーを完走させます。未処理分は再実行の"
                    f"増分処理で続きから処理されます。"
                )
                break

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
                        clinic_name_authoritative=clinic_name_is_authoritative,
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
                    clinic_recorder.record(
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

                # ドラフト OFF なら出力 PDF はもう不要（Drive 保存済み）。
                # 逐次削除してディスク滞留を防ぐ。
                if not keep_outputs:
                    shutil.rmtree(pdf_subdir, ignore_errors=True)

            except PermanentRunFailureError as e:
                # ラン全体で恒久的に失敗する条件（残高不足 / 認証 / 権限）を検知。
                # 以降のどの PDF も必ず同じエラーになるため、残りを処理せず即停止する
                # （無駄な API 呼び出し + エラーログの乱立を防ぐ）。既に成功・記録
                # 済みの件は影響なし。未処理分は出力一覧シート未記録なので、残高
                # チャージ後の再実行で処理対象として拾える。
                run_halted = e
                logger.error(
                    permanent_failure_message(
                        processed=stats["success"], detail=str(e),
                    )
                )
                break

            except RefreshError as e:
                # Google 認証の恒久失敗（OAuth トークン失効/取り消し）。Anthropic の
                # 恒久エラーと同様、以降の全 PDF も必ず失敗するため即停止する
                # （per-item fail-soft で数百件のエラーログを乱立させない）。
                run_halted = PermanentRunFailureError(
                    f"Google 認証エラー（OAuth トークン失効の可能性）: {e}。"
                    f"GOOGLE_OAUTH_TOKEN_JSON を再発行して再実行してください。"
                )
                logger.error(str(run_halted))
                break

            except Exception as e:
                logger.error(f"処理エラー: {file_name} - {e}", exc_info=True)
                stats["error"] += 1

        # ── 添付資料パススルー経路 ──
        # 添付資料 PDF は AI 処理（テキスト抽出 / Claude API / コメントページ生成
        # / 結合）を一切せず、同じ管理番号のメインと同じ出力フォルダへ元ファイル
        # のままコピーする。メイン処理が完了した後にまとめて処理する。
        # ラン停止中（恒久エラー検知）は添付資料も処理せずスキップする。
        if run_halted is not None:
            logger.warning(
                "恒久エラーのためラン停止中: 添付資料パススルーをスキップします"
                f"（未処理 {len(attachment_files)}件）"
            )
        # 添付資料の重複判定は「その添付資料自体が出力一覧シートに記録済みか」
        # （``【添付資料】<元名>`` マーカー）で行う。以前の「メインの管理番号が
        # 処理済みなら添付もスキップ」方式は、メイン処理後に添付だけ後から Drive
        # に追加されたケース等で添付資料が恒久ロストする欠陥があった。
        recorded_attachments: set[str] = set()
        if attachment_files and run_halted is None:
            logger.info(f"--- 添付資料パススルー: {len(attachment_files)}件 ---")
            recorded_attachments = sheets_client.get_recorded_attachment_names(
                sheet_name=cfg.output_sheet_name,
            )
        for pdf_file in [] if run_halted is not None else attachment_files:
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

            if f"【添付資料】{file_name}" in recorded_attachments:
                logger.info(f"記録済みのため添付資料をスキップ: {mgmt_num} ({file_name})")
                stats["skip_processed"] += 1
                continue

            # case_map（同一ラン内で処理されたメイン）を最優先。無ければ参加者
            # マスターだけで出力先を解決するフォールバック（メインが過去ラン処理
            # 済みで添付だけ後から追加されたケースの恒久ロスト防止）。
            case = case_map.get(mgmt_num)
            case_from_master = False
            if case is None:
                case = run_common.resolve_case_via_master(
                    sheets_client, master_records, mgmt_num, logger,
                )
                case_from_master = case is not None
            if case is None:
                logger.warning(
                    f"対応するメイン実践事例 PDF がこの実行で処理されておらず、"
                    f"参加者マスターにも管理番号 {mgmt_num} が未登録のため"
                    f"スキップ: {file_name}"
                )
                stats["skip_attachment_orphan"] += 1
                continue

            # case_map は (医院番号, 医院名, 個人名)。医院名はメイン処理ループで
            # 既に参加者マスター標準化済み（未登録時は AI 抽出値）。添付資料は
            # メインと同じ管理番号 = 同じ医院番号なので、``find_or_create_clinic_folder``
            # 経由でメインと同じ医院フォルダへコピーされる。
            clinic_number, clinic_name, person_name = case
            run_common.passthrough_attachment(
                drive_module=drive_client,
                sheets_module=sheets_client,
                logger=logger,
                stats=stats,
                collect_draft=_collect_draft_item,
                file_id=file_id,
                file_name=file_name,
                mgmt_num=mgmt_num,
                clinic_number=clinic_number,
                clinic_name=clinic_name,
                person_name=person_name,
                output_folder_id=cfg.output_folder_id,
                output_sheet_name=cfg.output_sheet_name,
                session_outputs_dir=session_outputs_dir,
                # マスター由来の確定名なら既存フォルダ名の同期を許可
                clinic_name_authoritative=case_from_master,
            )
            # ドラフト OFF なら添付コピーの一時ファイルも逐次削除
            # （ディスク滞留防止）。
            if not keep_outputs:
                shutil.rmtree(
                    session_outputs_dir / f"attach_{file_id}", ignore_errors=True,
                )

        # ── 集約下書き作成 ──
        # メイン + 添付資料ループで蓄積した ``draft_items`` をメールアドレスで
        # グルーピングし、グループごとに 1 通の Gmail 下書きを作成する。同じ
        # 個人の複数 PDF が 1 通の下書きに集約される（複数添付）。メールが
        # 空の項目は集約せず、PDF ごとに宛先空の下書きを作る（手動補完前提）。
        # Gmail 下書きの ON/OFF 判定は ``_create_grouped_drafts_for_run`` 内で行う
        # （ENABLE_GMAIL_DRAFTS=false ならスキップ）。一時ファイル削除は必ず行う。
        # 既に成功した分の下書き作成と一時ファイル削除は、ラン停止時でも行う
        # （完了済みの成果物は運用者に届ける）。
        _create_grouped_drafts_for_run(draft_items, gmail_client)
    finally:
        # PDF 一時ファイルは下書き作成が終わったので削除（例外経路でも必ず）
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

    # 出力一覧シートの最終行に「完了」マーカーを 1 行追加（運用者がシート上で
    # 一目で完了を把握できるように）。書き込み自体は fail-soft（ログ・本処理は
    # 既に終わっているため、マーカー失敗で例外を上げない）。ラン停止時は
    # 「中止」マーカーにして、シート上でも停止が分かるようにする。
    if run_halted is not None:
        marker_summary = (
            f"中止（残高/認証/権限エラー） 成功 {stats['success']}件 / "
            f"未処理あり・再実行が必要"
        )
    elif time_budget_hit:
        marker_summary = (
            f"時間切れ部分完了 成功 {stats['success']}件 / "
            f"エラー {stats['error']}件 / 未処理あり・再実行で続きから処理"
        )
    else:
        marker_summary = (
            f"成功 {stats['success']}件 / "
            f"エラー {stats['error']}件 / "
            f"スキップ {stats['skip'] + stats['skip_no_number'] + stats['skip_processed'] + stats['skip_attachment_orphan']}件"
        )
    run_common.append_completion_marker_safe(
        sheets_client, cfg.output_sheet_name, marker_summary, logger,
    )

    # ラン全体を停止する恒久エラーを検知していた場合、ここで再送出して
    # プロセスを異常終了させる（GitHub Actions のジョブを失敗で終わらせ、
    # 運用者に再実行を促す）。成果物の flush とマーカー追記は上で完了済み。
    if run_halted is not None:
        raise run_halted


def _create_grouped_drafts_for_run(
    draft_items: list[dict[str, Any]],
    gmail_module: Any,
) -> None:
    """``draft_items`` をメールアドレスでグループ化して下書きを作成する。

    実装は ``run_common.create_grouped_drafts`` に委譲する（Batch モードと
    共通、P-023）。``gmail_module`` はテスト注入用に引数で受ける。
    """
    run_common.create_grouped_drafts(draft_items, gmail_module)


def main() -> None:
    parser = argparse.ArgumentParser(description="じっせん君コメントシステム（通常モード）")
    parser.add_argument(
        "--test-count", type=int, default=0,
        help="テスト件数（0=全件処理）。重複・管理番号なしを除外した新規 PDF に適用",
    )
    parser.add_argument(
        "--max-runtime-minutes", type=int, default=320,
        help=(
            "実行時間バジェット（分、0=無制限）。到達でループを安全に打ち切り、"
            "処理済み分の下書き・完了マーカーまで完走させる（GHA 6h kill 対策）"
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
        test_count=args.test_count,
        profile_name=args.profile,
        target_folder=args.target_folder,
        max_runtime_minutes=args.max_runtime_minutes,
    )


if __name__ == "__main__":
    main()
