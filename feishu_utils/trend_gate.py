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
from mysql.connector import Error as MySQLError

from config.config import Config
from config.constants import (
    TREND_CHECK_SECONDS, TREND_SENT_CHECK_SECONDS, TREND_DEDUP_SECONDS, TREND_HARD_RATIO,
    TREND_MIN_REQUESTS, TREND_OBSERVE_SECONDS, TREND_RISE_RATIO,
    ALERT_CONFIG_CACHE_TTL, TREND_REQUEST_METRIC, TREND_HARD_FLOOR,
    TREND_SLOW_WINDOW_SECONDS, TREND_CONFIRM_CYCLES,
)
from db.pool import db_cursor

logger = logging.getLogger(__name__)
_rule_cache = {}
_rule_cache_lock = threading.Lock()
_state_locks = [threading.Lock() for _ in range(128)]
_uid_pattern = re.compile(r"/alerting/grafana/([^/]+)/")
_policy_cache_lock = threading.Lock()
_policy_cache = {}
_policy_cache_expire_at = 0.0
_legacy_warning_emitted = False


@dataclass(frozen=True)
class TrendPolicy:
    rule_uids: tuple[str, ...] = ()
    observe_seconds: int = TREND_OBSERVE_SECONDS
    rise_ratio: float = TREND_RISE_RATIO
    hard_ratio: float = TREND_HARD_RATIO
    hard_floor: float | None = TREND_HARD_FLOOR
    min_requests: int = TREND_MIN_REQUESTS
    request_metric: str = TREND_REQUEST_METRIC
    slow_window_seconds: int = TREND_SLOW_WINDOW_SECONDS
    confirm_cycles: int = TREND_CONFIRM_CYCLES


def get_policy(config_row):
    """解析单条路由策略；非法策略不阻断普通告警发送。"""
    raw = config_row.get('trend_policy')
    if raw is None:
        return None
    try:
        raw = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(raw, dict):
            raise ValueError('策略必须为 JSON 对象')
        if raw.get('enabled', True) is not True:
            raise ValueError('策略未启用')
        uids = raw.get('rule_uids', [])
        if (not isinstance(uids, list) or not uids
                or any(not isinstance(uid, str) or not uid.strip() for uid in uids)):
            raise ValueError('rule_uids 必须为非空字符串列表')
        values = {name: raw[name] for name in TrendPolicy.__dataclass_fields__ if name in raw}
        values['rule_uids'] = tuple(dict.fromkeys(uids))
        policy = TrendPolicy(**values)
        for name, minimum in (('observe_seconds', 1), ('min_requests', 0),
                              ('slow_window_seconds', 60), ('confirm_cycles', 1)):
            value = getattr(policy, name)
            if type(value) is not int or value < minimum:
                raise ValueError(f'{name} 必须为 >= {minimum} 的整数')
        for name in ('rise_ratio', 'hard_ratio', 'hard_floor'):
            value = getattr(policy, name)
            if name == 'hard_floor' and value is None:
                continue
            if (type(value) not in (int, float) or not math.isfinite(value)
                    or value < 0 or (name == 'hard_ratio' and value == 0)):
                raise ValueError(f'{name} 必须为有效的非负数（hard_ratio 须大于 0）')
        if (not isinstance(policy.request_metric, str)
                or not re.fullmatch(r'[a-zA-Z_:][a-zA-Z0-9_:]*', policy.request_metric)):
            raise ValueError('request_metric 必须为合法的 counter 指标名')
        return policy
    except (ValueError, TypeError, OverflowError) as error:
        logger.warning('趋势策略无效，沿用普通路由: config_id=%s reason=%s', config_row.get('id'), error)
        return None


def invalidate_policy_cache():
    global _policy_cache, _policy_cache_expire_at
    with _policy_cache_lock:
        _policy_cache = {}
        _policy_cache_expire_at = 0.0


def enabled_policies():
    """缓存所有启用策略；空结果和迁移缺失也缓存，避免高频重复查询。"""
    global _policy_cache, _policy_cache_expire_at, _legacy_warning_emitted
    now = time.monotonic()
    with _policy_cache_lock:
        if now < _policy_cache_expire_at:
            return dict(_policy_cache)
        policies = {}
        try:
            with db_cursor(dictionary=True) as (_, cursor):
                cursor.execute('SELECT id, group_id, trend_policy FROM alert_config WHERE trend_policy IS NOT NULL')
                rows = cursor.fetchall()
            for row in rows:
                policy = get_policy(row)
                if policy:
                    policies[row['id']] = policy
        except Exception:
            # 包括未执行迁移导致的未知列；只在首次回退时提示。
            rows = []
        if not policies and not _legacy_warning_emitted:
            logger.warning('趋势策略未配置或无法读取，回退旧环境变量试点（仅提示一次）')
            _legacy_warning_emitted = True
        _policy_cache = policies
        _policy_cache_expire_at = now + ALERT_CONFIG_CACHE_TTL
        return dict(policies)


def trends_enabled():
    return bool(enabled_policies()) or Config.TREND_GATE_ENABLED


def legacy_route_enabled(config_row, data):
    """仅 NULL/未迁移路由允许旧试点回退；非法或显式关闭策略直接放行。"""
    return (config_row.get('trend_policy') is None and not enabled_policies()
            and Config.TREND_GATE_ENABLED and rule_uid(data) == Config.TREND_RULE_UID)


@dataclass(frozen=True)
class Decision:
    action: str  # send / observe / cancel
    value: float | None
    reason: str
    urgent: bool | None = None  # None 表示查询不足，沿用路由原有 @ 策略
    direction: str = 'higher_worse'


def is_escalation(value, prior, direction='higher_worse'):
    if value is None or prior is None:
        return False
    if direction == 'lower_worse':
        return value <= prior * 0.75
    return value >= prior * 1.25


def is_alleviated(value, prior, direction='higher_worse'):
    return prior is not None and not is_escalation(value, prior, direction)


def oncall_mention_policy(decision, previous_value=None):
    """只在趋势明确恶化时升级 @；查询失败时沿用原有告警策略。"""
    if decision.action != 'send':
        return None
    if decision.urgent is None:
        return None
    if is_escalation(decision.value, previous_value, decision.direction):
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
    uid = rule_uid(data)
    if not uid or not alerts[0].get('fingerprint'):
        return False
    policies = enabled_policies()
    if policies:
        enabled = any(uid in policy.rule_uids for policy in policies.values())
    else:
        enabled = Config.TREND_GATE_ENABLED and uid == Config.TREND_RULE_UID
    if not enabled:
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
    directions = {'gt': 'higher_worse', 'lt': 'lower_worse'}
    if evaluator['type'] not in directions:
        raise ValueError('趋势规则只支持 gt/lt 阈值比较')
    direction = directions[evaluator['type']]
    threshold = float(evaluator['params'][0])
    recovery = model['conditions'][0].get('unloadEvaluator') or {}
    recovery_threshold = float((recovery.get('params') or [threshold])[0])
    source = next(x for x in body['data'] if x['refId'] == model['expression'])
    expression = source['model']['expr']
    result = (expression, threshold, recovery_threshold, direction)
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


def _request_count(labels, request_metric=TREND_REQUEST_METRIC):
    selector = ','.join(
        f'{key}={json.dumps(str(labels[key]), ensure_ascii=False)}'
        for key in ('tenant', 'model', 'ep') if labels.get(key)
    )
    if not selector:
        raise ValueError('缺少请求量查询维度')
    expression = f'sum(increase({request_metric}{{{selector}}}[1m]))'
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
    note = f"路由复查指标值: {decision.value:.6g}（{decision.reason}）"
    annotations['description'] = (annotations.get('description') or '') + '\n' + note
    alert['annotations'] = annotations
    updated['alerts'] = [alert]
    return updated


def classify(points, threshold, recovery_threshold, request_count, first_seen, now=None,
             policy=None, direction='higher_worse'):
    """根据真实时间序列判断；数据不足直接发送，避免误抑制。"""
    now = time.time() if now is None else now
    policy = policy or TrendPolicy()
    if direction not in ('higher_worse', 'lower_worse'):
        raise ValueError('未知的趋势判断方向')
    recent = [v for t, v in points if now - 60 <= t <= now + 5]
    previous = [v for t, v in points if now - 120 <= t < now - 60]
    slow = [v for t, v in points if now - policy.slow_window_seconds <= t <= now + 5]
    slow_incomplete = (direction == 'lower_worse' and (
        not points or points[0][0] > now - policy.slow_window_seconds + 15 or len(slow) < 2))
    if (request_count < policy.min_requests or len(recent) < 2
            or len(previous) < 2 or not points or now - points[-1][0] > 45 or slow_incomplete):
        return Decision('send', points[-1][1] if points else None, '指标样本不足，按原流程发送', direction=direction)

    latest = points[-1][1]
    current_median = statistics.median(recent)
    previous_median = statistics.median(previous)
    if direction == 'lower_worse':
        if policy.hard_floor is not None and latest <= policy.hard_floor:
            return Decision('send', latest, '跌破绝对下限', urgent=True, direction=direction)
        if latest >= recovery_threshold:
            return Decision('cancel', latest, '指标已回升至恢复阈值', direction=direction)
        if statistics.median(slow) <= threshold and current_median <= threshold:
            return Decision('send', latest, '快慢窗口同时越线', urgent=True, direction=direction)
        # 观察到期优先于快窗口继续观察，保证单窗口越线不会无限延迟。
        if now - first_seen >= policy.observe_seconds:
            if latest <= threshold:
                return Decision('send', latest, '达到最长观察时间且仍越线', urgent=False, direction=direction)
            return Decision('cancel', latest, '观察期末已高于触发阈值', direction=direction)
        return Decision('observe', latest, '快慢窗口未同时越线，继续观察', direction=direction)

    if latest >= threshold * policy.hard_ratio:
        return Decision('send', latest, '达到硬上限', urgent=True)
    if latest < recovery_threshold:
        return Decision('cancel', latest, '指标已回落至恢复阈值以下')
    if (current_median >= threshold
            and current_median - previous_median >= threshold * 0.05
            and current_median >= previous_median * (1 + policy.rise_ratio)):
        return Decision('send', latest, '最近一分钟明显恶化', urgent=True)
    if now - first_seen >= policy.observe_seconds:
        if latest >= threshold:
            return Decision('send', latest, '达到最长观察时间且仍越线', urgent=False)
        return Decision('cancel', latest, '观察期末已低于触发阈值')
    return Decision('observe', latest, '轻微越线，继续观察')


def decide(data, first_seen, policy=None):
    """查询规则和指标。任何查询/解析失败交由调用方按原流程发送。"""
    uid = rule_uid(data)
    policy = policy or TrendPolicy()
    if not (Config.GRAFANA_RULES_READ_KEY or Config.GRAFANA_API_KEY) or not Config.VM_QUERY_URL:
        raise ValueError('缺少 Grafana 或 VictoriaMetrics 查询配置')
    expression, threshold, recovery_threshold, direction = _load_rule(uid)
    labels = data['alerts'][0].get('labels') or {}
    now = time.time()
    points = _matching_points(
        _vm_query(expression, start=now - max(180, policy.slow_window_seconds + 60), end=now, step=15), labels
    )
    count = _request_count(labels, policy.request_metric)
    return classify(points, threshold, recovery_threshold, count, first_seen, now=now,
                    policy=policy, direction=direction)


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


def _execute_state(cursor, sql, params):
    """旧表没有 cancel_streak 时，去掉清零字段后执行原 SQL。"""
    try:
        cursor.execute(sql, params)
    except MySQLError as error:
        if error.errno != 1054 or 'cancel_streak=0, ' not in sql:
            raise
        cursor.execute(sql.replace('cancel_streak=0, ', ''), params)


def save_pending(key, config_id, data, first_seen, reason):
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with db_cursor() as (conn, cursor):
        _execute_state(cursor,
            '''INSERT INTO alert_trend_state
               (rule_uid, group_id, fingerprint, config_id, payload, status, first_seen, next_check, reason)
               VALUES (%s,%s,%s,%s,%s,'pending',%s,%s,%s)
               ON DUPLICATE KEY UPDATE cancel_streak=0, config_id=VALUES(config_id), payload=VALUES(payload),
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
        _execute_state(cursor,
            "UPDATE alert_trend_state SET cancel_streak=0, status='sent', last_sent_at=UTC_TIMESTAMP(6), "
            "`last_value`=%s, next_check=DATE_ADD(UTC_TIMESTAMP(6), INTERVAL %s SECOND), "
            "reason=%s WHERE rule_uid=%s AND group_id=%s AND fingerprint=%s",
            (value, TREND_SENT_CHECK_SECONDS, reason, *key),
        )
        conn.commit()


def mark_resolved(key, reason):
    with db_cursor() as (conn, cursor):
        _execute_state(cursor,
            "UPDATE alert_trend_state SET cancel_streak=0, status='resolved', reason=%s "
            "WHERE rule_uid=%s AND group_id=%s AND fingerprint=%s",
            (reason, *key),
        )
        conn.commit()


def restore_sent(key, reason):
    """升级发送失败后指标不再恶化，保留此前已发送的事件状态。"""
    with db_cursor() as (conn, cursor):
        _execute_state(cursor,
            "UPDATE alert_trend_state SET cancel_streak=0, status='sent', "
            "next_check=DATE_ADD(UTC_TIMESTAMP(6), INTERVAL %s SECOND), reason=%s "
            "WHERE rule_uid=%s AND group_id=%s AND fingerprint=%s AND status='pending'",
            (TREND_SENT_CHECK_SECONDS, reason, *key),
        )
        conn.commit()


def schedule_next(key, reason, delay=TREND_CHECK_SECONDS, *, cancel_streak=0):
    with db_cursor() as (conn, cursor):
        sql = (
            "UPDATE alert_trend_state SET cancel_streak=0, next_check=DATE_ADD(UTC_TIMESTAMP(6), INTERVAL %s SECOND), reason=%s "
            "WHERE rule_uid=%s AND group_id=%s AND fingerprint=%s AND status IN ('pending','sent')"
        )
        params = (delay, reason, *key)
        if cancel_streak:
            sql = sql.replace('cancel_streak=0', 'cancel_streak=%s')
            params = (cancel_streak, *params)
        _execute_state(cursor, sql, params)
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
