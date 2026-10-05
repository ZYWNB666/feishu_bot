"""持久化趋势告警的周期复查。当前部署为单副本，进程重启后从数据库恢复待观察记录。"""

import json
import logging
import threading
import time
from datetime import timezone

from config.config import Config
from config.constants import TREND_CHECK_SECONDS, TREND_SENT_CHECK_SECONDS
from db.pool import db_cursor
from feishu_utils import trend_gate

logger = logging.getLogger(__name__)


def run_pending_once(feishu_client):
    for row in trend_gate.due_active():
        key = (row['rule_uid'], row['group_id'], row['fingerprint'])
        try:
            with trend_gate.lock_for(key):
                _process_pending_row(key, feishu_client)
        except Exception:
            logger.exception("待观察告警复查失败: %s", key)
            try:
                trend_gate.schedule_next(key, '复查异常，等待重试')
            except Exception:
                logger.exception("更新待观察告警重试时间失败: %s", key)


def _process_pending_row(key, feishu_client):
    from feishu_utils.alert_handler import _process_single_alert_config
    from alerts_format.alert_json_format import extract_alertname

    # 与 HTTP 线程共用实例锁，避免恢复通知和待观察发送交错。
    current = trend_gate.get_state(key)
    if not current or current['status'] not in ('pending', 'sent'):
        return
    data = json.loads(current['payload']) if isinstance(current['payload'], str) else current['payload']
    try:
        decision = trend_gate.decide(
            data, current['first_seen'].replace(tzinfo=timezone.utc).timestamp()
        )
    except Exception:
        logger.exception("待观察告警指标查询失败，按原流程发送: %s", key)
        decision = trend_gate.Decision('send', None, '指标查询失败，按原流程发送')
    trend_gate.log_decision(key, decision)

    if current['status'] == 'sent':
        prior = current.get('last_value')
        worsened = (decision.action == 'send' and decision.value is not None
                    and prior is not None and decision.value >= prior * 1.25
                    and decision.reason != '指标样本不足，按原流程发送')
        if not worsened:
            trend_gate.schedule_next(key, '已通知，持续监测恶化', TREND_SENT_CHECK_SECONDS)
            return
        with db_cursor(dictionary=True) as (conn, cursor):
            cursor.execute('SELECT * FROM alert_config WHERE id=%s', (current['config_id'],))
            config_row = cursor.fetchone()
        if not config_row:
            trend_gate.mark_resolved(key, '原路由配置已删除')
            return
        send_data = trend_gate.with_decision_note(data, decision)
        response = _process_single_alert_config(
            send_data, config_row, extract_alertname(data), feishu_client,
            mention_oncall=trend_gate.oncall_mention_policy(decision, prior),
        )
        if response and response.get('message_id'):
            trend_gate.mark_sent(key, decision.value, decision.reason)
        else:
            trend_gate.schedule_next(key, '升级发送失败，等待重试', TREND_SENT_CHECK_SECONDS)
        return

    prior = current.get('last_value')
    if (current.get('last_sent_at') and prior is not None
            and (decision.value is None or decision.value < prior * 1.25)):
        trend_gate.restore_sent(key, '升级已缓解，保留原通知')
        return

    if decision.action == 'cancel':
        trend_gate.mark_resolved(key, decision.reason)
        return
    if decision.action == 'observe':
        trend_gate.schedule_next(key, decision.reason)
        return

    with db_cursor(dictionary=True) as (conn, cursor):
        cursor.execute('SELECT * FROM alert_config WHERE id=%s', (current['config_id'],))
        config_row = cursor.fetchone()
    if not config_row:
        trend_gate.mark_resolved(key, '原路由配置已删除')
        return
    send_data = trend_gate.with_decision_note(data, decision)
    response = _process_single_alert_config(
        send_data, config_row, extract_alertname(data), feishu_client,
        mention_oncall=trend_gate.oncall_mention_policy(decision, prior),
    )
    if response and response.get('message_id'):
        trend_gate.mark_sent(key, decision.value, decision.reason)
    else:
        trend_gate.schedule_next(key, '发送失败，等待重试')


def start_trend_worker(feishu_client):
    if not Config.TREND_GATE_ENABLED:
        return

    def loop():
        while True:
            try:
                run_pending_once(feishu_client)
            except Exception:
                logger.exception("趋势告警复查循环失败")
            time.sleep(TREND_CHECK_SECONDS)

    thread = threading.Thread(target=loop, name='trend-alert-worker', daemon=True)
    thread.start()
    logger.info("趋势告警复查线程已启动，间隔 %s 秒", TREND_CHECK_SECONDS)
