"""エッジケーステスト網羅（QA キャンペーン第 2 段）。

最近マージされた以下のモジュールを重点的にカバーする:

- ``src/profile.py``           : PR #19 で新規追加されたプロファイル loader
- ``src/sheets_client.py``     : PR #17 / #19 で管理番号 prefix 対応・出力一覧シート対応
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

from src import drive_client, profile as profile_module, sheets_client
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
        self.assertIn("management_number_prefix", msg)

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

    def test_management_number_prefix_immutable(self):
        with self.assertRaises(FrozenInstanceError):
            self._cfg().management_number_prefix = "X-"  # type: ignore[misc]

    def test_display_name_immutable(self):
        with self.assertRaises(FrozenInstanceError):
            self._cfg().display_name = "changed"  # type: ignore[misc]

    def test_cannot_add_new_field(self):
        """新規フィールド追加も拒否される（frozen=True かつ slots 風挙動）。"""
        with self.assertRaises(FrozenInstanceError):
            self._cfg().extra_field = "anything"  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# 2. sheets_client.get_max_management_number エッジケース
# ---------------------------------------------------------------------------


def _make_sheets_service_with_values(values: list[list[Any]]) -> MagicMock:
    """``_ensure_output_sheet`` を通過させつつ A 列の取得結果を任意に与えるヘルパ。"""
    service = MagicMock()
    # _ensure_output_sheet 用：シートは既存、ヘッダーも既存
    service.spreadsheets.return_value.get.return_value.execute.return_value = {
        "sheets": [{"properties": {"title": "out"}}]
    }
    service.spreadsheets.return_value.values.return_value.get.return_value.execute.side_effect = [
        {"values": [["管理番号"]]},  # _ensure_output_sheet 内のヘッダー読み取り
        {"values": values},          # 実際の A 列読み取り
    ]
    return service


class TestGetMaxManagementNumberEdgeCases(unittest.TestCase):
    """``get_max_management_number`` の prefix / 異常データ / 境界。"""

    @patch("src.sheets_client.get_sheets_service")
    def test_prefix_none_and_empty_string_are_equivalent(self, mock_service):
        """``prefix=None`` と ``prefix=""`` が同一挙動（jissen_default 互換）。"""
        data = [["管理番号"], ["000003"], ["J24Q1-999999"], ["000012"]]

        mock_service.return_value = _make_sheets_service_with_values(data)
        none_result = sheets_client.get_max_management_number(
            spreadsheet_id="s", sheet_name="out", prefix=None
        )

        mock_service.return_value = _make_sheets_service_with_values(data)
        empty_result = sheets_client.get_max_management_number(
            spreadsheet_id="s", sheet_name="out", prefix=""
        )

        self.assertEqual(none_result, empty_result)
        self.assertEqual(none_result, 12)

    @patch("src.sheets_client.get_sheets_service")
    def test_prefix_excludes_pure_numeric_rows(self, mock_service):
        """``prefix="J24Q1-"`` 指定時は prefix なしの純粋数値行が混入しない。"""
        mock_service.return_value = _make_sheets_service_with_values([
            ["管理番号"],
            ["J24Q1-000005"],
            ["000999"],  # 既存 legacy 形式 → 無視されるべき
            ["J24Q1-000003"],
        ])
        result = sheets_client.get_max_management_number(
            spreadsheet_id="s", sheet_name="out", prefix="J24Q1-"
        )
        self.assertEqual(result, 5)

    @patch("src.sheets_client.get_sheets_service")
    def test_prefix_ignores_other_quarter_prefix(self, mock_service):
        """``prefix="J24Q1-"`` 指定時、別四半期 ``J24Q2-`` は除外される。"""
        mock_service.return_value = _make_sheets_service_with_values([
            ["管理番号"],
            ["J24Q1-000010"],
            ["J24Q2-000999"],
            ["J24Q3-099999"],
            ["J24Q1-000008"],
        ])
        result = sheets_client.get_max_management_number(
            spreadsheet_id="s", sheet_name="out", prefix="J24Q1-"
        )
        self.assertEqual(result, 10)

    @patch("src.sheets_client.get_sheets_service")
    def test_strip_handles_leading_trailing_whitespace_in_cell(self, mock_service):
        """セル値 ``" J24Q1-000005 "`` （前後空白）も正しく解釈される。"""
        mock_service.return_value = _make_sheets_service_with_values([
            ["管理番号"],
            [" J24Q1-000005 "],
            ["J24Q1-000003"],
        ])
        result = sheets_client.get_max_management_number(
            spreadsheet_id="s", sheet_name="out", prefix="J24Q1-"
        )
        self.assertEqual(result, 5)

    @patch("src.sheets_client.get_sheets_service")
    def test_fullwidth_digits_in_cell_are_not_treated_as_match(self, mock_service):
        """全角数字 ``Ｊ２４Ｑ１ー００００５`` は ASCII prefix と一致しないので 0。

        全角文字は半角 ``J24Q1-`` で startswith しないため対象外となる。
        （これは仕様：表示文字列が違うものは混ぜない方が安全）
        """
        mock_service.return_value = _make_sheets_service_with_values([
            ["管理番号"],
            ["Ｊ２４Ｑ１ー００００５"],  # 全角
        ])
        result = sheets_client.get_max_management_number(
            spreadsheet_id="s", sheet_name="out", prefix="J24Q1-"
        )
        self.assertEqual(result, 0)

    @patch("src.sheets_client.get_sheets_service")
    def test_boundary_999999_then_one_million(self, mock_service):
        """999999 + 1 = 1000000 — 7 桁になっても int としては正常に最大値を取れる。

        書式（6 桁 vs 7 桁）の責務は呼び出し側（main.py / batch_main.py の f"{n:06d}"）
        にあるため、ここでは数値として 1000000 を返せること自体を検証する。
        """
        mock_service.return_value = _make_sheets_service_with_values([
            ["管理番号"],
            ["J24Q1-999999"],
            ["J24Q1-1000000"],
        ])
        result = sheets_client.get_max_management_number(
            spreadsheet_id="s", sheet_name="out", prefix="J24Q1-"
        )
        self.assertEqual(result, 1000000)

    @patch("src.sheets_client.get_sheets_service")
    def test_negative_numeric_part_is_lower_than_zero_so_max_stays_zero(self, mock_service):
        """負数 ``J24Q1--000001`` は int() 上 -1 として解釈されるが 0 より小さいので max は 0。"""
        mock_service.return_value = _make_sheets_service_with_values([
            ["管理番号"],
            ["J24Q1--000001"],  # int('-000001') == -1
            ["J24Q1--000099"],
        ])
        result = sheets_client.get_max_management_number(
            spreadsheet_id="s", sheet_name="out", prefix="J24Q1-"
        )
        self.assertEqual(result, 0)

    @patch("src.sheets_client.get_sheets_service")
    def test_decimal_numeric_part_is_skipped(self, mock_service):
        """``J24Q1-000005.5`` のような小数は int() に失敗してスキップ。"""
        mock_service.return_value = _make_sheets_service_with_values([
            ["管理番号"],
            ["J24Q1-000005.5"],
            ["J24Q1-000003"],
        ])
        result = sheets_client.get_max_management_number(
            spreadsheet_id="s", sheet_name="out", prefix="J24Q1-"
        )
        self.assertEqual(result, 3)

    @patch("src.sheets_client.get_sheets_service")
    def test_non_string_cell_is_coerced(self, mock_service):
        """セル値が int 型のままで来た場合も str に変換して処理される。"""
        mock_service.return_value = _make_sheets_service_with_values([
            ["管理番号"],
            [12345],  # int 型
            ["000099"],
        ])
        result = sheets_client.get_max_management_number(
            spreadsheet_id="s", sheet_name="out", prefix=None
        )
        self.assertEqual(result, 12345)

    @patch("src.sheets_client.get_sheets_service")
    def test_one_million_rows_completes_quickly_o_n(self, mock_service):
        """100 万行を超える Sheet（mock）でも O(n) で完走すること。

        基準：5 秒以内（CI 環境を考慮して非常に緩い閾値）。
        """
        import time
        large_data = [["管理番号"]] + [[f"J24Q1-{i:07d}"] for i in range(1_000_001)]
        mock_service.return_value = _make_sheets_service_with_values(large_data)

        start = time.perf_counter()
        result = sheets_client.get_max_management_number(
            spreadsheet_id="s", sheet_name="out", prefix="J24Q1-"
        )
        elapsed = time.perf_counter() - start
        self.assertEqual(result, 1_000_000)
        self.assertLess(elapsed, 5.0, f"100万行処理に {elapsed:.2f} 秒（O(n) を逸脱）")


# ---------------------------------------------------------------------------
# 3. normalize_name_for_match エッジケース
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
# 4. make_output_filename エッジケース
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

    @pytest.mark.skip(
        reason=(
            "Bug found (severity=low): sample_title に既に '.pdf' が "
            "含まれている場合、現実装は二重拡張子 '...＿xxx.pdf.pdf' を生成する。"
            "comment_generator が拡張子ありで返す可能性は低いが、"
            "防御的に _sanitize_filename で .pdf を 1 つだけ残す処理を入れる "
            "ことを後続 PR で検討。"
        )
    )
    def test_double_extension_is_collapsed(self):
        """``sample_title="x.pdf"`` の場合に ``.pdf.pdf`` にならない。"""
        result = make_output_filename("A", "B", "x.pdf")
        # 期待: ".pdf" は 1 回だけ末尾に付く
        self.assertEqual(result.count(".pdf"), 1)
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
# 5. find_or_create_folder エッジケース
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
# 6. 管理番号 prefix 衝突
# ---------------------------------------------------------------------------


class TestManagementNumberPrefixCollision(unittest.TestCase):
    """profile A と profile B の prefix が「片方が他方の前方一致」になる場合。

    例: A=``J24Q1-`` vs B=``J24Q1-X-`` の場合、A は B の行を巻き込んで
    最大値を計算してしまう可能性がある（``J24Q1-X-...`` は ``J24Q1-`` で
    startswith するため）。

    NOTE: 仕様上 prefix の一意性は profile-system-architect エージェントが
    検知すべき。本テストは「実装が現に何を返すか」を固定し、defect として
    認識できるようにする目的のもの。
    """

    @patch("src.sheets_client.get_sheets_service")
    def test_shorter_prefix_picks_up_longer_prefix_rows(self, mock_service):
        """A の prefix='J24Q1-' は B='J24Q1-X-' の行 'J24Q1-X-000005' を
        誤って取り込もうとするが、``int('X-000005')`` 失敗でスキップされる。
        結果として実害なしで 0 になることを確認する。
        """
        mock_service.return_value = _make_sheets_service_with_values([
            ["管理番号"],
            ["J24Q1-X-000005"],
            ["J24Q1-X-000099"],
        ])
        result = sheets_client.get_max_management_number(
            spreadsheet_id="s", sheet_name="out", prefix="J24Q1-"
        )
        # int('X-000005') は ValueError → スキップされて 0 のまま
        self.assertEqual(result, 0)

    @patch("src.sheets_client.get_sheets_service")
    def test_prefix_substring_without_separator_creates_collision(self, mock_service):
        """A='J24Q1' (区切り無し) は B='J24Q1-X-' の行を `'J24Q1' + '-X-...'` と
        解釈して数値部 '-X-000005' を int() しようとして失敗するため安全。

        ただし B='J24Q100005' のような数値直結 prefix だと collision が起き得る
        ことを示す（実害ケース）。
        """
        mock_service.return_value = _make_sheets_service_with_values([
            ["管理番号"],
            ["J24Q100005"],  # 仮にこの prefix を持つプロファイルが存在したら衝突
        ])
        result = sheets_client.get_max_management_number(
            spreadsheet_id="s", sheet_name="out", prefix="J24Q1"
        )
        # 数値部 '00005' → 5
        self.assertEqual(result, 5)
        # WARNING: prefix の一意性を YAML schema で担保すべき（test_profile_yaml_sanity
        # で検証）。


# ---------------------------------------------------------------------------
# 7. YAML プロファイル全件 sanity test
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
        """全プロファイルが必須 8 フィールドを持つ。"""
        required = (
            "display_name",
            "document_type",
            "period",
            "input_folder_id_secret",
            "output_folder_id_secret",
            "output_sheet_name",
            "management_number_prefix",
            "prompt_template",
        )
        for name, data in self.profiles.items():
            with self.subTest(profile=name):
                missing = [k for k in required if k not in data]
                self.assertEqual(missing, [], f"{name}: 欠落 {missing}")

    def test_management_number_prefix_is_unique(self):
        """同一 management_number_prefix を持つプロファイルが複数ないこと。

        prefix が衝突すると採番が混じり、別四半期に同一管理番号が振られる。
        """
        seen: dict[str, list[str]] = {}
        for name, data in self.profiles.items():
            prefix = data["management_number_prefix"]
            # 空文字は jissen_default のみ許容する仕様。それ以外で空はエラー。
            seen.setdefault(prefix, []).append(name)
        duplicates = {k: v for k, v in seen.items() if len(v) > 1}
        # 空文字 prefix は jissen_default 1 つだけ
        empty_prefix_owners = seen.get("", [])
        self.assertLessEqual(
            len(empty_prefix_owners),
            1,
            f"空 prefix を複数 profile が持っている: {empty_prefix_owners}",
        )
        # 空文字以外は完全に一意
        non_empty_duplicates = {
            k: v for k, v in duplicates.items() if k != ""
        }
        self.assertEqual(
            non_empty_duplicates, {},
            f"management_number_prefix が重複: {non_empty_duplicates}",
        )

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

    def test_no_prefix_is_a_strict_prefix_of_another(self):
        """ある profile の prefix が別 profile の prefix の前方一致になっていないこと。

        例: A=``J24Q1-`` vs B=``J24Q1-X-`` の組合せ。
        A の sheet を読む際に B の行を誤って巻き込む可能性がある（数値解析で
        ほぼ失敗するが、防御的に schema レベルで弾く）。
        """
        prefixes = [
            (name, data["management_number_prefix"])
            for name, data in self.profiles.items()
            if data["management_number_prefix"]  # 空文字は除外
        ]
        for a_name, a_prefix in prefixes:
            for b_name, b_prefix in prefixes:
                if a_name == b_name:
                    continue
                with self.subTest(a=a_name, b=b_name):
                    self.assertFalse(
                        a_prefix != b_prefix and b_prefix.startswith(a_prefix),
                        f"prefix '{a_prefix}' ({a_name}) は "
                        f"'{b_prefix}' ({b_name}) の前方一致になっている",
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
# 8. profile load の sanity（実 YAML 全件）
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


if __name__ == "__main__":
    unittest.main()
