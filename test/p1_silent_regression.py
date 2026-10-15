"""P1静默观察、P0升级通知的生命周期回归；禁止连接真实服务。"""
import json
import os
import sys
import unittest
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ.setdefault('APP_ID', 'test')
os.environ.setdefault('APP_SECRET', 'test')
os.environ.setdefault('MYSQL_PASSWORD', 'test')
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from feishu_utils import alert_handler, trend_gate as gate, trend_worker, trend_digest
save_pending = gate.save_pending


class SilentP1Tests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch('requests.sessions.Session.request', side_effect=AssertionError('禁止HTTP')))
        self.stack.enter_context(patch('db.pool._build_pool', side_effect=AssertionError('禁止真实数据库')))
        self.stack.enter_context(patch.object(gate, 'get_maid_by_fingerprints', return_value=''))
        self.stack.enter_context(patch.object(gate, 'policy_for_route', return_value=gate.TrendPolicy()))
        self.stack.enter_context(patch.object(gate, 'log_decision'))
        self.key = ('r', 'g', 'f')
        self.data = {'status': 'firing', '_route_maids': {'g': 'test-maid'}, 'alerts': [{
            'status': 'firing', 'fingerprint': 'f', 'generatorURL': 'https://g/alerting/grafana/r/view',
            'labels': {'alertname': 'TTFT P99', 'model': 'm', 'severity': 'p0'},
            'annotations': {'description': '原告警描述'}}]}
        self.route = {'id': 1, 'group_id': 'g', 'project': 'test', 'oncall_sync': 1}
        self.first_seen = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=5)
        self.p1 = gate.Decision('send', 40, '观察到期，影响未达到P0', urgent=False, notification_severity='p1')
        self.p0 = gate.Decision('send', 40, '确认持续扩大影响', urgent=True, notification_severity='p0')
        self.state = None
        self.save = self.stack.enter_context(patch.object(gate, 'save_pending', side_effect=self.save_pending))
        self.mark = self.stack.enter_context(patch.object(gate, 'mark_sent_decision', side_effect=self.mark_sent))
        self.schedule = self.stack.enter_context(patch.object(gate, 'schedule_next'))
        self.resolve = self.stack.enter_context(patch.object(gate, 'mark_resolved'))
        self.send = self.stack.enter_context(patch.object(alert_handler, '_process_single_alert_config',
                                                          return_value={'success': True, 'message_id': 'mid'}))
        self.stack.enter_context(patch.object(gate, 'get_state', side_effect=lambda key: self.state))
        db = self.stack.enter_context(patch.object(trend_worker, 'db_cursor'))
        db.return_value.__enter__.return_value = (MagicMock(), MagicMock(fetchone=lambda: self.route))

    def save_pending(self, key, config_id, data, first_seen, reason, *, keep_sent=False):
        previous = self.state or {}
        self.state = dict(previous, status='sent' if keep_sent else 'pending', config_id=config_id,
                          payload=json.dumps(data), first_seen=datetime.fromtimestamp(first_seen, timezone.utc).replace(tzinfo=None),
                          reason=reason)

    def mark_sent(self, key, decision):
        payload = json.loads(self.state['payload'])
        payload['_trend_last_notification_severity'] = decision.notification_severity
        self.state.update(status='sent', payload=json.dumps(payload), last_value=decision.value,
                          last_sent_at=datetime.now(timezone.utc).replace(tzinfo=None))

    def webhook(self, decision=None, error=None):
        with patch.object(gate, 'decide', return_value=decision, side_effect=error):
            return alert_handler._process_trend_config(self.data, self.route, 'TTFT P99', object(), self.key)

    def worker(self, decision=None, error=None):
        payload = json.loads(self.state['payload'])
        with patch.object(gate, 'decide', return_value=decision, side_effect=error):
            trend_worker._process_pending_state(self.key, object(), self.state, payload)

    def test_p1_is_silent_after_deadline_and_repeated_webhooks_keep_first_seen(self):
        result = self.webhook(self.p1)
        first_seen = self.state['first_seen']
        self.webhook(self.p1)
        self.worker(self.p1)
        self.assertTrue(result['success'] and result['skipped'])
        self.send.assert_not_called()
        self.mark.assert_not_called()
        self.assertEqual(self.state['status'], 'pending')
        self.assertEqual(self.state['first_seen'], first_seen)
        self.assertIsNone(self.state.get('last_sent_at'))
        payload = json.loads(self.state['payload'])
        self.assertTrue(payload['_trend_p1_observed'])
        self.assertEqual(payload['_route_maids']['g'], 'test-maid')

    def test_p1_restart_then_p0_upgrades_without_numeric_increase_in_both_paths(self):
        for path in ('webhook', 'worker'):
            with self.subTest(path=path):
                self.state = None
                self.send.reset_mock(); self.mark.reset_mock()
                self.webhook(self.p1)
                # 模拟重启：仅使用数据库JSON状态，不依赖内存去重。
                self.state['payload'] = json.dumps(json.loads(self.state['payload']))
                getattr(self, path)(self.p0)
                self.send.assert_called_once()
                self.assertTrue(self.send.call_args.kwargs['mention_oncall'])
                sent = self.send.call_args.args[0]
                self.assertIn('观察后升级为 P0', sent['alerts'][0]['annotations']['description'])
                self.assertEqual(sent['_route_maids']['g'], 'test-maid')
                self.assertEqual(sent['_trend_notification_severity'], 'p0')
                self.assertEqual(sent['alerts'][0]['labels']['severity'], 'p0')
                self.assertEqual(self.state['status'], 'sent')
                self.assertEqual(json.loads(self.state['payload'])['_trend_last_notification_severity'], 'p0')
                getattr(self, path)(self.p0)
                self.assertEqual(self.send.call_count, 1)

    def test_query_or_sample_failure_after_silent_p1_does_not_notify(self):
        self.webhook(self.p1)
        for error in (TimeoutError('VM超时'), ValueError('响应样本97<100')):
            with self.subTest(error=type(error).__name__):
                self.webhook(error=error)
                self.worker(error=error)
        self.send.assert_not_called()
        self.mark.assert_not_called()
        self.assertEqual(self.state['status'], 'pending')
        self.webhook(self.p0)
        self.send.assert_called_once()

    def test_silent_p1_recovery_does_not_send_orphan_recovery_card(self):
        self.webhook(self.p1)
        resolved = dict(self.data, status='resolved', alerts=[dict(self.data['alerts'][0], status='resolved')])
        result = alert_handler._process_trend_config(resolved, self.route, 'TTFT P99', object(), self.key)
        self.assertTrue(result['skipped'])
        self.send.assert_not_called()
        self.resolve.assert_called_once()

    def test_worker_confirms_silent_p1_recovery(self):
        self.webhook(self.p1)
        decision = gate.Decision('cancel', 20, '恢复', notification_severity='p1')
        self.worker(decision)
        self.resolve.assert_not_called()
        self.assertEqual(self.schedule.call_args.kwargs['cancel_streak'], 1)
        self.state['cancel_streak'] = 1
        self.worker(decision)
        self.resolve.assert_called_once()
        self.send.assert_not_called()

    def test_direct_p0_remains_immediate_and_is_not_mislabeled_as_upgrade(self):
        self.webhook(self.p0)
        self.send.assert_called_once()
        note = self.send.call_args.args[0]['alerts'][0]['annotations']['description']
        self.assertIn('直接判定为 P0', note)
        self.assertNotIn('观察后升级', note)
        self.assertEqual(self.data['alerts'][0]['annotations']['description'], '原告警描述')

    def test_tpot_p1_is_silent_then_p0_notifies_with_upgrade_note(self):
        self.data['alerts'][0]['labels']['alertname'] = 'TPOT P50'
        self.webhook(self.p1)
        self.send.assert_not_called()
        self.worker(self.p0)
        self.assertTrue(self.send.call_args.kwargs['mention_oncall'])
        self.assertIn('观察后升级为 P0', self.send.call_args.args[0]['alerts'][0]['annotations']['description'])

    def test_first_query_failure_without_p1_evidence_keeps_original_fallback(self):
        self.webhook(error=TimeoutError('首次查询失败'))
        self.send.assert_called_once()
        sent = self.send.call_args.args[0]
        self.assertIsNone(self.send.call_args.kwargs['mention_oncall'])
        self.assertNotIn('_trend_notification_severity', sent)
        self.assertNotIn('观察后升级', sent['alerts'][0]['annotations']['description'])

    def test_observe_then_p0_has_upgrade_note(self):
        self.webhook(gate.Decision('observe', 31, '继续观察'))
        self.webhook(self.p0)
        note = self.send.call_args.args[0]['alerts'][0]['annotations']['description']
        self.assertIn('观察后升级为 P0', note)

    def test_existing_pending_observation_also_has_upgrade_note(self):
        self.state = {'status': 'pending', 'config_id': 1, 'payload': json.dumps(self.data),
                      'first_seen': self.first_seen, 'reason': '轻微越线，继续观察'}
        self.worker(self.p0)
        self.assertIn('观察后升级为 P0', self.send.call_args.args[0]['alerts'][0]['annotations']['description'])

    def test_failed_direct_p0_retry_is_not_mislabeled_as_observed_upgrade(self):
        self.send.return_value = None
        self.webhook(self.p0)
        self.send.return_value = {'success': True, 'message_id': 'mid'}
        self.worker(self.p0)
        self.assertIn('直接判定为 P0', self.send.call_args.args[0]['alerts'][0]['annotations']['description'])

    def test_failed_p0_delivery_retries_without_marking_as_sent(self):
        self.webhook(self.p1)
        self.send.return_value = None
        self.worker(self.p0)
        self.mark.assert_not_called()
        self.assertEqual(self.state['status'], 'pending')
        self.send.return_value = {'success': True, 'message_id': 'mid'}
        self.worker(self.p0)
        self.assertEqual(self.send.call_count, 2)
        self.mark.assert_called_once()
        self.assertIn('观察后升级为 P0', self.send.call_args.args[0]['alerts'][0]['annotations']['description'])

    def test_previously_notified_p0_later_p1_still_has_recovery_notification(self):
        self.webhook(self.p0)
        self.worker(self.p1)
        self.assertEqual(self.state['status'], 'sent')
        self.assertEqual(json.loads(self.state['payload'])['_trend_last_notification_severity'], 'p0')
        resolved = dict(self.data, status='resolved', alerts=[dict(self.data['alerts'][0], status='resolved')])
        alert_handler._process_trend_config(resolved, self.route, 'TTFT P99', object(), self.key)
        self.assertEqual(self.send.call_count, 2)
        self.resolve.assert_called_once()

    def test_observation_write_failure_does_not_fall_back_to_p1_delivery(self):
        self.save.side_effect = RuntimeError('DB不可用')
        result = self.webhook(self.p1)
        self.assertFalse(result['success'])
        self.send.assert_not_called()
        self.mark.assert_not_called()

    def test_old_notified_p1_also_stays_silent_after_query_failure(self):
        payload = dict(self.data, _trend_last_notification_severity='p1')
        self.state = {'status': 'sent', 'config_id': 1, 'payload': json.dumps(payload),
                      'first_seen': self.first_seen, 'last_value': 40, 'last_sent_at': self.first_seen}
        self.worker(error=TimeoutError('VM超时'))
        self.send.assert_not_called()
        self.assertEqual(self.state['status'], 'sent')
        self.assertTrue(json.loads(self.state['payload'])['_trend_p1_observed'])

    def test_resolved_previous_episode_does_not_mark_new_direct_p0_as_upgrade(self):
        self.state = {'status': 'resolved', 'payload': json.dumps(dict(self.data, _trend_observed=True,
                      _trend_p1_observed=True, _trend_last_notification_severity='p1')), 'first_seen': self.first_seen}
        self.webhook(self.p0)
        self.assertIn('直接判定为 P0', self.send.call_args.args[0]['alerts'][0]['annotations']['description'])
        self.assertNotIn('_trend_p1_observed', json.loads(self.state['payload']))

    def test_unconfirmed_p0_note_does_not_claim_observed_upgrade(self):
        self.webhook(self.p1)
        fallback = gate.with_decision_note(self.data, gate.Decision('send', None, '查询失败'), self.state)
        self.assertNotIn('观察后升级', fallback['alerts'][0]['annotations']['description'])

    def test_digest_does_not_send_card_for_p1_only_group(self):
        self.webhook(self.p1)
        row = dict(self.state, group_id='g', rule_uid='r', fingerprint='f')
        client = MagicMock()
        with patch.object(trend_digest, '_load_rows', return_value=([row], {})):
            trend_digest.update_digests(client)
        client.send.assert_not_called()
        client.patch_message.assert_not_called()

    def test_digest_excludes_p1_from_mixed_group_and_clears_old_card(self):
        self.webhook(self.p1)
        silent = dict(self.state, group_id='g', rule_uid='r', fingerprint='f')
        visible = dict(silent, rule_uid='success', payload=json.dumps({'alerts': [{'labels': {'alertname': 'Success rate'}}]}))
        client = MagicMock()
        with patch.object(trend_digest, '_load_rows', return_value=([silent, visible], {})), \
             patch.object(trend_digest, '_update_group') as update:
            trend_digest.update_digests(client)
        self.assertEqual(update.call_args.args[2], [visible])
        with patch.object(trend_digest, '_load_rows', return_value=([silent], {'g': {'message_id': 'old'}})), \
             patch.object(trend_digest, '_update_group') as update:
            trend_digest.update_digests(client)
        self.assertEqual(update.call_args.args[2], [])

    def test_sql_observation_preserves_successful_notification_state(self):
        # 使用真实持久化函数验证参数绑定和keep_sent分支；不执行生产SQL。
        with patch.object(gate, 'db_cursor') as db:
            conn, cursor = MagicMock(), MagicMock()
            db.return_value.__enter__.return_value = conn, cursor
            save_pending(self.key, 1, dict(self.data, _trend_p1_observed=True),
                         1000, 'P1仅后台观察', keep_sent=True)
            sql, params = cursor.execute.call_args.args
            self.assertEqual(sql.count('%s'), len(params))
            self.assertIn("status=IF(status='sent' AND %s, 'sent', 'pending')", sql)
            self.assertTrue(params[-1])
            self.assertTrue(json.loads(params[4])['_trend_p1_observed'])
            self.assertIn("'$._trend_last_notification_severity'", sql)
            conn.commit.assert_called_once()
            self.mark.assert_not_called()


if __name__ == '__main__':
    unittest.main()
