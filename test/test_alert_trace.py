"""告警关联日志与观察时限回归；禁止访问真实 HTTP/数据库。"""

import copy
import json
import logging
import os
import sys
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ.setdefault('APP_ID', 'test-app-id')
os.environ.setdefault('APP_SECRET', 'test-app-secret')
os.environ.setdefault('MYSQL_PASSWORD', 'test-password')
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from alerts_format import savedb
from feishu_utils import alert_handler, callback_handler, trend_gate, trend_worker, trend_digest
from utils import alert_trace as trace


def payload(status='firing', start='2026-10-06T00:00:00Z'):
    return {'status': status, 'alerts': [{
        'status': status, 'fingerprint': 'fp-test', 'startsAt': start,
        'generatorURL': 'https://example.test/alerting/grafana/rule-test/view',
        'labels': {'alertname': 'Kimi TTFT', 'severity': 'p0'},
    }]}


class TraceTests(unittest.TestCase):
    def setUp(self):
        previous = logging.getLogRecordFactory()
        self.addCleanup(logging.setLogRecordFactory, previous)
        trace.install_log_context()
        for guard in (
            patch('requests.sessions.Session.request', side_effect=AssertionError('禁止外部 HTTP')),
            patch('db.pool._build_pool', side_effect=AssertionError('禁止真实数据库')),
            patch.object(trend_gate.Config, 'TREND_GATE_MODE', 'legacy'),
            patch.object(trend_gate, 'trends_enabled', return_value=False),
        ):
            guard.start()
            self.addCleanup(guard.stop)
        alert_handler._alert_dedup_cache.clear()
        alert_handler._alert_label_dedup_cache.clear()
        alert_handler._resolved_dedup_cache.clear()

    def test_identity_survives_redelivery_but_separates_groups_and_episodes(self):
        first = trace.route_maid(payload(), 'g1')
        self.assertEqual(first, trace.route_maid(payload(), 'g1'))
        self.assertEqual(first, trace.route_maid(payload('resolved'), 'g1'))
        self.assertNotEqual(first, trace.route_maid(payload(), 'g2'))
        self.assertNotEqual(first, trace.route_maid(payload(start='2026-10-06T01:00:00Z'), 'g1'))
        missing = payload(start='')
        generated = trace.route_maid(missing, 'g1')
        self.assertEqual(generated, trace.route_maid(missing, 'g1'))
        self.assertNotEqual(generated, trace.route_maid(payload(start=''), 'g1'))

    def test_batch_identity_ignores_alert_order(self):
        first = payload()
        second = copy.deepcopy(first['alerts'][0])
        second['fingerprint'] = 'fp-other'
        first['alerts'].append(second)
        reversed_data = copy.deepcopy(first)
        reversed_data['alerts'].reverse()
        self.assertEqual(trace.route_maid(first, 'g1'), trace.route_maid(reversed_data, 'g1'))

    def test_incoming_webhook_cannot_choose_persisted_id(self):
        data = payload()
        data['_route_maids'] = {'g1': 'injected-id'}

        @trace.traced_request
        def receive(body):
            return trace.route_maid(body, 'g1')

        self.assertNotEqual(receive(data), 'injected-id')
        self.assertEqual(data['_route_maids']['g1'], 'injected-id')

    def test_context_isolated_between_threads_and_reset_after_exception(self):
        barrier = threading.Barrier(2)
        logger = logging.getLogger('trace-test')

        def work(maid):
            with trace.log_context(maid=maid, group_id=maid):
                barrier.wait(timeout=5)
                record = logger.makeRecord('trace-test', logging.INFO, __file__, 1, 'test', (), None)
                return record.maid, record.group_id

        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(list(pool.map(work, ['one', 'two'])), [('one', 'one'), ('two', 'two')])
        with self.assertRaises(RuntimeError):
            with trace.log_context(maid='failed'):
                raise RuntimeError('test')
        record = logger.makeRecord('trace-test', logging.INFO, __file__, 1, 'after', (), None)
        self.assertEqual(record.maid, '-')
        self.assertIsNone(trace.current_maid())

    def test_callback_thread_inherits_maid_after_request_context_ends(self):
        results = []
        with trace.log_context(maid='callback-maid'):
            target = trace.contextual_target(lambda: results.append(trace.current_maid()))
        worker = threading.Thread(target=target)
        worker.start()
        worker.join(timeout=5)
        self.assertEqual(results, ['callback-maid'])
        self.assertIsNone(trace.current_maid())

    def test_callback_entry_and_action_logs_reuse_card_maid(self):
        data = {'action': {'value': {'action': 'ack_incident', 'maid': 'card-maid', 'incident_id': 'incident'}},
                'open_message_id': 'message-id', 'open_id': 'operator-id'}
        with patch.object(callback_handler, 'is_duplicate_callback', return_value=False), \
             patch.object(callback_handler, 'handle_ack_incident_action',
                          side_effect=lambda *a, **kw: logging.getLogger('trace-test').info('ack')), \
             self.assertLogs(level='INFO') as logs:
            callback_handler.process_card_callback(data, object())
        self.assertTrue(any('event=alert.callback' in r.getMessage() for r in logs.records))
        self.assertTrue(all(r.maid == 'card-maid' for r in logs.records))

    def test_observe_persists_maid_and_worker_reuses_it_for_send(self):
        data = payload()
        route = {'id': 1, 'group_id': 'g1'}
        key = ('rule-test', 'g1', 'fp-test')
        saved = {}

        def persist(key, config_id, body, first_seen, reason):
            saved.update(json.loads(json.dumps(body)))

        with patch.object(trend_gate, 'get_state', return_value=None), \
             patch.object(trend_gate, 'decide', return_value=trend_gate.Decision('observe', 31, '观察')), \
             patch.object(trend_gate, 'log_decision'), \
             patch.object(trend_gate, 'save_pending', side_effect=persist), \
             self.assertLogs(level='INFO') as received_logs:
            alert_handler._process_trend_config(data, route, 'TTFT', object(), key)
        maid = saved['_route_maids']['g1']
        self.assertTrue(all(r.maid == maid for r in received_logs.records))

        row = {'payload': json.dumps(saved), 'status': 'pending', 'config_id': 1,
               'first_seen': datetime.now(timezone.utc), 'last_sent_at': None}
        db = MagicMock()
        db.return_value.__enter__.return_value = MagicMock(), MagicMock()
        db.return_value.__enter__.return_value[1].fetchone.return_value = route
        sending = []

        def send(body, *args, **kwargs):
            sending.append((trace.current_maid(), body['_route_maids']['g1']))
            return {'message_id': 'message-id'}

        with patch.object(trend_gate, 'get_state', return_value=row), \
             patch.object(trend_worker, 'db_cursor', db), \
             patch.object(trend_gate, 'policy_for_route', return_value=None), \
             patch.object(trend_gate, 'decide', return_value=trend_gate.Decision('send', 31, '到期', False)), \
             patch.object(trend_gate, 'log_decision'), \
             patch.object(trend_gate, 'mark_sent'), \
             patch.object(alert_handler, '_process_single_alert_config', side_effect=send), \
             self.assertLogs(level='INFO') as worker_logs:
            trend_worker._process_pending_row(key, object())
        self.assertEqual(sending, [(maid, maid)])
        self.assertTrue(all(r.maid == maid for r in worker_logs.records))

    def test_recovery_uses_pending_maid_even_if_starts_at_changed(self):
        original = payload()
        maid = trace.route_maid(original, 'g1')
        state = {'status': 'pending', 'last_sent_at': None, 'payload': json.dumps(original)}
        data = payload('resolved', '2026-10-06T00:03:00Z')
        with patch.object(trend_gate, 'get_state', return_value=state), \
             patch.object(trend_gate, 'mark_resolved'), \
             self.assertLogs(level='INFO') as logs:
            response = alert_handler._process_trend_config(
                data, {'group_id': 'g1'}, 'TTFT', object(), ('rule-test', 'g1', 'fp-test'))
        self.assertTrue(response['skipped'])
        self.assertTrue(all(r.maid == maid for r in logs.records))

    def test_database_id_matches_trace_without_overwriting_message_or_silence(self):
        with patch.object(savedb, 'db_cursor') as db, trace.log_context(maid='trace-maid'):
            conn, cursor = MagicMock(), MagicMock()
            db.return_value.__enter__.return_value = conn, cursor
            for _ in range(2):
                self.assertEqual(savedb.save_dbdata(payload(), 'project', 'g1'), 'trace-maid')
            for call in cursor.execute.call_args_list:
                sql, params = call.args
                self.assertEqual(params[0], 'trace-maid')
                self.assertEqual(sql.split('ON DUPLICATE KEY UPDATE')[1].strip(), 'id=id')

    def test_normal_received_and_send_logs_share_card_id(self):
        route = {'id': 1, 'group_id': 'g1'}
        data = payload()
        expected = trace.route_maid(copy.deepcopy(data), 'g1')

        def send(*args, **kwargs):
            with trace.route_context(args[0], 'g1'):
                logging.getLogger('trace-test').info('send')
                return {'success': True, 'message_id': 'message-id'}

        with patch.object(alert_handler, 'get_alert_config_by_labels', return_value=[route]), \
             patch.object(trend_gate, 'is_enabled_for', return_value=False), \
             patch.object(alert_handler, '_process_single_alert_config', side_effect=send), \
             self.assertLogs(level='INFO') as logs:
            response, status = alert_handler.process_alert_request(data, object())
        self.assertEqual(status, 200)
        self.assertEqual(response['summary']['success'], 1)
        self.assertTrue(all(expected in r.maid for r in logs.records))

    def test_batch_trace_lookup_failure_does_not_drop_other_routes(self):
        data = payload()
        other = copy.deepcopy(data['alerts'][0])
        other['fingerprint'] = 'good-fingerprint'
        other['labels']['alertname'] = 'good-rule'
        data['alerts'].append(other)

        def lookup(labels):
            if labels.get('alertname') == 'Kimi TTFT':
                raise RuntimeError('route lookup failed')
            return [{'id': 2, 'group_id': 'good-group'}]

        with patch.object(alert_handler, 'get_alert_config_by_labels', side_effect=lookup), \
             patch.object(trend_gate, 'is_enabled_for', return_value=False), \
             patch.object(alert_handler, '_process_single_alert_config', return_value={'success': True}) as send, \
             self.assertLogs(level='ERROR'):
            response, status = alert_handler.process_alert_request(data, object())
        self.assertEqual(status, 200)
        self.assertEqual(response['summary'], {'total': 2, 'success': 1, 'failed': 1})
        self.assertEqual(send.call_count, 1)

    def test_old_normal_recovery_logs_use_existing_database_maid(self):
        with trace.log_context(), \
             patch.object(alert_handler, 'get_alert_config_by_labels', return_value=[{'group_id': 'g1'}]), \
             patch.object(alert_handler, 'get_maid_by_fingerprints', return_value='old-maid'), \
             self.assertLogs(level='INFO') as logs:
            alert_handler._find_alert_configs(payload('resolved'))
        self.assertTrue(all(r.maid == 'old-maid' for r in logs.records))

    def test_pending_recovery_uses_persisted_id_before_received_log(self):
        original = payload()
        maid = trace.route_maid(original, 'g1')
        state = {'status': 'pending', 'payload': json.dumps(original), 'last_sent_at': None}
        with trace.log_context(), \
             patch.object(trend_gate, 'trends_enabled', return_value=True), \
             patch.object(trend_gate, 'get_state', return_value=state), \
             patch.object(alert_handler, 'get_alert_config_by_labels', return_value=[{'group_id': 'g1'}]), \
             patch.object(alert_handler, 'get_maid_by_fingerprints') as old_record, \
             self.assertLogs(level='INFO') as logs:
            alert_handler._find_alert_configs(payload('resolved', '2026-10-06T00:03:00Z'))
        old_record.assert_not_called()
        self.assertTrue(all(r.maid == maid for r in logs.records))

    def test_old_sent_trend_state_reuses_existing_card_id(self):
        state = {'status': 'sent', 'payload': json.dumps(payload()), 'last_sent_at': datetime.now(timezone.utc)}
        with patch.object(trend_gate, 'get_maid_by_fingerprints', return_value='old-card-maid') as lookup:
            self.assertEqual(trend_gate.state_maid(state, 'g1'), 'old-card-maid')
        lookup.assert_called_once_with(['fp-test'], group_id='g1')

    def test_firing_redelivery_logs_use_persisted_id_before_route_match(self):
        state = {'status': 'sent', 'payload': json.dumps(payload()), 'last_sent_at': datetime.now(timezone.utc)}
        with trace.log_context(), \
             patch.object(trend_gate, 'trends_enabled', return_value=True), \
             patch.object(trend_gate, 'get_state', return_value=state), \
             patch.object(trend_gate, 'get_maid_by_fingerprints', return_value='old-card-maid'), \
             patch.object(alert_handler, 'get_alert_config_by_labels', return_value=[{'group_id': 'g1'}]), \
             self.assertLogs(level='INFO') as logs:
            alert_handler._find_alert_configs(payload())
        self.assertTrue(all(r.maid == 'old-card-maid' for r in logs.records))

    def test_legacy_pending_recovery_uses_original_start_for_trace_id(self):
        original = payload()
        expected = trace.route_maid(copy.deepcopy(original), 'g1')
        state = {'status': 'pending', 'payload': json.dumps(original), 'last_sent_at': None}
        with trace.log_context(), \
             patch.object(trend_gate, 'trends_enabled', return_value=True), \
             patch.object(trend_gate, 'get_state', return_value=state), \
             patch.object(alert_handler, 'get_alert_config_by_labels', return_value=[{'group_id': 'g1'}]), \
             self.assertLogs(level='INFO') as logs:
            alert_handler._find_alert_configs(payload('resolved', '2026-10-06T00:03:00Z'))
        self.assertTrue(all(r.maid == expected for r in logs.records))

    def test_worker_error_after_loading_legacy_state_keeps_actual_card_maid(self):
        row = {'rule_uid': 'rule-test', 'group_id': 'g1', 'fingerprint': 'fp-test',
               'status': 'sent', 'payload': json.dumps(payload()), 'last_sent_at': datetime.now(timezone.utc)}
        with patch.object(trend_gate, 'trends_enabled', return_value=True), \
             patch.object(trend_gate, 'due_active', return_value=[row]), \
             patch.object(trend_gate, 'get_state', return_value=row), \
             patch.object(trend_gate, 'get_maid_by_fingerprints', return_value='old-card-maid'), \
             patch.object(trend_gate, 'enabled_policies', return_value={}), \
             patch.object(trend_gate, 'global_policy', return_value=None), \
             patch.object(trend_gate, 'schedule_next'), \
             patch.object(trend_worker, '_process_pending_state', side_effect=RuntimeError('test failure')), \
             self.assertLogs(level='ERROR') as logs:
            trend_worker.run_pending_once(object())
        self.assertTrue(all(r.maid == 'old-card-maid' for r in logs.records))

    def test_malformed_worker_payload_does_not_stop_other_alerts(self):
        rows = [{'rule_uid': 'rule-test', 'group_id': 'g1', 'fingerprint': 'bad', 'payload': '{'},
                {'rule_uid': 'rule-test', 'group_id': 'g1', 'fingerprint': 'good', 'payload': payload()}]
        with patch.object(trend_gate, 'trends_enabled', return_value=True), \
             patch.object(trend_gate, 'due_active', return_value=rows), \
             patch.object(trend_gate, 'enabled_policies', return_value={}), \
             patch.object(trend_gate, 'global_policy', return_value=None), \
             patch.object(trend_gate, 'schedule_next') as retry, \
             patch.object(trend_worker, '_process_pending_row', side_effect=[ValueError('bad json'), None]) as process, \
             self.assertLogs(level='ERROR'):
            trend_worker.run_pending_once(object())
        self.assertEqual(process.call_count, 2)
        retry.assert_called_once_with(('rule-test', 'g1', 'bad'), '复查异常，等待重试')

    def test_default_observation_boundary_is_120_seconds(self):
        self.assertEqual(trend_gate.TrendPolicy().observe_seconds, 120)
        points = [(t, 31.0) for t in range(800, 1001, 15)]
        for age, expected in ((90, 'observe'), (119, 'observe'), (120, 'send')):
            with self.subTest(age=age):
                decision = trend_gate.classify(points, 30, 25, 100, 1000-age, now=1000)
                self.assertEqual(decision.action, expected)

    def test_evaluation_logs_explain_evidence_and_remaining_time(self):
        points = [(t, 31.0) for t in range(800, 1001, 15)]
        with trace.log_context(maid='evaluation-maid'), \
             patch.object(trend_gate.Config, 'GRAFANA_RULES_READ_KEY', 'test'), \
             patch.object(trend_gate.Config, 'VM_QUERY_URL', 'http://example.test/api/v1/query'), \
             patch.object(trend_gate, '_load_rule', return_value=('test_metric', 30, 25, 'higher_worse')), \
             patch.object(trend_gate, '_vm_query'), \
             patch.object(trend_gate, '_matching_points', return_value=points), \
             patch.object(trend_gate, '_request_count', return_value=100), \
             patch.object(trend_gate.time, 'time', return_value=1000), \
             self.assertLogs(level='INFO') as logs:
            decision = trend_gate.decide(payload(), 940)
        self.assertEqual(decision.action, 'observe')
        evidence = next(r for r in logs.records if 'event=trend.evaluate' in r.getMessage())
        self.assertEqual(evidence.maid, 'evaluation-maid')
        for field in ('threshold=30', 'recent_median=31', 'previous_median=31',
                      'observe_seconds=120', 'remaining_seconds=60.0', 'counter_count=100'):
            self.assertIn(field, evidence.getMessage())

    def test_digest_send_has_all_pending_maids(self):
        rows = []
        for maid in ('pending-one', 'pending-two'):
            data = payload()
            data['_route_maids'] = {'g1': maid}
            rows.append({'group_id': 'g1', 'payload': data})
        client = MagicMock()
        client.send.side_effect = lambda *a: logging.getLogger('trace-test').info('digest send') or 'mid'
        with patch.object(trend_digest, '_load_rows', return_value=(rows, {})), \
             patch.object(trend_digest, 'build_card', return_value=('{}', 'hash')), \
             patch.object(trend_digest, '_save_digest'), \
             self.assertLogs(level='INFO') as logs:
            trend_digest.update_digests(client)
        self.assertTrue(all(r.maid == 'pending-one,pending-two' for r in logs.records))


if __name__ == '__main__':
    unittest.main()
