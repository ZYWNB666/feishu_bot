"""单条 Grafana 告警的趋势判断与持久化观察状态。"""

import json
import logging
import math
import re
import statistics
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import requests

from config.config import Config
from config.constants import (
    TREND_CHECK_SECONDS, TREND_SENT_CHECK_SECONDS, TREND_DEDUP_SECONDS, TREND_HARD_RATIO,
    TREND_MIN_REQUESTS, TREND_OBSERVE_SECONDS, TREND_RISE_RATIO,
)
from db.pool import db_cursor

logger = logging.getLogger(__name__)
_rule_cache = {}
_rule_cache_lock = threading.Lock()
_state_locks = [threading.Lock() for _ in range(128)]
_uid_pattern = re.compile(r"/alerting/grafana/([^/]+)/")


@dataclass(frozen=True)
class Decision:
    action: str  # send / observe / cancel
    value: float | None
    reason: str
    urgent: bool | None = None  # None 表示查询不足，沿用路由原有 @ 策略


def oncall_mention_policy(decision, previous_value=None):
    """只在趋势明确恶化时升级 @；查询失败时沿用原有告警策略。"""
    if decision.action != 'send':
        return None
    if decision.urgent is None:
        return None
    if (decision.value is not None and previous_value is not None
            and decision.value >= previous_value * 1.25):
        return True
    return decision.urgent


def rule_uid(data):
    alerts = data.get('alerts') or []
    if len(alerts) != 1:
        return ''
    url = alerts[0].get('generatorURL') or ''
    match = _uid_pattern.search(url)
    return match.group(1) if match else ''


def is_enabled_for(data):
    alerts = data.get('alerts') or []
    if not Config.TREND_GATE_ENABLED or rule_uid(data) != Config.TREND_RULE_UID:
        return False
    if not alerts[0].get('fingerprint'):
        return False
    status = data.get('_original_status', data.get('status'))
    return alerts[0].get('status') == 'firing' or (
        status == 'resolved' and alerts[0].get('status') == 'resolved'
    )


def state_key(data, group_id):
    alert = data['alerts'][0]
    fingerprint = alert.get('fingerprint')
    if not fingerprint:
        raise ValueError('趋势告警缺少 fingerprint')
    return rule_uid(data), group_id, fingerprint


def lock_for(key):
    """当前单副本部署内，串行化同一实例的 firing、resolved 和定时复查。"""
    return _state_locks[hash(key) % len(_state_locks)]


def _request_json(url, params=None, auth=None, headers=None):
    response = requests.get(url, params=params, auth=auth, headers=headers, timeout=3)
    response.raise_for_status()
    body = response.json()
    if isinstance(body, dict) and body.get('status') == 'error':
        raise ValueError(body.get('error') or '指标查询失败')
    return body


def _load_rule(uid):
    now = time.monotonic()
    with _rule_cache_lock:
        cached = _rule_cache.get(uid)
        if cached and cached[0] > now:
            return cached[1]

    url = f"{Config.GRAFANA_API_URL.rstrip('/')}/api/v1/provisioning/alert-rules/{quote(uid, safe='')}"
    read_key = Config.GRAFANA_RULES_READ_KEY or Config.GRAFANA_API_KEY
    body = _request_json(url, headers={'Authorization': f'Bearer {read_key}'})
    condition = next(x for x in body['data'] if x['refId'] == body['condition'])
    model = condition['model']
    if model.get('type') != 'threshold':
        raise ValueError('试点规则的条件不是 threshold')
    evaluator = model['conditions'][0]['evaluator']
    if evaluator['type'] != 'gt':
        raise ValueError('试点只支持数值越高越严重的规则')
    threshold = float(evaluator['params'][0])
    recovery = model['conditions'][0].get('unloadEvaluator') or {}
    recovery_threshold = float((recovery.get('params') or [threshold])[0])
    source = next(x for x in body['data'] if x['refId'] == model['expression'])
    expression = source['model']['expr']
    result = (expression, threshold, recovery_threshold)
    with _rule_cache_lock:
        _rule_cache[uid] = (now + 300, result)
    return result


def _vm_url(suffix):
    url = Config.VM_QUERY_URL.rstrip('/')
    if url.endswith('/query_range'):
        return url if suffix == 'query_range' else url[:-len('query_range')] + suffix
    if url.endswith('/query'):
        return url if suffix == 'query' else url[:-len('query')] + suffix
    raise ValueError('VM_QUERY_URL 应以 /api/v1/query 或 /api/v1/query_range 结尾')


def _vm_query(query, *, start=None, end=None, step=None):
    auth = (Config.VM_USER, Config.VM_PASSWORD) if Config.VM_USER else None
    if start is None:
        body = _request_json(_vm_url('query'), params={'query': query}, auth=auth)
    else:
        body = _request_json(
            _vm_url('query_range'),
            params={'query': query, 'start': start, 'end': end, 'step': step},
            auth=auth,
        )
    return body['data']['result']


def _matching_points(result, labels):
    dimensions = {key: labels[key] for key in ('tenant', 'model', 'ep') if labels.get(key)}
    if not dimensions:
        raise ValueError('告警缺少可匹配的指标维度')
    matches = [
        series for series in result
        if all(series.get('metric', {}).get(key) == value for key, value in dimensions.items())
    ]
    if len(matches) != 1:
        raise ValueError(f'匹配到 {len(matches)} 条指标序列，预期恰好 1 条')
    points = []
    for timestamp, value in matches[0].get('values', []):
        number = float(value)
        if math.isfinite(number):
            points.append((float(timestamp), number))
    return points


def _request_count(labels):
    selector = ','.join(
        f'{key}={json.dumps(str(labels[key]), ensure_ascii=False)}'
        for key in ('tenant', 'model', 'ep') if labels.get(key)
    )
    if not selector:
        raise ValueError('缺少请求量查询维度')
    expression = f'sum(increase(magik_model_tpot_ms_count{{{selector}}}[1m]))'
    result = _vm_query(expression)
    if len(result) != 1:
        raise ValueError('请求量查询无唯一结果')
    count = float(result[0]['value'][1])
    if not math.isfinite(count):
        raise ValueError('请求量不是有效数字')
    return count


def with_decision_note(data, decision):
    """发送观察后的卡片时标出当前复查值，避免只展示首次 Webhook 的旧数值。"""
    if decision.value is None:
        return data
    updated = dict(data)
    alert = dict(data['alerts'][0])
    annotations = dict(alert.get('annotations') or {})
    note = f"路由复查 TPOT: {decision.value:.2f} ms（{decision.reason}）"
    annotations['description'] = (annotations.get('description') or '') + '\n' + note
    alert['annotations'] = annotations
    updated['alerts'] = [alert]
    return updated


def classify(points, threshold, recovery_threshold, request_count, first_seen, now=None):
    """根据真实时间序列判断；数据不足直接发送，避免误抑制。"""
    now = time.time() if now is None else now
    recent = [v for t, v in points if now - 60 <= t <= now + 5]
    previous = [v for t, v in points if now - 120 <= t < now - 60]
    if (request_count < TREND_MIN_REQUESTS or len(recent) < 2
            or len(previous) < 2 or not points or now - points[-1][0] > 45):
        return Decision('send', points[-1][1] if points else None, '指标样本不足，按原流程发送')

    latest = points[-1][1]
    current_median = statistics.median(recent)
    previous_median = statistics.median(previous)
    if latest >= threshold * TREND_HARD_RATIO:
        return Decision('send', latest, '达到硬上限', urgent=True)
    if latest < recovery_threshold:
        return Decision('cancel', latest, '指标已回落至恢复阈值以下')
    if (current_median >= threshold
            and current_median - previous_median >= threshold * 0.05
            and current_median >= previous_median * (1 + TREND_RISE_RATIO)):
        return Decision('send', latest, '最近一分钟明显恶化', urgent=True)
    if now - first_seen >= TREND_OBSERVE_SECONDS:
        if latest >= threshold:
            return Decision('send', latest, '达到最长观察时间且仍越线', urgent=False)
        return Decision('cancel', latest, '观察期末已低于触发阈值')
    return Decision('observe', latest, '轻微越线，继续观察')


def decide(data, first_seen):
    """查询规则和指标。任何查询/解析失败交由调用方按原流程发送。"""
    uid = rule_uid(data)
    if not (Config.GRAFANA_RULES_READ_KEY or Config.GRAFANA_API_KEY) or not Config.VM_QUERY_URL:
        raise ValueError('缺少 Grafana 或 VictoriaMetrics 查询配置')
    expression, threshold, recovery_threshold = _load_rule(uid)
    labels = data['alerts'][0].get('labels') or {}
    now = time.time()
    points = _matching_points(
        _vm_query(expression, start=now - 180, end=now, step=15), labels
    )
    count = _request_count(labels)
    return classify(points, threshold, recovery_threshold, count, first_seen, now=now)


def get_state(key):
    with db_cursor(dictionary=True) as (conn, cursor):
        cursor.execute(
            'SELECT *, TIMESTAMPDIFF(SECOND, last_sent_at, UTC_TIMESTAMP()) AS seconds_since_sent '
            'FROM alert_trend_state WHERE rule_uid=%s AND group_id=%s AND fingerprint=%s',
            key,
        )
        return cursor.fetchone()


def log_decision(key, decision):
    """保留每次路由决策，供试点复盘和后续模型评估。日志写入失败不影响发送。"""
    try:
        with db_cursor() as (conn, cursor):
            cursor.execute(
                'INSERT INTO alert_trend_decision_log '
                '(rule_uid, group_id, fingerprint, action, metric_value, reason) '
                'VALUES (%s,%s,%s,%s,%s,%s)',
                (*key, decision.action, decision.value, decision.reason),
            )
            conn.commit()
    except Exception:
        logger.exception('趋势决策日志写入失败: %s', key)


def save_pending(key, config_id, data, first_seen, reason):
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with db_cursor() as (conn, cursor):
        cursor.execute(
            '''INSERT INTO alert_trend_state
               (rule_uid, group_id, fingerprint, config_id, payload, status, first_seen, next_check, reason)
               VALUES (%s,%s,%s,%s,%s,'pending',%s,%s,%s)
               ON DUPLICATE KEY UPDATE config_id=VALUES(config_id), payload=VALUES(payload),
                 first_seen=VALUES(first_seen), next_check=VALUES(next_check), reason=VALUES(reason),
                 last_sent_at=IF(status='resolved', NULL, last_sent_at),
                 `last_value`=IF(status='resolved', NULL, `last_value`), status='pending' ''',
            (*key, config_id, json.dumps(data, ensure_ascii=False),
             datetime.fromtimestamp(first_seen, timezone.utc).replace(tzinfo=None),
             now + timedelta(seconds=TREND_CHECK_SECONDS), reason),
        )
        conn.commit()


def mark_sent(key, value, reason):
    with db_cursor() as (conn, cursor):
        cursor.execute(
            "UPDATE alert_trend_state SET status='sent', last_sent_at=UTC_TIMESTAMP(6), "
            "`last_value`=%s, next_check=DATE_ADD(UTC_TIMESTAMP(6), INTERVAL %s SECOND), "
            "reason=%s WHERE rule_uid=%s AND group_id=%s AND fingerprint=%s",
            (value, TREND_SENT_CHECK_SECONDS, reason, *key),
        )
        conn.commit()


def mark_resolved(key, reason):
    with db_cursor() as (conn, cursor):
        cursor.execute(
            "UPDATE alert_trend_state SET status='resolved', reason=%s "
            "WHERE rule_uid=%s AND group_id=%s AND fingerprint=%s",
            (reason, *key),
        )
        conn.commit()


def restore_sent(key, reason):
    """升级发送失败后指标不再恶化，保留此前已发送的事件状态。"""
    with db_cursor() as (conn, cursor):
        cursor.execute(
            "UPDATE alert_trend_state SET status='sent', "
            "next_check=DATE_ADD(UTC_TIMESTAMP(6), INTERVAL %s SECOND), reason=%s "
            "WHERE rule_uid=%s AND group_id=%s AND fingerprint=%s AND status='pending'",
            (TREND_SENT_CHECK_SECONDS, reason, *key),
        )
        conn.commit()


def schedule_next(key, reason, delay=TREND_CHECK_SECONDS):
    with db_cursor() as (conn, cursor):
        cursor.execute(
            "UPDATE alert_trend_state SET next_check=DATE_ADD(UTC_TIMESTAMP(6), INTERVAL %s SECOND), reason=%s "
            "WHERE rule_uid=%s AND group_id=%s AND fingerprint=%s AND status IN ('pending','sent')",
            (delay, reason, *key),
        )
        conn.commit()


def due_active(limit=50):
    with db_cursor(dictionary=True) as (conn, cursor):
        cursor.execute(
            "SELECT * FROM alert_trend_state WHERE status IN ('pending','sent') "
            "AND next_check<=UTC_TIMESTAMP(6) "
            "ORDER BY next_check LIMIT %s", (limit,),
        )
        return cursor.fetchall()


def duplicate_sent(state):
    if state['status'] != 'sent':
        return False
    seconds = state.get('seconds_since_sent')
    if seconds is not None:
        return 0 <= seconds < TREND_DEDUP_SECONDS
    sent_at = state.get('last_sent_at')
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    return bool(sent_at and (now - sent_at).total_seconds() < TREND_DEDUP_SECONDS)
