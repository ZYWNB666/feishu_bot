"""趋势试点关键决策的本地测试；不访问外部服务。"""

import os
import json
import sys
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import patch, MagicMock

os.environ.setdefault('APP_ID', 'test-app-id')
os.environ.setdefault('APP_SECRET', 'test-app-secret')
os.environ.setdefault('MYSQL_PASSWORD', 'test-password')
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from feishu_utils import alert_handler, trend_gate, trend_worker, trend_digest


def setUpModule():
    # 不允许测试因遗漏 mock 意外使用本地 .env 中的真实连接。
    global _external_guards
    _external_guards = [
        patch('requests.sessions.Session.request', side_effect=AssertionError('测试禁止外部 HTTP')),
        patch('db.pool._build_pool', side_effect=AssertionError('测试禁止真实数据库')),
        patch.object(trend_gate.Config, 'TREND_GATE_MODE', 'legacy'),
    ]
    for guard in _external_guards:
        guard.start()


def tearDownModule():
    for guard in reversed(_external_guards):
        guard.stop()


@contextmanager
def fake_db(row=None, rows=None):
    conn, cursor = MagicMock(), MagicMock()
    cursor.fetchone.return_value = row
    cursor.fetchall.return_value = rows or []
    yield conn, cursor


class TrendDecisionTests(unittest.TestCase):
    def test_small_stable_breach_waits_then_sends(self):
        points = [(t, 31.0) for t in range(20, 201, 15)]
        first = trend_gate.classify(points, 30, 25, 100, first_seen=160, now=200)
        due = trend_gate.classify(points, 30, 25, 100, first_seen=100, now=200)
        self.assertEqual(first.action, 'observe')
        self.assertEqual(due.action, 'send')
        self.assertIs(trend_gate.oncall_mention_policy(due), False)

    def test_rising_or_hard_breach_sends_without_waiting(self):
        points = [(t, 30.0 if t < 140 else 38.0) for t in range(20, 201, 15)]
        rising = trend_gate.classify(points, 30, 25, 100, first_seen=190, now=200)
        hard = trend_gate.classify(points[:-1] + [(200, 46.0)], 30, 25, 100,
                                   first_seen=190, now=200)
        self.assertEqual(rising.action, 'send')
        self.assertEqual(hard.action, 'send')
        self.assertIs(trend_gate.oncall_mention_policy(rising), True)
        self.assertIs(trend_gate.oncall_mention_policy(hard), True)

    def test_recovery_cancels_and_low_sample_volume_fails_open(self):
        points = [(t, 24.0) for t in range(20, 201, 15)]
        self.assertEqual(
            trend_gate.classify(points, 30, 25, 100, first_seen=160, now=200).action,
            'cancel',
        )
        self.assertEqual(
            trend_gate.classify(points, 30, 25, 3, first_seen=160, now=200).action,
            'send',
        )
        low_sample = trend_gate.classify(points, 30, 25, 3, first_seen=160, now=200)
        self.assertIsNone(trend_gate.oncall_mention_policy(low_sample))
        self.assertIsNone(trend_gate.oncall_mention_policy(low_sample, previous_value=10.0))

    def test_escalation_mentions_even_when_trend_reason_is_observation_timeout(self):
        decision = trend_gate.Decision('send', 42.0, '达到最长观察时间且仍越线', urgent=False)
        self.assertIs(trend_gate.oncall_mention_policy(decision, previous_value=32.0), True)


class TrendMentionTests(unittest.TestCase):
    def test_pilot_group_message_mentions_oncall_only_when_urgent(self):
        data = {'status': 'firing', 'alerts': [{'status': 'firing'}]}
        route = {'group_id': 'chat-1', 'project': 'test', 'rank': 'p0',
                 'oncall_sync': 1, 'template_type': 'ops'}
        mention_lists = []

        def send_card(client, content, mentioned_users, group_id, **kwargs):
            mention_lists.append(mentioned_users)
            return 'message-1'

        with patch.object(alert_handler, 'alert_data_api',
                          return_value=(['alert text'], ['p0'], None, {})), \
             patch.object(alert_handler, '_get_oncall_mentioned_users',
                          return_value=['ou-oncall']) as oncall, \
             patch.object(alert_handler, 'alert_to_feishu', side_effect=send_card):
            for policy in (False, True, None):
                result = alert_handler._process_single_alert_config(
                    data, route, 'pilot', object(), mention_oncall=policy)
                self.assertTrue(result['success'])

        self.assertEqual(mention_lists, [[], ['ou-oncall'], ['ou-oncall']])
        self.assertEqual(oncall.call_count, 2)


class TrendRoutingTests(unittest.TestCase):
    def setUp(self):
        self.data = {
            'status': 'firing',
            'alerts': [{
                'status': 'firing',
                'fingerprint': 'fp-1',
                'generatorURL': 'https://grafana.magikcloud.cn/alerting/grafana/tfk3tpot5p0e3f/view',
                'labels': {'alertname': 'Kimi-K3-TPOT', 'tenant': 'tenant-1'},
            }],
        }
        self.route = {'id': 1, 'group_id': 'chat-1'}
        self.key = ('tfk3tpot5p0e3f', 'chat-1', 'fp-1')

    def test_recovery_during_observation_never_sends_firing(self):
        self.data['status'] = 'resolved'
        self.data['alerts'][0]['status'] = 'resolved'
        state = {'status': 'pending', 'last_sent_at': None}
        with patch.object(trend_gate, 'get_state', return_value=state), \
             patch.object(trend_gate, 'mark_resolved') as mark, \
             patch.object(alert_handler, '_process_single_alert_config') as send:
            response = alert_handler._process_trend_config(
                self.data, self.route, 'Kimi-K3-TPOT', object(), self.key,
            )
        self.assertTrue(response['skipped'])
        mark.assert_called_once()
        send.assert_not_called()

    def test_initial_recovery_is_persisted_to_suppress_later_resolved(self):
        with patch.object(trend_gate, 'get_state', return_value=None), \
             patch.object(trend_gate, 'decide', return_value=trend_gate.Decision('cancel', 22.0, '已回落')), \
             patch.object(trend_gate, 'log_decision'), \
             patch.object(trend_gate, 'save_pending') as save, \
             patch.object(trend_gate, 'mark_resolved') as mark, \
             patch.object(alert_handler, '_process_single_alert_config') as send:
            response = alert_handler._process_trend_config(
                self.data, self.route, 'Kimi-K3-TPOT', object(), self.key,
            )
        self.assertTrue(response['skipped'])
        save.assert_called_once()
        mark.assert_called_once_with(self.key, '已回落')
        send.assert_not_called()

    def test_worsening_bypasses_recent_duplicate(self):
        state = {'status': 'sent', 'last_sent_at': datetime.now(), 'last_value': 32.0}
        with patch.object(trend_gate, 'get_state', return_value=state), \
             patch.object(trend_gate, 'decide', return_value=trend_gate.Decision('send', 45.0, '恶化', urgent=True)), \
             patch.object(trend_gate, 'log_decision'), \
             patch.object(trend_gate, 'save_pending'), \
             patch.object(trend_gate, 'mark_sent') as mark, \
             patch.object(alert_handler, '_process_single_alert_config',
                          return_value={'success': True, 'message_id': 'mid-2'}) as send:
            response = alert_handler._process_trend_config(
                self.data, self.route, 'Kimi-K3-TPOT', object(), self.key,
            )
        self.assertEqual(response['message_id'], 'mid-2')
        send.assert_called_once()
        self.assertIs(send.call_args.kwargs['mention_oncall'], True)
        mark.assert_called_once()

    def test_due_observation_uses_persisted_payload_and_sends_once(self):
        state = {
            'status': 'pending',
            'payload': json.dumps(self.data),
            'first_seen': datetime.now() - timedelta(seconds=100),
            'config_id': 1,
        }

        class Cursor:
            def execute(self, sql, params):
                self.params = params

            def fetchone(self):
                return self_route

        self_route = self.route

        @contextmanager
        def fake_cursor(dictionary=False):
            yield None, Cursor()

        with patch.object(trend_gate, 'get_state', return_value=state), \
             patch.object(trend_gate, 'decide', return_value=trend_gate.Decision('send', 33.0, '观察到期', urgent=False)), \
             patch.object(trend_gate, 'log_decision'), \
             patch.object(trend_worker, 'db_cursor', fake_cursor), \
             patch.object(alert_handler, '_process_single_alert_config',
                          return_value={'success': True, 'message_id': 'mid-3'}) as send, \
             patch.object(trend_gate, 'mark_sent') as mark:
            trend_worker._process_pending_row(self.key, object())
        send.assert_called_once()
        self.assertIs(send.call_args.kwargs['mention_oncall'], False)
        mark.assert_called_once_with(self.key, 33.0, '观察到期')

    def test_sent_alert_is_rechecked_and_renotified_only_when_worse(self):
        state = {
            'status': 'sent',
            'payload': json.dumps(self.data),
            'first_seen': datetime.now() - timedelta(minutes=5),
            'config_id': 1,
            'last_value': 32.0,
        }

        class Cursor:
            def execute(self, sql, params):
                pass

            def fetchone(self):
                return {'id': 1, 'group_id': 'chat-1'}

        @contextmanager
        def fake_cursor(dictionary=False):
            yield None, Cursor()

        with patch.object(trend_gate, 'get_state', return_value=state), \
             patch.object(trend_gate, 'decide', side_effect=[
                 trend_gate.Decision('send', 35.0, '观察到期', urgent=False),
                 trend_gate.Decision('send', 42.0, '最近一分钟明显恶化', urgent=True),
             ]), \
             patch.object(trend_gate, 'log_decision'), \
             patch.object(trend_gate, 'schedule_next') as schedule, \
             patch.object(trend_worker, 'db_cursor', fake_cursor), \
             patch.object(alert_handler, '_process_single_alert_config',
                          return_value={'success': True, 'message_id': 'mid-4'}) as send, \
             patch.object(trend_gate, 'mark_sent') as mark:
            trend_worker._process_pending_row(self.key, object())
            trend_worker._process_pending_row(self.key, object())
        schedule.assert_called_once()
        send.assert_called_once()
        self.assertIs(send.call_args.kwargs['mention_oncall'], True)
        mark.assert_called_once_with(self.key, 42.0, '最近一分钟明显恶化')


class MixedRouteDedupTests(unittest.TestCase):
    def setUp(self):
        from utils.bounded_cache import BoundedTTLCache
        self.data = {'status': 'firing', 'alerts': [{
            'status': 'firing', 'fingerprint': 'fp1',
            'generatorURL': 'https://g/alerting/grafana/a/view',
            'labels': {'alertname': 'TPOT'},
        }]}
        self.routes = [
            {'id': 1, 'group_id': 'gated', 'trend_policy': {'rule_uids': ['a']}},
            {'id': 2, 'group_id': 'disabled', 'trend_policy': {'enabled': False}},
            {'id': 3, 'group_id': 'unmatched', 'trend_policy': {'rule_uids': ['b']}},
        ]
        for guard in (
                patch.object(alert_handler, '_alert_label_dedup_cache', BoundedTTLCache(maxsize=50, ttl=300)),
                patch.object(alert_handler, '_find_alert_configs', return_value=self.routes),
                patch.object(trend_gate, 'legacy_route_enabled', return_value=False)):
            guard.start()
            self.addCleanup(guard.stop)

    def test_repeat_or_changed_fingerprint_dedups_only_normal_routes(self):
        with patch.object(alert_handler, '_process_trend_config', return_value={'success': True}) as gate, \
             patch.object(alert_handler, '_process_single_alert_config', return_value={'success': True}) as send:
            for fingerprint in ('fp1', 'fp1', 'fp2'):
                self.data['alerts'][0]['fingerprint'] = fingerprint
                result, status = alert_handler._process_trend_request(self.data, object())
                self.assertEqual(status, 200)
            self.assertEqual(send.call_count, 2)
            self.assertEqual(gate.call_count, 3)
            self.assertEqual(result['summary']['failed'], 0)

    def test_resolved_clears_cooldown_and_next_firing_sends(self):
        with patch.object(alert_handler, '_process_trend_config', return_value={'success': True}), \
             patch.object(alert_handler, '_process_single_alert_config', return_value={'success': True}) as send:
            for status in ('firing', 'firing', 'resolved', 'firing'):
                self.data['status'] = status
                self.data['alerts'][0]['status'] = status
                alert_handler._process_trend_request(self.data, object())
            self.assertEqual(send.call_count, 6)

    def test_cooldown_expires_after_five_minutes(self):
        with patch.object(alert_handler, '_process_trend_config', return_value={'success': True}), \
             patch.object(alert_handler, '_process_single_alert_config', return_value={'success': True}) as send, \
             patch('utils.bounded_cache.time.time', return_value=1000) as clock:
            alert_handler._process_trend_request(self.data, object())
            clock.return_value = 1299
            alert_handler._process_trend_request(self.data, object())
            self.assertEqual(send.call_count, 2)
            clock.return_value = 1300
            alert_handler._process_trend_request(self.data, object())
            self.assertEqual(send.call_count, 4)

    def test_failed_normal_route_retries_while_successful_route_stays_deduped(self):
        for failure in (None, {'success': False}, RuntimeError('send failed')):
            with self.subTest(failure=failure), \
                 patch.object(alert_handler, '_process_trend_config', return_value={'success': True}), \
                 patch.object(alert_handler, '_process_single_alert_config', side_effect=[
                     failure, {'success': True}, {'success': True}]) as send:
                alert_handler._alert_label_dedup_cache.clear()
                first, _ = alert_handler._process_trend_request(self.data, object())
                self.assertEqual(first['summary']['failed'], 1)
                second, status = alert_handler._process_trend_request(self.data, object())
                self.assertEqual(status, 200)
                self.assertEqual(second['summary']['failed'], 0)
                self.assertEqual([c.args[1]['group_id'] for c in send.call_args_list],
                                 ['disabled', 'unmatched', 'disabled'])


class PolicyTests(unittest.TestCase):
    def setUp(self):
        trend_gate.invalidate_policy_cache()

    def tearDown(self):
        trend_gate.invalidate_policy_cache()

    def test_legacy_override_warns_once_for_mode_or_route_policy(self):
        for mode, rows in [('labels', []), ('legacy', [{'id': 1, 'trend_policy': {'rule_uids': ['new']}}])]:
            trend_gate.invalidate_policy_cache()
            with self.subTest(mode=mode), fake_db(rows=rows) as pair, \
                 patch.object(trend_gate.Config, 'TREND_GATE_ENABLED', True), \
                 patch.object(trend_gate.Config, 'TREND_GATE_MODE', mode), \
                 patch.object(trend_gate, '_legacy_override_warning_emitted', False), \
                 patch.object(trend_gate, 'db_cursor') as db, \
                 patch.object(trend_gate, 'logger') as logger:
                db.return_value.__enter__.return_value = pair
                trend_gate.enabled_policies()
                trend_gate.enabled_policies()
                logger.warning.assert_called_once()
                self.assertIn('不再决定启用范围', logger.warning.call_args.args[0])

    def test_full_policy_and_defaults_are_frozen(self):
        raw = {'enabled': True, 'rule_uids': ['a', 'b'], 'observe_seconds': 120,
               'rise_ratio': 0.2, 'hard_ratio': 2.0, 'hard_floor': 0.95,
               'min_requests': 50, 'request_metric': 'requests_total',
               'slow_window_seconds': 300, 'confirm_cycles': 3}
        policy = trend_gate.get_policy({'id': 1, 'trend_policy': json.dumps(raw)})
        self.assertEqual(policy.rule_uids, ('a', 'b'))
        self.assertEqual(policy.request_metric, 'requests_total')
        self.assertEqual(policy.hard_floor, 0.95)
        with self.assertRaises(FrozenInstanceError):
            policy.observe_seconds = 2
        default = trend_gate.get_policy({'trend_policy': {'rule_uids': ['a']}})
        self.assertEqual(default.observe_seconds, trend_gate.TREND_OBSERVE_SECONDS)
        self.assertEqual(default.confirm_cycles, trend_gate.TREND_CONFIRM_CYCLES)

    def test_invalid_policies_fail_open(self):
        invalid = ['{', '[]', {'enabled': False, 'rule_uids': ['a']},
                   {'rule_uids': []}, {'rule_uids': 'a'},
                   {'rule_uids': ['a'], 'min_requests': True},
                   {'rule_uids': ['a'], 'observe_seconds': '90'},
                   {'rule_uids': ['a'], 'confirm_cycles': 0},
                   {'rule_uids': ['a'], 'rise_ratio': float('nan')},
                   {'rule_uids': ['a'], 'rise_ratio': 10**400},
                   {'rule_uids': ['a'], 'request_metric': 'bad{selector}'}]
        for raw in invalid:
            with self.subTest(raw=raw):
                self.assertIsNone(trend_gate.get_policy({'trend_policy': raw}))

    def test_policy_union_cache_and_crud_invalidation(self):
        from alerts_format.db_utils import invalidate_alert_config_cache
        rows = [{'id': 1, 'trend_policy': {'rule_uids': ['a', 'b']}},
                {'id': 2, 'trend_policy': {'rule_uids': ['c']}}]
        with fake_db(rows=rows) as pair, \
             patch.object(trend_gate, 'db_cursor') as db, \
             patch.object(trend_gate.Config, 'TREND_GATE_ENABLED', False):
            db.return_value.__enter__.return_value = pair
            for uid in ('a', 'b', 'c'):
                data = {'status': 'firing', 'alerts': [{'status': 'firing', 'fingerprint': 'f',
                        'generatorURL': f'https://g/alerting/grafana/{uid}/view'}]}
                self.assertTrue(trend_gate.is_enabled_for(data))
            self.assertEqual(db.call_count, 1)
            invalidate_alert_config_cache()
            trend_gate.enabled_policies()
            self.assertEqual(db.call_count, 2)

    def test_missing_migration_and_empty_policies_use_legacy_once(self):
        data = {'status': 'firing', 'alerts': [{'status': 'firing', 'fingerprint': 'f',
                'generatorURL': 'https://g/alerting/grafana/legacy/view'}]}
        for missing in (True, False):
            trend_gate.invalidate_policy_cache()
            with patch.object(trend_gate, '_legacy_warning_emitted', False), \
                 patch.object(trend_gate.Config, 'TREND_GATE_ENABLED', True), \
                 patch.object(trend_gate.Config, 'TREND_RULE_UID', 'legacy'), \
                 patch.object(trend_gate, 'logger') as logger, \
                 patch.object(trend_gate, 'db_cursor') as db:
                if missing:
                    db.side_effect = RuntimeError('unknown column trend_policy')
                else:
                    db.return_value.__enter__.return_value = (MagicMock(), MagicMock(fetchall=lambda: []))
                self.assertTrue(trend_gate.is_enabled_for(data))
                self.assertTrue(trend_gate.legacy_route_enabled({}, data))
                self.assertFalse(trend_gate.legacy_route_enabled({'trend_policy': '{'}, data))
                self.assertEqual(db.call_count, 1)
                logger.warning.assert_called_once()

    def test_only_matching_route_uses_policy(self):
        data = {'alerts': [{'fingerprint': 'f', 'generatorURL': 'https://g/alerting/grafana/a/view'}]}
        routes = [{'id': 1, 'group_id': 'one', 'trend_policy': {'rule_uids': ['a']}},
                  {'id': 2, 'group_id': 'two', 'trend_policy': {'rule_uids': ['b']}},
                  {'id': 3, 'group_id': 'three', 'trend_policy': None},
                  {'id': 4, 'group_id': 'four', 'trend_policy': '{'}]
        with patch.object(alert_handler, '_find_alert_configs', return_value=routes), \
             patch.object(trend_gate, 'legacy_route_enabled', return_value=False), \
             patch.object(alert_handler, '_process_trend_config', return_value={'success': True}) as gate, \
             patch.object(alert_handler, '_process_single_alert_config', return_value={'success': True}) as send:
            _, code = alert_handler._process_trend_request(data, object())
        self.assertEqual(code, 200)
        gate.assert_called_once()
        self.assertEqual(send.call_count, 3)


class LabelPolicyTests(unittest.TestCase):
    def setUp(self):
        self.data = {'status': 'firing', 'alerts': [{
            'status': 'firing', 'fingerprint': 'f',
            'generatorURL': 'https://g/alerting/grafana/new-rule/view',
            'labels': {'tenant': 'tenant-a', 'model': 'Kimi-K3',
                       'alertname': 'Kimi-TPOT', 'severity': 'p0'},
        }]}
        trend_gate._global_policy.cache_clear()
        rule = patch.object(trend_gate, '_load_rule', return_value=(
            'rate(magik_model_tpot_ms_bucket[1m])', 30, 25, 'higher_worse'))
        rule.start()
        self.addCleanup(rule.stop)

    def test_labels_match_all_keys_and_entire_values_without_uids(self):
        policy = trend_gate.get_policy({'trend_policy': {'match_labels': {
            'tenant': 'tenant-a', 'alertname': '.*(TPOT|TTFT).*'}}})
        self.assertEqual(policy.request_metric, 'auto')
        self.assertTrue(trend_gate.policy_matches(policy, self.data))
        self.data['alerts'][0]['labels']['tenant'] = 'tenant-ab'
        self.assertFalse(trend_gate.policy_matches(policy, self.data))
        del self.data['alerts'][0]['labels']['tenant']
        self.assertFalse(trend_gate.policy_matches(policy, self.data))

    def test_invalid_filters_do_not_enable_all(self):
        for raw in ({'match_labels': {}}, {'match_labels': []},
                    {'match_labels': {'tenant': '['}}, {'match_labels': {'tenant': True}},
                    {'match_all': 'true'}, {'match_all': False}):
            with self.subTest(raw=raw):
                self.assertIsNone(trend_gate.get_policy({'trend_policy': raw}))
        for label_json in ('{}', '[]', '{', '{"tenant":"["}', '{},"match_all":true'):
            self.assertIsNone(trend_gate._global_policy('labels', label_json))
        self.assertIsNone(trend_gate._global_policy('typo', '{}'))

    def test_global_labels_select_new_rules_without_route_policy(self):
        with patch.object(trend_gate.Config, 'TREND_GATE_MODE', 'labels'), \
             patch.object(trend_gate.Config, 'TREND_LABEL_MATCHERS', '{"tenant":"tenant-a"}'), \
             patch.object(trend_gate, 'enabled_policies', return_value={}):
            self.assertTrue(trend_gate.is_enabled_for(self.data))
            self.assertTrue(trend_gate.trends_enabled())
            self.assertIsNotNone(trend_gate.policy_for_route({'trend_policy': None}, self.data))
            self.data['alerts'][0]['labels']['tenant'] = 'tenant-b'
            self.assertFalse(trend_gate.is_enabled_for(self.data))

    def test_global_all_respects_route_override_phone_and_opt_out(self):
        with patch.object(trend_gate.Config, 'TREND_GATE_MODE', 'all'), \
             patch.object(trend_gate, 'enabled_policies', return_value={}):
            self.assertTrue(trend_gate.is_enabled_for(self.data))
            self.assertIsNotNone(trend_gate.policy_for_route({}, self.data))
            for raw in ({'enabled': False}, '{', {'rule_uids': ['different']}):
                self.assertIsNone(trend_gate.policy_for_route({'trend_policy': raw}, self.data))
            override = trend_gate.policy_for_route({'trend_policy': {
                'match_all': True, 'observe_seconds': 45}}, self.data)
            self.assertEqual(override.observe_seconds, 45)
            self.data['alerts'][0]['labels']['severity'] = 'phone'
            self.assertFalse(trend_gate.is_enabled_for(self.data))
            self.data['alerts'][0]['labels']['severity'] = 'p0'
            self.data['alerts'][0]['labels']['trend_gate'] = 'false'
            self.assertFalse(trend_gate.is_enabled_for(self.data))

    def test_route_labels_override_global_and_legacy_uid_can_intersect_labels(self):
        raw = {'match_labels': {'model': 'Kimi-.*'}, 'rule_uids': ['new-rule']}
        with patch.object(trend_gate.Config, 'TREND_GATE_MODE', 'legacy'):
            policy = trend_gate.policy_for_route({'trend_policy': raw}, self.data)
            self.assertIsNotNone(policy)
            self.data['alerts'][0]['generatorURL'] = 'https://g/alerting/grafana/other/view'
            self.assertIsNone(trend_gate.policy_for_route({'trend_policy': raw}, self.data))

    def test_counter_auto_detection_and_unsupported_rules(self):
        expressions = [
            ('histogram_quantile(.5, rate(magik_model_tpot_ms_bucket[1m]))',
             'higher_worse', 'magik_model_tpot_ms_count'),
            ('histogram_quantile(.99, rate(magik_model_ttft_ms_bucket[5m])) / 1000',
             'higher_worse', 'magik_model_ttft_ms_count'),
            ('sum(rate(magik_model_response_total{code=~"2.."}[5m])) / '
             'sum(rate(magik_model_response_total[5m])) * 100',
             'lower_worse', 'magik_model_response_total'),
        ]
        for expression, direction, counter in expressions:
            self.assertEqual(trend_gate._auto_request_metric(expression, direction), counter)
        for expression, direction in (
                ('up', 'higher_worse'), ('magik_model_response_total', 'higher_worse'),
                ('rate(magik_model_response_total[5m])', 'lower_worse'),
                ('magik_model_tpot_ms_bucket', 'lower_worse'),
                ('magik_model_tpot_ms_bucket + magik_model_ttft_ms_bucket', 'higher_worse'),
                ('up # magik_model_tpot_ms_bucket', 'higher_worse'),
                ('up{model="magik_model_tpot_ms_bucket"}', 'higher_worse')):
            with self.assertRaises(ValueError):
                trend_gate._auto_request_metric(expression, direction)

    def test_auto_decide_queries_ttft_counter(self):
        policy = trend_gate.get_policy({'trend_policy': {'match_all': True}})
        with patch.object(trend_gate.Config, 'GRAFANA_RULES_READ_KEY', 'test'), \
             patch.object(trend_gate.Config, 'VM_QUERY_URL', 'http://vm/api/v1/query'), \
             patch.object(trend_gate, '_load_rule', return_value=(
                 'rate(magik_model_ttft_ms_bucket[5m])', 4, 4, 'higher_worse')), \
             patch.object(trend_gate, '_vm_query', return_value=[]), \
             patch.object(trend_gate, '_matching_points', return_value=[]), \
             patch.object(trend_gate, '_request_count', return_value=100) as count:
            decision = trend_gate.decide(self.data, 0, policy)
        count.assert_called_once_with(self.data['alerts'][0]['labels'], 'magik_model_ttft_ms_count')
        self.assertEqual(decision.action, 'send')

    def test_unsupported_auto_rule_bypasses_state_and_uses_original_send(self):
        policy = trend_gate.get_policy({'trend_policy': {'match_all': True}})
        with patch.object(trend_gate, '_load_rule', return_value=('up', 1, 1, 'lower_worse')), \
             patch.object(trend_gate, 'get_state') as state, \
             patch.object(alert_handler, '_process_single_alert_config', return_value={'success': True}) as send:
            result = alert_handler._process_trend_config(
                self.data, {'id': 1}, 'test', object(), ('new-rule', 'g', 'f'), policy)
        self.assertTrue(result['success'])
        state.assert_not_called()
        send.assert_called_once()

    def test_unsupported_rules_and_rule_query_failures_keep_original_entry(self):
        with patch.object(trend_gate.Config, 'TREND_GATE_MODE', 'all'), \
             patch.object(trend_gate, 'enabled_policies', return_value={}), \
             patch.object(trend_gate, '_load_rule', return_value=('up', 1, 1, 'lower_worse')) as load:
            self.assertFalse(trend_gate.is_enabled_for(self.data))
            load.side_effect = RuntimeError('Grafana unavailable')
            self.assertFalse(trend_gate.is_enabled_for(self.data))

    def test_global_worker_inherits_policy_and_confirms_recovery(self):
        row = {'status': 'pending', 'config_id': 1, 'payload': self.data,
               'first_seen': datetime.now(), 'cancel_streak': 0}
        with patch.object(trend_gate.Config, 'TREND_GATE_MODE', 'all'), \
             fake_db(row={'id': 1, 'trend_policy': None}) as pair, \
             patch.object(trend_worker, 'db_cursor') as db, \
             patch.object(trend_gate, 'get_state', return_value=row), \
             patch.object(trend_gate, 'decide', return_value=trend_gate.Decision('cancel', 24, '恢复')) as decide, \
             patch.object(trend_gate, 'log_decision'), \
             patch.object(trend_gate, 'schedule_next') as schedule:
            db.return_value.__enter__.return_value = pair
            trend_worker._process_pending_row(('new-rule', 'g', 'f'), object())
        self.assertEqual(decide.call_args.args[2].request_metric, 'auto')
        schedule.assert_called_once_with(('new-rule', 'g', 'f'), '恢复待确认(1/2)', cancel_streak=1)

    def test_global_worker_refreshes_digest_without_route_policies(self):
        with patch.object(trend_gate.Config, 'TREND_GATE_MODE', 'all'), \
             patch.object(trend_gate, 'enabled_policies', return_value={}), \
             patch.object(trend_gate, 'due_active', return_value=[]), \
             patch.object(trend_digest, 'update_digests') as digest, \
             patch.object(trend_worker, 'cleanup_decision_logs') as cleanup:
            client = object()
            trend_worker.run_pending_once(client)
        digest.assert_called_once_with(client)
        cleanup.assert_called_once()


class DirectionTests(unittest.TestCase):
    def points(self, fast, slow=1.0):
        return [(t, fast if t >= 940 else slow) for t in range(340, 1001, 15)]

    def decide_lower(self, fast, slow=1.0, age=30, count=100):
        return trend_gate.classify(self.points(fast, slow), 0.99, 0.995, count,
                                   first_seen=1000-age, now=1000,
                                   policy=trend_gate.TrendPolicy(hard_floor=0.95),
                                   direction='lower_worse')

    def test_lower_worse_windows_floor_recovery_and_timeout(self):
        cases = [(0.94, 1.0, 30, 'send', True, '跌破绝对下限'),
                 (0.98, 0.98, 30, 'send', True, '快慢窗口同时越线'),
                 (0.98, 1.0, 30, 'observe', None, None),
                 (0.996, 0.98, 30, 'cancel', None, None),
                 (0.993, 0.98, 30, 'observe', None, None),
                 (0.98, 1.0, 90, 'send', False, '达到最长观察时间且仍越线'),
                 (0.993, 1.0, 90, 'cancel', None, None)]
        for fast, slow, age, action, urgent, reason in cases:
            with self.subTest(fast=fast, slow=slow, age=age):
                decision = self.decide_lower(fast, slow, age)
                self.assertEqual(decision.action, action)
                self.assertIs(decision.urgent, urgent)
                self.assertEqual(decision.direction, 'lower_worse')
                if reason:
                    self.assertEqual(decision.reason, reason)
        low = self.decide_lower(0.98, count=3)
        self.assertEqual(low.reason, '指标样本不足，按原流程发送')
        self.assertIsNone(low.urgent)

    def test_higher_worse_boundaries_and_missing_samples(self):
        for value, age, action in ((24.99, 10, 'cancel'), (25, 10, 'observe'),
                                   (29.99, 90, 'cancel'), (30, 90, 'send'), (45, 1, 'send')):
            d = trend_gate.classify(self.points(value, value), 30, 25, 20, 1000-age, now=1000)
            self.assertEqual(d.action, action)
        for points in ([], [(1000, 31)], [(t-100, v) for t,v in self.points(31,31)]):
            d = trend_gate.classify(points, 30, 25, 100, 990, now=1000)
            self.assertEqual(d.reason, '指标样本不足，按原流程发送')

    def test_policy_overrides_and_all_three_higher_worse_rise_checks(self):
        policy = trend_gate.TrendPolicy(hard_ratio=2, rise_ratio=.01, observe_seconds=120)
        # 已越阈值且增长比率足够，但绝对增幅不足阈值的 5%，仍观察。
        points = self.points(30.1, 29.7)
        decision = trend_gate.classify(points, 30, 25, 100, 900, now=1000, policy=policy)
        self.assertEqual(decision.action, 'observe')
        # 增长显著，但当前中位数未到阈值，也不提前发送。
        decision = trend_gate.classify(self.points(29, 20), 30, 25, 100, 990, now=1000)
        self.assertEqual(decision.action, 'observe')
        custom = trend_gate.classify(self.points(50, 50), 30, 25, 100, 990, now=1000, policy=policy)
        self.assertEqual(custom.action, 'observe')
        self.assertEqual(trend_gate.classify(self.points(50, 50), 30, 25, 100, 990, now=1000).action, 'send')

    def test_lower_incomplete_slow_window_fails_open(self):
        d = trend_gate.classify(self.points(.98)[-10:], .99, .995, 100, 990,
                                now=1000, direction='lower_worse')
        self.assertEqual(d.reason, '指标样本不足，按原流程发送')

    def test_directional_escalation_and_alleviation(self):
        for direction, prior, boundary, relieved in (('higher_worse', 32, 40, 39.9),
                                                    ('lower_worse', 1, 0.75, 0.76)):
            self.assertTrue(trend_gate.is_escalation(boundary, prior, direction))
            self.assertFalse(trend_gate.is_escalation(relieved, prior, direction))
            self.assertTrue(trend_gate.is_alleviated(relieved, prior, direction))
            self.assertTrue(trend_gate.is_alleviated(None, prior, direction))
            self.assertFalse(trend_gate.is_escalation(boundary, None, direction))
            decision = trend_gate.Decision('send', boundary, 'timeout', False, direction)
            self.assertTrue(trend_gate.oncall_mention_policy(decision, prior))

    def test_rule_direction_and_query_policy_wiring(self):
        body = {'condition': 'B', 'data': [
            {'refId': 'A', 'model': {'expr': 'success_rate'}},
            {'refId': 'B', 'model': {'type': 'threshold', 'expression': 'A', 'conditions': [
                {'evaluator': {'type': 'lt', 'params': [0.99]},
                 'unloadEvaluator': {'params': [0.995]}}]}}]}
        with patch.object(trend_gate, '_rule_cache', {}), \
             patch.object(trend_gate, '_request_json', return_value=body):
            self.assertEqual(trend_gate._load_rule('x'), ('success_rate', 0.99, 0.995, 'lower_worse'))
        policy = trend_gate.TrendPolicy(request_metric='requests_total', slow_window_seconds=900)
        data = {'alerts': [{'generatorURL': 'https://g/alerting/grafana/x/view', 'labels': {'model': 'm'}}]}
        with patch.object(trend_gate.Config, 'GRAFANA_RULES_READ_KEY', 'test'), \
             patch.object(trend_gate.Config, 'VM_QUERY_URL', 'http://vm/api/v1/query'), \
             patch.object(trend_gate, '_load_rule', return_value=('q', .99, .995, 'lower_worse')), \
             patch.object(trend_gate.time, 'time', return_value=1000), \
             patch.object(trend_gate, '_vm_query', return_value=[{'metric': {'model': 'm'}, 'values': self.points(.98)}]) as query, \
             patch.object(trend_gate, '_request_count', return_value=100) as count:
            trend_gate.decide(data, 990, policy)
        self.assertEqual(query.call_args.kwargs['start'], 40)
        count.assert_called_once_with({'model': 'm'}, 'requests_total')


class RecoveryConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.key = ('r', 'g', 'f')
        self.row = {'status': 'pending', 'config_id': 1, 'cancel_streak': 0,
                    'first_seen': datetime.now() - timedelta(seconds=100),
                    'payload': {'alerts': [{'generatorURL': 'https://g/alerting/grafana/r/view',
                                            'labels': {'alertname': 'test'}}]}}
        self.route = {'id': 1, 'group_id': 'g', 'trend_policy': {'rule_uids': ['r'], 'confirm_cycles': 2}}

    def test_cancel_streak_survives_checks_then_resolves(self):
        def schedule(key, reason, **kwargs):
            self.row['cancel_streak'] = kwargs.get('cancel_streak', 0)
        with fake_db(row=self.route) as pair, \
             patch.object(trend_worker, 'db_cursor') as db, \
             patch.object(trend_gate, 'get_state', side_effect=lambda key: dict(self.row)), \
             patch.object(trend_gate, 'decide', return_value=trend_gate.Decision('cancel', 24, '恢复')), \
             patch.object(trend_gate, 'log_decision'), \
             patch.object(trend_gate, 'schedule_next', side_effect=schedule) as retry, \
             patch.object(trend_gate, 'mark_resolved') as resolved:
            db.return_value.__enter__.return_value = pair
            trend_worker._process_pending_row(self.key, object())
            retry.assert_called_once_with(self.key, '恢复待确认(1/2)', cancel_streak=1)
            resolved.assert_not_called()
            trend_worker._process_pending_row(self.key, object())
            resolved.assert_called_once_with(self.key, '恢复')

    def test_observe_breaks_consecutive_recovery(self):
        def schedule(key, reason, **kwargs):
            self.row['cancel_streak'] = kwargs.get('cancel_streak', 0)
        with fake_db(row=self.route) as pair, \
             patch.object(trend_worker, 'db_cursor') as db, \
             patch.object(trend_gate, 'get_state', side_effect=lambda key: dict(self.row)), \
             patch.object(trend_gate, 'decide', side_effect=[
                 trend_gate.Decision('cancel', 24, '恢复'), trend_gate.Decision('observe', 31, '观察'),
                 trend_gate.Decision('cancel', 24, '恢复')]), \
             patch.object(trend_gate, 'log_decision'), \
             patch.object(trend_gate, 'schedule_next', side_effect=schedule), \
             patch.object(trend_gate, 'mark_resolved') as resolved:
            db.return_value.__enter__.return_value = pair
            for _ in range(3):
                trend_worker._process_pending_row(self.key, object())
            self.assertEqual(self.row['cancel_streak'], 1)
            resolved.assert_not_called()

    def test_legacy_policy_cancels_immediately(self):
        with fake_db(row={'id': 1}) as pair, patch.object(trend_worker, 'db_cursor') as db, \
             patch.object(trend_gate, 'get_state', return_value=self.row), \
             patch.object(trend_gate, 'decide', return_value=trend_gate.Decision('cancel', 24, '恢复')), \
             patch.object(trend_gate, 'log_decision'), patch.object(trend_gate, 'mark_resolved') as resolved:
            db.return_value.__enter__.return_value = pair
            trend_worker._process_pending_row(self.key, object())
        resolved.assert_called_once_with(self.key, '恢复')

    def test_state_transitions_reset_streak_and_old_schema_retries_original_sql(self):
        from mysql.connector import Error
        transitions = [lambda: trend_gate.save_pending(self.key, 1, {}, 1000, '观察'),
                       lambda: trend_gate.mark_sent(self.key, 32, '发送'),
                       lambda: trend_gate.mark_resolved(self.key, '恢复'),
                       lambda: trend_gate.restore_sent(self.key, '缓解'),
                       lambda: trend_gate.schedule_next(self.key, '继续')]
        for transition in transitions:
            with self.subTest(transition=transition), fake_db() as pair, \
                 patch.object(trend_gate, 'db_cursor') as db:
                db.return_value.__enter__.return_value = pair
                pair[1].execute.side_effect = [Error('Unknown column cancel_streak', errno=1054), None]
                transition()
                calls = pair[1].execute.call_args_list
                self.assertIn('cancel_streak=0', calls[0].args[0])
                self.assertNotIn('cancel_streak', calls[1].args[0])
                pair[0].commit.assert_called_once()

    def test_lower_worse_worker_restores_or_escalates_in_correct_direction(self):
        self.row.update(last_sent_at=datetime.now(), last_value=1.0, cancel_streak=1)
        with fake_db(row=self.route) as pair, patch.object(trend_worker, 'db_cursor') as db, \
             patch.object(trend_gate, 'get_state', return_value=self.row), \
             patch.object(trend_gate, 'decide', side_effect=[
                 trend_gate.Decision('send', .8, '下限', True, 'lower_worse'),
                 trend_gate.Decision('send', .7, '下限', True, 'lower_worse')]), \
             patch.object(trend_gate, 'log_decision'), \
             patch.object(trend_gate, 'restore_sent') as restore, \
             patch.object(trend_gate, 'mark_sent') as mark, \
             patch.object(alert_handler, '_process_single_alert_config', return_value={'message_id': 'm'}) as send:
            db.return_value.__enter__.return_value = pair
            trend_worker._process_pending_row(self.key, object())
            restore.assert_called_once()
            send.assert_not_called()
            self.row['status'] = 'sent'
            trend_worker._process_pending_row(self.key, object())
            send.assert_called_once()
            mark.assert_called_once_with(self.key, .7, '下限')


class DigestTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 6, 10, 0, 10, tzinfo=timezone.utc)
        self.row = {'group_id': 'g', 'rule_uid': 'r', 'fingerprint': 'f',
                    'first_seen': self.now.replace(tzinfo=None) - timedelta(seconds=100),
                    'next_check': self.now + timedelta(seconds=30),
                    'metric_value': .98, 'reason': '观察',
                    'payload': {'alerts': [{'labels': {'alertname': '<at id="all">test</at>',
                                                      'tenant': 't', 'model': 'm', 'ep': 'e'}}]}}

    def test_card_hash_ignores_update_clock_and_never_renders_mentions(self):
        with patch.object(trend_gate, '_load_rule', return_value=('q', .99, .995, 'lower_worse')):
            first, digest = trend_digest.build_card([self.row], now=self.now)
            _, unchanged = trend_digest.build_card([self.row], now=self.now + timedelta(seconds=5))
            _, changed = trend_digest.build_card([{**self.row, 'metric_value': .97}], now=self.now)
        card = json.loads(first)
        self.assertTrue(card['config']['update_multi'])
        self.assertEqual(digest, unchanged)
        self.assertNotEqual(digest, changed)
        self.assertTrue(all(e['text']['tag'] == 'plain_text' for e in card['elements'] if 'text' in e))
        self.assertIn('0.98 / 触发阈值 <= 0.99', card['elements'][0]['text']['content'])
        self.assertIn('tenant=t / model=m / ep=e', card['elements'][0]['text']['content'])
        self.assertIn('更新于', card['elements'][-1]['elements'][0]['content'])
        _, empty_a = trend_digest.build_card([], now=self.now)
        _, empty_b = trend_digest.build_card([], now=self.now + timedelta(hours=1))
        self.assertEqual(empty_a, empty_b)

    def test_one_card_per_group_patch_skip_clear_and_reuse(self):
        client = MagicMock()
        client.send.side_effect = ['mid-g', 'mid-h']
        rows = [self.row, {**self.row, 'fingerprint': 'f2'}, {**self.row, 'group_id': 'h'}]
        saved = {}
        def save(group, mid, content_hash):
            saved[group] = {'message_id': mid, 'content_hash': content_hash}
        original_build = trend_digest.build_card
        with patch.object(trend_digest, '_load_rows', side_effect=lambda: (rows, dict(saved))), \
             patch.object(trend_digest, '_save_digest', side_effect=save), \
             patch.object(trend_digest, 'build_card', side_effect=lambda items: original_build(items, self.now)), \
             patch.object(trend_gate, '_load_rule', return_value=('q', 30, 25, 'higher_worse')):
            trend_digest.update_digests(client)
            self.assertEqual(client.send.call_count, 2)
            g_card = json.loads(client.send.call_args_list[0].args[3])
            self.assertEqual(len([e for e in g_card['elements'] if e['tag'] == 'div']), 2)
            trend_digest.update_digests(client)
            client.patch_message.assert_not_called()
            rows.clear()
            trend_digest.update_digests(client)
            self.assertEqual(client.patch_message.call_count, 2)
            self.assertIn('当前无观察中的趋势告警', client.patch_message.call_args.args[1])
            trend_digest.update_digests(client)
            self.assertEqual(client.patch_message.call_count, 2)
            rows.append(self.row)
            trend_digest.update_digests(client)
            self.assertEqual(client.send.call_count, 2)
            self.assertEqual(client.patch_message.call_args.args[0], 'mid-g')

    def test_digest_failures_are_isolated_and_do_not_store_failed_hash(self):
        client = MagicMock()
        client.patch_message.side_effect = RuntimeError('飞书异常')
        client.send.return_value = 'mid-h'
        with patch.object(trend_digest, '_load_rows', return_value=(
                [self.row, {**self.row, 'group_id': 'h'}], {'g': {'message_id': 'mid-g'}})), \
             patch.object(trend_digest, '_save_digest') as save, \
             patch.object(trend_gate, '_load_rule', return_value=('q', 30, 25, 'higher_worse')):
            trend_digest.update_digests(client)
            self.assertEqual(save.call_count, 1)
            self.assertEqual(save.call_args.args[0], 'h')
        with patch.object(trend_digest, '_load_rows', side_effect=RuntimeError('DB unavailable')):
            trend_digest.update_digests(client)

    def test_pending_query_and_empty_due_round_still_refresh_digest(self):
        with fake_db() as pair, patch.object(trend_digest, 'db_cursor') as db:
            db.return_value.__enter__.return_value = pair
            pair[1].fetchall.side_effect = [[self.row], []]
            pending, _ = trend_digest._load_rows()
            self.assertEqual(len(pending), 1)
            self.assertNotIn('next_check', pair[1].execute.call_args_list[0].args[0])
        with patch.object(trend_gate, 'trends_enabled', return_value=True), \
             patch.object(trend_gate, 'enabled_policies', return_value={1: trend_gate.TrendPolicy()}), \
             patch.object(trend_gate, 'due_active', return_value=[]), \
             patch.object(trend_worker, 'cleanup_decision_logs'), \
             patch.object(trend_digest, 'update_digests') as update:
            trend_worker.run_pending_once(object())
            update.assert_called_once()
        with patch.object(trend_gate, 'trends_enabled', return_value=True), \
             patch.object(trend_gate, 'enabled_policies', return_value={}), \
             patch.object(trend_gate, 'due_active', return_value=[]), \
             patch.object(trend_worker, 'cleanup_decision_logs') as cleanup, \
             patch.object(trend_digest, 'update_digests') as update:
            trend_worker.run_pending_once(object())
            update.assert_not_called()
            cleanup.assert_not_called()


class LogCleanupTests(unittest.TestCase):
    def test_cleanup_is_hourly_and_uses_configured_retention(self):
        with fake_db() as pair, patch.object(trend_worker, 'db_cursor') as db, \
             patch.object(trend_worker, '_last_log_cleanup_at', None), \
             patch.object(trend_worker, 'TREND_LOG_RETENTION_DAYS', 90), \
             patch.object(trend_worker, 'TREND_LOG_CLEANUP_SECONDS', 3600), \
             patch.object(trend_worker.time, 'monotonic', side_effect=[0, 3599, 3600]):
            db.return_value.__enter__.return_value = pair
            for _ in range(3):
                trend_worker.cleanup_decision_logs()
            self.assertEqual(db.call_count, 2)
            sql, params = pair[1].execute.call_args.args
            self.assertIn('created_at < UTC_TIMESTAMP() - INTERVAL %s DAY', sql)
            self.assertEqual(params, (90,))
            self.assertEqual(pair[0].commit.call_count, 2)

    def test_failed_cleanup_does_not_raise_or_retry_every_poll(self):
        with patch.object(trend_worker, '_last_log_cleanup_at', None), \
             patch.object(trend_worker, 'db_cursor', side_effect=RuntimeError('db error')) as db, \
             patch.object(trend_worker.time, 'monotonic', side_effect=[10, 25]):
            trend_worker.cleanup_decision_logs()
            trend_worker.cleanup_decision_logs()
            db.assert_called_once()

    def test_bad_retention_cannot_delete_all_logs(self):
        with patch.object(trend_worker, '_last_log_cleanup_at', None), \
             patch.object(trend_worker, 'TREND_LOG_RETENTION_DAYS', 0), \
             patch.object(trend_worker, 'db_cursor') as db:
            trend_worker.cleanup_decision_logs()
            db.assert_not_called()


class TrendFailureTests(unittest.TestCase):
    def test_http_metric_query_failure_still_sends(self):
        key = ('r', 'g', 'f')
        data = {'status': 'firing', 'alerts': [{'status': 'firing'}]}
        with patch.object(trend_gate, 'get_state', return_value=None), \
             patch.object(trend_gate, 'decide', side_effect=RuntimeError('VM unavailable')), \
             patch.object(trend_gate, 'log_decision'), patch.object(trend_gate, 'save_pending'), \
             patch.object(trend_gate, 'mark_sent'), \
             patch.object(alert_handler, '_process_single_alert_config', return_value={'message_id': 'm'}) as send:
            alert_handler._process_trend_config(data, {'id': 1, 'group_id': 'g'}, 'a', object(), key,
                                                trend_gate.TrendPolicy(rule_uids=('r',)))
        send.assert_called_once()
        self.assertIsNone(send.call_args.kwargs['mention_oncall'])

    def test_worker_exception_does_not_block_other_instances_or_digest(self):
        rows = [{'rule_uid': 'r', 'group_id': 'g', 'fingerprint': fp} for fp in ('a', 'b')]
        with patch.object(trend_gate, 'trends_enabled', return_value=True), \
             patch.object(trend_gate, 'due_active', return_value=rows), \
             patch.object(trend_gate, 'enabled_policies', return_value={1: trend_gate.TrendPolicy()}), \
             patch.object(trend_worker, '_process_pending_row', side_effect=[RuntimeError('one failed'), None]) as process, \
             patch.object(trend_gate, 'schedule_next'), \
             patch.object(trend_digest, 'update_digests') as digest, \
             patch.object(trend_worker, 'cleanup_decision_logs') as cleanup:
            trend_worker.run_pending_once(object())
        self.assertEqual(process.call_count, 2)
        digest.assert_called_once()
        cleanup.assert_called_once()


class PolicyApiTests(unittest.TestCase):
    def test_crud_accepts_objects_and_rejects_invalid_json_without_db(self):
        from flask import Flask
        from routes import alert_rules
        app = Flask(__name__)
        app.register_blueprint(alert_rules.alert_rules_bp)
        client = app.test_client()
        base = {'group_id': 'g', 'users': [], 'alert_id': 'a', 'rank': 'p0', 'project': 'p'}
        with patch.object(alert_rules, 'db_cursor') as db:
            for value in ('{', '[]', [], True, 3):
                self.assertEqual(client.post('/api/alert_rules', json={**base, 'trend_policy': value}).status_code, 400)
                self.assertEqual(client.put('/api/alert_rules/1', json={'trend_policy': value}).status_code, 400)
            db.assert_not_called()
        with fake_db() as pair, patch.object(alert_rules, 'db_cursor') as db, \
             patch.object(alert_rules, 'invalidate_alert_config_cache') as invalidate:
            db.return_value.__enter__.return_value = pair
            pair[1].lastrowid = 1
            policy = {'rule_uids': ['a'], 'enabled': True}
            self.assertEqual(client.post('/api/alert_rules', json={**base, 'trend_policy': policy}).status_code, 200)
            sql, values = pair[1].execute.call_args.args
            self.assertEqual(sql.count('%s'), len(values))
            self.assertEqual(json.loads(values[-1]), policy)
            self.assertEqual(client.put('/api/alert_rules/1', json={'trend_policy': json.dumps(policy)}).status_code, 200)
            self.assertEqual(client.put('/api/alert_rules/1', json={'trend_policy': None}).status_code, 200)
            self.assertEqual(invalidate.call_count, 3)


if __name__ == '__main__':
    unittest.main()
