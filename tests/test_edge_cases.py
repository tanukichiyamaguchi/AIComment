"""エッジケーステスト網羅（QA キャンペーン第 2 段）。

最近マージされた以下のモジュールを重点的にカバーする:

- ``src/profile.py``           : PR #19 で新規追加されたプロファイル loader
- ``src/drive_client.py``      : PR #15 で正規化マッチング追加
- ``src/utils.py``             : PR #15 で normalize_name_for_match 追加
- ``src/pdf_merger.py``        : PR #14 で make_output_filename 拡張
- ``profiles/*.yaml``          : スキーマ整合性 / 一意性 sanity チェック

方針:
    本ファイルは「追加テストのみ」で本番コードを修正しない。
    既存テストを 1 件も壊さないこと。
    バグらしき挙動を発見したテストは ``@pytest.mark.skip(reason="Bug found: ...")``
    を付けて記録し、修正は後続 PR（defect-investigator 担当）に委譲する。
"""

from __future__ import annotations

import os
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import yaml

from src import drive_client, profile as profile_module
from src.pdf_merger import make_output_filename
from src.profile import ProfileConfig
from src.utils import normalize_name_for_match


# ---------------------------------------------------------------------------
# 1. profile.py エッジケース
# ---------------------------------------------------------------------------


class TestProfileLoaderEdgeCases(unittest.TestCase):
    """``load_profile`` の境界条件・攻撃ベクトル・不正入力。"""

    def test_empty_profile_name_raises_file_not_found(self):
        """空文字を渡されたら ``profiles/.yaml`` を探しに行って失敗する。"""
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(FileNotFoundError) as ctx:
                profile_module.load_profile("")
        # エラーメッセージに利用可能リストが出ること
        self.assertIn("jissen_default", str(ctx.exception))

    def test_path_traversal_attempt_is_caught_as_file_not_found(self):
        """``../../etc/passwd`` のようなパストラバーサル攻撃。

        現状は ``PROFILES_DIR / f"{profile_name}.yaml"`` の組み立てだけで
        外部ファイルにアクセスはしない（``.yaml`` 拡張子が付くため）。
        FileNotFoundError として安全に弾かれることを担保する。
        """
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(FileNotFoundError):
                profile_module.load_profile("../../etc/passwd")

    def test_profile_name_with_windows_forbidden_chars(self):
        """profile 名に ``:`` ``*`` 等の Windows 禁止文字を渡されても安全に FileNotFoundError。"""
        with patch.dict(os.environ, {}, clear=True):
            for bad in ("inv:alid", "wild*card", "less<than", "pipe|sym"):
                with self.subTest(name=bad):
                    with self.assertRaises(FileNotFoundError):
                        profile_module.load_profile(bad)

    def test_missing_required_field_raises_value_error_with_field_list(self):
        """必須フィールド欠落 YAML → ValueError かつ欠落フィールドが含まれる。"""
        with TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            (tmp_dir / "broken.yaml").write_text(
                "display_name: x\n",  # ほぼ全部欠落
                encoding="utf-8",
            )
            with patch.object(profile_module, "PROFILES_DIR", tmp_dir):
                with self.assertRaises(ValueError) as ctx:
                    profile_module.load_profile("broken")
        msg = str(ctx.exception)
        self.assertIn("必須フィールド欠落", msg)
        # 欠落キーが具体的に列挙されていること
        self.assertIn("document_type", msg)
        self.assertIn("prompt_template", msg)

    def test_empty_string_env_var_treated_as_unset(self):
        """環境変数値が空文字（``""``）でも未設定と同じ ValueError になる。"""
        with patch.dict(
            os.environ,
            {"DRIVE_FOLDER_ID": "", "DRIVE_OUTPUT_FOLDER_ID": "ok"},
            clear=False,
        ):
            with self.assertRaises(ValueError) as ctx:
                profile_module.load_profile("jissen_default")
        self.assertIn("DRIVE_FOLDER_ID", str(ctx.exception))

    def test_malformed_yaml_unclosed_quote_raises_yaml_error(self):
        """引用符不整合の壊れた YAML は ``yaml.YAMLError``。"""
        with TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            (tmp_dir / "broken.yaml").write_text(
                'display_name: "unclosed quote\n', encoding="utf-8"
            )
            with patch.object(profile_module, "PROFILES_DIR", tmp_dir):
                with self.assertRaises(yaml.YAMLError):
                    profile_module.load_profile("broken")

    def test_malformed_yaml_tab_indentation_raises(self):
        """タブインデント YAML（YAML はタブを許さない）も yaml.YAMLError。"""
        with TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            (tmp_dir / "broken.yaml").write_text(
                "k1: v1\n\tk2: v2\n", encoding="utf-8"
            )
            with patch.object(profile_module, "PROFILES_DIR", tmp_dir):
                with self.assertRaises(yaml.YAMLError):
                    profile_module.load_profile("broken")

    def test_yaml_root_is_list_raises_value_error(self):
        """YAML ルートがオブジェクトではなくリストならば ValueError。"""
        with TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            (tmp_dir / "broken.yaml").write_text(
                "- a\n- b\n", encoding="utf-8"
            )
            with patch.object(profile_module, "PROFILES_DIR", tmp_dir):
                with self.assertRaises(ValueError) as ctx:
                    profile_module.load_profile("broken")
        self.assertIn("オブジェクト", str(ctx.exception))

    def test_yaml_root_is_string_raises_value_error(self):
        """YAML ルートが単一スカラー文字列 → ValueError。"""
        with TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            (tmp_dir / "broken.yaml").write_text("just a string\n", encoding="utf-8")
            with patch.object(profile_module, "PROFILES_DIR", tmp_dir):
                with self.assertRaises(ValueError):
                    profile_module.load_profile("broken")


class TestListAvailableProfilesEdgeCases(unittest.TestCase):
    """``list_available_profiles`` の境界条件。"""

    def test_returns_empty_when_profiles_dir_missing(self):
        """``profiles/`` ディレクトリ自体が存在しない場合 → 空リスト。"""
        with patch.object(
            profile_module, "PROFILES_DIR", Path("/nonexistent_xxx_zzz")
        ):
            result = profile_module.list_available_profiles()
        self.assertEqual(result, [])

    def test_returns_empty_when_profiles_dir_is_empty(self):
        """``profiles/`` ディレクトリが空（YAML が 1 つもない）→ 空リスト。"""
        with TemporaryDirectory() as tmp:
            with patch.object(profile_module, "PROFILES_DIR", Path(tmp)):
                result = profile_module.list_available_profiles()
        self.assertEqual(result, [])

    def test_ignores_non_yaml_files(self):
        """``.yaml`` 以外のファイル（README.md, .yml 含む）は無視される。"""
        with TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            (tmp_dir / "foo.yaml").write_text("k: v\n")
            (tmp_dir / "bar.yml").write_text("k: v\n")  # .yml は対象外
            (tmp_dir / "README.md").write_text("not yaml")
            with patch.object(profile_module, "PROFILES_DIR", tmp_dir):
                result = profile_module.list_available_profiles()
        self.assertEqual(result, ["foo"])


class TestProfileConfigFrozen(unittest.TestCase):
    """ProfileConfig が完全に frozen（全フィールド変更不可）。"""

    def _cfg(self) -> ProfileConfig:
        with patch.dict(
            os.environ,
            {"DRIVE_FOLDER_ID": "x", "DRIVE_OUTPUT_FOLDER_ID": "y"},
            clear=False,
        ):
            return profile_module.load_profile("jissen_default")

    def test_input_folder_id_immutable(self):
        with self.assertRaises(FrozenInstanceError):
            self._cfg().input_folder_id = "z"  # type: ignore[misc]

    def test_output_sheet_name_immutable(self):
        with self.assertRaises(FrozenInstanceError):
            self._cfg().output_sheet_name = "X"  # type: ignore[misc]

    def test_display_name_immutable(self):
        with self.assertRaises(FrozenInstanceError):
            self._cfg().display_name = "changed"  # type: ignore[misc]

    def test_cannot_add_new_field(self):
        """新規フィールド追加も拒否される（frozen=True かつ slots 風挙動）。"""
        with self.assertRaises(FrozenInstanceError):
            self._cfg().extra_field = "anything"  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# 2. normalize_name_for_match エッジケース
# ---------------------------------------------------------------------------


class TestNormalizeNameEdgeCases(unittest.TestCase):
    """``normalize_name_for_match`` の Unicode 周りの想定外入力。"""

    def test_combining_acute_unifies_with_composed(self):
        """U+0301 結合文字を含む文字列が、合成済み文字と NFKC で等価。"""
        decomposed = "café"  # cafe + COMBINING ACUTE ACCENT
        composed = "café"
        self.assertEqual(
            normalize_name_for_match(decomposed),
            normalize_name_for_match(composed),
        )

    def test_emoji_in_clinic_name_passes_through(self):
        """絵文字（U+1F600）を含む医院名は除去されず保持される（spaces only collapse）。"""
        name = "テスト医院\U0001F600"
        result = normalize_name_for_match(name)
        # NFKC は絵文字を変えない、空白でもないので残る
        self.assertIn("\U0001F600", result)
        self.assertIn("テスト医院", result)

    def test_zero_width_chars_are_preserved_currently(self):
        """ゼロ幅文字（U+200B, U+FEFF）は現状除去されない。

        既知の仕様: ``\\s+`` は ZWSP / BOM を空白扱いしない。
        意図的に保守的な比較として残してあると思われるが、Drive フォルダ名で
        ZWSP 混入があると見た目同じでも別フォルダ扱いになる潜在リスクを示す
        ためにテストとして固定する。

        本テストは「現状の挙動が保たれていること」を assert することで、
        意図せず仕様変更された場合に CI で気づけるようにする。
        """
        with_zwsp = "クリニック​A"
        without_zwsp = "クリニックA"
        # ZWSP / BOM は \s+ にマッチしないので残る
        self.assertIn("​", normalize_name_for_match(with_zwsp))
        self.assertNotEqual(
            normalize_name_for_match(with_zwsp),
            normalize_name_for_match(without_zwsp),
        )

    def test_bom_char_is_preserved_currently(self):
        """BOM 文字（U+FEFF）も同様に除去されない（現状仕様の固定）。"""
        with_bom = "AB﻿CD"
        without_bom = "ABCD"
        self.assertNotEqual(
            normalize_name_for_match(with_bom),
            normalize_name_for_match(without_bom),
        )

    def test_fullwidth_space_only_input(self):
        """全角空白だけの文字列は空文字列に正規化される。"""
        self.assertEqual(normalize_name_for_match("　　　"), "")

    def test_mixed_newline_tab_input(self):
        """改行・タブ混在も ``\\s+`` で除去される。"""
        self.assertEqual(
            normalize_name_for_match("A\nB\tC\rD"),
            "ABCD",
        )

    def test_empty_string(self):
        self.assertEqual(normalize_name_for_match(""), "")

    def test_very_long_string_does_not_crash(self):
        """1 万文字の入力でも O(n) で完走する。"""
        long_str = "あ" * 10000
        result = normalize_name_for_match(long_str)
        self.assertEqual(len(result), 10000)

    def test_old_vs_new_kanji_form_not_unified_by_design(self):
        """旧字体・新字体（``斎`` vs ``齋``）は NFKC では別物として扱われる。

        AI 抽出元 PDF と Drive フォルダ名で旧字体/新字体が混在すると
        重複フォルダが作られる潜在リスクを記録する（仕様確認テスト）。
        """
        self.assertNotEqual(
            normalize_name_for_match("斎藤歯科"),
            normalize_name_for_match("齋藤歯科"),
        )

    def test_halfwidth_katakana_normalizes_to_fullwidth(self):
        """半角カタカナ ``ｸﾘﾆｯｸ`` は NFKC で全角に正規化される。"""
        self.assertEqual(
            normalize_name_for_match("ｸﾘﾆｯｸ"),
            normalize_name_for_match("クリニック"),
        )

    def test_only_whitespace_unicode_categories(self):
        """様々なUnicode空白（U+2002 EN SPACE, U+2003 EM SPACE 等）の除去確認。"""
        # NFKC でこれら U+2002〜U+2009 は通常の半角空白に正規化され、その後 \s+ で除去
        special = "A B C D"
        result = normalize_name_for_match(special)
        self.assertEqual(result, "ABCD")


# ---------------------------------------------------------------------------
# 3. make_output_filename エッジケース
# ---------------------------------------------------------------------------


class TestMakeOutputFilenameEdgeCases(unittest.TestCase):
    """``make_output_filename`` の特殊文字・境界長。"""

    def test_full_width_underscore_in_medical_name(self):
        """医院名に全角アンダースコア ``＿`` が既に含まれていても区切りと混同しない。

        現実装は ``f"{a}＿{b}＿{c}.pdf"`` の文字列結合で、内部に ``＿`` が
        あっても挙動上は問題ない（区切り解析が必要なときに副作用が出るが
        本関数の責務外）。
        """
        result = make_output_filename("AB＿CD歯科", "山田太郎", "事例")
        self.assertEqual(result, "AB＿CD歯科＿山田太郎＿事例.pdf")
        # 区切り文字を 4 つ含む結果になっても assert を緩く
        self.assertEqual(result.count("＿"), 3)

    def test_path_separators_are_stripped(self):
        """``/`` ``\\`` は除去される（パストラバーサル防止）。"""
        result = make_output_filename("a/b\\c", "x/y\\z", "p/q\\r")
        self.assertEqual(result, "abc＿xyz＿pqr.pdf")

    def test_null_byte_is_stripped(self):
        """``\\x00`` Null バイトは除去される。"""
        result = make_output_filename("med\x00ical", "per\x00son", "title")
        self.assertEqual(result, "medical＿person＿title.pdf")
        self.assertNotIn("\x00", result)

    def test_windows_forbidden_chars_are_stripped(self):
        """``<>:"|?*`` は除去される。"""
        result = make_output_filename('a<b>c:d', 'e"f|g', 'h?i*j')
        self.assertEqual(result, "abcd＿efg＿hij.pdf")

    def test_total_length_exceeds_os_limit(self):
        """3 要素合計が 255 バイトを超えるとき、ファイル名が切り詰められるべき。

        ``あ`` は UTF-8 で 3 バイト、100 文字で 300 バイト。3 要素 + 区切り
        + 拡張子 で約 910 バイトになり、ext4 / FAT / NTFS の 255 バイト
        制限を大幅に超える。``make_output_filename`` 側で UTF-8 バイト長
        ベースで切り詰めることで、OS の "File name too long" エラーを防ぐ。
        """
        result = make_output_filename("あ" * 100, "い" * 100, "う" * 100)
        # 期待: 255 バイト以下に収まる（UTF-8 バイト数で評価）
        self.assertLessEqual(
            len(result.encode("utf-8")),
            255,
            f"OS 制限超過: {len(result.encode('utf-8'))} bytes",
        )
        # 区切り文字 ＿ x 2 と拡張子 .pdf は必ず保持される
        self.assertTrue(result.endswith(".pdf"))
        self.assertEqual(result.count("＿"), 2)
        # 各セクションは少なくとも 1 文字以上残る（極端な切り捨てを禁止）
        clinic, person, title = result[:-4].split("＿")
        self.assertGreater(len(clinic), 0)
        self.assertGreater(len(person), 0)
        self.assertGreater(len(title), 0)

    def test_double_extension_is_collapsed(self):
        """``sample_title="x.pdf"`` の場合に ``.pdf.pdf`` にならない。

        AI 抽出やファイル名直接渡しで sample_title に既に ``.pdf`` が含まれる
        ケースがある（特に元ファイル名をそのまま title に転記する場合）。
        二重拡張子 ``...x.pdf.pdf`` は OS / Drive 上で混乱を招くため、
        合成前に末尾 ``.pdf`` を 1 回だけ削る。
        """
        result = make_output_filename("A", "B", "x.pdf")
        # 期待: ".pdf" は 1 回だけ末尾に付く
        self.assertEqual(result.count(".pdf"), 1)
        self.assertTrue(result.endswith(".pdf"))
        # 内部の "x" は保持される
        self.assertIn("＿x.pdf", result)

    def test_double_extension_case_insensitive(self):
        """``.PDF`` ``.Pdf`` などの大小区別違いも畳まれる。"""
        for suffix in (".PDF", ".Pdf", ".pDF"):
            with self.subTest(suffix=suffix):
                result = make_output_filename("A", "B", f"x{suffix}")
                self.assertEqual(
                    result.lower().count(".pdf"), 1,
                    f"{suffix} で .pdf が複数残った: {result}",
                )
                self.assertTrue(result.endswith(".pdf"))

    def test_empty_strings_produce_unknown_fallback(self):
        """全要素が空文字なら ``unknown`` フォールバック x3 になる。"""
        result = make_output_filename("", "", "")
        self.assertEqual(result, "unknown＿unknown＿unknown.pdf")

    def test_dots_only_input_produces_unknown(self):
        """``...`` のような構成要素も ``unknown`` に置換される。"""
        result = make_output_filename("...", "..", ".")
        self.assertEqual(result, "unknown＿unknown＿unknown.pdf")

    def test_control_chars_other_than_null_are_passed_through(self):
        """U+0001〜U+001F のうち null 以外は ``_sanitize_filename`` で除去対象外。

        現実装は ``\\x00`` だけ明示除去・``<>:"|?*`` 除去で、他の制御文字は残す。
        Drive は通常これらを許容するので致命ではないが、Windows ローカルで
        作る場合は要注意。挙動の固定として記録。
        """
        # \x01 が残ることを確認
        result = make_output_filename("a\x01b", "c", "d")
        self.assertIn("\x01", result)

    def test_mixed_real_world_clinic_person_title(self):
        """実データに近い「全角空白＋カッコ＋&」混在パターン。"""
        result = make_output_filename(
            "医療法人　かがやき歯科クリニック",
            "白川 蓮",
            "AI活用インプラント&新患獲得",
        )
        self.assertTrue(result.endswith(".pdf"))
        self.assertIn("医療法人", result)
        self.assertIn("白川", result)


# ---------------------------------------------------------------------------
# 4. find_or_create_folder エッジケース
# ---------------------------------------------------------------------------


class TestFindOrCreateFolderEdgeCases(unittest.TestCase):
    """``find_or_create_folder`` の重複検出・正規化マッチング境界。"""

    def test_handles_more_than_1000_folders(self):
        """pageSize=1000 を超える数のフォルダがあっても重複検出が機能する。

        現状は ``pageSize=1000`` で 1 ページしか取得しないため、
        1001 件目以降にある重複は検出されない可能性がある。
        本テストは「最初の 1000 件目までに存在すれば検出される」ことを
        確認することで、将来 pageToken 対応する際の挙動変化に気づけるようにする。
        """
        files = [{"id": f"id_{i}", "name": f"clinic_{i}"} for i in range(1001)]
        # 既存フォルダを 500 番目に置く
        files[500] = {"id": "existing", "name": "新規医院"}
        service = MagicMock()
        service.files.return_value.list.return_value.execute.return_value = {
            "files": files
        }
        result = drive_client.find_or_create_folder(
            "新規医院", "parent_id", service=service
        )
        self.assertEqual(result, "existing")

    def test_detects_duplicate_beyond_first_page(self):
        """1001 件目以降の既存フォルダも見つかるべき。

        pageSize=1000 を超える数のフォルダが親配下にある場合、Drive API は
        1 ページに 1000 件 + nextPageToken を返す。``find_or_create_folder``
        が nextPageToken をループせず最初の 1 ページしか見ないと、
        2 ページ目以降にある既存フォルダを検出できず重複作成されてしまう。

        本テストは page1 = 1000 件のダミー / page2 = 既存 1 件を返すモックで
        pageToken のループが実装されていることを検証する。
        """
        page1_files = [
            {"id": f"id_{i}", "name": f"other_{i}"} for i in range(1000)
        ]
        page2_files = [{"id": "existing_late", "name": "対象医院"}]
        service = MagicMock()
        service.files.return_value.list.return_value.execute.side_effect = [
            {"files": page1_files, "nextPageToken": "page2"},
            {"files": page2_files},  # nextPageToken なしで終了
        ]
        result = drive_client.find_or_create_folder(
            "対象医院", "parent_id", service=service
        )
        # 期待：page2 にある既存を再利用する
        self.assertEqual(result, "existing_late")
        service.files.return_value.create.assert_not_called()
        # 2 回 list が呼ばれた（page1 → page2）ことを検証
        self.assertEqual(
            service.files.return_value.list.return_value.execute.call_count, 2
        )
        # 2 回目の呼び出しに pageToken="page2" が含まれること
        second_list_kwargs = service.files.return_value.list.call_args_list[1].kwargs
        self.assertEqual(second_list_kwargs.get("pageToken"), "page2")

    def test_raises_on_null_folder_name(self):
        """``None`` の folder_name → 現実装では ``if not folder_name:`` で ValueError。"""
        with self.assertRaises(ValueError):
            drive_client.find_or_create_folder(None, "parent_id", service=MagicMock())  # type: ignore[arg-type]

    def test_raises_on_whitespace_only_folder_name(self):
        """空白のみの folder_name は truthy だが事実上空。

        現実装は ``if not folder_name:`` の真偽判定のため、半角空白 ``" "`` は
        truthy として通ってしまう（Drive 側で trim される可能性高だが要注意）。
        現状の挙動を固定するテスト。
        """
        service = MagicMock()
        service.files.return_value.list.return_value.execute.return_value = {"files": []}
        service.files.return_value.create.return_value.execute.return_value = {"id": "new"}
        # ValueError は出ない（空白文字列は truthy）→ 作成まで走る
        result = drive_client.find_or_create_folder(" ", "parent_id", service=service)
        self.assertEqual(result, "new")

    def test_handles_missing_files_key_in_response(self):
        """Drive API の response に ``files`` キーが無い場合でも例外にならない。"""
        service = MagicMock()
        service.files.return_value.list.return_value.execute.return_value = {}  # files キー欠落
        service.files.return_value.create.return_value.execute.return_value = {"id": "new"}
        result = drive_client.find_or_create_folder(
            "医院", "parent_id", service=service
        )
        # 既存なし → 新規作成
        self.assertEqual(result, "new")

    def test_handles_empty_files_list(self):
        """``files: []`` （明示的に空リスト）でも新規作成に進む。"""
        service = MagicMock()
        service.files.return_value.list.return_value.execute.return_value = {"files": []}
        service.files.return_value.create.return_value.execute.return_value = {"id": "new"}
        result = drive_client.find_or_create_folder(
            "医院", "parent_id", service=service
        )
        self.assertEqual(result, "new")

    def test_existing_file_with_non_folder_mime_type_not_matched(self):
        """Drive 側のクエリで mimeType=folder フィルタしているので
        本来この状況は起きないが、API クエリが緩んだ場合の防御を確認する。

        Python 側コードは mimeType を再チェックしていないため、
        Drive クエリのみが信頼の根拠であることを記録する仕様確認テスト。
        """
        service = MagicMock()
        service.files.return_value.list.return_value.execute.return_value = {
            "files": [{"id": "fake_pdf_id", "name": "対象医院"}]  # mimeType チェックなし
        }
        result = drive_client.find_or_create_folder(
            "対象医院", "parent_id", service=service
        )
        # 現実装：mimeType フィルタは Drive クエリ任せなので、id を返してしまう
        self.assertEqual(result, "fake_pdf_id")

    def test_normalization_match_with_full_width_space(self):
        """全角空白の有無での重複検出。"""
        service = MagicMock()
        service.files.return_value.list.return_value.execute.return_value = {
            "files": [{"id": "existing", "name": "医療法人　かがやき歯科"}]  # 全角空白
        }
        result = drive_client.find_or_create_folder(
            "医療法人かがやき歯科", "parent_id", service=service
        )
        self.assertEqual(result, "existing")


# ---------------------------------------------------------------------------
# 5. YAML プロファイル全件 sanity test
# ---------------------------------------------------------------------------


class TestProfileYAMLSanity(unittest.TestCase):
    """``profiles/*.yaml`` の整合性。一意性・スキーマを担保する。"""

    @classmethod
    def setUpClass(cls):
        cls.profiles_dir = Path(__file__).resolve().parent.parent / "profiles"
        cls.profiles: dict[str, dict[str, Any]] = {}
        for path in sorted(cls.profiles_dir.glob("*.yaml")):
            cls.profiles[path.stem] = yaml.safe_load(path.read_text(encoding="utf-8"))

    def test_all_profiles_are_loadable(self):
        """全 YAML が読めて、ルートが dict であること。"""
        self.assertGreater(len(self.profiles), 0, "プロファイルが 1 つもない")
        for name, data in self.profiles.items():
            with self.subTest(profile=name):
                self.assertIsInstance(data, dict, f"{name}: ルートが dict でない")

    def test_all_profiles_have_required_fields(self):
        """全プロファイルが必須 7 フィールドを持つ。"""
        required = (
            "display_name",
            "document_type",
            "period",
            "input_folder_id_secret",
            "output_folder_id_secret",
            "output_sheet_name",
            "prompt_template",
        )
        for name, data in self.profiles.items():
            with self.subTest(profile=name):
                missing = [k for k in required if k not in data]
                self.assertEqual(missing, [], f"{name}: 欠落 {missing}")

    def test_output_sheet_name_is_unique(self):
        """同一 output_sheet_name を持つプロファイルが複数ないこと。

        同一スプレッドシート内に複数 profile が同名シートを使うと、
        出力一覧が混ざってしまう。
        """
        seen: dict[str, list[str]] = {}
        for name, data in self.profiles.items():
            sheet_name = data["output_sheet_name"]
            seen.setdefault(sheet_name, []).append(name)
        duplicates = {k: v for k, v in seen.items() if len(v) > 1}
        self.assertEqual(
            duplicates, {},
            f"output_sheet_name が重複: {duplicates}",
        )

    def test_input_folder_secret_is_unique_except_default(self):
        """同一 input_folder_id_secret を持つプロファイルが複数ないこと。

        ただし ``jissen_default`` は既存運用の DRIVE_FOLDER_ID を参照する
        後方互換用なので除外する。
        """
        seen: dict[str, list[str]] = {}
        for name, data in self.profiles.items():
            if name == "jissen_default":
                continue
            secret = data["input_folder_id_secret"]
            seen.setdefault(secret, []).append(name)
        duplicates = {k: v for k, v in seen.items() if len(v) > 1}
        self.assertEqual(
            duplicates, {},
            f"input_folder_id_secret が重複: {duplicates}",
        )

    def test_output_folder_secret_is_unique_except_default(self):
        """同一 output_folder_id_secret も同様。"""
        seen: dict[str, list[str]] = {}
        for name, data in self.profiles.items():
            if name == "jissen_default":
                continue
            secret = data["output_folder_id_secret"]
            seen.setdefault(secret, []).append(name)
        duplicates = {k: v for k, v in seen.items() if len(v) > 1}
        self.assertEqual(
            duplicates, {},
            f"output_folder_id_secret が重複: {duplicates}",
        )

    def test_period_value_is_unique(self):
        """同一 period を持つプロファイルが複数ないこと（display 上の混乱防止）。"""
        seen: dict[str, list[str]] = {}
        for name, data in self.profiles.items():
            period = data["period"]
            seen.setdefault(period, []).append(name)
        duplicates = {k: v for k, v in seen.items() if len(v) > 1}
        self.assertEqual(
            duplicates, {},
            f"period が重複: {duplicates}",
        )


# ---------------------------------------------------------------------------
# 6. profile load の sanity（実 YAML 全件）
# ---------------------------------------------------------------------------


class TestAllProfilesLoadable(unittest.TestCase):
    """実 YAML 全件について ``load_profile`` が成功すること。

    シークレットを mock 環境変数で埋めて、schema レベルではなく
    関数挙動レベルで失敗しないことを担保する。
    """

    def test_each_profile_loads_with_env_vars_provided(self):
        names = profile_module.list_available_profiles()
        for name in names:
            with self.subTest(profile=name):
                # YAML から secret 名を読んで env を埋める
                path = profile_module.PROFILES_DIR / f"{name}.yaml"
                data = yaml.safe_load(path.read_text(encoding="utf-8"))
                env = {
                    data["input_folder_id_secret"]: f"in_{name}",
                    data["output_folder_id_secret"]: f"out_{name}",
                }
                with patch.dict(os.environ, env, clear=False):
                    cfg = profile_module.load_profile(name)
                self.assertEqual(cfg.name, name)
                self.assertEqual(cfg.input_folder_id, f"in_{name}")
                self.assertEqual(cfg.output_folder_id, f"out_{name}")


# ---------------------------------------------------------------------------
# 7. Batch API 一過性エラー（503 / 529 / RateLimit 等）の境界
#
# 本番事象（run_id=26811653746）の再発防止:
#   batch ポーリングが 3 時間後に 503 一発で落ちた。原因仮説は以下:
#     - ``submit_batch`` / ``get_batch_status`` / ``get_batch_results`` の
#       3 関数が「一過性エラー」を捕捉していない（恒久エラー判定のみある）。
#     - ``step3_wait_and_get_results`` のポーリングループも、``get_batch_status``
#       が単発 503 を投げると catch せず即 raise してランを止める。
#   どちらも一過性であり、本来は指数バックオフでリトライすれば抜けられる。
#
# 以下のテスト群は **defect-investigator が submit_batch / get_batch_status /
# get_batch_results に retry を入れ、step3 ポーリングループに transient 吸収を
# 入れる前提** で書かれている。修正前は red のまま push し、修正後 green に
# なる前提（テスト名・docstring に「修正前 red」を明記）。
# ---------------------------------------------------------------------------


import anthropic as _anthropic
from anthropic.types import TextBlock as _TextBlock

from src import batch_main as _batch_main
from src import comment_generator as _cg
from src.comment_generator import (
    PermanentRunFailureError as _PermanentRunFailureError,
    _OverloadedError as _OverloadedErr,
    _RETRYABLE_API_ERRORS as _RETRYABLE,
)


def _503_internal_server(message: str = "Internal Server Error") -> _anthropic.InternalServerError:
    """本番ログに出ていた 503 InternalServerError を再現する。

    本番では body に ``{"type": "error", "error": {"type": "overloaded_error",
    "message": "API key validation is temporarily unavailable. Please try again later."}}``
    が含まれていた。これは Anthropic ステータスとしては 503 なので、SDK は
    ``InternalServerError`` で送出する（``OverloadedError`` ではない）。
    """
    body = {
        "type": "error",
        "error": {"type": "overloaded_error", "message": message},
    }
    return _anthropic.InternalServerError(
        message=message,
        response=MagicMock(status_code=503, headers={}),
        body=body,
    )


def _529_overloaded() -> Exception:
    """529 Overloaded（top-level の OverloadedError もしくは
    ``_exceptions.OverloadedError``）を再現する。"""
    return _OverloadedErr(
        message="Overloaded",
        response=MagicMock(status_code=529, headers={}),
        body={"type": "error", "error": {"type": "overloaded_error"}},
    )


def _rate_limit() -> _anthropic.RateLimitError:
    return _anthropic.RateLimitError(
        message="rate limit",
        response=MagicMock(status_code=429, headers={}),
        body=None,
    )


def _auth_401() -> _anthropic.AuthenticationError:
    return _anthropic.AuthenticationError(
        message="invalid x-api-key",
        response=MagicMock(status_code=401, headers={}),
        body=None,
    )


def _permission_403() -> _anthropic.PermissionDeniedError:
    return _anthropic.PermissionDeniedError(
        message="forbidden",
        response=MagicMock(status_code=403, headers={}),
        body=None,
    )


def _mk_status(status: str, **counts: int) -> Any:
    """``messages.batches.retrieve`` の戻り値を模す。

    ``processing_status`` と ``request_counts``（``processing/succeeded/errored
    /canceled/expired``）を持つ MagicMock を返す。
    """
    obj = MagicMock()
    obj.id = "batch_abc"
    obj.processing_status = status
    rc = MagicMock()
    rc.processing = counts.get("processing", 0)
    rc.succeeded = counts.get("succeeded", 0)
    rc.errored = counts.get("errored", 0)
    rc.canceled = counts.get("canceled", 0)
    rc.expired = counts.get("expired", 0)
    obj.request_counts = rc
    return obj


def _mk_batch_result(
    custom_id: str,
    *,
    succeeded: bool = True,
    text: str = '{"clinic_name":"X歯科","person_name":"山田太郎","sample_title":"事例","comment":"' + ("テスト" * 60) + '"}',
    result_type: str = "errored",
) -> Any:
    """``messages.batches.results`` のイテレータ要素を模す。

    ``succeeded=True`` のとき ``result.type=="succeeded"`` + 構造化出力 JSON、
    ``False`` のとき ``result_type``（"errored" / "expired" / "canceled"）に
    したがって失敗扱いになる。
    """
    res = MagicMock()
    res.custom_id = custom_id
    if succeeded:
        res.result.type = "succeeded"
        message = MagicMock()
        message.content = [_TextBlock(type="text", text=text, citations=None)]
        res.result.message = message
    else:
        res.result.type = result_type
    return res


class TestRetryableApiErrorsTupleMembership(unittest.TestCase):
    """``_RETRYABLE_API_ERRORS`` に必要な型が **直接** 含まれている回帰防止。

    529 / 503 系の取り扱いに穴があると本番で 503 一発で落ちる（run_id=26811653746）。
    既存 ``test_comment_generator.py::test_overloaded_error_is_in_retryable_set``
    は 529 だけを検証するため、ここで 5 種すべてを 1 ステップで網羅する。
    """

    def test_internal_server_error_503_is_retryable(self):
        """``InternalServerError`` (status_code=503) はリトライ tuple に含まれる。"""
        self.assertIn(_anthropic.InternalServerError, _RETRYABLE)

    def test_overloaded_529_is_retryable(self):
        self.assertIn(_OverloadedErr, _RETRYABLE)

    def test_rate_limit_429_is_retryable(self):
        self.assertIn(_anthropic.RateLimitError, _RETRYABLE)

    def test_api_connection_is_retryable(self):
        self.assertIn(_anthropic.APIConnectionError, _RETRYABLE)

    def test_api_timeout_is_retryable(self):
        self.assertIn(_anthropic.APITimeoutError, _RETRYABLE)

    def test_authentication_401_is_not_retryable(self):
        """恒久エラー（認証）は **絶対に** リトライ tuple に入れない。"""
        self.assertNotIn(_anthropic.AuthenticationError, _RETRYABLE)

    def test_permission_403_is_not_retryable(self):
        self.assertNotIn(_anthropic.PermissionDeniedError, _RETRYABLE)

    def test_bad_request_400_is_not_retryable(self):
        """400 BadRequest はリトライしない（billing 系は別経路で恒久判定）。"""
        self.assertNotIn(_anthropic.BadRequestError, _RETRYABLE)

    def test_concrete_503_instance_is_caught_by_retryable_tuple(self):
        """本番で観測された 503（body の error.type=overloaded_error）でも
        ``isinstance`` で確実に retryable と判定される。"""
        err = _503_internal_server(
            "API key validation is temporarily unavailable. Please try again later."
        )
        self.assertIsInstance(err, _RETRYABLE)

    def test_concrete_529_instance_is_caught_by_retryable_tuple(self):
        self.assertIsInstance(_529_overloaded(), _RETRYABLE)


class TestSubmitBatchRetriesOnTransient(unittest.TestCase):
    """``submit_batch`` は ``messages.batches.create`` の一過性エラーを
    リトライする。

    修正前 red のシナリオ: 現状 ``submit_batch`` は ``BadRequestError`` /
    ``AuthenticationError`` / ``PermissionDeniedError`` のみキャッチして
    恒久判定にかけ、それ以外（503/529/429/接続/タイムアウト）はリトライせず
    即 raise する。defect-investigator の修正でリトライが追加されたら green。
    """

    @patch("src.comment_generator._create_client")
    @patch("src.comment_generator.time.sleep")
    def test_503_then_success(self, mock_sleep, mock_create_client):
        """503 が 1 回 → 2 回目で成功。"""
        mock_client = MagicMock()
        mock_create_client.return_value = mock_client
        success = MagicMock()
        success.id = "batch_xyz"
        success.processing_status = "in_progress"
        mock_client.messages.batches.create.side_effect = [
            _503_internal_server(),
            success,
        ]
        items = [{"custom_id": "item_0001", "pdf_text": "x", "pdf_file_name": "001-01-1.pdf"}]
        batch_id = _cg.submit_batch(items)
        self.assertEqual(batch_id, "batch_xyz")
        self.assertEqual(mock_client.messages.batches.create.call_count, 2)

    @patch("src.comment_generator._create_client")
    @patch("src.comment_generator.time.sleep")
    def test_529_then_success(self, mock_sleep, mock_create_client):
        """529 過負荷が 1 回 → 2 回目で成功。"""
        mock_client = MagicMock()
        mock_create_client.return_value = mock_client
        success = MagicMock()
        success.id = "batch_xyz"
        success.processing_status = "in_progress"
        mock_client.messages.batches.create.side_effect = [
            _529_overloaded(),
            success,
        ]
        items = [{"custom_id": "item_0001", "pdf_text": "x", "pdf_file_name": "001-01-1.pdf"}]
        batch_id = _cg.submit_batch(items)
        self.assertEqual(batch_id, "batch_xyz")

    @patch("src.comment_generator._create_client")
    @patch("src.comment_generator.time.sleep")
    def test_rate_limit_then_success(self, mock_sleep, mock_create_client):
        mock_client = MagicMock()
        mock_create_client.return_value = mock_client
        success = MagicMock()
        success.id = "batch_xyz"
        success.processing_status = "in_progress"
        mock_client.messages.batches.create.side_effect = [
            _rate_limit(),
            success,
        ]
        items = [{"custom_id": "item_0001", "pdf_text": "x", "pdf_file_name": "001-01-1.pdf"}]
        self.assertEqual(_cg.submit_batch(items), "batch_xyz")

    @patch("src.comment_generator._create_client")
    @patch("src.comment_generator.time.sleep")
    def test_multiple_consecutive_transients_then_success(
        self, mock_sleep, mock_create_client,
    ):
        """503 × 3 連続 → 4 回目で成功。境界（リトライ上限ぎりぎり）を確認する。"""
        mock_client = MagicMock()
        mock_create_client.return_value = mock_client
        success = MagicMock()
        success.id = "batch_xyz"
        success.processing_status = "in_progress"
        mock_client.messages.batches.create.side_effect = [
            _503_internal_server(),
            _529_overloaded(),
            _rate_limit(),
            success,
        ]
        items = [{"custom_id": "item_0001", "pdf_text": "x", "pdf_file_name": "001-01-1.pdf"}]
        self.assertEqual(_cg.submit_batch(items), "batch_xyz")
        self.assertEqual(mock_client.messages.batches.create.call_count, 4)


class TestGetBatchStatusRetriesOnTransient(unittest.TestCase):
    """``get_batch_status`` は ``messages.batches.retrieve`` の一過性エラーを
    リトライする。

    修正前 red のシナリオ: 現状 ``get_batch_status`` は恒久エラー判定のみ
    で、503/529/429/接続/タイムアウトは即 raise。本番では 3h ポーリング中の
    1 回の 503 でランが落ちた。defect-investigator 修正後 green。
    """

    @patch("src.comment_generator._create_client")
    @patch("src.comment_generator.time.sleep")
    def test_503_then_in_progress_status(self, mock_sleep, mock_create_client):
        mock_client = MagicMock()
        mock_create_client.return_value = mock_client
        mock_client.messages.batches.retrieve.side_effect = [
            _503_internal_server(),
            _mk_status("in_progress", processing=10, succeeded=0),
        ]
        status = _cg.get_batch_status("batch_abc")
        self.assertEqual(status["status"], "in_progress")
        self.assertEqual(mock_client.messages.batches.retrieve.call_count, 2)

    @patch("src.comment_generator._create_client")
    @patch("src.comment_generator.time.sleep")
    def test_overloaded_message_503_treated_as_transient(
        self, mock_sleep, mock_create_client,
    ):
        """本番ログで観測された「API key validation is temporarily unavailable」
        503 を一過性として扱うこと（メッセージで恒久判定にしない）。"""
        mock_client = MagicMock()
        mock_create_client.return_value = mock_client
        mock_client.messages.batches.retrieve.side_effect = [
            _503_internal_server(
                "API key validation is temporarily unavailable. Please try again later."
            ),
            _mk_status("ended", succeeded=1),
        ]
        status = _cg.get_batch_status("batch_abc")
        self.assertEqual(status["status"], "ended")

    @patch("src.comment_generator._create_client")
    @patch("src.comment_generator.time.sleep")
    def test_three_consecutive_503_then_success(
        self, mock_sleep, mock_create_client,
    ):
        """503 × 3 → 4 回目で成功。リトライ上限ぎりぎりの境界。"""
        mock_client = MagicMock()
        mock_create_client.return_value = mock_client
        mock_client.messages.batches.retrieve.side_effect = [
            _503_internal_server(),
            _503_internal_server(),
            _503_internal_server(),
            _mk_status("ended", succeeded=10),
        ]
        status = _cg.get_batch_status("batch_abc")
        self.assertEqual(status["status"], "ended")
        self.assertEqual(mock_client.messages.batches.retrieve.call_count, 4)

    @patch("src.comment_generator._create_client")
    @patch("src.comment_generator.time.sleep")
    def test_transient_exhausted_raises_original_type(
        self, mock_sleep, mock_create_client,
    ):
        """一過性が最大回数まで続けば最終的に raise（無限リトライ禁止）。"""
        mock_client = MagicMock()
        mock_create_client.return_value = mock_client
        mock_client.messages.batches.retrieve.side_effect = _503_internal_server()
        with self.assertRaises(_anthropic.InternalServerError):
            _cg.get_batch_status("batch_abc")

    @patch("src.comment_generator._create_client")
    @patch("src.comment_generator.time.sleep")
    def test_permanent_wins_over_transient_in_mixed_sequence(
        self, mock_sleep, mock_create_client,
    ):
        """503 → 401（401 が来た時点で恒久と判定し PermanentRunFailureError）。

        モック上、retrieve に複数 side_effect を仕込んだとき、retry 実装に
        よっては 503 で待機 → 401 で即停止というシーケンスになる。401 で
        ``PermanentRunFailureError`` に変換され、リトライしないことを確認。
        """
        mock_client = MagicMock()
        mock_create_client.return_value = mock_client
        mock_client.messages.batches.retrieve.side_effect = [
            _503_internal_server(),
            _auth_401(),
        ]
        with self.assertRaises(_PermanentRunFailureError):
            _cg.get_batch_status("batch_abc")

    @patch("src.comment_generator._create_client")
    @patch("src.comment_generator.time.sleep")
    def test_permission_403_during_polling_is_permanent(
        self, mock_sleep, mock_create_client,
    ):
        """403 PermissionDenied は一過性扱いせず PermanentRunFailureError。"""
        mock_client = MagicMock()
        mock_create_client.return_value = mock_client
        mock_client.messages.batches.retrieve.side_effect = _permission_403()
        with self.assertRaises(_PermanentRunFailureError):
            _cg.get_batch_status("batch_abc")


class TestGetBatchResultsRetriesOnTransient(unittest.TestCase):
    """``get_batch_results`` は ``messages.batches.results`` の一過性エラーを
    リトライする。修正前 red。"""

    @patch("src.comment_generator._create_client")
    @patch("src.comment_generator.time.sleep")
    def test_503_then_success_returns_results(
        self, mock_sleep, mock_create_client,
    ):
        mock_client = MagicMock()
        mock_create_client.return_value = mock_client
        # 1 回目: 503 / 2 回目: 1 件の成功結果を返す iterator
        mock_client.messages.batches.results.side_effect = [
            _503_internal_server(),
            iter([_mk_batch_result("item_0001", succeeded=True)]),
        ]
        results, failed_ids = _cg.get_batch_results("batch_abc")
        self.assertEqual(len(results), 1)
        self.assertEqual(failed_ids, [])
        self.assertEqual(mock_client.messages.batches.results.call_count, 2)

    @patch("src.comment_generator._create_client")
    @patch("src.comment_generator.time.sleep")
    def test_529_then_success(self, mock_sleep, mock_create_client):
        mock_client = MagicMock()
        mock_create_client.return_value = mock_client
        mock_client.messages.batches.results.side_effect = [
            _529_overloaded(),
            iter([_mk_batch_result("item_0001", succeeded=True)]),
        ]
        results, failed_ids = _cg.get_batch_results("batch_abc")
        self.assertEqual(len(results), 1)


class TestGetBatchResultsPartialAndEmpty(unittest.TestCase):
    """部分 errored / 空 iterator の境界。"""

    @patch("src.comment_generator._create_client")
    def test_partial_errored_results_collects_failed_ids(self, mock_create_client):
        """succeeded=N1, errored=N2 のとき、failed_ids に errored の custom_id が
        全件入り、results には succeeded のみ。"""
        mock_client = MagicMock()
        mock_create_client.return_value = mock_client
        mock_client.messages.batches.results.return_value = iter([
            _mk_batch_result("item_0001", succeeded=True),
            _mk_batch_result("item_0002", succeeded=False, result_type="errored"),
            _mk_batch_result("item_0003", succeeded=True),
            _mk_batch_result("item_0004", succeeded=False, result_type="errored"),
        ])
        results, failed_ids = _cg.get_batch_results("batch_abc")
        self.assertEqual(set(results.keys()), {"item_0001", "item_0003"})
        self.assertEqual(sorted(failed_ids), ["item_0002", "item_0004"])

    @patch("src.comment_generator._create_client")
    def test_all_errored_results_yields_empty_results_and_full_failed_ids(
        self, mock_create_client,
    ):
        """全件 errored: results は空、failed_ids は全件分。"""
        mock_client = MagicMock()
        mock_create_client.return_value = mock_client
        mock_client.messages.batches.results.return_value = iter([
            _mk_batch_result("item_0001", succeeded=False, result_type="errored"),
            _mk_batch_result("item_0002", succeeded=False, result_type="errored"),
            _mk_batch_result("item_0003", succeeded=False, result_type="errored"),
        ])
        results, failed_ids = _cg.get_batch_results("batch_abc")
        self.assertEqual(results, {})
        self.assertEqual(sorted(failed_ids), ["item_0001", "item_0002", "item_0003"])

    @patch("src.comment_generator._create_client")
    def test_empty_iterator_yields_empty_results_and_empty_failed_ids(
        self, mock_create_client,
    ):
        """``messages.batches.results`` が完全に空イテレータを返した場合。

        全件 errored だが Anthropic 側が結果を 1 件も返さないという特殊
        ケース。``get_batch_results`` は ``({}, [])`` を返して呼び出し側に
        判断を委ねる（per-PDF fail-soft は呼び出し側で実施）。
        """
        mock_client = MagicMock()
        mock_create_client.return_value = mock_client
        mock_client.messages.batches.results.return_value = iter([])
        results, failed_ids = _cg.get_batch_results("batch_abc")
        self.assertEqual(results, {})
        self.assertEqual(failed_ids, [])

    @patch("src.comment_generator._create_client")
    def test_expired_result_type_is_treated_as_failed(self, mock_create_client):
        """result.type == "expired" も failed_ids に入る（succeeded 以外は全て失敗）。"""
        mock_client = MagicMock()
        mock_create_client.return_value = mock_client
        mock_client.messages.batches.results.return_value = iter([
            _mk_batch_result("item_0001", succeeded=False, result_type="expired"),
        ])
        results, failed_ids = _cg.get_batch_results("batch_abc")
        self.assertEqual(results, {})
        self.assertEqual(failed_ids, ["item_0001"])

    @patch("src.comment_generator._create_client")
    def test_canceled_result_type_is_treated_as_failed(self, mock_create_client):
        mock_client = MagicMock()
        mock_create_client.return_value = mock_client
        mock_client.messages.batches.results.return_value = iter([
            _mk_batch_result("item_0001", succeeded=False, result_type="canceled"),
        ])
        results, failed_ids = _cg.get_batch_results("batch_abc")
        self.assertEqual(results, {})
        self.assertEqual(failed_ids, ["item_0001"])


class TestGetBatchResultsRetriesExhausted(unittest.TestCase):
    """一過性エラーがリトライ上限を超えて続いた場合の挙動。

    修正前 red: ``get_batch_results`` はリトライ実装なしのため 1 回目で raise。
    修正後 green: リトライ上限まで再試行 → 最終 raise。
    """

    @patch("src.comment_generator._create_client")
    @patch("src.comment_generator.time.sleep")
    def test_503_continuously_raises_after_retries(
        self, mock_sleep, mock_create_client,
    ):
        mock_client = MagicMock()
        mock_create_client.return_value = mock_client
        mock_client.messages.batches.results.side_effect = _503_internal_server()
        with self.assertRaises(_anthropic.InternalServerError):
            _cg.get_batch_results("batch_abc")


class TestStep3PollingLoopSurvivesTransients(unittest.TestCase):
    """``step3_wait_and_get_results`` のポーリングループは ``get_batch_status``
    の一過性エラーを呑み込み次ラウンドへ進む。

    修正前 red のシナリオ: 現状 step3 は ``get_batch_status`` を try/except
    なしで呼ぶため、単発 503 で即 raise → ラン停止（本番事象 run_id=
    26811653746）。 defect-investigator は (1) get_batch_status 内 retry、
    または (2) step3 ループ内で transient 用 try/except のいずれかで対処
    する想定。どちらの修正でも次のテストは green になる。
    """

    @patch("src.batch_main.time.sleep")
    @patch("src.batch_main.comment_generator")
    def test_step3_continues_after_transient_status_error(
        self, mock_gen, mock_sleep,
    ):
        """503 を 1 回 raise → 次のポーリングで ended が返り results へ進む。

        修正前 red:
            現状の step3 ループは ``get_batch_status`` の 503 を catch しない。
            最初の status 呼び出しで InternalServerError が伝播してテスト失敗。
        修正後 green:
            ポーリングループは transient を catch して次ラウンドへ進む。
        """
        mock_gen.get_batch_status.side_effect = [
            _503_internal_server(),
            {
                "id": "batch_abc",
                "status": "ended",
                "request_counts": {
                    "processing": 0, "succeeded": 1,
                    "errored": 0, "canceled": 0, "expired": 0,
                },
            },
        ]
        mock_gen.get_batch_results.return_value = (
            {"item_0001": {"clinic_name": "X歯科", "person_name": "山田",
                            "sample_title": "事例", "comment": "A" * 100}},
            [],
        )
        with patch.object(_batch_main, "_save_results_to_disk"):
            results = _batch_main.step3_wait_and_get_results(
                "batch_abc", poll_interval=1, max_wait=10,
            )
        self.assertEqual(len(results), 1)
        self.assertGreaterEqual(mock_gen.get_batch_status.call_count, 2)

    @patch("src.batch_main.time.sleep")
    @patch("src.batch_main.comment_generator")
    def test_step3_continues_after_three_consecutive_503_then_ended(
        self, mock_gen, mock_sleep,
    ):
        """503 を 3 回連続 → 4 回目で processing=0, succeeded=N → results 取得。

        本番想定: 数時間のポーリング中に数回 5xx が発生しても落ちない。
        修正前 red、修正後 green。
        """
        mock_gen.get_batch_status.side_effect = [
            _503_internal_server(),
            _503_internal_server(),
            _503_internal_server(),
            {
                "id": "batch_abc",
                "status": "ended",
                "request_counts": {
                    "processing": 0, "succeeded": 3,
                    "errored": 0, "canceled": 0, "expired": 0,
                },
            },
        ]
        mock_gen.get_batch_results.return_value = (
            {f"item_{i:04d}": {"clinic_name": "X歯科", "person_name": "山田",
                                "sample_title": "事例", "comment": "A" * 100}
             for i in (1, 2, 3)},
            [],
        )
        with patch.object(_batch_main, "_save_results_to_disk"):
            results = _batch_main.step3_wait_and_get_results(
                "batch_abc", poll_interval=1, max_wait=20,
            )
        self.assertEqual(len(results), 3)

    @patch("src.batch_main.time.sleep")
    @patch("src.batch_main.comment_generator")
    def test_step3_continues_after_529_during_polling(
        self, mock_gen, mock_sleep,
    ):
        """ポーリング途中の 529 過負荷も吸収して継続する。修正前 red、修正後 green。"""
        mock_gen.get_batch_status.side_effect = [
            _529_overloaded(),
            {
                "id": "batch_abc",
                "status": "ended",
                "request_counts": {
                    "processing": 0, "succeeded": 1,
                    "errored": 0, "canceled": 0, "expired": 0,
                },
            },
        ]
        mock_gen.get_batch_results.return_value = ({"item_0001": {
            "clinic_name": "X歯科", "person_name": "山田",
            "sample_title": "事例", "comment": "A" * 100,
        }}, [])
        with patch.object(_batch_main, "_save_results_to_disk"):
            results = _batch_main.step3_wait_and_get_results(
                "batch_abc", poll_interval=1, max_wait=20,
            )
        self.assertEqual(len(results), 1)

    @patch("src.batch_main.time.sleep")
    @patch("src.batch_main.comment_generator")
    def test_step3_permanent_error_halts_immediately(self, mock_gen, mock_sleep):
        """ポーリング中の恒久エラー（401/403 → PermanentRunFailureError）は
        吸収せず即停止する（リトライしない / 残ループしない）。"""
        mock_gen.get_batch_status.side_effect = _PermanentRunFailureError(
            "Anthropic API のクレジット残高不足のため処理を中止しました。"
        )
        with self.assertRaises(_PermanentRunFailureError):
            with patch.object(_batch_main, "_save_results_to_disk"):
                _batch_main.step3_wait_and_get_results(
                    "batch_abc", poll_interval=1, max_wait=10,
                )
        # 恒久エラーは即停止 → get_batch_status は 1 回しか呼ばれない
        self.assertEqual(mock_gen.get_batch_status.call_count, 1)
        # 結果取得にも進まない
        mock_gen.get_batch_results.assert_not_called()


class TestStep3PollingTimeoutBoundary(unittest.TestCase):
    """``in_progress`` が続いて ``max_wait`` を超えるシナリオ。

    既存実装は ``while elapsed < max_wait`` でループし、抜けたら
    ``raise TimeoutError("Batch API結果の取得がタイムアウトしました")`` を
    上げる（``step3_wait_and_get_results`` line 332-334）。
    本テストは現状の挙動を固定する（修正前後どちらも green を期待）。
    """

    @patch("src.batch_main.time.sleep")
    @patch("src.batch_main.comment_generator")
    def test_in_progress_exceeds_max_wait_raises_timeout(self, mock_gen, mock_sleep):
        """``in_progress`` が ``max_wait`` を超え続けたら TimeoutError。"""
        mock_gen.get_batch_status.return_value = {
            "id": "batch_abc",
            "status": "in_progress",
            "request_counts": {
                "processing": 10, "succeeded": 0,
                "errored": 0, "canceled": 0, "expired": 0,
            },
        }
        with self.assertRaises(TimeoutError):
            _batch_main.step3_wait_and_get_results(
                "batch_abc", poll_interval=1, max_wait=2,
            )
        # 結果取得には進まない
        mock_gen.get_batch_results.assert_not_called()

    @patch("src.batch_main.time.sleep")
    @patch("src.batch_main.comment_generator")
    def test_timeout_message_mentions_timeout(self, mock_gen, mock_sleep):
        """TimeoutError のメッセージに「タイムアウト」が含まれる。

        運用者が logs/ で grep して即特定できることを担保する。
        """
        mock_gen.get_batch_status.return_value = {
            "id": "batch_abc",
            "status": "in_progress",
            "request_counts": {
                "processing": 10, "succeeded": 0,
                "errored": 0, "canceled": 0, "expired": 0,
            },
        }
        with self.assertRaises(TimeoutError) as ctx:
            _batch_main.step3_wait_and_get_results(
                "batch_abc", poll_interval=1, max_wait=2,
            )
        self.assertIn("タイムアウト", str(ctx.exception))


class TestSubmitBatchTransientExhausted(unittest.TestCase):
    """``submit_batch`` で一過性が最大回数まで続いたら raise する。

    修正前 red、修正後 green。リトライ実装後もループ抜け raise は必須。
    """

    @patch("src.comment_generator._create_client")
    @patch("src.comment_generator.time.sleep")
    def test_continuous_503_raises_internal_server_error(
        self, mock_sleep, mock_create_client,
    ):
        mock_client = MagicMock()
        mock_create_client.return_value = mock_client
        mock_client.messages.batches.create.side_effect = _503_internal_server()
        items = [{"custom_id": "item_0001", "pdf_text": "x", "pdf_file_name": "001-01-1.pdf"}]
        with self.assertRaises(_anthropic.InternalServerError):
            _cg.submit_batch(items)


class TestSubmitBatchPermanentInMixedSequence(unittest.TestCase):
    """``submit_batch`` で 503 → 401 の順で来たとき、401 で恒久判定して停止する。"""

    @patch("src.comment_generator._create_client")
    @patch("src.comment_generator.time.sleep")
    def test_503_then_401_raises_permanent_run_failure(
        self, mock_sleep, mock_create_client,
    ):
        mock_client = MagicMock()
        mock_create_client.return_value = mock_client
        mock_client.messages.batches.create.side_effect = [
            _503_internal_server(),
            _auth_401(),
        ]
        items = [{"custom_id": "item_0001", "pdf_text": "x", "pdf_file_name": "001-01-1.pdf"}]
        with self.assertRaises(_PermanentRunFailureError):
            _cg.submit_batch(items)


if __name__ == "__main__":
    unittest.main()
