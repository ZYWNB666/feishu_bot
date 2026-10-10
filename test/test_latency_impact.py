"""影响分级与发送升级回归；禁止外部 HTTP 和数据库连接。"""
import json
import math
import os
import sys
import unittest
from contextlib import ExitStack
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ.setdefault('APP_ID', 'test')
os.environ.setdefault('APP_SECRET', 'test')
os.environ.setdefault('MYSQL_PASSWORD', 'test')
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from feishu_utils import latency_impact as impact, trend_gate as gate, alert_handler, trend_worker

NOW = 1000
LABELS = {'tenant': 't', 'model': 'm'}
EXPR = 'histogram_quantile(0.99, sum by(model, le, tenant)(rate(magik_model_ttft_ms_bucket{tenant="t",model="m"}[10m]))) / 1000'


def window(slow, total=1000, severe=0):
    return impact.HistogramWindow({10000: total-slow, 25000: total-slow,
                                  30000: total-slow, 50000: total-severe,
                                  75000: total-severe, 100000: total, math.inf: total})


def matrix(windows):
    return [{'metric': dict(LABELS, le=str(le), impact_window=str(seconds)),
             'values': [[NOW, str(n)]]}
            for seconds, data in windows.items() for le, n in data.counts.items()]


def responses(code_counts):
    return [{'metric': dict(LABELS, code=code), 'values': [[NOW, str(n)]]}
            for code, n in code_counts.items()]


def setUpModule():
    global guards
    guards = [patch('requests.sessions.Session.request', side_effect=AssertionError('禁止外部 HTTP')),
              patch('db.pool._build_pool', side_effect=AssertionError('禁止真实数据库')),
              patch.object(gate.Config, 'TREND_IMPACT_ENABLED', True),
              patch.object(gate.Config, 'TREND_GATE_MODE', 'legacy'),
              patch.object(gate, 'get_maid_by_fingerprints', return_value='')]
    for guard in guards:
        guard.start()


def tearDownModule():
    for guard in reversed(guards):
        guard.stop()


class ImpactAlgorithmTests(unittest.TestCase):
    def setUp(self):
        self.spec = impact.parse_histogram(EXPR, LABELS)

    def test_quantile_boundaries_do_not_use_one_global_ratio(self):
        for q, permitted in ((.99, 10), (.95, 50), (.5, 500)):
            spec = replace(self.spec, quantile=q)
            for slow, expected in ((permitted-1, 'p1'), (permitted, 'p1'), (permitted+1, 'p0')):
                with self.subTest(q=q, slow=slow):
                    windows = {90: window(slow), 300: window(slow)}
                    self.assertEqual(impact.impact_level(spec, windows, 30, 30, 2.5)[0], expected)

    def test_sustained_plateau_is_urgent_without_growth(self):
        windows = {90: window(20), 300: window(60, 3000)}
        decision = gate.Decision('observe', 31, '稳定超标')
        with patch.object(impact, 'read_windows', return_value=windows):
            result = gate._latency_decision(decision, EXPR, LABELS, 30, 30, gate.TrendPolicy(), NOW)
        self.assertEqual((result.action, result.notification_severity, result.urgent), ('send', 'p0', True))

    def test_short_severe_window_does_not_wait_for_long_window(self):
        windows = {90: window(20, severe=11), 300: window(20, 10000, severe=11)}
        level, evidence = impact.impact_level(self.spec, windows, 30, 30, 2.5)
        self.assertEqual(level, 'p0')
        self.assertTrue(evidence['severe'])
        self.assertFalse(evidence['sustained'])

    def test_rare_spikes_and_historical_residue_allow_p1(self):
        for slow in (0, 5, 10):
            windows = {90: window(slow), 300: window(150, 3000)}
            self.assertEqual(impact.impact_level(self.spec, windows, 30, 30, 2.5)[0], 'p1')

    def test_bucket_bounds_do_not_guess_unknown_distribution(self):
        self.assertEqual(window(15).slow_bounds(35000), (0, .015))
        self.assertIsNone(impact.impact_level(self.spec, {90: window(15), 300: window(15)}, 35, 30, 2.5)[0])

    def test_recovery_threshold_is_stricter_than_trigger(self):
        # All but one observation is <=30s, but 20 observations still exceed 25s.
        w = window(0)
        w.counts[25000] = 980
        self.assertIsNone(impact.impact_level(self.spec, {90: w, 300: w}, 30, 25, 2.5)[0])

    def test_units_and_selector_are_preserved(self):
        self.assertEqual(self.spec.divisor, 1000)
        tpot = impact.parse_histogram(EXPR.replace('ttft', 'tpot').replace(' / 1000', ''), LABELS)
        self.assertEqual(tpot.divisor, 1)
        query = MagicMock(return_value=matrix({90: window(0), 300: window(0)}))
        impact.read_windows(self.spec, LABELS, NOW, 100, query)
        text = query.call_args.args[0]
        self.assertIn('tenant="t",model="m"', text)
        self.assertIn('timestamp(', text)
        self.assertIn('resets(', text)
        self.assertEqual(query.call_args.kwargs, {'start': NOW, 'end': NOW, 'step': 15})

    def test_unsupported_expressions_and_dimensions_fail_closed_to_downgrade(self):
        for expr, labels in ((EXPR+' + 1', LABELS), (EXPR, dict(LABELS, ep='other')),
                             (EXPR.replace('/ 1000', '/ 100'), LABELS),
                             (EXPR.replace('0.99', '1'), LABELS), (EXPR, {})):
            with self.subTest(expr=expr, labels=labels), self.assertRaises(ValueError):
                impact.parse_histogram(expr, labels)

    def test_incomplete_or_invalid_histograms_never_allow_downgrade(self):
        valid = matrix({90: window(0), 300: window(0)})
        mutations = [lambda r: r.pop(), lambda r: r[0]['values'][0].__setitem__(1, 'NaN'),
                     lambda r: r[0]['values'][0].__setitem__(0, 900),
                     lambda r: r.append(r[0]),
                     lambda r: r[0]['values'][0].__setitem__(1, '2000'),
                     lambda r: r[0]['metric'].__setitem__('tenant', 'wrong')]
        for mutate in mutations:
            rows = json.loads(json.dumps(valid))
            mutate(rows)
            with self.subTest(mutate=mutate), self.assertRaises(ValueError):
                impact.read_windows(self.spec, LABELS, NOW, 100, MagicMock(return_value=rows))
        for windows in ({90: window(0, 19), 300: window(0, 1000)},
                        {90: window(0, 1001), 300: window(0, 1000)}):
            with self.assertRaises(ValueError):
                impact.read_windows(self.spec, LABELS, NOW, 100, MagicMock(return_value=matrix(windows)))

    def test_response_errors_and_timeouts_prevent_p1(self):
        windows = {90: window(0), 300: window(0)}
        baseline = gate.Decision('send', 40, '旧窗口恶化', urgent=True)
        for code, expected in (('500', 'p0'), ('408', 'p0'), ('400', 'p1')):
            with self.subTest(code=code), patch.object(impact, 'read_windows', return_value=windows), \
                 patch.object(gate, '_vm_query', return_value=responses({'200': 200, code: 1})):
                result = gate._latency_decision(baseline, EXPR, LABELS, 30, 30, gate.TrendPolicy(), NOW)
                self.assertEqual(result.notification_severity, expected)

    def test_missing_or_insufficient_response_evidence_cannot_downgrade(self):
        for rows in ([], responses({'500': 200}), responses({'200': 99}), responses({'unknown': 1000})):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                impact.response_health(self.spec, LABELS, NOW, 100, MagicMock(return_value=rows))

    def test_p99_requires_at_least_100_observations(self):
        with patch.object(impact, 'read_windows', side_effect=ValueError('不足')) as read:
            with self.assertRaises(ValueError):
                gate._latency_decision(gate.Decision('send', 40, '越线'), EXPR, LABELS,
                                       30, 30, gate.TrendPolicy(), NOW)
        self.assertEqual(read.call_args.args[3], 100)

    def test_uncertain_at_deadline_keeps_urgent_fallback(self):
        windows = {90: window(15), 300: window(15, 3000)}
        with patch.object(impact, 'read_windows', return_value=windows):
            for action in ('send', 'cancel'):
                result = gate._latency_decision(gate.Decision(action, 36, '观察到期', urgent=False),
                                                EXPR, LABELS, 35, 30, gate.TrendPolicy(), NOW)
                self.assertEqual((result.action, result.notification_severity), ('send', 'p0'))

    def test_healthy_observation_respects_180_second_deadline(self):
        with patch.object(impact, 'read_windows', return_value={90: window(0), 300: window(0)}), \
             patch.object(impact, 'response_health', return_value=(1000, 0)):
            result = gate._latency_decision(gate.Decision('observe', 31, '继续观察'), EXPR, LABELS,
                                            30, 30, gate.TrendPolicy(), NOW)
            self.assertEqual(result.action, 'observe')


class NotificationTests(unittest.TestCase):
    def setUp(self):
        self.key = ('r', 'g', 'f')
        self.data = {'status': 'firing', 'alerts': [{'status': 'firing', 'fingerprint': 'f',
                     'generatorURL': 'https://g/alerting/grafana/r/view',
                     'labels': dict(LABELS, severity='p0', alertname='TTFT'), 'annotations': {}}]}
        self.route = {'id': 1, 'group_id': 'g', 'project': 'test', 'rank': 'p0',
                      'oncall_sync': 1, 'template_type': 'ops'}
        self.state = {'status': 'sent', 'payload': dict(self.data, _trend_last_notification_severity='p1'),
                      'last_value': 40, 'first_seen': datetime.now()-timedelta(minutes=5),
                      'last_sent_at': datetime.now(), 'config_id': 1}
        self.p0 = gate.Decision('send', 40, '持续影响', urgent=True, notification_severity='p0')

    def test_p1_stays_unmentioned_even_if_long_value_rises_25_percent(self):
        d = replace(self.p0, notification_severity='p1', urgent=False, value=60)
        self.assertIs(gate.oncall_mention_policy(d, 40), False)

    def test_grade_upgrade_and_error_fallback_ignore_numeric_growth(self):
        self.assertTrue(gate.grade_upgrade(self.p0, self.state))
        self.assertTrue(gate.grade_upgrade(gate.Decision('send', None, '查询失败'), self.state))
        self.assertFalse(gate.grade_upgrade(replace(self.p0, action='observe'), self.state))
        state = dict(self.state, payload=dict(self.data, _trend_last_notification_severity='p0'))
        self.assertFalse(gate.grade_upgrade(self.p0, state))

    def test_http_p1_to_p0_sends_once_without_increase(self):
        with patch.object(gate, 'get_state', return_value=self.state), \
             patch.object(gate, 'decide', return_value=self.p0), patch.object(gate, 'log_decision'), \
             patch.object(gate, 'save_pending'), patch.object(gate, 'mark_sent_decision') as mark, \
             patch.object(alert_handler, '_process_single_alert_config', return_value={'message_id': 'mid'}) as send:
            alert_handler._process_trend_config(self.data, self.route, 'TTFT', object(), self.key)
            self.assertTrue(send.call_args.kwargs['mention_oncall'])
            self.assertEqual(send.call_args.args[0]['_trend_notification_severity'], 'p0')
            mark.assert_called_once()
            self.state['payload']['_trend_last_notification_severity'] = 'p0'
            alert_handler._process_trend_config(self.data, self.route, 'TTFT', object(), self.key)
            self.assertEqual(send.call_count, 1)

    def test_worker_upgrade_and_failed_send_retry_not_restored_to_p1(self):
        for status in ('sent', 'pending'):
            state = dict(self.state, status=status)
            with self.subTest(status=status), patch.object(trend_worker, 'db_cursor') as db, \
                 patch.object(gate, 'decide', return_value=self.p0), patch.object(gate, 'log_decision'), \
                 patch.object(gate, 'restore_sent') as restore, patch.object(gate, 'mark_sent_decision') as mark, \
                 patch.object(gate, 'schedule_next') as schedule, \
                 patch.object(alert_handler, '_process_single_alert_config', side_effect=[None, {'message_id': 'm'}]) as send:
                db.return_value.__enter__.return_value = (MagicMock(), MagicMock(fetchone=lambda: self.route))
                trend_worker._process_pending_state(self.key, object(), state, self.data)
                mark.assert_not_called()
                schedule.assert_called_once()
                trend_worker._process_pending_state(self.key, object(), state, self.data)
                mark.assert_called_once_with(self.key, self.p0)
                self.assertEqual(send.call_count, 2)
                restore.assert_not_called()

    def test_card_grade_and_mentions_original_silence_labels_unchanged(self):
        for template in ('ops', 'biz'):
            for grade in ('p0', 'p1'):
                with self.subTest(template=template, grade=grade), ExitStack() as stack:
                    decision = replace(self.p0, notification_severity=grade, urgent=grade=='p0')
                    data = gate.with_decision_note(self.data, decision)
                    formatter = stack.enter_context(patch.object(alert_handler, 'alert_data_api',
                            return_value=(['severity: p0', 'alert text'], ['p0'], None, {})))
                    oncall = stack.enter_context(patch.object(alert_handler, '_get_oncall_mentioned_users', return_value=['u']))
                    sender = stack.enter_context(patch.object(alert_handler, 'alert_to_feishu', return_value='mid'))
                    builder = stack.enter_context(patch.object(alert_handler, 'build_biz_firing_card', return_value='card'))
                    client = MagicMock(); client.send.return_value='mid'
                    alert_handler._process_single_alert_config(data, dict(self.route, template_type=template),
                                                               'TTFT', client, mention_oncall=grade=='p0')
                    self.assertEqual(formatter.call_args.args[0]['alerts'][0]['labels']['severity'], 'p0')
                    self.assertEqual(self.data['alerts'][0]['labels']['severity'], 'p0')
                    self.assertEqual(oncall.call_count, 1 if grade=='p0' else 0)
                    if template == 'ops':
                        self.assertEqual(sender.call_args.kwargs['severity'], grade)
                        self.assertEqual(sender.call_args.args[2], ['u'] if grade=='p0' else [])
                    else:
                        self.assertEqual(builder.call_args.args[1], grade)

    def test_p0_uses_static_users_even_when_original_severity_is_p1(self):
        route = dict(self.route, oncall_sync=0, users='["static-user"]')
        data = gate.with_decision_note(self.data, self.p0)
        with patch.object(alert_handler, 'alert_data_api', return_value=(['severity: p1'], ['p1'], None, {})), \
             patch.object(alert_handler, 'alert_to_feishu', return_value='mid') as send:
            alert_handler._process_single_alert_config(data, route, 'TTFT', object(), mention_oncall=True)
        self.assertEqual(send.call_args.args[2], ['static-user'])

    def test_query_failure_after_p1_sends_original_grade_once(self):
        with patch.object(gate, 'get_state', return_value=self.state), \
             patch.object(gate, 'decide', side_effect=TimeoutError('VM 不可用')), \
             patch.object(gate, 'log_decision'), patch.object(gate, 'save_pending'), \
             patch.object(gate, 'mark_sent') as mark, \
             patch.object(alert_handler, '_process_single_alert_config', return_value={'message_id': 'm'}) as send:
            alert_handler._process_trend_config(self.data, self.route, 'TTFT', object(), self.key)
        self.assertIsNone(send.call_args.kwargs['mention_oncall'])
        self.assertNotIn('_trend_notification_severity', send.call_args.args[0])
        self.assertEqual(mark.call_args.kwargs['notification_severity'], 'original')

    def test_persistence_grade_only_on_success_and_pending_preserves_old_grade(self):
        with patch.object(gate, 'db_cursor') as db:
            conn, cursor = MagicMock(), MagicMock()
            db.return_value.__enter__.return_value = (conn, cursor)
            gate.mark_sent_decision(self.key, self.p0)
            sql, params = cursor.execute.call_args.args
            self.assertIn('JSON_SET', sql)
            self.assertEqual(sql.count('%s'), len(params))
            self.assertEqual(params[2], 'p0')
            gate.save_pending(self.key, 1, self.data, NOW, '重试')
            sql, params = cursor.execute.call_args.args
            self.assertIn("status!='resolved'", sql)
            self.assertIn('JSON_EXTRACT(payload', sql)
            self.assertEqual(sql.count('%s'), len(params))

    def test_low_main_samples_remain_original_and_never_downgrade(self):
        with patch.object(gate.Config, 'GRAFANA_RULES_READ_KEY', 'test'), \
             patch.object(gate.Config, 'VM_QUERY_URL', 'http://test/api/v1/query'), \
             patch.object(gate, '_load_rule', return_value=(EXPR, 30, 30, 'higher_worse')), \
             patch.object(gate, '_vm_query', return_value=[{'metric': LABELS, 'values': [[NOW, '35']]}]), patch.object(gate, '_request_count', return_value=0), \
             patch.object(gate, '_latency_decision') as evaluate:
            result = gate.decide(self.data, NOW, gate.TrendPolicy(request_metric='auto'))
        self.assertEqual(result.action, 'send')
        self.assertIsNone(result.notification_severity)
        evaluate.assert_not_called()

    def test_decide_integrates_fresh_buckets_and_response_evidence(self):
        for slow, grade in ((0, 'p1'), (20, 'p0')):
            results = [{'metric': LABELS, 'values': [[t, '31'] for t in range(820, 1001, 15)]}]
            queries = [results, matrix({90: window(slow), 300: window(slow)})]
            if grade == 'p1':
                queries.append(responses({'200': 1000}))
            with self.subTest(grade=grade), patch.object(gate.Config, 'GRAFANA_RULES_READ_KEY', 'test'), \
                 patch.object(gate.Config, 'VM_QUERY_URL', 'http://test/api/v1/query'), \
                 patch.object(gate, '_load_rule', return_value=(EXPR, 30, 30, 'higher_worse')), \
                 patch.object(gate, '_vm_query', side_effect=queries), \
                 patch.object(gate, '_request_count', return_value=1000), patch.object(gate.time, 'time', return_value=NOW):
                result = gate.decide(self.data, 820, gate.TrendPolicy(request_metric='auto'))
            self.assertEqual((result.action, result.notification_severity), ('send', grade))

    def test_actual_bucket_query_failure_immediately_uses_original_delivery(self):
        results = [{'metric': LABELS, 'values': [[t, '31'] for t in range(820, 1001, 15)]}]
        with patch.object(gate.Config, 'GRAFANA_RULES_READ_KEY', 'test'), \
             patch.object(gate.Config, 'VM_QUERY_URL', 'http://test/api/v1/query'), \
             patch.object(gate, '_load_rule', return_value=(EXPR, 30, 30, 'higher_worse')), \
             patch.object(gate, '_vm_query', side_effect=[results, []]), \
             patch.object(gate, '_request_count', return_value=1000), patch.object(gate.time, 'time', return_value=NOW), \
             patch.object(gate, 'get_state', return_value=None), patch.object(gate, 'log_decision'), \
             patch.object(gate, 'save_pending'), patch.object(gate, 'mark_sent'), \
             patch.object(gate, 'can_evaluate', return_value=True), \
             patch.object(alert_handler, '_process_single_alert_config', return_value={'message_id': 'm'}) as send:
            alert_handler._process_trend_config(self.data, self.route, 'TTFT', object(), self.key,
                                               gate.TrendPolicy(request_metric='auto'))
        self.assertIsNone(send.call_args.kwargs['mention_oncall'])
        self.assertNotIn('_trend_notification_severity', send.call_args.args[0])


if __name__ == '__main__':
    unittest.main()
