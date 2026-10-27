"""电话告警夜间降级及 webhook label 路由回归；禁止外部访问。"""
import copy
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from feishu_utils import alert_handler as handler
from utils.bounded_cache import BoundedTTLCache


class PhonePolicyTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for guard in (
            patch('requests.sessions.Session.request', side_effect=AssertionError('禁止外部 HTTP')),
            patch('db.pool._build_pool', side_effect=AssertionError('禁止真实数据库')),
            patch.object(handler, '_night_phone_counts', BoundedTTLCache(maxsize=100, ttl=float('inf'))),
            patch.object(handler, '_alert_dedup_cache', BoundedTTLCache(maxsize=100, ttl=300)),
            patch.object(handler, '_alert_label_dedup_cache', BoundedTTLCache(maxsize=100, ttl=300)),
            patch.object(handler.trend_gate, 'is_enabled_for', return_value=False),
        ):
            self.stack.enter_context(guard)
        self.clock = self.stack.enter_context(patch.object(handler, 'datetime'))
        self.at(0)
        self.data = {'status': 'firing', 'alerts': [{
            'status': 'firing', 'fingerprint': 'fp',
            'generatorURL': 'https://g/alerting/grafana/rule/view',
            'labels': {'alertname': 'DatabaseDown', 'severity': 'phone'},
        }]}
        self.route = {'group_id': 'g', 'project': 'test', 'template_type': 'biz'}

    def at(self, hour, day=21):
        self.clock.now.return_value = datetime(2026, 10, day, hour, tzinfo=timezone(timedelta(hours=8)))

    def test_first_three_then_fourth_and_later_call_across_days(self):
        for i in range(1, 7):
            reason = handler._night_phone_degradation_reason(self.data, 'g')
            if i <= 3:
                self.assertIn(f'第 {i} 次', reason)
            else:
                self.assertIsNone(reason)
        self.at(0, day=22)
        self.assertIsNone(handler._night_phone_degradation_reason(self.data, 'g'))
        self.clock.now.assert_called_with(timezone(timedelta(hours=8)))

    def test_daytime_does_not_count_and_eight_is_excluded(self):
        for hour in (8, 9, 12, 23):
            self.at(hour)
            self.assertIsNone(handler._night_phone_degradation_reason(self.data, 'g'))
        self.assertEqual(handler._night_phone_counts.get(('rule', 'g'))['count'], 0)
        self.clock.now.return_value = datetime(2026, 10, 21, 7, 59, 59, tzinfo=timezone(timedelta(hours=8)))
        self.assertIn('第 1 次', handler._night_phone_degradation_reason(self.data, 'g'))

    def test_rule_and_group_are_independent(self):
        for _ in range(4):
            handler._night_phone_degradation_reason(self.data, 'g')
        self.assertIn('第 1 次', handler._night_phone_degradation_reason(self.data, 'other'))
        self.data['alerts'][0]['generatorURL'] = 'https://g/alerting/grafana/other/view'
        self.assertIn('第 1 次', handler._night_phone_degradation_reason(self.data, 'g'))

    def test_aggregated_instances_share_the_rule_counter(self):
        handler._night_phone_degradation_reason(self.data, 'g')
        self.data['alerts'].append(copy.deepcopy(self.data['alerts'][0]))
        self.assertIn('第 2 次', handler._night_phone_degradation_reason(self.data, 'g'))

    def test_concurrent_requests_only_downgrade_three(self):
        with ThreadPoolExecutor(max_workers=8) as executor:
            reasons = list(executor.map(lambda _: handler._night_phone_degradation_reason(self.data, 'g'), range(20)))
        self.assertEqual(sum(reason is not None for reason in reasons), 3)

    def test_delivery_downgrades_both_templates_then_calls(self):
        for template in ('biz', 'ops'):
            with self.subTest(template=template), ExitStack() as stack:
                handler._night_phone_counts.clear()
                client = Mock()
                client.send.return_value = 'message'
                stack.enter_context(patch.object(handler, 'alert_data_api', return_value=(['severity: phone'], ['phone'], 'maid', {})))
                stack.enter_context(patch.object(handler, '_get_oncall_mentioned_users', return_value=[]))
                stack.enter_context(patch.object(handler, 'update_message_id'))
                stack.enter_context(patch.object(handler, 'save_card_content'))
                stack.enter_context(patch.object(handler.Config, 'FLASHCAT_APP_KEY', 'app'))
                probe = stack.enter_context(patch.object(handler, 'probe_flashcat_api', return_value=(True, '')))
                incident = stack.enter_context(patch.object(handler, '_create_phone_incident', return_value='incident'))
                biz = stack.enter_context(patch.object(handler, 'build_biz_firing_card', return_value='{}'))
                ops = stack.enter_context(patch.object(handler, 'alert_to_feishu', return_value='message'))
                for i in range(1, 5):
                    result = handler._process_single_alert_config(self.data, dict(self.route, template_type=template), 'DatabaseDown', client)
                    self.assertTrue(result['success'])
                    call = biz.call_args if template == 'biz' else ops.call_args
                    severity = call.args[1] if template == 'biz' else call.kwargs['severity']
                    self.assertEqual(severity, 'p0' if i <= 3 else 'phone')
                    self.assertEqual(bool(call.kwargs['severity_note']), i <= 3)
                    self.assertEqual(incident.call_count, int(i == 4))
                probe.assert_called_once()
                self.assertEqual(self.data['alerts'][0]['labels']['severity'], 'phone')

    def test_resolved_never_counts_or_calls(self):
        for _ in range(4):
            handler._night_phone_degradation_reason(self.data, 'g')
        self.data['status'] = self.data['alerts'][0]['status'] = 'resolved'
        with patch.object(handler, 'alert_data_api', return_value=([], ['phone'], None, {})), \
             patch.object(handler, 'get_message_id_by_fingerprint', return_value=''), \
             patch.object(handler, 'get_all_fingerprints_by_fingerprint', return_value=[]), \
             patch.object(handler, 'get_alerttime_by_fingerprint', return_value=None), \
             patch.object(handler, '_night_phone_degradation_reason') as night, \
             patch.object(handler, '_create_phone_incident') as incident:
            handler._process_single_alert_config(self.data, self.route, 'DatabaseDown', Mock())
        night.assert_not_called()
        incident.assert_not_called()
        self.data['status'] = self.data['alerts'][0]['status'] = 'firing'
        self.assertIn('第 1 次', handler._night_phone_degradation_reason(self.data, 'g'))

    def test_partial_recovery_keeps_count_until_all_instances_recover(self):
        other = copy.deepcopy(self.data['alerts'][0])
        other['fingerprint'] = 'other-fp'
        self.data['alerts'].append(other)
        for _ in range(4):
            handler._night_phone_degradation_reason(self.data, 'g')
        resolved = copy.deepcopy(self.data)
        resolved['status'] = 'resolved'
        resolved['alerts'] = [resolved['alerts'][0]]
        resolved['alerts'][0]['status'] = 'resolved'
        handler._reset_night_phone_for_resolved(resolved, 'g')
        self.data['alerts'] = [other]
        self.assertIsNone(handler._night_phone_degradation_reason(self.data, 'g'))
        resolved['alerts'][0]['fingerprint'] = 'other-fp'
        handler._reset_night_phone_for_resolved(resolved, 'g')
        self.assertIn('第 1 次', handler._night_phone_degradation_reason(self.data, 'g'))

    def test_daytime_recovery_resets_the_next_night(self):
        for _ in range(4):
            handler._night_phone_degradation_reason(self.data, 'g')
        self.at(12)
        resolved = copy.deepcopy(self.data)
        resolved['status'] = resolved['alerts'][0]['status'] = 'resolved'
        handler._reset_night_phone_for_resolved(resolved, 'g')
        self.at(0, day=22)
        self.assertIn('第 1 次', handler._night_phone_degradation_reason(self.data, 'g'))

    def test_duplicate_webhook_does_not_count(self):
        def send_alert(data, route, *args):
            handler._night_phone_degradation_reason(data, route['group_id'])
            return {'success': True}

        with patch.object(handler, '_find_alert_configs', return_value=[self.route]), \
             patch.object(handler, '_process_single_alert_config', side_effect=send_alert) as send:
            for _ in range(2):
                _, status = handler.process_alert_request(copy.deepcopy(self.data), Mock())
                self.assertEqual(status, 200)
        send.assert_called_once()
        self.assertIn('第 2 次', handler._night_phone_degradation_reason(self.data, 'g'))

    def test_label_overrides_default_channel_and_all_fallbacks(self):
        self.data['alerts'][0]['labels']['flashcat_phone_integration_key'] = ' custom-key '
        with patch.object(handler.Config, 'FLASHCAT_APP_KEY', 'app'), \
             patch.object(handler.Config, 'FLASHCAT_CHANNEL_ID', '123'), \
             patch.object(handler.Config, 'FLASHCAT_PHONE_INTEGRATION_KEY', 'default-key'), \
             patch.object(handler, 'create_phone_incident') as direct, \
             patch.object(handler, 'create_phone_incident_from_event', side_effect=['incident', None]) as event, \
             patch.object(handler, 'send_phone_alert', return_value=True) as legacy, \
             patch.object(handler, 'update_incident_id'), \
             patch.object(handler.threading, 'Thread') as thread:
            thread.return_value.start.side_effect = lambda: thread.call_args.kwargs['target']()
            self.assertEqual(handler._create_phone_incident(self.data, 'maid'), 'incident')
            handler._create_phone_incident(self.data, 'maid')
            direct.assert_not_called()
            self.assertEqual(event.call_args.args[2], 'custom-key')
            self.assertEqual(legacy.call_args.args[1], 'custom-key')

    def test_missing_or_empty_label_preserves_defaults(self):
        for value in (None, '', '   '):
            if value is not None:
                self.data['alerts'][0]['labels']['flashcat_phone_integration_key'] = value
            with patch.object(handler.Config, 'FLASHCAT_APP_KEY', 'app'), \
                 patch.object(handler.Config, 'FLASHCAT_CHANNEL_ID', '123'), \
                 patch.object(handler.Config, 'FLASHCAT_PHONE_INTEGRATION_KEY', 'default-key'), \
                 patch.object(handler, 'create_phone_incident', return_value=None) as direct, \
                 patch.object(handler, 'create_phone_incident_from_event', return_value='incident') as event, \
                 patch.object(handler, 'update_incident_id'):
                handler._create_phone_incident(self.data, 'maid')
                self.assertEqual(direct.call_args.args[2], '123')
                self.assertEqual(event.call_args.args[2], 'default-key')

    def test_different_webhook_labels_are_not_aggregated_or_deduped_together(self):
        other = copy.deepcopy(self.data)
        other['alerts'][0]['labels']['flashcat_phone_integration_key'] = 'other-key'
        subs = [self.data, other]
        for sub in subs:
            sub['commonLabels'] = sub['alerts'][0]['labels']
        self.assertEqual(len(handler._group_and_aggregate_by_alertname(subs)), 2)
        self.assertNotEqual(handler._make_label_dedup_key(self.data, 'g'), handler._make_label_dedup_key(other, 'g'))

    def test_channel_label_overrides_global_and_integration_label(self):
        for value in (' 5502714125895 ', 5502714125895):
            with self.subTest(value=value), ExitStack() as stack:
                self.data['alerts'][0]['labels']['FLASHCAT_CHANNEL_ID'] = value
                self.data['alerts'][0]['labels']['flashcat_phone_integration_key'] = 'custom-key'
                stack.enter_context(patch.object(handler.Config, 'FLASHCAT_APP_KEY', 'app'))
                stack.enter_context(patch.object(handler.Config, 'FLASHCAT_CHANNEL_ID', 'global-channel'))
                direct = stack.enter_context(patch.object(handler, 'create_phone_incident', return_value='incident'))
                event = stack.enter_context(patch.object(handler, 'create_phone_incident_from_event'))
                update = stack.enter_context(patch.object(handler, 'update_incident_id'))
                self.assertEqual(handler._create_phone_incident(self.data, 'maid'), 'incident')
                direct.assert_called_once_with(self.data, 'app', '5502714125895', maid='maid')
                event.assert_not_called()
                update.assert_called_once_with('maid', 'incident')

    def test_channel_label_absent_or_blank_uses_global(self):
        for value in (None, '', '   '):
            if value is not None:
                self.data['alerts'][0]['labels']['FLASHCAT_CHANNEL_ID'] = value
            with patch.object(handler.Config, 'FLASHCAT_APP_KEY', 'app'), \
                 patch.object(handler.Config, 'FLASHCAT_CHANNEL_ID', 'global-channel'), \
                 patch.object(handler, 'create_phone_incident', return_value='incident') as direct, \
                 patch.object(handler, 'update_incident_id'):
                handler._create_phone_incident(self.data, 'maid')
                self.assertEqual(direct.call_args.args[2], 'global-channel')

    def test_failed_label_channel_preserves_event_fallback(self):
        self.data['alerts'][0]['labels']['FLASHCAT_CHANNEL_ID'] = '5502714125895'
        with patch.object(handler.Config, 'FLASHCAT_APP_KEY', 'app'), \
             patch.object(handler.Config, 'FLASHCAT_PHONE_INTEGRATION_KEY', 'default-key'), \
             patch.object(handler, 'create_phone_incident', return_value=None) as direct, \
             patch.object(handler, 'create_phone_incident_from_event', return_value='incident') as event, \
             patch.object(handler, 'update_incident_id'):
            self.assertEqual(handler._create_phone_incident(self.data, 'maid'), 'incident')
            self.assertEqual(direct.call_args.args[2], '5502714125895')
            self.assertEqual(event.call_args.args[2], 'default-key')

    def test_different_channel_labels_are_not_aggregated_or_deduped_together(self):
        other = copy.deepcopy(self.data)
        self.data['alerts'][0]['labels']['FLASHCAT_CHANNEL_ID'] = '5502714125895'
        other['alerts'][0]['labels']['FLASHCAT_CHANNEL_ID'] = 'other-channel'
        for data in (self.data, other):
            data['commonLabels'] = data['alerts'][0]['labels']
        self.assertEqual(len(handler._group_and_aggregate_by_alertname([self.data, other])), 2)
        self.assertNotEqual(handler._make_label_dedup_key(self.data, 'g'), handler._make_label_dedup_key(other, 'g'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
