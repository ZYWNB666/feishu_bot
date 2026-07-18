#!/usr/bin/env python3
"""重构关键路径回归测试，不访问任何外部服务。"""

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from mysql.connector.errors import PoolError


os.environ.setdefault("APP_ID", "test-app-id")
os.environ.setdefault("APP_SECRET", "test-app-secret")
os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("LOG_LEVEL", "CRITICAL")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class _FakeCursor:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.executions = []
        self.closed = False

    def execute(self, sql, params=None):
        self.executions.append((sql, params))

    def fetchall(self):
        return self.rows

    def close(self):
        self.closed = True


class _FakeConnection:
    def __init__(self, cursor=None):
        self._cursor = cursor or _FakeCursor()
        self.rolled_back = False
        self.closed = False

    def cursor(self, dictionary=False):
        return self._cursor

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


class ConnectionPoolTests(unittest.TestCase):
    def test_waits_for_a_connection_during_a_short_burst(self):
        from db import pool as pool_module

        expected_connection = object()

        class BusyPool:
            def __init__(self):
                self.calls = 0

            def get_connection(self):
                self.calls += 1
                if self.calls < 3:
                    raise PoolError("pool exhausted")
                return expected_connection

        busy_pool = BusyPool()
        with (
            patch.object(pool_module, "get_pool", return_value=busy_pool),
            patch.object(pool_module, "MYSQL_POOL_ACQUIRE_TIMEOUT", 1),
            patch.object(pool_module.time, "sleep", return_value=None),
        ):
            connection = pool_module.get_connection()

        self.assertIs(connection, expected_connection)
        self.assertEqual(busy_pool.calls, 3)

    def test_pool_timeout_is_reported_as_pool_error(self):
        from db import pool as pool_module

        class ExhaustedPool:
            def get_connection(self):
                raise PoolError("pool exhausted")

        with (
            patch.object(pool_module, "get_pool", return_value=ExhaustedPool()),
            patch.object(pool_module, "MYSQL_POOL_ACQUIRE_TIMEOUT", 0),
        ):
            with self.assertRaisesRegex(PoolError, "连接池获取超时"):
                pool_module.get_connection()

    def test_db_cursor_rolls_back_and_returns_connection_on_error(self):
        from db import pool as pool_module

        cursor = _FakeCursor()
        connection = _FakeConnection(cursor)
        with patch.object(pool_module, "get_connection", return_value=connection):
            with self.assertRaisesRegex(RuntimeError, "boom"):
                with pool_module.db_cursor():
                    raise RuntimeError("boom")

        self.assertTrue(connection.rolled_back)
        self.assertTrue(connection.closed)
        self.assertTrue(cursor.closed)


class DatabaseErrorTests(unittest.TestCase):
    def test_alert_config_query_propagates_database_errors(self):
        from alerts_format import db_utils

        with patch.object(db_utils, "db_cursor", side_effect=RuntimeError("db down")):
            with self.assertRaisesRegex(RuntimeError, "db down"):
                db_utils.get_alert_config_by_alertid("alert-1")

    def test_label_config_load_without_cache_propagates_database_errors(self):
        from alerts_format import db_utils

        db_utils.invalidate_alert_config_cache()
        with patch.object(db_utils, "db_cursor", side_effect=RuntimeError("db down")):
            with self.assertRaisesRegex(RuntimeError, "db down"):
                db_utils.get_alert_config_by_labels({"alertname": "DiskFull"})

    def test_alert_handler_returns_500_for_database_errors(self):
        from feishu_utils import alert_handler

        data = {
            "status": "firing",
            "alerts": [{
                "status": "firing",
                "fingerprint": "test-fingerprint",
                "labels": {"alertname": "DiskFull"},
            }],
        }
        with patch.object(
            alert_handler,
            "get_alert_config_by_labels",
            side_effect=RuntimeError("db down"),
        ):
            result, status_code = alert_handler.process_alert_request(data, object())

        self.assertEqual(status_code, 500)
        self.assertEqual(result["code"], 500)
        self.assertIn("db down", result["msg"])


class AlertStatsTests(unittest.TestCase):
    def test_sql_aggregation_counts_each_database_record_once(self):
        from routes.alert_stats import _try_sql_aggregate_top

        cursor = _FakeCursor([{"alertname": "DiskFull", "cnt": 2}])
        result = _try_sql_aggregate_top(cursor, "2026-07-01", "2026-07-02", 20)

        sql, params = cursor.executions[0]
        self.assertIn("jt.`value` AS alertname", sql)
        self.assertIn("COUNT(DISTINCT alert_data.id)", sql)
        self.assertIn("GROUP BY jt.`value`", sql)
        self.assertEqual(params, ("2026-07-01", "2026-07-02", 20))
        self.assertEqual(result, [{"alertname": "DiskFull", "count": 2}])


class RouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import main

        cls.app = main.app

    def test_message_endpoint_has_no_rate_limit(self):
        calls = []

        class FakeFeishuClient:
            def send(self, *args):
                calls.append(args)

        self.app.config["FEISHU_CLIENT"] = FakeFeishuClient()
        client = self.app.test_client()
        for index in range(130):
            response = client.post(
                "/api/send_text",
                json={"chat_id": "oc_test", "text": f"message-{index}"},
            )
            self.assertEqual(response.status_code, 200)

        self.assertEqual(len(calls), 130)

    def test_alert_endpoint_has_no_rate_limit(self):
        client = self.app.test_client()
        with patch(
            "feishu_utils.alert_handler.process_alert_request",
            return_value=({"code": 0, "msg": "success"}, 200),
        ):
            for _ in range(610):
                response = client.post("/api/v1/alerts", json={"alerts": [{}]})
                self.assertEqual(response.status_code, 200)


if __name__ == "__main__":
    unittest.main(verbosity=2)
