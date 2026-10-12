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
SNAPSHOT_TIME = NOW - impact.SNAPSHOT_LAG_SECONDS
LABELS = {'tenant': 't', 'model': 'm'}
EXPR = 'histogram_quantile(0.99, sum by(model, le, tenant)(rate(magik_model_ttft_ms_bucket{tenant="t",model="m"}[10m]))) / 1000'


def window(slow, total=1000, severe=0):
    return impact.HistogramWindow({10000: total-slow, 25000: total-slow,
                                  30000: total-slow, 50000: total-severe,
                                  75000: total-severe, 100000: total, math.inf: total})


def matrix(windows):
    return [{'metric': dict(LABELS, le=str(le), impact_window=str(seconds)),
             'values': [[SNAPSHOT_TIME, str(n)]]}
            for seconds, data in windows.items() for le, n in data.counts.items()]


def responses(code_counts):
    return [{'metric': dict(LABELS, code=code), 'values': [[SNAPSHOT_TIME, str(n)]]}
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

    def test_realtime_single_point_query_overrides_vm_latency_offset(self):
        with patch.object(gate.Config, 'VM_QUERY_URL', 'http://test/api/v1/query_range'), \
             patch.object(gate, '_request_json', return_value={'data': {'result': []}}) as request:
            gate._vm_query('metric', start=NOW, end=NOW, step=15)
            self.assertEqual(request.call_args.kwargs['params'],
                             {'query': 'metric', 'start': NOW, 'end': NOW, 'step': 15, 'latency_offset': '1ms'})
            self.assertTrue(request.call_args.args[0].endswith('/api/v1/query_range'))

    def test_normal_range_and_instant_queries_keep_existing_parameters(self):
        with patch.object(gate.Config, 'VM_QUERY_URL', 'http://test/api/v1/query_range'), \
             patch.object(gate, '_request_json', return_value={'data': {'result': []}}) as request:
            gate._vm_query('metric', start=NOW-600, end=NOW, step=15)
            self.assertNotIn('latency_offset', request.call_args.kwargs['params'])
            gate._vm_query('metric')
            self.assertEqual(request.call_args.kwargs['params'], {'query': 'metric'})
            self.assertTrue(request.call_args.args[0].endswith('/api/v1/query'))

    def test_quantile_boundaries_do_not_use_one_global_ratio(self):
        for q, permitted in ((.99, 10), (.95, 50), (.5, 500)):
            spec = replace(self.spec, quantile=q)
            for slow, expected in ((permitted-1, 'p1'), (permitted, 'p1'),
                                   (permitted+1, 'p1'), (min(permitted*4, 1000), 'p0')):
                with self.subTest(q=q, slow=slow):
                    windows = {90: window(slow), 300: window(slow*3, 3000)}
                    self.assertEqual(impact.impact_level(spec, windows, 30, 30, 2.5)[0], expected)

    def test_sustained_plateau_is_urgent_without_growth(self):
        windows = {90: window(100), 300: window(300, 3000)}
        decision = gate.Decision('observe', 31, '稳定超标')
        with patch.object(impact, 'read_windows', return_value=windows):
            result = gate._latency_decision(decision, EXPR, LABELS, 30, 30, gate.TrendPolicy(), NOW)
        self.assertEqual((result.action, result.notification_severity, result.urgent), ('send', 'p0', True))

    def test_wilson_lower_bound_matches_reference_and_extremes(self):
        # NIST 的单侧95%示例：26/200 的下限约9.577%。
        self.assertAlmostEqual(impact.slow_ratio_lower_bound(.13, 200), .09577, places=5)
        self.assertEqual(impact.slow_ratio_lower_bound(0, 100), 0)
        self.assertEqual(impact.slow_ratio_lower_bound(-1e-12, 100), 0)
        self.assertGreater(impact.slow_ratio_lower_bound(1, 20), .5)
        self.assertLess(impact.slow_ratio_lower_bound(.02, 100),
                        impact.slow_ratio_lower_bound(.02, 10000))

    def test_overnight_borderline_sustained_is_p1_and_keeps_monitoring(self):
        # 昨晚实际计数，阈值边界附近的几个观测不能直接触发 P0。
        for q, short_slow, short_n, long_slow, long_n in (
                (.99, 5, 294, 16, 977), (.5, 135, 209, 402, 788),
                (.95, 3, 47, 10, 187), (.5, 27, 46, 98, 173)):
            expr = EXPR.replace('0.99', str(q))
            windows = {90: window(short_slow, short_n), 300: window(long_slow, long_n)}
            with self.subTest(q=q, short_n=short_n), \
                 patch.object(impact, 'read_windows', return_value=windows), \
                 patch.object(impact, 'response_health', return_value=(1000, 0)):
                d = gate._latency_decision(gate.Decision('send', 31, '观察到期'),
                                          expr, LABELS, 30, 30, gate.TrendPolicy(), NOW)
                self.assertEqual((d.action, d.notification_severity, d.urgent), ('send', 'p1', False))
                self.assertFalse(d.short_recovered)
                self.assertIn('样本', d.reason)

    def test_borderline_severity_cannot_downgrade_unknown_hard_bounds(self):
        short, long = window(11, severe=11), window(31, 3000, severe=11)
        del short.counts[75000]
        del long.counts[75000]
        level, _ = impact.impact_level(self.spec, {90: short, 300: long}, 30, 30, 2.5)
        self.assertIsNone(level)

    def test_borderline_still_notifies_by_original_observation_deadline(self):
        policy = gate.TrendPolicy()
        windows = {90: window(5, 294), 300: window(16, 977)}
        with patch.object(impact, 'read_windows', return_value=windows), \
             patch.object(impact, 'response_health', return_value=(279, 0)):
            for elapsed, expected in ((179, 'observe'), (180, 'send')):
                with self.subTest(elapsed=elapsed):
                    baseline = gate.classify([(t, 31) for t in range(20, 201, 15)],
                                             30, 30, 100, first_seen=200-elapsed, now=200,
                                             policy=policy)
                    d = gate._latency_decision(baseline, EXPR, LABELS, 30, 30, policy, NOW)
                    self.assertEqual((d.action, d.notification_severity), (expected, 'p1'))

    def test_borderline_response_errors_and_missing_evidence_keep_safety_fallback(self):
        windows = {90: window(5, 294), 300: window(16, 977)}
        with patch.object(impact, 'read_windows', return_value=windows):
            with patch.object(impact, 'response_health', return_value=(279, 1)):
                d = gate._latency_decision(gate.Decision('send', 31, '观察到期'),
                                          EXPR, LABELS, 30, 30, gate.TrendPolicy(), NOW)
                self.assertEqual((d.action, d.notification_severity), ('send', 'p0'))
            with patch.object(impact, 'response_health', side_effect=ValueError('样本不足')):
                with self.assertRaises(ValueError):
                    gate._latency_decision(gate.Decision('send', 31, '观察到期'),
                                          EXPR, LABELS, 30, 30, gate.TrendPolicy(), NOW)

    def test_overnight_major_tpot_deterioration_still_p0_without_waiting(self):
        windows = {90: window(57, 57), 300: window(154, 163)}
        with patch.object(impact, 'read_windows', return_value=windows), \
             patch.object(impact, 'response_health', side_effect=AssertionError('P0不需响应降级证据')):
            d = gate._latency_decision(gate.Decision('observe', 43.75, '尚在观察'),
                                      EXPR.replace('0.99', '0.5').replace('ttft', 'tpot'),
                                      LABELS, 30, 30, gate.TrendPolicy(), NOW)
            self.assertEqual((d.action, d.notification_severity, d.urgent), ('send', 'p0', True))

    def test_overnight_short_bucket_ambiguity_with_healthy_long_window_is_p1(self):
        short = impact.HistogramWindow({30000: 218, 50000: 220, 100000: 220, math.inf: 221})
        long = impact.HistogramWindow({30000: 736, 50000: 738, 100000: 738, math.inf: 739})
        level, evidence = impact.impact_level(self.spec, {90: short, 300: long}, 35, 30, 2.5)
        self.assertEqual(level, 'p1')
        self.assertTrue(evidence['transient'])
        self.assertFalse(evidence['recovered'])
        self.assertLess(evidence['short_slow_low'], .01)
        self.assertGreater(evidence['short_slow_high'], .01)

    def test_trigger_healthy_recovery_unconfirmed_is_p1_but_not_resolved(self):
        short, long = window(0), window(0, 3000)
        short.counts[25000], long.counts[25000] = 980, 2940
        with patch.object(impact, 'read_windows', return_value={90: short, 300: long}), \
             patch.object(impact, 'response_health', return_value=(1000, 0)):
            for action in ('send', 'cancel', 'observe'):
                with self.subTest(action=action):
                    d = gate._latency_decision(gate.Decision(action, 28, '处于恢复区间'),
                                              EXPR, LABELS, 30, 25, gate.TrendPolicy(), NOW)
                    self.assertEqual((d.action, d.notification_severity),
                                     ('observe' if action == 'observe' else 'send', 'p1'))
                    self.assertFalse(d.short_recovered)

    def test_short_severe_window_does_not_wait_for_long_window(self):
        windows = {90: window(20, severe=11), 300: window(20, 10000, severe=11)}
        level, evidence = impact.impact_level(self.spec, windows, 30, 30, 2.5)
        self.assertEqual(level, 'p0')
        self.assertTrue(evidence['severe'])
        self.assertFalse(evidence['sustained'])
        # 11/1000 的严重尾部无需通过样本量缓冲，任何阶段立即升级。
        with patch.object(impact, 'read_windows', return_value=windows), \
             patch.object(impact, 'response_health', side_effect=AssertionError('严重超标不等待降级证据')):
            result = gate._latency_decision(gate.Decision('observe', 31, '刚开始观察'),
                                           EXPR, LABELS, 30, 30, gate.TrendPolicy(), NOW)
            self.assertEqual((result.action, result.notification_severity, result.urgent), ('send', 'p0', True))

    def test_rare_spikes_and_historical_residue_allow_p1(self):
        for slow in (0, 5, 10):
            windows = {90: window(slow), 300: window(150, 3000)}
            self.assertEqual(impact.impact_level(self.spec, windows, 30, 30, 2.5)[0], 'p1')

    def test_bucket_bounds_do_not_guess_unknown_distribution(self):
        self.assertEqual(window(15).slow_bounds(35000), (0, .015))
        level, evidence = impact.impact_level(self.spec, {90: window(15), 300: window(15)}, 35, 30, 2.5)
        self.assertEqual(level, 'p1')
        self.assertTrue(evidence['uncertain_nonsevere'])
        self.assertFalse(evidence['recovered'])
        self.assertEqual((evidence['short_slow_low'], evidence['short_slow_high']), (0, .015))

    def test_recovery_threshold_is_stricter_than_trigger(self):
        # All but one observation is <=30s, but 20 observations still exceed 25s.
        w = window(0)
        w.counts[25000] = 980
        level, evidence = impact.impact_level(self.spec, {90: w, 300: w}, 30, 25, 2.5)
        self.assertEqual(level, 'p1')
        self.assertTrue(evidence['below_trigger'])
        self.assertFalse(evidence['recovered'])

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
        self.assertEqual(query.call_args.kwargs, {'start': SNAPSHOT_TIME, 'end': SNAPSHOT_TIME, 'step': 15})

    def test_histogram_and_response_share_settled_snapshot_with_wall_clock_freshness(self):
        histogram_query = MagicMock(return_value=matrix({90: window(0), 300: window(0)}))
        response_query = MagicMock(return_value=responses({'200': 1000}))
        impact.read_windows(self.spec, LABELS, NOW, 100, histogram_query)
        impact.response_health(self.spec, LABELS, NOW, 100, response_query)
        self.assertEqual(impact.SNAPSHOT_LAG_SECONDS, 30)
        for query in (histogram_query, response_query):
            self.assertEqual(query.call_args.kwargs,
                             {'start': NOW-30, 'end': NOW-30, 'step': 15})
            # 新鲜度仍以决策时刻计算，不能因快照后移放宽到 75 秒。
            self.assertIn(f'>= {NOW-45}', query.call_args.args[0])

    def test_partial_latest_buckets_do_not_enter_settled_snapshot(self):
        valid = matrix({90: window(0), 300: window(0)})
        partial = json.loads(json.dumps(valid))
        partial[0]['values'][0][1] = '2000'
        for row in partial:
            row['values'][0][0] = NOW
        query = MagicMock(side_effect=lambda expr, **kw:
                          valid if kw['start'] == NOW-30 else partial)
        windows = impact.read_windows(self.spec, LABELS, NOW, 100, query)
        self.assertEqual(impact.impact_level(self.spec, windows, 30, 30, 2.5)[0], 'p1')

    def test_settled_snapshot_inversion_remains_failure_with_bucket_details(self):
        rows = matrix({90: window(0), 300: window(0)})
        rows[0]['values'][0][1] = '1001'
        with self.assertRaisesRegex(ValueError, '90秒累计桶计数不一致: le=10000 count=1001, le=25000 count=1000'):
            impact.read_windows(self.spec, LABELS, NOW, 100, MagicMock(return_value=rows))

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

    def test_sparse_response_errors_do_not_force_latency_p0(self):
        windows = {90: window(0), 300: window(0)}
        baseline = gate.Decision('send', 45.4333, '原窗口超标', urgent=True)
        for code in ('500', '408'):
            for success, errors, grade in ((3146, 3, 'p1'), (999, 1, 'p1'),
                                           (998, 2, 'p0'), (0, 1000, None)):
                with self.subTest(code=code, success=success, errors=errors), \
                     patch.object(impact, 'read_windows', return_value=windows), \
                     patch.object(gate, '_vm_query', return_value=responses({'200': success, code: errors})):
                    if grade is None:
                        with self.assertRaises(ValueError):
                            gate._latency_decision(baseline, EXPR, LABELS, 60, 60, gate.TrendPolicy(), NOW)
                    else:
                        d = gate._latency_decision(baseline, EXPR, LABELS, 60, 60, gate.TrendPolicy(), NOW)
                        self.assertEqual(d.notification_severity, grade)

    def test_error_ratio_limit_can_restore_strict_behavior_and_reject_invalid_limits(self):
        windows = {90: window(0), 300: window(0)}
        with patch.object(impact, 'read_windows', return_value=windows), \
             patch.object(impact, 'response_health', return_value=(3149, 3)):
            with patch.object(impact, 'MAX_ERROR_RATIO', 0):
                d = gate._latency_decision(gate.Decision('send', 40, '越线'),
                                          EXPR, LABELS, 30, 30, gate.TrendPolicy(), NOW)
                self.assertEqual(d.notification_severity, 'p0')
            for limit in (-.001, 1.1, float('nan'), float('inf')):
                with self.subTest(limit=limit), patch.object(impact, 'MAX_ERROR_RATIO', limit):
                    with self.assertRaises(ValueError):
                        gate._latency_decision(gate.Decision('send', 40, '越线'),
                                              EXPR, LABELS, 30, 30, gate.TrendPolicy(), NOW)

    def test_valid_bucket_ambiguity_needs_healthy_responses_to_send_p1(self):
        # 35秒阈值夹在30/50秒桶之间，无法确认越线；87.5秒严重尾部上界为0。
        windows = {90: window(15), 300: window(45, 3000)}
        with patch.object(impact, 'read_windows', return_value=windows):
            for errors, expected in ((0, 'p1'), (1, 'p1'), (2, 'p0')):
                with self.subTest(errors=errors), \
                     patch.object(impact, 'response_health', return_value=(1000, errors)):
                    d = gate._latency_decision(gate.Decision('send', 42, '观察到期'),
                                              EXPR, LABELS, 35, 30, gate.TrendPolicy(), NOW)
                    self.assertEqual((d.action, d.notification_severity), ('send', expected))
                    self.assertFalse(d.short_recovered)
            with patch.object(impact, 'response_health', side_effect=ValueError('响应不足')):
                with self.assertRaises(ValueError):
                    gate._latency_decision(gate.Decision('send', 42, '观察到期'),
                                          EXPR, LABELS, 35, 30, gate.TrendPolicy(), NOW)

    def test_unknown_severe_tail_with_value_at_hard_limit_keeps_p0(self):
        short, long = window(15, severe=15), window(45, 3000, severe=15)
        del short.counts[75000]
        del long.counts[75000]
        with patch.object(impact, 'read_windows', return_value={90: short, 300: long}), \
             patch.object(impact, 'response_health', side_effect=AssertionError('严重尾部不确定不能降级')):
            d = gate._latency_decision(gate.Decision('send', 75, '达到硬上限'),
                                      EXPR, LABELS, 30, 30, gate.TrendPolicy(), NOW)
            self.assertEqual((d.action, d.notification_severity, d.urgent), ('send', 'p0', True))

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

    def test_uncertain_trigger_at_deadline_sends_p1_without_claiming_recovery(self):
        windows = {90: window(15), 300: window(45, 3000)}
        with patch.object(impact, 'read_windows', return_value=windows), \
             patch.object(impact, 'response_health', return_value=(1000, 0)):
            for action in ('send', 'cancel'):
                result = gate._latency_decision(gate.Decision(action, 36, '观察到期', urgent=False),
                                                EXPR, LABELS, 35, 30, gate.TrendPolicy(), NOW)
                self.assertEqual((result.action, result.notification_severity), ('send', 'p1'))
                self.assertFalse(result.short_recovered)

    def test_healthy_observation_respects_180_second_deadline(self):
        with patch.object(impact, 'read_windows', return_value={90: window(0), 300: window(0)}), \
             patch.object(impact, 'response_health', return_value=(1000, 0)):
            result = gate._latency_decision(gate.Decision('observe', 31, '继续观察'), EXPR, LABELS,
                                            30, 30, gate.TrendPolicy(), NOW)
            self.assertEqual(result.action, 'observe')

    def test_aacc_alert_short_spike_is_p1_and_not_recovered(self):
        # MAID aacc05edaef2a307e7e5 的实际计数：短窗口8/391，长窗口9/1226。
        windows = {90: window(8, 391), 300: window(9, 1226)}
        baseline = gate.Decision('send', 38.3697, '达到最长观察时间且仍越线', urgent=False)
        with patch.object(impact, 'read_windows', return_value=windows), \
             patch.object(impact, 'response_health', return_value=(393, 0)):
            result = gate._latency_decision(baseline, EXPR, LABELS, 30, 30, gate.TrendPolicy(), NOW)
        self.assertEqual((result.action, result.notification_severity, result.urgent), ('send', 'p1', False))
        self.assertFalse(result.short_recovered)
        self.assertFalse(gate.oncall_mention_policy(result))
        self.assertIn('继续监测', result.reason)

    def test_short_only_spike_respects_observation_and_cannot_cancel(self):
        windows = {90: window(20), 300: window(20, 3000)}
        with patch.object(impact, 'read_windows', return_value=windows), \
             patch.object(impact, 'response_health', return_value=(1000, 0)):
            for original, expected in (('observe', 'observe'), ('send', 'send'), ('cancel', 'send')):
                with self.subTest(original=original):
                    result = gate._latency_decision(gate.Decision(original, 29, '原窗口判断'),
                                                    EXPR, LABELS, 30, 30, gate.TrendPolicy(), NOW)
                    self.assertEqual((result.action, result.notification_severity), (expected, 'p1'))
                    self.assertFalse(result.short_recovered)
                    self.assertNotIn('恢复', result.reason)

    def test_short_only_spike_uses_quantile_boundary_for_ttft_and_tpot(self):
        for q, slow in ((.99, 20), (.95, 60), (.5, 600)):
            for metric in ('ttft', 'tpot'):
                expr = EXPR.replace('0.99', str(q)).replace('ttft', metric)
                spec = impact.parse_histogram(expr, LABELS)
                level, evidence = impact.impact_level(spec, {90: window(slow), 300: window(slow, 3000)},
                                                       30, 30, 2.5)
                with self.subTest(q=q, metric=metric):
                    self.assertEqual(level, 'p1')
                    self.assertTrue(evidence['transient'])
                    self.assertFalse(evidence['recovered'])

    def test_short_only_spike_does_not_guess_long_or_severe_bounds(self):
        long = window(40, 3000)
        # 35s 不在桶上；长窗口真实超标比例可能位于0～1.33%，不能判为正常。
        short = window(20, severe=20)
        self.assertIsNone(impact.impact_level(self.spec, {90: short, 300: long}, 35, 30, 2.5)[0])
        short = window(20, severe=20)
        del short.counts[75000]
        # 75s 不在桶上，严重慢观测占比可能0～2%，不能判为非紧急。
        self.assertIsNone(impact.impact_level(self.spec, {90: short, 300: window(20, 3000)},
                                            30, 30, 2.5)[0])

    def test_short_only_spike_long_boundary_and_p0_priority(self):
        for long_slow, severe, expected in ((30, 0, 'p1'), (31, 0, 'p1'),
                                           (60, 0, 'p1'), (20, 11, 'p0')):
            with self.subTest(long_slow=long_slow, severe=severe):
                level, _ = impact.impact_level(self.spec,
                    {90: window(20, severe=severe), 300: window(long_slow, 3000, severe=severe)},
                    30, 30, 2.5)
                self.assertEqual(level, expected)

    def test_reported_oct12_spike_is_observed_then_p1_without_mentions(self):
        # MAID e12c... 的真实发送点：8/394、24/1195、零5xx/408。
        windows = {90: window(8, 394), 300: window(24, 1195)}
        with patch.object(impact, 'read_windows', return_value=windows), \
             patch.object(impact, 'response_health', return_value=(386, 0)):
            for action in ('observe', 'send'):
                d = gate._latency_decision(gate.Decision(action, 36.2667, '原窗口判断', urgent=True),
                                          EXPR, LABELS, 30, 30, gate.TrendPolicy(), NOW)
                self.assertEqual((d.action, d.notification_severity, d.urgent), (action, 'p1', False))
                self.assertFalse(d.short_recovered)
                self.assertFalse(gate.oncall_mention_policy(d))

    def test_continuous_two_percent_p99_is_not_emergency(self):
        windows = {90: window(200, 10000), 300: window(600, 30000)}
        level, e = impact.impact_level(self.spec, windows, 30, 30, 2.5)
        self.assertGreater(e['short_slow_confidence_low'], .01)
        self.assertEqual(e['urgent_ratio'], .03)
        self.assertEqual(level, 'p1')

    def test_nested_burst_cannot_supply_earlier_window_evidence(self):
        # 最近90秒突刺会把300秒均值一并拉高，但此前210秒完全正常。
        windows = {90: window(200), 300: window(200, 3000)}
        level, e = impact.impact_level(self.spec, windows, 30, 30, 2.5)
        self.assertGreater(e['long_slow_confidence_low'], .03)
        self.assertEqual(e['prior_slow_low'], 0)
        self.assertFalse(e['sustained'])
        self.assertEqual(level, 'p1')

    def test_prior_weak_or_insufficient_samples_cannot_confirm_continuity(self):
        for short, long in ((window(100), window(101, 1050)),
                            (window(100), window(120, 2000))):
            with self.subTest(long_n=long.total):
                level, e = impact.impact_level(self.spec, {90: short, 300: long}, 30, 30, 2.5)
                self.assertEqual(level, 'p1')
                self.assertFalse(e['sustained'])

    def test_invalid_difference_is_not_used_as_persistence_evidence(self):
        # 原窗口各自有效，但差分桶不单调；不从差分推断持续扩大。
        short, long = window(100), window(300, 3000)
        long.counts[75000] = 2890
        level, e = impact.impact_level(self.spec, {90: short, 300: long}, 30, 30, 2.5)
        self.assertFalse(e['prior_valid'])
        self.assertFalse(e['sustained'])
        self.assertEqual(level, 'p1')

    def test_p0_boundaries_remain_reachable_for_all_quantiles(self):
        for q, boundary, slow in ((.99, .03, 100), (.95, .15, 300), (.5, .6, 800)):
            with self.subTest(q=q):
                level, e = impact.impact_level(replace(self.spec, quantile=q),
                        {90: window(slow), 300: window(slow*3, 3000)}, 30, 30, 2.5)
                self.assertAlmostEqual(e['urgent_ratio'], boundary)
                self.assertEqual(level, 'p0')

    def test_coarse_severe_bucket_with_mild_value_needs_response_evidence(self):
        short, long = window(15, severe=15), window(45, 3000, severe=15)
        del short.counts[75000]
        del long.counts[75000]
        with patch.object(impact, 'read_windows', return_value={90: short, 300: long}):
            for errors, expected in ((0, 'p1'), (2, 'p0')):
                with self.subTest(errors=errors), \
                     patch.object(impact, 'response_health', return_value=(1000, errors)):
                    d = gate._latency_decision(gate.Decision('send', 42, '观察到期'),
                                              EXPR, LABELS, 30, 30, gate.TrendPolicy(), NOW)
                    self.assertEqual(d.notification_severity, expected)
                    self.assertFalse(d.short_recovered)
            with patch.object(impact, 'response_health', side_effect=ValueError('响应不足')):
                with self.assertRaises(ValueError):
                    gate._latency_decision(gate.Decision('send', 42, '观察到期'),
                                          EXPR, LABELS, 30, 30, gate.TrendPolicy(), NOW)

    def test_unknown_tail_does_not_downgrade_missing_value_or_hard_limit(self):
        short, long = window(15, severe=15), window(45, 3000, severe=15)
        del short.counts[75000]
        del long.counts[75000]
        for value, policy in ((None, gate.TrendPolicy()), (float('nan'), gate.TrendPolicy()),
                              (75, gate.TrendPolicy())):
            with self.subTest(value=value), \
                 patch.object(impact, 'read_windows', return_value={90: short, 300: long}), \
                 patch.object(impact, 'response_health', side_effect=AssertionError('不能降级')):
                d = gate._latency_decision(gate.Decision('send', value, '保守兜底'),
                                          EXPR, LABELS, 30, 30, policy, NOW)
                self.assertEqual(d.notification_severity, 'p0')

    def test_short_only_spike_response_errors_keep_p0_and_missing_evidence_fails(self):
        windows = {90: window(20), 300: window(20, 3000)}
        with patch.object(impact, 'read_windows', return_value=windows):
            for code in ('500', '408'):
                with self.subTest(code=code), patch.object(gate, '_vm_query',
                        return_value=responses({'200': 393, code: 1})):
                    result = gate._latency_decision(gate.Decision('send', 38, '观察到期'),
                                                    EXPR, LABELS, 30, 30, gate.TrendPolicy(), NOW)
                    self.assertEqual(result.notification_severity, 'p0')
            for rows in ([], responses({'200': 99}), responses({'unknown': 393})):
                with self.subTest(rows=rows), patch.object(gate, '_vm_query', return_value=rows), \
                     self.assertRaises(ValueError):
                    gate._latency_decision(gate.Decision('send', 38, '观察到期'),
                                          EXPR, LABELS, 30, 30, gate.TrendPolicy(), NOW)


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

    def test_sent_p1_short_spike_continues_monitoring_without_grade_upgrade(self):
        with patch.object(impact, 'read_windows', return_value={90: window(8, 391), 300: window(9, 1226)}), \
             patch.object(impact, 'response_health', return_value=(393, 0)):
            decision = gate._latency_decision(gate.Decision('send', 38.3697, '观察到期'),
                                              EXPR, LABELS, 30, 30, gate.TrendPolicy(), NOW)
        with patch.object(trend_worker, 'db_cursor') as db, patch.object(gate, 'decide', return_value=decision), \
             patch.object(gate, 'log_decision'), patch.object(gate, 'schedule_next') as schedule, \
             patch.object(gate, 'mark_sent_decision') as mark, \
             patch.object(alert_handler, '_process_single_alert_config') as send:
            db.return_value.__enter__.return_value = (MagicMock(), MagicMock(fetchone=lambda: self.route))
            trend_worker._process_pending_state(self.key, gate.TrendPolicy(), self.state, self.data)
            send.assert_not_called()
            mark.assert_not_called()
            schedule.assert_called_once()

    def test_transient_p1_later_sustained_or_query_failure_still_upgrades(self):
        baseline = gate.Decision('send', 38.3697, '观察到期')
        with patch.object(impact, 'read_windows') as read, \
             patch.object(impact, 'response_health', return_value=(393, 0)):
            read.return_value = {90: window(8, 391), 300: window(9, 1226)}
            mild = gate._latency_decision(baseline, EXPR, LABELS, 30, 30, gate.TrendPolicy(), NOW)
            self.assertFalse(gate.grade_upgrade(mild, self.state))
            read.return_value = {90: window(40, 391), 300: window(120, 1226)}
            severe = gate._latency_decision(baseline, EXPR, LABELS, 30, 30, gate.TrendPolicy(), NOW)
            self.assertEqual((severe.action, severe.notification_severity, severe.urgent), ('send', 'p0', True))
            self.assertTrue(gate.grade_upgrade(severe, self.state))
            self.assertTrue(gate.oncall_mention_policy(severe))
            self.assertTrue(gate.grade_upgrade(gate.Decision('send', None, '查询失败'), self.state))

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

    def test_same_grade_repeat_respects_cooldown_in_http_and_worker(self):
        for grade in ('p0', 'p1'):
            for seconds, expected in ((0, False), (299.999, False), (300, True), (301, True)):
                state = dict(self.state, seconds_since_sent=seconds,
                             payload=json.dumps(dict(self.data, _trend_last_notification_severity=grade)))
                decision = replace(self.p0, value=100, notification_severity=grade, urgent=grade == 'p0')
                with self.subTest(grade=grade, seconds=seconds), \
                     patch.object(gate, 'get_state', return_value=state), \
                     patch.object(gate, 'decide', return_value=decision), patch.object(gate, 'log_decision'), \
                     patch.object(gate, 'save_pending') as pending, patch.object(gate, 'mark_sent_decision') as mark, \
                     patch.object(gate, 'schedule_next') as schedule, patch.object(trend_worker, 'db_cursor') as db, \
                     patch.object(alert_handler, '_process_single_alert_config', return_value={'message_id': 'm'}) as send:
                    db.return_value.__enter__.return_value = (MagicMock(), MagicMock(fetchone=lambda: self.route))
                    alert_handler._process_trend_config(self.data, self.route, 'TTFT', object(), self.key)
                    trend_worker._process_pending_state(self.key, object(), state, self.data)
                    self.assertEqual(send.call_count, 2 if expected else 0)
                    self.assertEqual(mark.call_count, 2 if expected else 0)
                    if not expected:
                        pending.assert_not_called()
                        schedule.assert_called_once()

    def test_persistent_cooldown_survives_restart_without_elapsed_field(self):
        now = datetime(2026, 10, 11, 1)
        state = dict(self.state, last_sent_at=now-timedelta(seconds=299),
                     payload=dict(self.data, _trend_last_notification_severity='p0'))
        with patch.object(gate, 'datetime') as clock:
            clock.now.return_value = now
            self.assertFalse(gate.should_repeat_notification(replace(self.p0, value=100), state))
            clock.now.return_value = now+timedelta(seconds=1)
            self.assertTrue(gate.should_repeat_notification(replace(self.p0, value=100), state))

    def test_failed_repeat_after_cooldown_is_retried_without_recording_success(self):
        state = dict(self.state, seconds_since_sent=300,
                     payload=dict(self.data, _trend_last_notification_severity='p0'))
        with patch.object(trend_worker, 'db_cursor') as db, \
             patch.object(gate, 'decide', return_value=replace(self.p0, value=100)), \
             patch.object(gate, 'log_decision'), patch.object(gate, 'mark_sent_decision') as mark, \
             patch.object(gate, 'schedule_next'), \
             patch.object(alert_handler, '_process_single_alert_config', side_effect=[None, {'message_id': 'm'}]) as send:
            db.return_value.__enter__.return_value = (MagicMock(), MagicMock(fetchone=lambda: self.route))
            trend_worker._process_pending_state(self.key, object(), state, self.data)
            mark.assert_not_called()
            trend_worker._process_pending_state(self.key, object(), state, self.data)
            mark.assert_called_once()
            self.assertEqual(send.call_count, 2)

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
        for slow, grade in ((0, 'p1'), (100, 'p0')):
            results = [{'metric': LABELS, 'values': [[t, '31'] for t in range(820, 1001, 15)]}]
            queries = [results, matrix({90: window(slow), 300: window(slow*3, 3000)})]
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
