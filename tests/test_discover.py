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
    """F-09 回帰防止: ``RunConfig.from_discovered`` は ``target_folder_name``
    から ``master_sheet_name`` を派生させる（全セミナーで同じデフォルトタブを
    共有すると別セミナーの参加者へ誤送信するリスクがあるため）。
    """

    def test_master_sheet_name_derived_from_target_folder(self):
        ctx = discover.DiscoveredContext(
            target_folder_name="テスト5",
            input_folder_id="in_id",
            output_folder_id="out_id",
            output_sheet_name="テスト5",
        )
        cfg = discover.RunConfig.from_discovered(ctx)
        self.assertEqual(cfg.master_sheet_name, "テスト5_参加者マスター")

    def test_different_target_folders_get_different_master_tabs(self):
        """別フォルダ → 別タブ。デフォルト共有による誤送信を防ぐ。"""
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
        self.assertNotEqual(cfg_a.master_sheet_name, cfg_b.master_sheet_name)
        self.assertEqual(cfg_a.master_sheet_name, "セミナーA_参加者マスター")
        self.assertEqual(cfg_b.master_sheet_name, "セミナーB_参加者マスター")


if __name__ == "__main__":
    unittest.main()
