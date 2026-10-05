"""趋势试点关键决策的本地测试；不访问外部服务。"""

import os
import json
import sys
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault('APP_ID', 'test-app-id')
os.environ.setdefault('APP_SECRET', 'test-app-secret')
os.environ.setdefault('MYSQL_PASSWORD', 'test-password')
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from feishu_utils import alert_handler, trend_gate, trend_worker


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


if __name__ == '__main__':
    unittest.main()
