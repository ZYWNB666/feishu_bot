"""告警日志上下文：MAID 在路由阶段生成，并随观察 payload 持久化。"""

import hashlib
import json
import logging
import uuid
from contextlib import contextmanager
from contextvars import ContextVar, copy_context
from functools import wraps

_context = ContextVar('alert_log_context', default=None)


def current_maid():
    return (_context.get() or {}).get('maid')


def register_maid(maid):
    context = _context.get()
    if context and maid:
        context['related_maids'].add(maid)


def set_maid(maid):
    context = _context.get()
    if context and maid:
        context['maid'] = maid
        register_maid(maid)


def route_maid(data, group_id):
    """同一群、同一 startsAt 的告警重投/重启复用 ID；不同轮次独立。"""
    mapping = data.setdefault('_route_maids', {})
    if group_id not in mapping:
        identities = []
        for alert in data.get('alerts', []):
            starts_at = alert.get('startsAt')
            if not starts_at or starts_at == '0001-01-01T00:00:00Z':
                # 非标准输入无法判断是否为新一轮，不能永久复用旧记录。
                starts_at = uuid.uuid4().hex
            identities.append({
                'fingerprint': alert.get('fingerprint') or alert.get('labels', {}),
                'startsAt': starts_at,
                'generatorURL': alert.get('generatorURL', ''),
            })
        ordered = sorted(json.dumps(item, sort_keys=True, ensure_ascii=False) for item in identities)
        raw = json.dumps([group_id, ordered], ensure_ascii=False)
        mapping[group_id] = hashlib.sha256(raw.encode()).hexdigest()[:20]
    register_maid(mapping[group_id])
    return mapping[group_id]


@contextmanager
def log_context(**fields):
    parent = _context.get()
    context = dict(parent) if parent else {'request_id': uuid.uuid4().hex[:16], 'related_maids': set()}
    context.update(fields)
    token = _context.set(context)
    register_maid(context.get('maid'))
    try:
        yield context
    finally:
        _context.reset(token)


@contextmanager
def route_context(data, group_id):
    alert = (data.get('alerts') or [{}])[0]
    with log_context(maid=route_maid(data, group_id), group_id=group_id,
                     fingerprint=alert.get('fingerprint', '-')):
        yield


def traced_route(function):
    @wraps(function)
    def wrapped(data, config_row, *args, **kwargs):
        with route_context(data, config_row.get('group_id', '')):
            return function(data, config_row, *args, **kwargs)
    return wrapped


def traced_request(function):
    @wraps(function)
    def wrapped(data, *args, **kwargs):
        if _context.get():
            return function(data, *args, **kwargs)
        # 外部 webhook 不得指定内部 MAID，防止覆盖其他告警的记录。
        if isinstance(data, dict):
            data = {key: value for key, value in data.items() if key != '_route_maids'}
        with log_context():
            return function(data, *args, **kwargs)
    return wrapped


def contextual_target(function):
    """回调的现有后台线程显式继承上下文，避免日志丢失 MAID。"""
    context = copy_context()
    parent = _context.get()
    if parent:
        context.run(_context.set, {**parent, 'related_maids': set(parent['related_maids'])})
    return lambda: context.run(function)


def install_log_context():
    """在日志创建时注入字段，应用模块和依赖模块共用同一上下文。"""
    previous = logging.getLogRecordFactory()
    if getattr(previous, '_alert_trace_factory', False):
        return

    def factory(*args, **kwargs):
        record = previous(*args, **kwargs)
        context = _context.get() or {}
        record.maid = context.get('maid') or ','.join(sorted(context.get('related_maids', ()))) or '-'
        record.request_id = context.get('request_id', '-')
        record.group_id = context.get('group_id', '-')
        record.fingerprint = context.get('fingerprint', '-')
        return record

    factory._alert_trace_factory = True
    logging.setLogRecordFactory(factory)
