"""每群一张观察期汇总卡片；失败只记录日志，不阻断告警复查。"""

import hashlib
import json
import logging
from collections import defaultdict
from datetime import datetime, timezone

from db.pool import db_cursor
from feishu_utils import trend_gate
from utils.alert_trace import log_context, route_maid

logger = logging.getLogger(__name__)


def _load_rows():
    with db_cursor(dictionary=True) as (_, cursor):
        # last_value 是上次通知值；展示当前值应取最新决策日志，不能混用。
        cursor.execute(
            "SELECT s.*, (SELECT d.metric_value FROM alert_trend_decision_log d "
            "WHERE d.rule_uid=s.rule_uid AND d.group_id=s.group_id AND d.fingerprint=s.fingerprint "
            "ORDER BY d.id DESC LIMIT 1) AS metric_value "
            "FROM alert_trend_state s WHERE s.status='pending' "
            "ORDER BY s.group_id, s.rule_uid, s.fingerprint"
        )
        pending = cursor.fetchall()
        cursor.execute('SELECT group_id, message_id, content_hash FROM alert_trend_digest')
        digests = {row['group_id']: row for row in cursor.fetchall()}
    return pending, digests


def _number(value):
    return '未知' if value is None else f'{float(value):.6g}'


def build_card(rows, now=None):
    """正文用纯文本防止告警标签被当作 @；时间以分钟显示，减少无意义更新。"""
    now = now or datetime.now(timezone.utc)
    elements = []
    thresholds = {}
    for row in rows:
        try:
            payload = json.loads(row['payload']) if isinstance(row['payload'], str) else row['payload']
            labels = payload['alerts'][0].get('labels') or {}
        except (ValueError, KeyError, IndexError, TypeError):
            logger.warning('观察卡片无法解析告警标签: group_id=%s fingerprint=%s',
                           row['group_id'], row['fingerprint'])
            labels = {}
        uid = row['rule_uid']
        if uid not in thresholds:
            try:
                _, threshold, _, direction = trend_gate._load_rule(uid)
                thresholds[uid] = ('<=' if direction == 'lower_worse' else '>=', threshold)
            except Exception:
                logger.exception('观察卡片读取阈值失败: group_id=%s rule_uid=%s', row['group_id'], uid)
                thresholds[uid] = ('', None)
        operator, threshold = thresholds[uid]
        first_seen = row['first_seen']
        if first_seen.tzinfo is None:
            first_seen = first_seen.replace(tzinfo=timezone.utc)
        minutes = max(0, int((now - first_seen).total_seconds() // 60))
        duration = f'{minutes} 分钟' if minutes else '不足 1 分钟'
        dimensions = ' / '.join(f'{key}={labels[key]}' for key in ('tenant', 'model', 'ep') if labels.get(key))
        text = (
            f"{labels.get('alertname') or uid}\n{dimensions or '无关键维度'}\n"
            f"当前值 {_number(row.get('metric_value'))} / 触发阈值 {operator} {_number(threshold)}\n"
            f"已观察 {duration} · {row.get('reason') or '等待复查'}"
        )
        elements.append({'tag': 'div', 'text': {'tag': 'plain_text', 'content': text}})
    if not rows:
        elements.append({'tag': 'div', 'text': {'tag': 'plain_text', 'content': '当前无观察中的趋势告警'}})
    card = {
        'config': {'wide_screen_mode': True, 'update_multi': True},
        'header': {'template': 'blue' if rows else 'green', 'title': {
            'tag': 'plain_text', 'content': '👀 趋势告警观察中' if rows else '当前无观察中的趋势告警'}},
        'elements': elements,
    }
    # “更新于”仅代表实际发送/PATCH 时间，不参与 hash，空卡片不会每分钟刷新。
    canonical = json.dumps(card, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    content_hash = hashlib.md5(canonical.encode('utf-8')).hexdigest()
    elements.append({'tag': 'note', 'elements': [
        {'tag': 'plain_text', 'content': f'更新于 {now.astimezone():%H:%M}'}]})
    return json.dumps(card, ensure_ascii=False), content_hash


def _save_digest(group_id, message_id, content_hash):
    with db_cursor() as (conn, cursor):
        cursor.execute(
            'INSERT INTO alert_trend_digest (group_id, message_id, content_hash) VALUES (%s,%s,%s) '
            'ON DUPLICATE KEY UPDATE message_id=VALUES(message_id), content_hash=VALUES(content_hash)',
            (group_id, message_id, content_hash),
        )
        conn.commit()


def update_digests(feishu_client):
    try:
        pending, digests = _load_rows()
    except Exception:
        logger.exception('加载趋势观察汇总失败')
        return
    groups = defaultdict(list)
    for row in pending:
        groups[row['group_id']].append(row)
    for group_id in sorted(set(groups) | set(digests)):
        rows = groups[group_id]
        maids = []
        for row in rows:
            try:
                payload = json.loads(row['payload']) if isinstance(row['payload'], str) else row['payload']
                maid = trend_gate.state_maid(row, group_id)
                if maid:
                    payload.setdefault('_route_maids', {})[group_id] = maid
                maids.append(route_maid(payload, group_id))
            except (ValueError, KeyError, TypeError, AttributeError):
                pass  # build_card 会记录具体解析错误，不阻断其他告警。
        with log_context(maid=','.join(sorted(set(maids))) or '-', group_id=group_id):
            _update_group(feishu_client, group_id, rows, digests.get(group_id) or {})


def _update_group(feishu_client, group_id, rows, previous):
    try:
        with trend_gate.lock_for(('trend-digest', group_id)):
            message_id = previous.get('message_id')
            if not rows and not message_id:
                return
            content, content_hash = build_card(rows)
            if message_id and content_hash == previous.get('content_hash'):
                logger.debug('event=trend.digest.unchanged message_id=%s pending=%s', message_id, len(rows))
                return
            operation = 'patch' if message_id else 'send'
            if message_id:
                feishu_client.patch_message(message_id, content)
            else:
                message_id = feishu_client.send('chat_id', group_id, 'interactive', content)
                if not message_id:
                    raise RuntimeError('飞书未返回观察卡片 message_id')
            _save_digest(group_id, message_id, content_hash)
            logger.info('event=trend.digest.%s message_id=%s pending=%s', operation, message_id, len(rows))
    except Exception:
        logger.exception('更新趋势观察汇总失败: group_id=%s', group_id)
