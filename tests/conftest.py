"""テスト共通フィクスチャ。

プロセス内キャッシュ（実運用では 1 ラン = 1 プロセスで安全）はテストプロセス
では全テストが共有してしまうため、テストごとに必ずリセットする。autouse
フィクスチャは unittest.TestCase ベースのテストにも適用される。
"""

from __future__ import annotations

import pytest

from src import drive_client, sheets_client


@pytest.fixture(autouse=True)
def _reset_module_caches():
    """モジュールレベルのキャッシュをテストごとにクリアする。"""
    sheets_client._reset_ensured_sheets_cache()
    sheets_client.reset_service_cache()
    sheets_client.reset_master_records_cache()
    drive_client.reset_folder_caches()
    drive_client.reset_service_cache()
    yield
    sheets_client._reset_ensured_sheets_cache()
    sheets_client.reset_service_cache()
    sheets_client.reset_master_records_cache()
    drive_client.reset_folder_caches()
    drive_client.reset_service_cache()
