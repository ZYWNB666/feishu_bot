#!/usr/bin/env python3
"""飞书用户启动同步测试，不访问外部服务或真实数据库。"""

import os
import sys
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("APP_ID", "test-app-id")
os.environ.setdefault("APP_SECRET", "test-app-secret")
os.environ.setdefault("MYSQL_PASSWORD", "test-password")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from feishu_utils import user_sync


class FakeFeishuClient:
    def __init__(self):
        self.calls = []

    def get_json(self, uri, params=None):
        self.calls.append((uri, dict(params or {})))
        if uri == user_sync.DEPARTMENT_CHILDREN_URI:
            if params.get("page_token") == "departments-page-2":
                return {"code": 0, "data": {"items": [
                    {"open_department_id": "od_2"},
                ], "has_more": False}}
            return {"code": 0, "data": {"items": [
                {"open_department_id": "od_1"},
            ], "has_more": True, "page_token": "departments-page-2"}}

        department_id = params["department_id"]
        users = {
            "0": [{"name": "Root User", "open_id": "ou_root"}],
            "od_1": [{"name": "Alice", "open_id": "ou_alice"}],
            "od_2": [
                {"name": "Alice", "open_id": "ou_alice"},
                {"name": "Bob", "open_id": "ou_bob"},
            ],
        }
        return {"code": 0, "data": {"items": users[department_id], "has_more": False}}


class FakeCursor:
    def __init__(self, existing_rows):
        self.existing_rows = existing_rows
        self.executions = []
        self.inserted = []

    def execute(self, sql, params=None):
        self.executions.append((sql, params))

    def fetchall(self):
        return self.existing_rows

    def executemany(self, sql, params):
        self.executions.append((sql, params))
        self.inserted.extend(params)


class FakeConnection:
    def __init__(self):
        self.commits = 0

    def commit(self):
        self.commits += 1


def fake_db_context(existing_rows):
    connection = FakeConnection()
    cursor = FakeCursor(existing_rows)

    @contextmanager
    def context(dictionary=False):
        yield connection, cursor

    return context, connection, cursor


class FeishuUserFetchTests(unittest.TestCase):
    def test_fetches_root_and_all_department_pages_and_deduplicates_users(self):
        client = FakeFeishuClient()

        users = user_sync.fetch_all_feishu_users(client)

        self.assertEqual(
            users,
            [
                {"name": "Alice", "open_id": "ou_alice"},
                {"name": "Bob", "open_id": "ou_bob"},
                {"name": "Root User", "open_id": "ou_root"},
            ],
        )
        queried_departments = [
            params["department_id"]
            for uri, params in client.calls
            if uri == user_sync.USERS_BY_DEPARTMENT_URI
        ]
        self.assertEqual(queried_departments, ["0", "od_1", "od_2"])

    def test_rejects_different_users_with_the_same_name(self):
        with self.assertRaisesRegex(user_sync.FeishuUserSyncError, "同名"):
            user_sync._validate_users([
                {"name": "Alice", "open_id": "ou_1"},
                {"name": "Alice", "open_id": "ou_2"},
            ])

    def test_rejects_empty_directory_instead_of_clearing_database(self):
        class EmptyClient:
            def get_json(self, uri, params=None):
                return {"code": 0, "data": {"items": [], "has_more": False}}

        with self.assertRaisesRegex(user_sync.FeishuUserSyncError, "拒绝清空"):
            user_sync.fetch_all_feishu_users(EmptyClient())


class FeishuUserDatabaseSyncTests(unittest.TestCase):
    def test_skips_database_writes_when_mapping_is_unchanged(self):
        remote = [{"name": "Alice", "open_id": "ou_alice"}]
        context, connection, cursor = fake_db_context(remote)

        with (
            patch.object(user_sync, "fetch_all_feishu_users", return_value=remote),
            patch.object(user_sync, "db_cursor", context),
        ):
            result = user_sync.sync_feishu_users(object())

        self.assertFalse(result.changed)
        self.assertEqual(connection.commits, 0)
        self.assertEqual(len(cursor.executions), 1)

    def test_full_replacement_reports_added_updated_and_deleted(self):
        remote = [
            {"name": "Alice", "open_id": "ou_alice_new"},
            {"name": "Bob", "open_id": "ou_bob"},
        ]
        existing = [
            {"name": "Alice", "open_id": "ou_alice_old"},
            {"name": "Former User", "open_id": "ou_former"},
        ]
        context, connection, cursor = fake_db_context(existing)

        with (
            patch.object(user_sync, "fetch_all_feishu_users", return_value=remote),
            patch.object(user_sync, "db_cursor", context),
        ):
            result = user_sync.sync_feishu_users(object())

        self.assertEqual(result.to_dict(), {
            "fetched": 2,
            "added": 1,
            "updated": 1,
            "deleted": 1,
            "changed": True,
        })
        self.assertEqual(connection.commits, 1)
        self.assertIn("DELETE FROM feishu_users", [sql for sql, _ in cursor.executions])
        self.assertEqual(cursor.inserted, [("Alice", "ou_alice_new"), ("Bob", "ou_bob")])


if __name__ == "__main__":
    unittest.main()
