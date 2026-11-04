"""恢复原地更新回归：禁止真实 HTTP 和数据库访问。"""
import copy
import json
import unittest
from contextlib import ExitStack
from unittest.mock import Mock, patch

from feishu_utils import alert_handler as handler
from feishu_utils.alert_card_biz import build_biz_firing_card
from feishu_utils.resolved_card import build_resolved_card, mention_names


class ResolvedCardTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch('requests.sessions.Session.request', side_effect=AssertionError('禁止 HTTP')))
        self.stack.enter_context(patch('db.pool._build_pool', side_effect=AssertionError('禁止数据库')))
        self.alert = {'status': 'firing', 'fingerprint': 'fp', 'labels': {},
                      'annotations': {'description': '当前成功率: 11.57%'},
                      'startsAt': '2026-11-04T13:33:40+08:00', 'endsAt': '2026-11-04T05:40:00Z'}
        self.original = json.loads(build_biz_firing_card(
            '成功率低于20%', 'phone', [self.alert], {'generatorURL': 'https://grafana.test'},
            'maid', {'model': 'Kimi-K3'}, ['ou_user']))

    def test_snapshot_names_buttons_and_duration(self):
        before = copy.deepcopy(self.original)
        card = json.loads(build_resolved_card(self.original, [self.alert], {'ou_user': '宋小兵'}))
        text = json.dumps(card, ensure_ascii=False)
        self.assertEqual(self.original, before)
        self.assertEqual(card['header']['template'], 'green')
        self.assertIn('[Phone]', card['header']['title']['content'])
        for value in ('原通知人员', '宋小兵', '触发时成功率: 11.57%', '2026-11-04 13:40:00', '6 分 20 秒', 'maid', 'Kimi-K3'):
            self.assertIn(value, text)
        self.assertNotIn('<at ', text)
        self.assertNotIn('show_custom_silence', text)
        self.assertNotIn('"action": "silence"', text)
        self.assertEqual(build_resolved_card(card, [self.alert], {}), json.dumps(card, ensure_ascii=False))

    def test_recovery_is_immediately_after_trigger_time(self):
        card = json.loads(build_resolved_card(self.original, [self.alert], {}))
        elements = card['elements']
        trigger = next(i for i, e in enumerate(elements) if '触发时间' in e.get('text', {}).get('content', ''))
        self.assertIn('恢复时间', elements[trigger + 1]['text']['content'])

    def test_database_local_time_and_missing_name_are_safe(self):
        alert = dict(self.alert, startsAt='2026-11-04 13:33:40')
        text = build_resolved_card(self.original, [alert], {})
        self.assertIn('6 分 20 秒', text)
        self.assertIn('ou_user', text)
        self.assertNotIn('<at ', text)

    def test_links_preserved_and_interactive_actions_removed(self):
        self.original['elements'].append({'tag': 'action', 'actions': [
            {'tag': 'button', 'url': 'https://grafana.test', 'text': {'tag': 'plain_text', 'content': '告警源'}},
            {'tag': 'picker_datetime', 'value': {'action': 'silence_until'}},
            {'tag': 'button', 'value': {'action': 'ack_incident'}}]})
        card = json.loads(build_resolved_card(self.original, [self.alert], {}))
        actions = [a for e in card['elements'] if e.get('tag') == 'action' for a in e['actions']]
        self.assertTrue(actions)
        self.assertTrue(all(a.get('url') or a.get('multi_url') for a in actions))
        self.assertIn('ou_user', json.dumps(card))

    def test_missing_time_not_fabricated(self):
        alert = dict(self.alert, endsAt='0001-01-01T00:00:00Z')
        text = build_resolved_card(self.original, [alert], {})
        self.assertIn('未提供', text)
        self.assertNotIn('持续时长', text)

    def test_aggregate_uses_last_recovery_regardless_of_order(self):
        later = dict(self.alert, endsAt='2026-11-04T13:43:40+08:00')
        text = build_resolved_card(self.original, [later, self.alert], {})
        self.assertIn('10 分 0 秒', text)
        self.assertIn('全部 2 个实例已恢复', text)

    def test_name_lookup_uses_only_mentioned_ids(self):
        with patch('feishu_utils.resolved_card.db_cursor') as db:
            cursor = Mock()
            db.return_value.__enter__.return_value = (Mock(), cursor)
            cursor.fetchall.return_value = [('ou_user', '宋小兵')]
            self.assertEqual(mention_names(self.original), {'ou_user': '宋小兵'})
            self.assertEqual(cursor.execute.call_args.args[1], ('ou_user',))

    def setup_delivery(self):
        for name, value in (
            ('alert_data_api', ([], ['phone'], 'maid', {})),
            ('get_message_id_by_fingerprint', 'message'),
            ('get_all_fingerprints_by_fingerprint', ['fp']),
            ('get_alerttime_by_fingerprint', self.alert['startsAt']),
            ('get_card_content', json.dumps(self.original)),
            ('mention_names', {'ou_user': '宋小兵'}),
        ):
            self.stack.enter_context(patch.object(handler, name, return_value=value))
        self.save = self.stack.enter_context(patch.object(handler, 'save_card_content'))
        self.client = Mock()
        self.data = {'status': 'resolved', 'alerts': [dict(self.alert, status='resolved')]}

    def deliver(self, template='biz'):
        return handler._process_single_alert_config(
            self.data, {'group_id': 'group', 'template_type': template}, '成功率低于20%', self.client)

    def test_delivery_patches_same_message_and_persists_both_templates(self):
        self.setup_delivery()
        for template in ('biz', 'ops'):
            self.client.reset_mock()
            self.assertTrue(self.deliver(template)['success'])
            self.client.patch_message.assert_called_once()
            self.assertEqual(self.client.patch_message.call_args.args[0], 'message')
            self.client.reply_message.assert_not_called()
            self.client.send.assert_not_called()
            self.save.assert_called_with('maid', self.client.patch_message.call_args.args[1])

    def test_patch_failure_returns_failure_without_new_message(self):
        self.setup_delivery()
        self.client.patch_message.side_effect = RuntimeError('failed')
        self.assertFalse(self.deliver()['success'])
        self.save.assert_not_called()
        self.client.reply_message.assert_not_called()
        self.client.send.assert_not_called()

    def test_old_message_fetch_fallback_and_missing_original_failure(self):
        self.setup_delivery()
        with patch.object(handler, 'get_card_content', return_value=''):
            self.client.get_message.return_value = {'body': {'content': json.dumps(self.original)}}
            self.assertTrue(self.deliver()['success'])
            self.client.get_message.assert_called_with('message')
            self.client.reset_mock()
            self.client.get_message.return_value = None
            self.assertFalse(self.deliver()['success'])
            self.client.patch_message.assert_not_called()
            self.client.reply_message.assert_not_called()

    def test_partial_recovery_does_not_turn_card_green(self):
        self.setup_delivery()
        with patch.object(handler, 'get_all_fingerprints_by_fingerprint', return_value=['fp', 'other']):
            self.assertTrue(self.deliver()['skipped'])
            self.client.patch_message.assert_not_called()


if __name__ == '__main__':
    unittest.main()
