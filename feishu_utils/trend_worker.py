"""持久化趋势告警的周期复查。当前部署为单副本，进程重启后从数据库恢复待观察记录。"""

import json
import logging
import threading
import time
from datetime import timezone

from config.constants import (
    TREND_CHECK_SECONDS, TREND_SENT_CHECK_SECONDS,
    TREND_LOG_RETENTION_DAYS, TREND_LOG_CLEANUP_SECONDS,
)
from db.pool import db_cursor
from feishu_utils import trend_gate, trend_digest
from utils.alert_trace import route_context, set_maid

logger = logging.getLogger(__name__)
_last_log_cleanup_at = None


def cleanup_decision_logs():
    """复用复查线程每小时清理一次；失败也限频，避免每轮重试拖慢告警。"""
    global _last_log_cleanup_at
    now = time.monotonic()
    if _last_log_cleanup_at is not None and now - _last_log_cleanup_at < TREND_LOG_CLEANUP_SECONDS:
        return
    _last_log_cleanup_at = now
    if TREND_LOG_RETENTION_DAYS < 1:
        logger.error('趋势日志保留天数无效，跳过清理: days=%s', TREND_LOG_RETENTION_DAYS)
        return
    try:
        with db_cursor() as (conn, cursor):
            cursor.execute(
                'DELETE FROM alert_trend_decision_log '
                'WHERE created_at < UTC_TIMESTAMP() - INTERVAL %s DAY',
                (TREND_LOG_RETENTION_DAYS,),
            )
            conn.commit()
            logger.info('趋势决策日志清理完成: retention_days=%s rows=%s',
                        TREND_LOG_RETENTION_DAYS, cursor.rowcount)
    except Exception:
        logger.exception('趋势决策日志清理失败')


def run_pending_once(feishu_client):
    if not trend_gate.trends_enabled():
        return
    for row in trend_gate.due_active():
        key = (row['rule_uid'], row['group_id'], row['fingerprint'])
        payload = row.get('payload') or {}
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except ValueError:
                # 仅建立错误日志上下文；实际处理仍读取原值并报错、安排重试。
                payload = {}
        if not isinstance(payload, dict):
            payload = {}
        if not payload:
            payload = {'alerts': [{'fingerprint': key[2]}]}
        with route_context(payload, key[1]):
            try:
                with trend_gate.lock_for(key):
                    _process_pending_row(key, feishu_client)
            except Exception:
                logger.exception("待观察告警复查失败: %s", key)
                try:
                    trend_gate.schedule_next(key, '复查异常，等待重试')
                except Exception:
                    logger.exception("更新待观察告警重试时间失败: %s", key)
    # 无新策略时不产生额外群消息，保持旧试点的发送行为。
    if trend_gate.enabled_policies() or trend_gate.global_policy():
        trend_digest.update_digests(feishu_client)
        cleanup_decision_logs()


def _process_pending_row(key, feishu_client):
    # 与 HTTP 线程共用实例锁，避免恢复通知和待观察发送交错。
    current = trend_gate.get_state(key)
    if not current or current['status'] not in ('pending', 'sent'):
        return
    data = json.loads(current['payload']) if isinstance(current['payload'], str) else current['payload']
    maid = trend_gate.state_maid(current, key[1])
    if maid:
        data.setdefault('_route_maids', {})[key[1]] = maid
        # 外层负责异常与重试日志，也要沿用旧卡片的 MAID。
        set_maid(maid)
    with route_context(data, key[1]):
        logger.info('event=trend.recheck rule_uid=%s state=%s cancel_streak=%s',
                    key[0], current['status'], current.get('cancel_streak', 0))
        _process_pending_state(key, feishu_client, current, data)


def _process_pending_state(key, feishu_client, current, data):
    from feishu_utils.alert_handler import _process_single_alert_config
    from alerts_format.alert_json_format import extract_alertname

    with db_cursor(dictionary=True) as (conn, cursor):
        cursor.execute('SELECT * FROM alert_config WHERE id=%s', (current['config_id'],))
        config_row = cursor.fetchone()
    policy = trend_gate.policy_for_route(config_row, data) if config_row else None
    try:
        if (config_row and (config_row.get('trend_policy') is not None
                            or trend_gate.Config.TREND_GATE_MODE != 'legacy')
                and policy is None):
            decision = trend_gate.Decision('send', None, '路由趋势策略未启用，按原流程发送')
        else:
            decision = trend_gate.decide(
                data, current['first_seen'].replace(tzinfo=timezone.utc).timestamp(), policy
            )
    except trend_gate.RuleDeletedError:
        reason = 'Grafana规则已删除，停止后台复查（非指标恢复）'
        # 仅后台关闭旧观察记录；不发恢复卡片，不修改原告警/静默记录。
        trend_gate.mark_resolved(key, reason)
        trend_gate.log_decision(key, trend_gate.Decision('cancel', None, reason))
        logger.warning('event=trend.rule.deleted rule_uid=%s previous_state=%s action=stop_recheck',
                       key[0], current['status'])
        return
    except Exception:
        logger.exception("待观察告警指标查询失败，按原流程发送: %s", key)
        decision = trend_gate.Decision('send', None, '指标查询失败，按原流程发送')
    trend_gate.log_decision(key, decision)

    if current['status'] == 'sent':
        prior = current.get('last_value')
        worsened = (decision.action == 'send'
                    and (trend_gate.grade_upgrade(decision, current)
                         or (trend_gate.is_escalation(decision.value, prior, decision.direction)
                             and decision.reason != '指标样本不足，按原流程发送')))
        if not worsened:
            trend_gate.schedule_next(key, '已通知，持续监测恶化', TREND_SENT_CHECK_SECONDS)
            return
        if not config_row:
            trend_gate.mark_resolved(key, '原路由配置已删除')
            return
        send_data = trend_gate.with_decision_note(data, decision)
        response = _process_single_alert_config(
            send_data, config_row, extract_alertname(data), feishu_client,
            mention_oncall=trend_gate.oncall_mention_policy(decision, prior),
        )
        if response and response.get('message_id'):
            trend_gate.mark_sent_decision(key, decision)
        else:
            trend_gate.schedule_next(key, '升级发送失败，等待重试', TREND_SENT_CHECK_SECONDS)
        return

    prior = current.get('last_value')
    if (current.get('last_sent_at')
            and not trend_gate.grade_upgrade(decision, current)
            and trend_gate.is_alleviated(decision.value, prior, decision.direction)):
        trend_gate.restore_sent(key, '升级已缓解，保留原通知')
        return

    if decision.action == 'cancel':
        # NULL 策略仍执行旧试点的立即取消行为；新策略才启用连续确认。
        streak = current.get('cancel_streak', 0) + 1
        if policy is None or streak >= policy.confirm_cycles:
            trend_gate.mark_resolved(key, decision.reason)
        else:
            trend_gate.schedule_next(
                key, f'恢复待确认({streak}/{policy.confirm_cycles})', cancel_streak=streak)
        return
    if decision.action == 'observe':
        trend_gate.schedule_next(key, decision.reason)
        return

    if not config_row:
        trend_gate.mark_resolved(key, '原路由配置已删除')
        return
    send_data = trend_gate.with_decision_note(data, decision)
    response = _process_single_alert_config(
        send_data, config_row, extract_alertname(data), feishu_client,
        mention_oncall=trend_gate.oncall_mention_policy(decision, prior),
    )
    if response and response.get('message_id'):
        trend_gate.mark_sent_decision(key, decision)
    else:
        trend_gate.schedule_next(key, '发送失败，等待重试')


def start_trend_worker(feishu_client):
    # 复用原有唯一复查线程；运行时新增策略也能在缓存刷新后生效。
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
