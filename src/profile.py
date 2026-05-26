"""プロファイル loader: profiles/*.yaml を読み込んで実行時設定を解決する。

「ドキュメントタイプ × 提出期間」の組み合わせごとに
入力フォルダ・出力フォルダ・出力シートを切り替えるための基盤。

1ワークフロー → profile引数 → プロファイル定義 → 各種設定を解決
の流れを実現する。

既存挙動の完全維持のため ``jissen_default`` プロファイルは
従来の ``DRIVE_FOLDER_ID`` / ``DRIVE_OUTPUT_FOLDER_ID`` /
``OUTPUT_SHEET_NAME="出力一覧"`` をそのまま参照する。

管理番号は採番せず、実践事例 PDF のファイル名先頭（``NNN-NN-N`` 形式）から
抽出する（``src.utils.extract_management_number`` を参照）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from src.config import EMAIL_SHEET_NAME

PROFILES_DIR = Path(__file__).resolve().parent.parent / "profiles"

_REQUIRED_FIELDS: tuple[str, ...] = (
    "display_name",
    "document_type",
    "period",
    "input_folder_id_secret",
    "output_folder_id_secret",
    "output_sheet_name",
    "prompt_template",
)


@dataclass(frozen=True)
class ProfileConfig:
    """プロファイル設定（実行時に解決された値）。

    シークレット参照（``*_secret`` フィールド）は ``load_profile`` 内で
    環境変数を参照して解決済みの値（folder ID 等）に置き換わる。

    ``email_sheet_name`` は Gmail 下書き作成用のメールアドレス一覧シートの
    タブ名。YAML で省略可能で、省略時は ``EMAIL_SHEET_NAME`` グローバル値
    （``メールアドレス一覧``）にフォールバックする。
    """

    name: str
    display_name: str
    document_type: str
    period: str
    input_folder_id: str
    output_folder_id: str
    output_sheet_name: str
    prompt_template: str
    email_sheet_name: str = EMAIL_SHEET_NAME


def load_profile(profile_name: str) -> ProfileConfig:
    """プロファイル YAML を読み込み、シークレット参照を解決して ProfileConfig を返す。

    Args:
        profile_name: ``profiles/<profile_name>.yaml`` に対応する識別子

    Raises:
        FileNotFoundError: プロファイル定義が存在しない
        ValueError: シークレット参照が環境変数に存在しない、または必須フィールド欠落
    """
    path = PROFILES_DIR / f"{profile_name}.yaml"
    if not path.exists():
        available = ", ".join(list_available_profiles())
        raise FileNotFoundError(
            f"プロファイル '{profile_name}' が見つかりません。"
            f"利用可能: {available}"
        )

    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(
            f"プロファイル '{profile_name}' の内容がオブジェクトではありません: "
            f"{type(raw).__name__}"
        )
    data: dict[str, Any] = raw

    # 必須フィールド検証
    missing = [k for k in _REQUIRED_FIELDS if k not in data]
    if missing:
        raise ValueError(
            f"プロファイル '{profile_name}' に必須フィールド欠落: {missing}"
        )

    # シークレット解決
    input_secret_name = data["input_folder_id_secret"]
    output_secret_name = data["output_folder_id_secret"]
    input_id = os.environ.get(input_secret_name, "")
    output_id = os.environ.get(output_secret_name, "")
    if not input_id:
        raise ValueError(
            f"プロファイル '{profile_name}': 環境変数 "
            f"'{input_secret_name}' が未設定"
        )
    if not output_id:
        raise ValueError(
            f"プロファイル '{profile_name}': 環境変数 "
            f"'{output_secret_name}' が未設定"
        )

    # メールアドレスシート名は YAML で省略可能（必須フィールド外）。
    # 省略時はグローバル既定値 ``EMAIL_SHEET_NAME`` を使う。
    email_sheet_name = (
        str(data["email_sheet_name"])
        if "email_sheet_name" in data
        else EMAIL_SHEET_NAME
    )

    return ProfileConfig(
        name=profile_name,
        display_name=str(data["display_name"]),
        document_type=str(data["document_type"]),
        period=str(data["period"]),
        input_folder_id=input_id,
        output_folder_id=output_id,
        output_sheet_name=str(data["output_sheet_name"]),
        prompt_template=str(data["prompt_template"]),
        email_sheet_name=email_sheet_name,
    )


def list_available_profiles() -> list[str]:
    """利用可能なプロファイル名のリスト（YAML 拡張子を除いた識別子）。"""
    if not PROFILES_DIR.exists():
        return []
    return sorted(p.stem for p in PROFILES_DIR.glob("*.yaml"))
