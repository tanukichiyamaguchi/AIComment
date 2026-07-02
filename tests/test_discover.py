"""src/discover.py: フォルダ自動検出システムの単体テスト。

3 関数を検証する:
    - ``list_input_subfolders``: 通常 / 空 / pagination（>1000 件）
    - ``resolve_context``: 正常解決 / 見つからない場合の ValueError /
       表記揺れ吸収 / 各種空引数の早期 ValueError
    - ``list_target_folder_names``: ソート済み / 空ディレクトリ
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from src import discover


class TestListInputSubfolders(unittest.TestCase):

    def test_returns_folders_in_single_page(self):
        """1 ページに収まる通常ケース。"""
        service = MagicMock()
        service.files.return_value.list.return_value.execute.return_value = {
            "files": [
                {"id": "id_a", "name": "2024_Q1_実践事例"},
                {"id": "id_b", "name": "2024_Q2_実践事例"},
            ]
            # nextPageToken なし → 1 ループで終了
        }

        result = discover.list_input_subfolders("root_id", service=service)

        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["id"], "id_a")
        self.assertEqual(result[1]["name"], "2024_Q2_実践事例")
        # 1 ページで終わるので files().list は 1 回だけ
        self.assertEqual(service.files.return_value.list.call_count, 1)

    def test_returns_empty_list_when_no_folders(self):
        service = MagicMock()
        service.files.return_value.list.return_value.execute.return_value = {
            "files": []
        }

        result = discover.list_input_subfolders("root_id", service=service)

        self.assertEqual(result, [])

    def test_follows_pagination_across_multiple_pages(self):
        """P-010 ルール: nextPageToken を辿って全ページを取得する。

        1 ページ目に 1000 件 + nextPageToken="page2"、
        2 ページ目に 500 件 + nextPageToken なし → 全 1500 件返す。
        """
        service = MagicMock()
        page1 = {
            "files": [{"id": f"id_{i}", "name": f"folder_{i:04d}"} for i in range(1000)],
            "nextPageToken": "page2",
        }
        page2 = {
            "files": [{"id": f"id_{i}", "name": f"folder_{i:04d}"} for i in range(1000, 1500)],
            # nextPageToken なし
        }
        service.files.return_value.list.return_value.execute.side_effect = [page1, page2]

        result = discover.list_input_subfolders("root_id", service=service)

        self.assertEqual(len(result), 1500)
        # 2 ページ取得 → list 呼び出しも 2 回
        self.assertEqual(service.files.return_value.list.call_count, 2)
        # 2 回目の呼び出しに pageToken="page2" が渡されている
        second_call_kwargs = service.files.return_value.list.call_args_list[1].kwargs
        self.assertEqual(second_call_kwargs.get("pageToken"), "page2")

    def test_passes_shared_drive_flags(self):
        """共有ドライブ対応: supportsAllDrives / includeItemsFromAllDrives が必須。"""
        service = MagicMock()
        service.files.return_value.list.return_value.execute.return_value = {
            "files": []
        }

        discover.list_input_subfolders("root_id", service=service)

        list_kwargs = service.files.return_value.list.call_args.kwargs
        self.assertTrue(list_kwargs.get("supportsAllDrives"))
        self.assertTrue(list_kwargs.get("includeItemsFromAllDrives"))

    def test_passes_num_retries_for_transient_errors(self):
        """一過性エラー対策: files.list.execute に num_retries が渡る（P-017）。"""
        service = MagicMock()
        service.files.return_value.list.return_value.execute.return_value = {
            "files": []
        }

        discover.list_input_subfolders("root_id", service=service)

        list_execute = service.files.return_value.list.return_value.execute
        self.assertEqual(
            list_execute.call_args.kwargs.get("num_retries"),
            discover.GOOGLE_API_NUM_RETRIES,
        )

    def test_query_filters_to_folder_mime_type_only(self):
        service = MagicMock()
        service.files.return_value.list.return_value.execute.return_value = {
            "files": []
        }

        discover.list_input_subfolders("parent_xxx", service=service)

        query = service.files.return_value.list.call_args.kwargs["q"]
        self.assertIn("parent_xxx", query)
        self.assertIn("application/vnd.google-apps.folder", query)
        self.assertIn("trashed=false", query)

    def test_raises_on_empty_input_root_id(self):
        with self.assertRaises(ValueError):
            discover.list_input_subfolders("", service=MagicMock())


class TestResolveContext(unittest.TestCase):

    def _service_with_folders(self, folders: list[dict]) -> MagicMock:
        """list_input_subfolders 用のレスポンスを返すサービスを作る。"""
        service = MagicMock()
        service.files.return_value.list.return_value.execute.return_value = {
            "files": folders
        }
        # find_or_create_folder は新規作成パスに入った場合の戻り値
        service.files.return_value.create.return_value.execute.return_value = {
            "id": "created_output_id"
        }
        return service

    def test_resolves_normal_case(self):
        service = self._service_with_folders([
            {"id": "input_id_q1", "name": "2024_Q1_実践事例"},
            {"id": "input_id_q2", "name": "2024_Q2_実践事例"},
        ])

        ctx = discover.resolve_context(
            target_folder="2024_Q1_実践事例",
            input_root_id="input_root",
            output_root_id="output_root",
            spreadsheet_id="spreadsheet_xxx",
            service_drive=service,
        )

        self.assertEqual(ctx.target_folder_name, "2024_Q1_実践事例")
        self.assertEqual(ctx.input_folder_id, "input_id_q1")
        # 出力フォルダ ID は find_or_create_folder（同じ service 経由）が返した値
        # ここでは output_root の list で「2024_Q1_実践事例」は見つからない
        # （input_root と output_root の files() は同じ mock を共有しているが、
        #  output_folder 用の find_or_create で作成パスに入って "created_output_id"）
        self.assertEqual(ctx.output_sheet_name, "2024_Q1_実践事例")

    def test_resolves_with_whitespace_variation(self):
        """表記揺れ（半角空白の有無）を吸収する。"""
        service = self._service_with_folders([
            {"id": "input_id", "name": "2024 Q1 実践事例"},  # 半角スペース入り
        ])

        ctx = discover.resolve_context(
            target_folder="2024Q1実践事例",  # スペースなし
            input_root_id="input_root",
            output_root_id="output_root",
            spreadsheet_id="spreadsheet_xxx",
            service_drive=service,
        )

        # マッチした場合は Drive 上の元表記を採用
        self.assertEqual(ctx.target_folder_name, "2024 Q1 実践事例")
        self.assertEqual(ctx.input_folder_id, "input_id")
        # シートタブ名も Drive 上の元表記から派生する
        self.assertEqual(ctx.output_sheet_name, "2024 Q1 実践事例")

    def test_resolves_with_fullwidth_variation(self):
        """全角/半角の表記揺れも吸収する（NFKC 正規化）。"""
        service = self._service_with_folders([
            {"id": "input_id", "name": "２０２４Q1"},  # 全角数字
        ])

        ctx = discover.resolve_context(
            target_folder="2024Q1",  # 半角数字
            input_root_id="input_root",
            output_root_id="output_root",
            spreadsheet_id="spreadsheet_xxx",
            service_drive=service,
        )

        # 元表記（全角）を採用
        self.assertEqual(ctx.target_folder_name, "２０２４Q1")
        self.assertEqual(ctx.input_folder_id, "input_id")

    def test_raises_when_target_not_found(self):
        service = self._service_with_folders([
            {"id": "x", "name": "別のフォルダ"},
            {"id": "y", "name": "もうひとつ"},
        ])

        with self.assertRaises(ValueError) as cm:
            discover.resolve_context(
                target_folder="存在しないフォルダ",
                input_root_id="input_root",
                output_root_id="output_root",
                spreadsheet_id="spreadsheet_xxx",
                service_drive=service,
            )
        # エラーメッセージに利用可能な候補が含まれている
        message = str(cm.exception)
        self.assertIn("存在しないフォルダ", message)
        self.assertIn("別のフォルダ", message)
        self.assertIn("もうひとつ", message)

    def test_raises_when_target_folder_empty(self):
        with self.assertRaises(ValueError):
            discover.resolve_context(
                target_folder="",
                input_root_id="input_root",
                output_root_id="output_root",
                spreadsheet_id="spreadsheet_xxx",
                service_drive=MagicMock(),
            )

    def test_raises_when_input_root_empty(self):
        with self.assertRaises(ValueError):
            discover.resolve_context(
                target_folder="x",
                input_root_id="",
                output_root_id="output_root",
                spreadsheet_id="spreadsheet_xxx",
                service_drive=MagicMock(),
            )

    def test_raises_when_output_root_empty(self):
        with self.assertRaises(ValueError):
            discover.resolve_context(
                target_folder="x",
                input_root_id="input_root",
                output_root_id="",
                spreadsheet_id="spreadsheet_xxx",
                service_drive=MagicMock(),
            )

    def test_raises_when_spreadsheet_id_empty(self):
        with self.assertRaises(ValueError):
            discover.resolve_context(
                target_folder="x",
                input_root_id="input_root",
                output_root_id="output_root",
                spreadsheet_id="",
                service_drive=MagicMock(),
            )

    def test_reuses_existing_output_folder(self):
        """OUTPUT_ROOT 配下に同名フォルダが既存なら再利用（drive_client 経由）。"""
        service = MagicMock()
        # 1 回目の list（list_input_subfolders）→ input フォルダ
        # 2 回目の list（drive_client.find_or_create_folder）→ output フォルダ既存
        service.files.return_value.list.return_value.execute.side_effect = [
            {"files": [{"id": "input_id", "name": "X"}]},
            {"files": [{"id": "existing_output_id", "name": "X"}]},
        ]

        ctx = discover.resolve_context(
            target_folder="X",
            input_root_id="input_root",
            output_root_id="output_root",
            spreadsheet_id="spreadsheet_xxx",
            service_drive=service,
        )

        self.assertEqual(ctx.output_folder_id, "existing_output_id")
        # 既存があれば create は呼ばれない
        service.files.return_value.create.assert_not_called()

    def test_creates_output_folder_when_not_found(self):
        """OUTPUT_ROOT 配下に同名フォルダが無ければ新規作成する。"""
        service = MagicMock()
        # input には存在 / output には無いので create に進む
        service.files.return_value.list.return_value.execute.side_effect = [
            {"files": [{"id": "input_id", "name": "X"}]},  # input root の list
            {"files": []},  # output root の list（empty → create に進む）
        ]
        service.files.return_value.create.return_value.execute.return_value = {
            "id": "new_output_id"
        }

        ctx = discover.resolve_context(
            target_folder="X",
            input_root_id="input_root",
            output_root_id="output_root",
            spreadsheet_id="spreadsheet_xxx",
            service_drive=service,
        )

        self.assertEqual(ctx.output_folder_id, "new_output_id")
        # create が 1 回呼ばれ、parent_id は output_root
        create_call = service.files.return_value.create.call_args
        self.assertEqual(create_call.kwargs["body"]["parents"], ["output_root"])
        self.assertEqual(create_call.kwargs["body"]["name"], "X")


class TestListTargetFolderNames(unittest.TestCase):

    def test_returns_sorted_names(self):
        service = MagicMock()
        service.files.return_value.list.return_value.execute.return_value = {
            "files": [
                {"id": "a", "name": "zebra"},
                {"id": "b", "name": "apple"},
                {"id": "c", "name": "mango"},
            ]
        }

        result = discover.list_target_folder_names("root_id", service=service)

        self.assertEqual(result, ["apple", "mango", "zebra"])

    def test_returns_empty_when_no_folders(self):
        service = MagicMock()
        service.files.return_value.list.return_value.execute.return_value = {
            "files": []
        }

        result = discover.list_target_folder_names("root_id", service=service)

        self.assertEqual(result, [])

    def test_japanese_names_sorted_by_unicode(self):
        """日本語フォルダ名も sorted() で安定順序になる（Unicode コードポイント順）。"""
        service = MagicMock()
        service.files.return_value.list.return_value.execute.return_value = {
            "files": [
                {"id": "a", "name": "実践事例_2024_Q3"},
                {"id": "b", "name": "実践事例_2024_Q1"},
                {"id": "c", "name": "実践事例_2024_Q2"},
            ]
        }

        result = discover.list_target_folder_names("root_id", service=service)

        self.assertEqual(result, [
            "実践事例_2024_Q1",
            "実践事例_2024_Q2",
            "実践事例_2024_Q3",
        ])


class TestServiceInjection(unittest.TestCase):
    """テスト時に外部から渡した service を尊重する（Drive 認証を実行しない）。"""

    @patch("src.discover.drive_client.get_drive_service")
    def test_list_input_subfolders_uses_injected_service(self, mock_get):
        """service 引数があれば drive_client.get_drive_service は呼ばれない。"""
        injected = MagicMock()
        injected.files.return_value.list.return_value.execute.return_value = {
            "files": []
        }

        discover.list_input_subfolders("root_id", service=injected)

        mock_get.assert_not_called()


class TestRunConfigFromDiscovered(unittest.TestCase):
    """自動検出モードは共有の既定タブ ``参加者マスター`` を読む。

    ユーザーは 1 枚の ``参加者マスター`` を実行ごとに対象セミナーの内容へ
    差し替える運用のため、F-09 の ``<フォルダ名>_参加者マスター`` 派生は廃止
    （空の per-folder タブを読んで医院名が引けずフォルダ名が AI 抽出名になる
    事故を解消。経緯は lessons.md 2026-05-29 エントリ）。
    """

    def test_master_sheet_name_is_derived_from_target_folder(self):
        """target_folder='新人育成塾' → '参加者マスター(新人育成塾)'。"""
        ctx = discover.DiscoveredContext(
            target_folder_name="新人育成塾",
            input_folder_id="in_id",
            output_folder_id="out_id",
            output_sheet_name="新人育成塾",
        )
        cfg = discover.RunConfig.from_discovered(ctx)
        self.assertEqual(cfg.master_sheet_name, "参加者マスター(新人育成塾)")
        self.assertTrue(cfg.master_sheet_strict)

    def test_different_target_folders_get_independent_master_tabs(self):
        """別フォルダなら別タブ（セミナーごとに独立した参加者管理）。"""
        ctx_a = discover.DiscoveredContext(
            target_folder_name="セミナーA",
            input_folder_id="a_in",
            output_folder_id="a_out",
            output_sheet_name="セミナーA",
        )
        ctx_b = discover.DiscoveredContext(
            target_folder_name="セミナーB",
            input_folder_id="b_in",
            output_folder_id="b_out",
            output_sheet_name="セミナーB",
        )
        cfg_a = discover.RunConfig.from_discovered(ctx_a)
        cfg_b = discover.RunConfig.from_discovered(ctx_b)
        self.assertEqual(cfg_a.master_sheet_name, "参加者マスター(セミナーA)")
        self.assertEqual(cfg_b.master_sheet_name, "参加者マスター(セミナーB)")
        self.assertNotEqual(cfg_a.master_sheet_name, cfg_b.master_sheet_name)

    def test_from_profile_keeps_strict_false_for_backward_compat(self):
        """プロファイルモードは strict=False（共有タブの 1 枚使い回し運用を維持）。"""
        from src.profile import ProfileConfig
        profile = ProfileConfig(
            name="jissen_default",
            display_name="既定",
            document_type="jissen_practice_case",
            period="default",
            input_folder_id="in",
            output_folder_id="out",
            output_sheet_name="出力一覧",
            master_sheet_name=discover.MASTER_SHEET_NAME,
            prompt_template="jissen_practice_case",
        )
        cfg = discover.RunConfig.from_profile(profile)
        self.assertFalse(cfg.master_sheet_strict)


class TestResolveMasterSheetName(unittest.TestCase):
    """``resolve_master_sheet_name``: フォルダ名 → 既存タブ名の名寄せ。

    フォルダ名が既存タブ ``参加者マスター(<セミナー名>)`` のセミナー名を
    含む場合、対応するタブを返す。複数マッチは最長一致、マッチなしは
    ``参加者マスター(<folder>)`` を返す（後段の HARD FAIL に倒す）。
    """

    def test_returns_matching_tab_when_folder_contains_seminar_name(self):
        result = discover.resolve_master_sheet_name(
            target_folder_name="新人育成塾_2026_Q1",
            available_master_tabs=["参加者マスター(新人育成塾)"],
        )
        self.assertEqual(result, "参加者マスター(新人育成塾)")

    def test_exact_folder_name_match(self):
        """フォルダ名がセミナー名と完全一致するケース（既存挙動の互換）。"""
        result = discover.resolve_master_sheet_name(
            target_folder_name="新人育成塾",
            available_master_tabs=["参加者マスター(新人育成塾)"],
        )
        self.assertEqual(result, "参加者マスター(新人育成塾)")

    def test_picks_longest_match_when_multiple_seminars_match(self):
        """複数のセミナー名がフォルダ名に含まれるとき、最長一致を採用する。"""
        result = discover.resolve_master_sheet_name(
            target_folder_name="新人育成塾2026_Q1",
            available_master_tabs=[
                "参加者マスター(新人育成塾)",
                "参加者マスター(新人育成塾2026)",  # より具体的
            ],
        )
        self.assertEqual(result, "参加者マスター(新人育成塾2026)")

    def test_same_length_matches_are_deterministic(self):
        """同長マッチは文字列順で最大のものを採用（再走で同じ結果）。"""
        result = discover.resolve_master_sheet_name(
            target_folder_name="AB_資料",  # "A"・"B" のどちらも含む
            available_master_tabs=[
                "参加者マスター(A)",
                "参加者マスター(B)",
            ],
        )
        # "A" と "B" は同じ長さなので、文字列順で大きい "B" を採用
        self.assertEqual(result, "参加者マスター(B)")

    def test_no_match_returns_fallback(self):
        """マッチしないときは ``参加者マスター(<folder>)`` を返す（HARD FAIL 経路へ）。"""
        result = discover.resolve_master_sheet_name(
            target_folder_name="未登録セミナー_Q1",
            available_master_tabs=[
                "参加者マスター(新人育成塾)",
                "参加者マスター(経営塾ベーシック)",
            ],
        )
        self.assertEqual(result, "参加者マスター(未登録セミナー_Q1)")

    def test_empty_master_tabs_returns_fallback(self):
        """既存タブが 1 つも無いときも fallback（後段で HARD FAIL）。"""
        result = discover.resolve_master_sheet_name(
            target_folder_name="新人育成塾",
            available_master_tabs=[],
        )
        self.assertEqual(result, "参加者マスター(新人育成塾)")

    def test_ignores_non_master_format_tabs(self):
        """``参加者マスター(...)`` 形式でない混入タブは無視する。"""
        result = discover.resolve_master_sheet_name(
            target_folder_name="新人育成塾_Q1",
            available_master_tabs=[
                "シート1",
                "出力一覧",
                "参加者マスター(新人育成塾)",
                "参加者マスター",  # 括弧なし → 形式不一致
            ],
        )
        self.assertEqual(result, "参加者マスター(新人育成塾)")

    def test_ignores_empty_seminar_name_in_tab(self):
        """``参加者マスター()`` のような空セミナー名タブは無視する（誤マッチ防止）。"""
        result = discover.resolve_master_sheet_name(
            target_folder_name="新人育成塾_Q1",
            available_master_tabs=[
                "参加者マスター()",
                "参加者マスター(新人育成塾)",
            ],
        )
        self.assertEqual(result, "参加者マスター(新人育成塾)")


class TestResolveContextUsesMasterTabNameResolver(unittest.TestCase):
    """``resolve_context`` が既存マスタータブを列挙し、名寄せ結果を
    ``DiscoveredContext.master_sheet_name`` に格納する。
    """

    def _service_with_folders(self, folders: list[dict]) -> MagicMock:
        service = MagicMock()
        service.files.return_value.list.return_value.execute.return_value = {
            "files": folders
        }
        service.files.return_value.create.return_value.execute.return_value = {
            "id": "created_output_id"
        }
        return service

    @patch("src.discover.sheets_client.list_master_sheet_tabs")
    def test_uses_resolved_master_tab_when_folder_contains_seminar_name(
        self, mock_list_tabs,
    ):
        """フォルダ名 ``新人育成塾_2026_Q1`` + タブ ``参加者マスター(新人育成塾)``
        → ``master_sheet_name = '参加者マスター(新人育成塾)'``。
        """
        mock_list_tabs.return_value = ["参加者マスター(新人育成塾)"]
        service = self._service_with_folders([
            {"id": "input_id", "name": "新人育成塾_2026_Q1"},
        ])

        ctx = discover.resolve_context(
            target_folder="新人育成塾_2026_Q1",
            input_root_id="input_root",
            output_root_id="output_root",
            spreadsheet_id="ssid",
            service_drive=service,
        )

        self.assertEqual(ctx.master_sheet_name, "参加者マスター(新人育成塾)")
        mock_list_tabs.assert_called_once_with("ssid")

    @patch("src.discover.sheets_client.list_master_sheet_tabs")
    def test_falls_back_when_no_seminar_matches(self, mock_list_tabs):
        """マッチなしのとき ``参加者マスター(<folder>)`` が入る（HARD FAIL 経路）。"""
        mock_list_tabs.return_value = ["参加者マスター(経営塾)"]
        service = self._service_with_folders([
            {"id": "input_id", "name": "新規セミナー"},
        ])

        ctx = discover.resolve_context(
            target_folder="新規セミナー",
            input_root_id="input_root",
            output_root_id="output_root",
            spreadsheet_id="ssid",
            service_drive=service,
        )

        self.assertEqual(ctx.master_sheet_name, "参加者マスター(新規セミナー)")

    @patch("src.discover.sheets_client.list_master_sheet_tabs")
    def test_tab_listing_failure_falls_back_to_default(self, mock_list_tabs):
        """タブ列挙が失敗しても致命扱いせず fallback で続行する。"""
        mock_list_tabs.side_effect = RuntimeError("Sheets API unavailable")
        service = self._service_with_folders([
            {"id": "input_id", "name": "新人育成塾_Q1"},
        ])

        ctx = discover.resolve_context(
            target_folder="新人育成塾_Q1",
            input_root_id="input_root",
            output_root_id="output_root",
            spreadsheet_id="ssid",
            service_drive=service,
        )

        # 列挙失敗 → fallback で folder 名そのままを括弧内に入れる
        self.assertEqual(ctx.master_sheet_name, "参加者マスター(新人育成塾_Q1)")

    @patch("src.discover.sheets_client.list_master_sheet_tabs")
    def test_run_config_uses_resolved_master_sheet_name(self, mock_list_tabs):
        """``RunConfig.from_discovered`` が ``ctx.master_sheet_name`` を採用する。"""
        mock_list_tabs.return_value = ["参加者マスター(新人育成塾)"]
        service = self._service_with_folders([
            {"id": "input_id", "name": "新人育成塾_2026_Q1"},
        ])

        ctx = discover.resolve_context(
            target_folder="新人育成塾_2026_Q1",
            input_root_id="input_root",
            output_root_id="output_root",
            spreadsheet_id="ssid",
            service_drive=service,
        )
        cfg = discover.RunConfig.from_discovered(ctx)

        self.assertEqual(cfg.master_sheet_name, "参加者マスター(新人育成塾)")
        self.assertTrue(cfg.master_sheet_strict)


class TestResolveMasterSheetNameNormalization(unittest.TestCase):
    """名寄せの NFKC 正規化（全角/半角・空白の表記揺れで HARD FAIL しない）。"""

    def test_fullwidth_digits_in_tab_match_halfwidth_folder(self):
        """タブ側 ``新人育成塾２０２６``（全角）× フォルダ側 ``新人育成塾2026``。"""
        result = discover.resolve_master_sheet_name(
            target_folder_name="新人育成塾2026_Q1",
            available_master_tabs=["参加者マスター(新人育成塾２０２６)"],
        )
        self.assertEqual(result, "参加者マスター(新人育成塾２０２６)")

    def test_space_variation_in_seminar_name(self):
        """タブ側 ``新人 育成塾``（空白入り）× フォルダ側 ``新人育成塾_Q1``。"""
        result = discover.resolve_master_sheet_name(
            target_folder_name="新人育成塾_Q1",
            available_master_tabs=["参加者マスター(新人 育成塾)"],
        )
        self.assertEqual(result, "参加者マスター(新人 育成塾)")

    def test_fullwidth_folder_matches_halfwidth_tab(self):
        """フォルダ側が全角英数でもタブ（半角）にマッチする。"""
        result = discover.resolve_master_sheet_name(
            target_folder_name="ＪＩＳＳＥＮ塾＿2026",
            available_master_tabs=["参加者マスター(JISSEN塾)"],
        )
        self.assertEqual(result, "参加者マスター(JISSEN塾)")

    def test_returned_tab_name_keeps_original_notation(self):
        """返り値は元のタブ表記のまま（正規化形ではない）。"""
        result = discover.resolve_master_sheet_name(
            target_folder_name="経営塾2026",
            available_master_tabs=["参加者マスター(経営塾　２０２６)"],
        )
        self.assertEqual(result, "参加者マスター(経営塾　２０２６)")


if __name__ == "__main__":
    unittest.main()
