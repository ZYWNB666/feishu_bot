"""趋势试点关键决策的本地测试；不访问外部服务。"""

import os
import json
import sys
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import patch, MagicMock

os.environ.setdefault('APP_ID', 'test-app-id')
os.environ.setdefault('APP_SECRET', 'test-app-secret')
os.environ.setdefault('MYSQL_PASSWORD', 'test-password')
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from feishu_utils import alert_handler, trend_gate, trend_worker


def setUpModule():
    # 不允许测试因遗漏 mock 意外使用本地 .env 中的真实连接。
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


class PolicyTests(unittest.TestCase):
    def setUp(self):
        trend_gate.invalidate_policy_cache()

    def tearDown(self):
        trend_gate.invalidate_policy_cache()

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
                    'payload': {'alerts': [{'labels': {'alertname': 'test'}}]}}
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
