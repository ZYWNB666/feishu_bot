#!/usr/bin/env python3
"""重构关键路径回归测试，不访问任何外部服务。"""

import os
import json
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


def setUpModule():
    # 测试遗漏 mock 时也禁止访问真实 HTTP 和数据库。
    global _external_guards
    _external_guards = [
        patch('requests.sessions.Session.request', side_effect=AssertionError('测试禁止外部 HTTP')),
        patch('db.pool._build_pool', side_effect=AssertionError('测试禁止真实数据库')),
    ]
    for guard in _external_guards:
        guard.start()


def tearDownModule():
    for guard in reversed(_external_guards):
        guard.stop()


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


class _FakeHTTPResponse:
    def __init__(self, body, status_code=200, headers=None):
        self._body = body
        self.status_code = status_code
        self.headers = headers or {}
        self.text = json.dumps(body, ensure_ascii=False)

    def json(self):
        return self._body

    def raise_for_status(self):
        if not 200 <= self.status_code < 300:
            raise RuntimeError(f"HTTP {self.status_code}")


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


class AlertLifecycleTests(unittest.TestCase):
    def test_resolved_alert_reuses_firing_maid(self):
        from alerts_format import alert_json_format

        data = {
            "status": "resolved",
            "alerts": [{
                "status": "resolved",
                "fingerprint": "fp-1",
                "labels": {"alertname": "NodeIsNotReady", "severity": "phone"},
            }],
        }
        with (
            patch.object(alert_json_format, "save_dbdata", return_value=None),
            patch.object(
                alert_json_format,
                "get_maid_by_fingerprints",
                return_value="maid-1",
            ) as lookup,
        ):
            _, _, maid, _ = alert_json_format.alert_data_api(
                data,
                "test-project",
                "http://alertmanager.test",
                group_id="chat-1",
            )

        self.assertEqual(maid, "maid-1")
        lookup.assert_called_once_with(["fp-1"], group_id="chat-1")

    def test_firing_alert_keeps_newly_created_maid(self):
        from alerts_format import alert_json_format

        data = {
            "status": "firing",
            "alerts": [{
                "status": "firing",
                "fingerprint": "fp-1",
                "labels": {"alertname": "NodeIsNotReady", "severity": "phone"},
            }],
        }
        with (
            patch.object(alert_json_format, "save_dbdata", return_value="maid-new"),
            patch.object(alert_json_format, "get_maid_by_fingerprints") as lookup,
        ):
            _, _, maid, _ = alert_json_format.alert_data_api(
                data,
                "test-project",
                "http://alertmanager.test",
                group_id="chat-1",
            )

        self.assertEqual(maid, "maid-new")
        lookup.assert_not_called()

    def test_large_biz_card_is_bounded_and_keeps_actions(self):
        import json

        from feishu_utils.alert_card_biz import (
            BIZ_CARD_MAX_BYTES,
            BIZ_CARD_MAX_INSTANCES,
            build_biz_firing_card,
        )

        raw_alerts = [
            {
                "status": "firing",
                "labels": {
                    "pod": f"mars2-pod-{index}",
                    "model_name": "model-" + ("x" * 500),
                },
                "annotations": {"description": "failure " + ("detail " * 500)},
                "startsAt": "2026-07-24T14:33:40+08:00",
            }
            for index in range(119)
        ]
        content = build_biz_firing_card(
            "Mars2-Pod-Status-Error",
            "p0",
            raw_alerts,
            {"panelURL": "https://grafana.test/panel"},
            "maid-large",
            {"namespace": "model-serving"},
            ["ou_test"],
        )
        card = json.loads(content)

        self.assertLessEqual(len(content.encode("utf-8")), BIZ_CARD_MAX_BYTES)
        self.assertIn("共 119 个", content)
        self.assertIn(f"展示前 {BIZ_CARD_MAX_INSTANCES} 个", content)
        self.assertIn("maid-large", content)
        self.assertTrue(
            any(element.get("tag") == "action" for element in card["elements"])
        )
        custom_row = next(
            element
            for element in card["elements"]
            if element.get("tag") == "action"
            and any(
                button.get("text", {}).get("content") == "📅 自定义时间"
                for button in element.get("actions", [])
            )
        )
        button_texts = [
            button.get("text", {}).get("content")
            for button in custom_row["actions"]
        ]
        self.assertEqual(
            button_texts.index("📅 自定义时间"),
            button_texts.index("🔕 静默2小时") + 1,
        )

    def test_aggregated_batch_returns_500_when_every_route_fails(self):
        from feishu_utils import alert_handler

        data = {
            "status": "firing",
            "alerts": [
                {
                    "status": "firing",
                    "fingerprint": f"aggregate-fp-{index}",
                    "labels": {
                        "alertname": "Mars2-Pod-Status-Error",
                        "severity": "p0",
                    },
                }
                for index in range(2)
            ],
        }
        config_row = {
            "alert_id": "alert-1",
            "group_id": "chat-1",
            "project": "test-project",
        }
        with (
            patch.object(alert_handler, "_find_alert_configs", return_value=[config_row]),
            patch.object(alert_handler, "_process_single_alert_config", return_value=None),
        ):
            result, status_code = alert_handler.process_alert_request(data, object())

        self.assertEqual(status_code, 500)
        self.assertEqual(result["code"], 500)
        self.assertEqual(result["summary"]["failed"], 1)


class PhoneAlertFallbackTests(unittest.TestCase):
    def test_flashcat_probe_failure_downgrades_phone_to_p0(self):
        import json

        from feishu_utils import alert_handler

        data = {
            "status": "firing",
            "alerts": [
                {
                    "status": "firing",
                    "fingerprint": "phone-fallback-fp",
                    "labels": {
                        "alertname": "Upstream5xx",
                        "severity": "phone",
                    },
                    "annotations": {"summary": "upstream failure"},
                }
            ],
        }
        config_row = {
            "alert_id": "alert-phone-fallback",
            "group_id": "chat-phone-fallback",
            "project": "test-project",
            "template_type": "biz",
        }

        class FakeFeishuClient:
            def __init__(self):
                self.contents = []

            def send(self, *args):
                self.contents.append(args[-1])
                return "message-phone-fallback"

        feishu_client = FakeFeishuClient()
        with (
            patch.object(
                alert_handler,
                "alert_data_api",
                return_value=(["alert text"], ["phone"], "maid-phone-fallback", {}),
            ),
            patch.object(
                alert_handler,
                "probe_flashcat_api",
                return_value=(False, "Flashcat API \u7f51\u7edc\u4e0d\u53ef\u8fbe\uff08DNS/\u8fde\u63a5\u5931\u8d25\uff09"),
            ),
            patch.object(alert_handler.Config, "FLASHCAT_APP_KEY", "test-app-key"),
            patch.object(alert_handler, "_create_phone_incident") as create_incident,
            patch.object(alert_handler, "update_message_id"),
            patch.object(alert_handler, "save_card_content"),
            patch.object(alert_handler, "_get_oncall_mentioned_users", return_value=[]),
        ):
            result = alert_handler._process_single_alert_config(
                data, config_row, "Upstream5xx", feishu_client
            )

        self.assertTrue(result["success"])
        create_incident.assert_not_called()
        card = json.loads(feishu_client.contents[0])
        self.assertIn("[P0]", card["header"]["title"]["content"])
        card_text = json.dumps(card, ensure_ascii=False)
        self.assertIn("\u544a\u8b66\u964d\u7ea7", card_text)
        self.assertIn("\u7f51\u7edc\u4e0d\u53ef\u8fbe", card_text)

class SilenceExtensionTests(unittest.TestCase):
    def test_ops_card_keeps_all_silence_options_and_custom_time_on_one_row(self):
        from feishu_utils.event_handler import alert_to_feishu

        class FakeFeishuClient:
            def __init__(self):
                self.content = None

            def send(self, receive_id_type, receive_id, msg_type, content):
                self.content = content
                return "message-ops-custom"

        client = FakeFeishuClient()
        with patch("alerts_format.savedb.save_card_content"):
            alert_to_feishu(
                client,
                "test alert",
                [],
                "chat-ops-custom",
                maid="maid-ops-custom",
                incident_id="incident-ops-custom",
            )

        card = json.loads(client.content)
        action_rows = [item for item in card["elements"] if item.get("tag") == "action"]
        silence_row = next(
            row
            for row in action_rows
            if any(
                button.get("text", {}).get("content") == "📅 自定义时间"
                for button in row["actions"]
            )
        )
        button_texts = [button["text"]["content"] for button in silence_row["actions"]]
        self.assertEqual(
            button_texts,
            [
                "🔕 静默2小时",
                "🔕 静默12小时",
                "🔕 静默24小时",
                "🔕 静默3天",
                "📅 自定义时间",
            ],
        )
        self.assertTrue(
            any(
                row["actions"][0].get("text", {}).get("content") == "📞 认领告警"
                for row in action_rows
                if len(row["actions"]) == 1
            )
        )

    def test_custom_picker_replaces_only_button_in_same_action_row(self):
        from feishu_utils.callback_handler import create_custom_silence_picker_card

        original = {
            "config": {"wide_screen_mode": True, "update_multi": True},
            "elements": [{
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "🔕 静默2小时"},
                        "value": {"action": "silence", "maid": "maid-picker", "duration": 7200},
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "📅 自定义时间"},
                        "value": {"action": "show_custom_silence", "maid": "maid-picker"},
                    },
                ],
            }],
        }

        updated = create_custom_silence_picker_card(original, "maid-picker")
        actions = updated["elements"][0]["actions"]

        self.assertEqual(len(actions), 2)
        self.assertEqual(actions[0]["tag"], "button")
        self.assertEqual(actions[1]["tag"], "picker_datetime")
        self.assertEqual(actions[1]["value"]["action"], "silence_until")

    def test_custom_time_selection_restores_original_card(self):
        from feishu_utils import callback_handler

        original = {
            "config": {"wide_screen_mode": True, "update_multi": True},
            "elements": [{
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "🔕 静默2小时"},
                        "value": {"action": "silence", "maid": "maid-restore", "duration": 7200},
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "📅 自定义时间"},
                        "value": {"action": "show_custom_silence", "maid": "maid-restore"},
                    },
                ],
            }],
        }
        callback_data = {
            "schema": "2.0",
            "event": {
                "operator": {"open_id": "operator-restore"},
                "context": {"open_message_id": "message-restore"},
                "action": {
                    "tag": "picker_datetime",
                    "timezone": "Asia/Shanghai",
                    "option": "2099-08-28 23:15 +0800",
                    "value": {"action": "silence_until", "maid": "maid-restore"},
                },
            },
        }

        with (
            patch.object(callback_handler, "_load_original_card", return_value=original),
            patch.object(callback_handler, "handle_silence_until_action") as handle,
        ):
            result = callback_handler.process_card_callback(callback_data, object())

        handle.assert_called_once()
        restored_text = json.dumps(result["card"]["data"], ensure_ascii=False)
        self.assertIn("📅 自定义时间", restored_text)
        self.assertNotIn("picker_datetime", restored_text)

    def test_show_custom_time_returns_picker_card(self):
        from feishu_utils import callback_handler

        original = {
            "config": {"wide_screen_mode": True},
            "elements": [{
                "tag": "action",
                "actions": [{
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "📅 自定义时间"},
                    "value": {"action": "show_custom_silence", "maid": "maid-show-picker"},
                }],
            }],
        }
        callback_data = {
            "action": {
                "value": {"action": "show_custom_silence", "maid": "maid-show-picker"},
            },
            "open_message_id": "message-show-picker",
            "open_id": "operator-show-picker",
        }

        with patch.object(callback_handler, "_load_original_card", return_value=original):
            result = callback_handler.process_card_callback(callback_data, object())

        self.assertIn("picker_datetime", json.dumps(result["card"]["data"]))

    def test_silence_success_card_has_all_extension_options_and_warning(self):
        from feishu_utils.callback_handler import create_silence_success_card

        maid = "maid-card-options"
        card = create_silence_success_card(
            maid,
            30 * 3600,
            "operator-1",
        )

        card_text = json.dumps(card, ensure_ascii=False)
        self.assertIn("已静默 1 天 6 小时", card_text)
        self.assertIn("当前静默结束时间基础上继续累加", card_text)
        self.assertIn("请根据实际业务场景谨慎选择", card_text)
        self.assertIn("长时间静默可能掩盖持续故障", card_text)

        action_rows = [
            element
            for element in card["elements"]
            if element.get("tag") == "action"
        ]
        self.assertTrue(all(len(row["actions"]) <= 5 for row in action_rows))

        buttons = [
            button
            for row in action_rows
            for button in row["actions"]
        ]
        silence_buttons = {
            button["text"]["content"]: button["value"]
            for button in buttons
            if button["value"]["action"] == "silence"
        }
        self.assertEqual(
            silence_buttons,
            {
                "🔕 静默6小时": {
                    "action": "silence",
                    "maid": maid,
                    "duration": 6 * 3600,
                },
                "🔕 静默12小时": {
                    "action": "silence",
                    "maid": maid,
                    "duration": 12 * 3600,
                },
                "🔕 静默24小时": {
                    "action": "silence",
                    "maid": maid,
                    "duration": 24 * 3600,
                },
                "🔕 静默3天": {
                    "action": "silence",
                    "maid": maid,
                    "duration": 3 * 24 * 3600,
                },
                "🔕 静默7天": {
                    "action": "silence",
                    "maid": maid,
                    "duration": 7 * 24 * 3600,
                },
            },
        )
        self.assertTrue(
            any(button["value"]["action"] == "cancel_silence" for button in buttons)
        )

    def test_existing_silence_is_extended_from_current_end(self):
        from datetime import datetime, timedelta, timezone
        from unittest.mock import Mock, patch

        from alerts_format.silence_extension import extend_existing_silences

        current_end = datetime.now(timezone.utc) + timedelta(hours=2)
        get_response = Mock(status_code=200)
        get_response.json.return_value = {
            "id": "silence-1",
            "matchers": [{"name": "alertname", "value": "Test", "isEqual": True}],
            "startsAt": datetime.now(timezone.utc).isoformat(),
            "endsAt": current_end.isoformat(),
            "createdBy": "feishu_bot",
            "comment": "test",
            "status": {"state": "active"},
        }
        post_response = Mock(status_code=200)
        post_response.json.return_value = {"silenceID": "silence-1"}

        with (
            patch("alerts_format.silence_extension.requests.get", return_value=get_response),
            patch("alerts_format.silence_extension.requests.post", return_value=post_response) as post,
        ):
            result = extend_existing_silences(
                "https://alertmanager.test/api/v2/silence",
                ["silence-1"],
                2,
                backend="Alertmanager",
            )

        self.assertTrue(result["success"])
        self.assertEqual(result["silence_ids"], ["silence-1"])
        self.assertEqual(
            post.call_args.args[0],
            "https://alertmanager.test/api/v2/silences",
        )
        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["id"], "silence-1")
        updated_end = datetime.fromisoformat(payload["endsAt"])
        self.assertGreater(
            updated_end,
            current_end + timedelta(hours=1, minutes=59),
        )
        self.assertNotIn("status", payload)

    def test_grafana_replacement_silence_id_is_saved(self):
        from datetime import datetime, timedelta, timezone
        from unittest.mock import Mock

        from alerts_format.grafana_silence import grafana_create_silence

        now = datetime.now(timezone.utc)
        get_response = Mock(status_code=200)
        get_response.json.return_value = {
            "id": "expired-silence",
            "matchers": [{"name": "alertname", "value": "Test", "isEqual": True}],
            "startsAt": (now - timedelta(hours=3)).isoformat(),
            "endsAt": (now - timedelta(hours=1)).isoformat(),
            "createdBy": "feishu_bot",
            "comment": "test",
            "status": {"state": "expired"},
        }

        for response_key in ("silenceID", "id"):
            with self.subTest(response_key=response_key):
                post_response = Mock(status_code=200)
                post_response.json.return_value = {response_key: "replacement-silence"}
                with (
                    patch("alerts_format.grafana_silence.Config.GRAFANA_API_KEY", "test-key"),
                    patch("alerts_format.grafana_silence._get_alert_data", return_value={
                        "alertlabels": {"matchers": [{"matchers": get_response.json.return_value["matchers"]}]},
                        "silenceid": ["expired-silence"],
                    }),
                    patch("alerts_format.grafana_silence._save_silence_ids") as save,
                    patch("alerts_format.silence_extension.requests.get", return_value=get_response),
                    patch("alerts_format.silence_extension.requests.post", return_value=post_response) as post,
                ):
                    result = grafana_create_silence("maid-replacement", 2, "https://grafana.test")

                self.assertTrue(result["success"])
                self.assertEqual(result["silence_ids"], ["replacement-silence"])
                save.assert_called_once_with("maid-replacement", ["replacement-silence"])
                self.assertEqual(post.call_count, 1)
                self.assertEqual(post.call_args.kwargs["json"]["id"], "expired-silence")
                self.assertGreater(datetime.fromisoformat(post.call_args.kwargs["json"]["endsAt"]), now)

    def test_existing_silence_can_be_set_to_an_absolute_end(self):
        from datetime import datetime, timedelta, timezone
        from unittest.mock import Mock, patch

        from alerts_format.silence_extension import extend_existing_silences

        now = datetime.now(timezone.utc)
        chosen_end = now + timedelta(days=2, hours=3, minutes=17)
        get_response = Mock(status_code=200)
        get_response.json.return_value = {
            "id": "silence-absolute",
            "matchers": [{"name": "alertname", "value": "Test", "isEqual": True}],
            "startsAt": now.isoformat(),
            "endsAt": (now + timedelta(days=7)).isoformat(),
            "createdBy": "feishu_bot",
            "comment": "test",
        }
        post_response = Mock(status_code=200)
        post_response.json.return_value = {"silenceID": "silence-absolute"}

        with (
            patch("alerts_format.silence_extension.requests.get", return_value=get_response),
            patch("alerts_format.silence_extension.requests.post", return_value=post_response) as post,
        ):
            result = extend_existing_silences(
                "https://alertmanager.test/api/v2/silence",
                ["silence-absolute"],
                None,
                backend="Alertmanager",
                ends_at=chosen_end,
            )

        self.assertTrue(result["success"])
        payload_end = datetime.fromisoformat(post.call_args.kwargs["json"]["endsAt"])
        self.assertEqual(payload_end, chosen_end.astimezone())

    def test_three_grafana_clicks_keep_one_id_and_add_six_hours(self):
        from datetime import datetime, timedelta, timezone
        from unittest.mock import Mock, patch

        from alerts_format.silence_extension import extend_existing_silences

        now = datetime.now(timezone.utc)
        silence_id = "grafana-silence-1"

        def get_response(ends_at):
            response = Mock(status_code=200)
            response.json.return_value = {
                "id": silence_id,
                "matchers": [
                    {
                        "name": "alertname",
                        "value": "Test",
                        "isEqual": True,
                        "isRegex": False,
                    }
                ],
                "startsAt": now.isoformat(),
                "endsAt": ends_at.isoformat(),
                "createdBy": "feishu_bot",
                "comment": "test",
            }
            return response

        post_response = Mock(status_code=200)
        post_response.json.return_value = {"silenceID": silence_id}

        with (
            patch(
                "alerts_format.silence_extension.requests.get",
                side_effect=[
                    get_response(now + timedelta(hours=2)),
                    get_response(now + timedelta(hours=4)),
                ],
            ),
            patch("alerts_format.silence_extension.requests.post", return_value=post_response) as post,
        ):
            second_click = extend_existing_silences(
                "https://grafana.test/api/alertmanager/grafana/api/v2/silence",
                [silence_id],
                2,
                headers={"Authorization": "Bearer test"},
                backend="Grafana",
            )
            third_click = extend_existing_silences(
                "https://grafana.test/api/alertmanager/grafana/api/v2/silence",
                [silence_id],
                2,
                headers={"Authorization": "Bearer test"},
                backend="Grafana",
            )

        self.assertTrue(second_click["success"])
        self.assertTrue(third_click["success"])
        self.assertEqual(second_click["silence_ids"], [silence_id])
        self.assertEqual(third_click["silence_ids"], [silence_id])
        self.assertEqual(post.call_count, 2)
        for call in post.call_args_list:
            self.assertEqual(
                call.args[0],
                "https://grafana.test/api/alertmanager/grafana/api/v2/silences",
            )
            self.assertEqual(call.kwargs["json"]["id"], silence_id)

        payload = post.call_args_list[-1].kwargs["json"]
        self.assertGreater(
            datetime.fromisoformat(payload["endsAt"]),
            now + timedelta(hours=5, minutes=59),
        )

    def test_silence_callbacks_are_not_deduplicated(self):
        from unittest.mock import patch

        from feishu_utils import callback_handler

        callback_data = {
            "action": {
                "value": {
                    "action": "silence",
                    "maid": "maid-repeat",
                    "duration": 7200,
                }
            },
            "open_message_id": "message-repeat",
            "open_id": "operator-repeat",
        }
        with patch.object(callback_handler, "handle_silence_action") as handle:
            callback_handler.process_card_callback(callback_data, object())
            callback_handler.process_card_callback(callback_data, object())

        self.assertEqual(handle.call_count, 2)

class FlashcatAckTests(unittest.TestCase):
    def test_ack_validates_response_and_confirms_incident_state(self):
        from alerts_format import flashcat_utils

        incident_id = "69da451ef77b1b51f40e83ee"
        ack_response = _FakeHTTPResponse(
            {"request_id": "req-ack", "data": {}},
            headers={"Flashcat-Request-Id": "req-ack-header"},
        )
        info_response = _FakeHTTPResponse({
            "request_id": "req-info",
            "data": {
                "incident_id": incident_id,
                "ack_time": 1775972200,
                "progress": "Processing",
                "full_response_marker": "x" * 600 + "must-not-be-truncated",
            },
        })

        with (
            patch.object(
                flashcat_utils.requests,
                "post",
                side_effect=[ack_response, info_response],
            ) as post,
            patch.object(flashcat_utils, "MAX_RETRIES", 1),
            self.assertLogs(flashcat_utils.logger, level="INFO") as logs,
        ):
            success = flashcat_utils.ack_incident(
                "test-app-key", incident_id, maid="maid-ack"
            )

        self.assertTrue(success)
        self.assertEqual(post.call_count, 2)
        self.assertTrue(
            post.call_args_list[0].args[0].endswith(
                "/incident/ack?app_key=test-app-key"
            )
        )
        self.assertEqual(
            post.call_args_list[0].kwargs["json"],
            {"incident_ids": [incident_id]},
        )
        self.assertTrue(
            post.call_args_list[1].args[0].endswith(
                "/incident/info?app_key=test-app-key"
            )
        )
        self.assertEqual(
            post.call_args_list[1].kwargs["json"],
            {"incident_id": incident_id},
        )
        output = "\n".join(logs.output)
        self.assertIn('"request_id": "req-ack"', output)
        self.assertIn("must-not-be-truncated", output)
        self.assertIn("认领状态确认成功", output)

    def test_ack_rejects_business_error_even_when_http_is_200(self):
        from alerts_format import flashcat_utils

        response = _FakeHTTPResponse({
            "request_id": "req-business-error",
            "error": {
                "code": "InvalidParameter",
                "message": "incident cannot be acknowledged",
            },
        })
        with (
            patch.object(flashcat_utils.requests, "post", return_value=response) as post,
            patch.object(flashcat_utils, "MAX_RETRIES", 1),
        ):
            success = flashcat_utils.ack_incident(
                "test-app-key", "69da451ef77b1b51f40e83ee", maid="maid-error"
            )

        self.assertFalse(success)
        # 业务响应失败时不能继续查询状态，更不能让上层更新飞书卡片。
        self.assertEqual(post.call_count, 1)

    def test_ack_fails_when_follow_up_state_is_not_acknowledged(self):
        from alerts_format import flashcat_utils

        incident_id = "69da451ef77b1b51f40e83ee"
        responses = [
            _FakeHTTPResponse({"request_id": "req-ack", "data": {}}),
            _FakeHTTPResponse({
                "request_id": "req-info",
                "data": {
                    "incident_id": incident_id,
                    "ack_time": 0,
                    "progress": "Triggered",
                },
            }),
        ]
        with (
            patch.object(flashcat_utils.requests, "post", side_effect=responses),
            patch.object(flashcat_utils, "MAX_RETRIES", 1),
        ):
            success = flashcat_utils.ack_incident(
                "test-app-key", incident_id, maid="maid-not-acked"
            )

        self.assertFalse(success)


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
