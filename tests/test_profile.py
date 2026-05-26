"""profile loader / resolver のユニットテスト。

実 YAML（``profiles/*.yaml``）を直接参照することで、
schema 違反や typo が混入したら即座に検知できる safety net とする。
"""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from src import profile as profile_module


class TestListAvailableProfiles(unittest.TestCase):
    """``list_available_profiles`` の挙動。"""

    def test_returns_five_profiles(self):
        """default + 2024 Q1〜Q4 の計 5 つを返す。"""
        names = profile_module.list_available_profiles()
        self.assertIn("jissen_default", names)
        self.assertIn("jissen_2024_q1", names)
        self.assertIn("jissen_2024_q2", names)
        self.assertIn("jissen_2024_q3", names)
        self.assertIn("jissen_2024_q4", names)
        # 想定外のプロファイルが混ざっていないことの目安
        self.assertGreaterEqual(len(names), 5)

    def test_returns_sorted_list(self):
        """戻り値はソート済み。"""
        names = profile_module.list_available_profiles()
        self.assertEqual(names, sorted(names))


class TestLoadDefaultProfile(unittest.TestCase):
    """既存挙動を担保する jissen_default プロファイル。"""

    def test_loads_with_existing_secrets(self):
        """既存環境変数 DRIVE_FOLDER_ID / DRIVE_OUTPUT_FOLDER_ID で読める。"""
        with patch.dict(
            os.environ,
            {
                "DRIVE_FOLDER_ID": "input_folder_xxx",
                "DRIVE_OUTPUT_FOLDER_ID": "output_folder_yyy",
            },
            clear=False,
        ):
            cfg = profile_module.load_profile("jissen_default")

        self.assertEqual(cfg.name, "jissen_default")
        self.assertEqual(cfg.document_type, "jissen_practice_case")
        self.assertEqual(cfg.input_folder_id, "input_folder_xxx")
        self.assertEqual(cfg.output_folder_id, "output_folder_yyy")
        self.assertEqual(cfg.output_sheet_name, "出力一覧")
        self.assertEqual(cfg.prompt_template, "jissen_practice_case")


class TestLoadQuarterlyProfiles(unittest.TestCase):
    """2024 年度 Q1〜Q4 プロファイル。"""

    def test_loads_2024_q1(self):
        with patch.dict(
            os.environ,
            {
                "DRIVE_FOLDER_JISSEN_2024_Q1": "in_q1",
                "DRIVE_OUTPUT_JISSEN_2024_Q1": "out_q1",
            },
            clear=False,
        ):
            cfg = profile_module.load_profile("jissen_2024_q1")

        self.assertEqual(cfg.name, "jissen_2024_q1")
        self.assertEqual(cfg.period, "2024_q1")
        self.assertEqual(cfg.input_folder_id, "in_q1")
        self.assertEqual(cfg.output_folder_id, "out_q1")
        self.assertEqual(cfg.output_sheet_name, "実践事例_2024Q1_出力一覧")
        self.assertEqual(cfg.prompt_template, "jissen_practice_case")

    def test_loads_2024_q2(self):
        with patch.dict(
            os.environ,
            {
                "DRIVE_FOLDER_JISSEN_2024_Q2": "in_q2",
                "DRIVE_OUTPUT_JISSEN_2024_Q2": "out_q2",
            },
            clear=False,
        ):
            cfg = profile_module.load_profile("jissen_2024_q2")
        self.assertEqual(cfg.period, "2024_q2")
        self.assertEqual(cfg.output_sheet_name, "実践事例_2024Q2_出力一覧")

    def test_loads_2024_q3(self):
        with patch.dict(
            os.environ,
            {
                "DRIVE_FOLDER_JISSEN_2024_Q3": "in_q3",
                "DRIVE_OUTPUT_JISSEN_2024_Q3": "out_q3",
            },
            clear=False,
        ):
            cfg = profile_module.load_profile("jissen_2024_q3")
        self.assertEqual(cfg.period, "2024_q3")
        self.assertEqual(cfg.output_sheet_name, "実践事例_2024Q3_出力一覧")

    def test_loads_2024_q4(self):
        with patch.dict(
            os.environ,
            {
                "DRIVE_FOLDER_JISSEN_2024_Q4": "in_q4",
                "DRIVE_OUTPUT_JISSEN_2024_Q4": "out_q4",
            },
            clear=False,
        ):
            cfg = profile_module.load_profile("jissen_2024_q4")
        self.assertEqual(cfg.period, "2024_q4")
        self.assertEqual(cfg.output_sheet_name, "実践事例_2024Q4_出力一覧")

    def test_all_quarterlies_share_prompt_template(self):
        """今回は全プロファイルでプロンプトを共有する設計（jissen_practice_case）。"""
        env_pairs = [
            ("DRIVE_FOLDER_JISSEN_2024_Q1", "DRIVE_OUTPUT_JISSEN_2024_Q1"),
            ("DRIVE_FOLDER_JISSEN_2024_Q2", "DRIVE_OUTPUT_JISSEN_2024_Q2"),
            ("DRIVE_FOLDER_JISSEN_2024_Q3", "DRIVE_OUTPUT_JISSEN_2024_Q3"),
            ("DRIVE_FOLDER_JISSEN_2024_Q4", "DRIVE_OUTPUT_JISSEN_2024_Q4"),
        ]
        env = {k: f"id_{k}" for pair in env_pairs for k in pair}
        with patch.dict(os.environ, env, clear=False):
            for q in ("q1", "q2", "q3", "q4"):
                cfg = profile_module.load_profile(f"jissen_2024_{q}")
                self.assertEqual(cfg.prompt_template, "jissen_practice_case")
                self.assertEqual(cfg.document_type, "jissen_practice_case")


class TestErrorHandling(unittest.TestCase):

    def test_missing_profile_raises_file_not_found(self):
        with self.assertRaises(FileNotFoundError) as ctx:
            profile_module.load_profile("does_not_exist_xxx")
        # 利用可能なプロファイル名がエラーメッセージに含まれる
        self.assertIn("jissen_default", str(ctx.exception))

    def test_missing_input_secret_raises_value_error(self):
        """input_folder_id_secret が指す環境変数が未設定なら ValueError。"""
        # クリーンな環境で実行
        env = {
            k: v
            for k, v in os.environ.items()
            if k
            not in (
                "DRIVE_FOLDER_JISSEN_2024_Q1",
                "DRIVE_OUTPUT_JISSEN_2024_Q1",
            )
        }
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaises(ValueError) as ctx:
                profile_module.load_profile("jissen_2024_q1")
        self.assertIn("DRIVE_FOLDER_JISSEN_2024_Q1", str(ctx.exception))

    def test_missing_output_secret_raises_value_error(self):
        """output_folder_id_secret が指す環境変数が未設定なら ValueError。
        input は設定済みでも output が無ければ失敗するという独立性を担保。
        """
        env = {
            k: v
            for k, v in os.environ.items()
            if k
            not in (
                "DRIVE_FOLDER_JISSEN_2024_Q1",
                "DRIVE_OUTPUT_JISSEN_2024_Q1",
            )
        }
        env["DRIVE_FOLDER_JISSEN_2024_Q1"] = "in_q1"
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaises(ValueError) as ctx:
                profile_module.load_profile("jissen_2024_q1")
        self.assertIn("DRIVE_OUTPUT_JISSEN_2024_Q1", str(ctx.exception))


class TestProfileConfigImmutable(unittest.TestCase):
    """ProfileConfig は frozen dataclass であり、解決後の値は不変であるべき。"""

    def test_is_frozen(self):
        with patch.dict(
            os.environ,
            {
                "DRIVE_FOLDER_ID": "x",
                "DRIVE_OUTPUT_FOLDER_ID": "y",
            },
            clear=False,
        ):
            cfg = profile_module.load_profile("jissen_default")
        with self.assertRaises(Exception):
            cfg.input_folder_id = "z"  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()
