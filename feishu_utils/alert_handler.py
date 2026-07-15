#!/usr/bin/env python3
"""
告警处理模块
处理来自 Alertmanager 的告警请求
"""

import hashlib
import json
import logging
import threading
import time

from config.config import Config

# ── 告警去重缓存（基于 fingerprint+status 哈希）──
_alert_dedup_cache: dict[str, float] = {}
_alert_dedup_lock = threading.Lock()
_ALERT_DEDUP_TTL = 300  # 秒（5分钟）：防止 Grafana repeat_interval 重复投递同一 firing 告警

# ── resolved 去重缓存（30 分钟 TTL，覆盖多个 Grafana repeat_interval 周期）──
_resolved_dedup_cache: dict[str, float] = {}
_resolved_dedup_lock = threading.Lock()
_RESOLVED_DEDUP_TTL = 1800  # 秒（30分钟）：防止 Grafana repeat_interval 重复投递同一 resolved 告警

# ── alertname+labels 维度去重缓存 ──
# Grafana 不同评估周期的 fingerprint 可能不同（value 变化导致），
# 单靠 fingerprint 去重无法拦截"同一告警规则重复触发"的情况。
# 额外维护一个基于 alertname + group_id 的去重维度，
# TTL 与 _ALERT_DEDUP_TTL 一致，确保同一告警在冷却期内不重复发送。
_alert_label_dedup_cache: dict[str, float] = {}
_alert_label_dedup_lock = threading.Lock()


def _make_dedup_key(data: dict) -> str:
    """用所有 firing alert 的 fingerprint 生成去重 key（仅用于 firing 批次）"""
    pairs = sorted(
        a.get('fingerprint', '')
        for a in data.get('alerts', [])
        if a.get('status') == 'firing'
    )
    raw = json.dumps(pairs, ensure_ascii=False, sort_keys=True)
    return hashlib.md5(raw.encode()).hexdigest()


def _make_label_dedup_key(data: dict, group_id: str) -> str:
    """用 alertname + group_id 生成语义级去重 key（仅用于 firing 批次）。

    fingerprint 去重只能拦截 Grafana repeat_interval 的完全相同重发，
    但不同评估周期因 value 变化导致 fingerprint 不同时，同一告警仍会重复发送。
    本 key 基于 alertname + group_id，确保同一告警规则在同一群组的冷却期内不重复。
    """
    alertname = ''
    common_labels = data.get('commonLabels', {})
    if isinstance(common_labels, dict):
        alertname = common_labels.get('alertname', '')
    if not alertname:
        alerts = data.get('alerts', [])
        if alerts:
            alertname = alerts[0].get('labels', {}).get('alertname', '')
    raw = json.dumps({'alertname': alertname, 'group_id': group_id}, ensure_ascii=False, sort_keys=True)
    return hashlib.md5(raw.encode()).hexdigest()


def _is_all_resolved(data: dict) -> bool:
    """判断是否为纯 resolved 批次。

    关键：必须同时满足两个条件：
    1. 原始顶层 status='resolved'（Grafana 主动发送的恢复通知）
    2. 所有 alert 的 status 都是 resolved

    当 Grafana 未开启恢复通知时，顶层 status='firing'，即使批次中混有 resolved
    实例（或拆分后产生的 resolved 子批次），也不应触发恢复通知路径。
    """
    original_status = data.get('_original_status', data.get('status', ''))
    alerts = data.get('alerts', [])
    return (original_status == 'resolved'
            and bool(alerts)
            and all(a.get('status') == 'resolved' for a in alerts))


def _clear_dedup_for_resolved(data: dict) -> None:
    """resolved 批次到来时，清除对应 fingerprint 组合的 firing 去重缓存
    防止 firing→resolved→firing 在 5 分钟内第二次 firing 被拦截"""
    # 用 resolved 批次的 fingerprint 重建一个假设的 firing key——即如果这些指纹全部 firing 时的 key
    pairs = sorted(a.get('fingerprint', '') for a in data.get('alerts', []))
    raw = json.dumps(pairs, ensure_ascii=False, sort_keys=True)
    key = hashlib.md5(raw.encode()).hexdigest()
    with _alert_dedup_lock:
        _alert_dedup_cache.pop(key, None)


def _make_resolved_dedup_key(data: dict) -> str:
    """用所有 resolved alert 的 fingerprint 生成 resolved 去重 key"""
    pairs = sorted(
        a.get('fingerprint', '')
        for a in data.get('alerts', [])
        if a.get('status') == 'resolved'
    )
    raw = json.dumps(pairs, ensure_ascii=False, sort_keys=True)
    return hashlib.md5(raw.encode()).hexdigest()


def _clear_resolved_dedup_for_firing(data: dict) -> None:
    """firing 批次到来时，清除对应 fingerprint 的 resolved 去重缓存
    防止 resolved→firing→resolved 在 30 分钟内第二次 resolved 被拦截"""
    pairs = sorted(
        a.get('fingerprint', '')
        for a in data.get('alerts', [])
        if a.get('status') == 'firing'
    )
    raw = json.dumps(pairs, ensure_ascii=False, sort_keys=True)
    key = hashlib.md5(raw.encode()).hexdigest()
    with _resolved_dedup_lock:
        _resolved_dedup_cache.pop(key, None)


def _is_duplicate(key: str, cache: dict = None, lock: threading.Lock = None,
                  ttl: int = _ALERT_DEDUP_TTL) -> bool:
    """通用去重检查，支持自定义缓存、锁和 TTL

    Args:
        key: 去重 key
        cache: 缓存字典，默认使用 _alert_dedup_cache
        lock: 线程锁，默认使用 _alert_dedup_lock
        ttl: 缓存 TTL（秒），默认 5 分钟
    """
    if cache is None:
        cache = _alert_dedup_cache
    if lock is None:
        lock = _alert_dedup_lock
    now = time.time()
    with lock:
        # 清理过期记录
        expired = [k for k, t in cache.items() if now - t > ttl]
        for k in expired:
            del cache[k]
        if key in cache:
            return True
        cache[key] = now
        return False


def _evict_dedup(key: str, cache: dict = None, lock: threading.Lock = None) -> None:
    """撤销去重缓存中的 key，用于处理失败后允许 Grafana 重试。

    _is_duplicate() 在处理前就写入缓存，若处理失败（无匹配配置、发送异常等），
    缓存残留会阻止 Grafana 的后续重试投递，导致恢复告警永久丢失。
    本函数在各失败返回路径调用，撤销对应的缓存条目。
    """
    if cache is None:
        cache = _alert_dedup_cache
    if lock is None:
        lock = _alert_dedup_lock
    if not key:
        return
    with lock:
        cache.pop(key, None)
from alerts_format.flashcat_utils import get_oncall_open_ids, send_phone_alert, create_phone_incident
from alerts_format.alert_json_format import (
    extract_all_labels,
    extract_alertids,
    extract_alertname,
    extract_alert_raw,
    extract_grafana_urls,
    extract_fingerprints,
    alert_data_api,
)
from alerts_format.db_utils import (
    get_alert_config_by_labels,
    get_alert_config_by_alertid
)
from alerts_format.savedb import (
    update_message_id,
    update_incident_id,
    save_card_content,
    get_message_id_by_fingerprint,
    get_alerttime_by_fingerprint,
    get_all_fingerprints_by_fingerprint,
)
from feishu_utils.event_handler import alert_to_feishu
from feishu_utils.alert_card_biz import build_biz_firing_card, build_biz_resolved_card

logger = logging.getLogger(__name__)


def _split_by_alert(data: dict) -> list:
    """
    将批量 payload 拆分为单条 alert 的子 payload 列表，用于独立路由。

    当 Grafana 将多个不同 tenant 的告警打包到同一批次时，commonLabels 中不含 tenant，
    导致 extract_all_labels 只能取第一条 alert 的 tenant 进行路由，造成路由错误。
    拆分后每条 alert 独立路由，避免被错误投递到其他群组。

    关键：记录原始批次的顶层 status。Grafana 未开启恢复通知时，顶层 status='firing'，
    批次中可能混有 resolved 实例。拆分后这些 resolved 子批次不应触发恢复通知。
    """
    alerts = data.get('alerts', [])
    if len(alerts) <= 1:
        return [data]
    # 记录原始顶层状态（Grafana webhook 顶层 status：firing / resolved）
    original_status = data.get('status', '')
    sub_payloads = []
    for alert in alerts:
        sub = dict(data)
        sub['alerts'] = [alert]
        # 保留原始顶层状态，供 _is_all_resolved 判断使用
        sub['_original_status'] = original_status
        # 用该 alert 自身的 labels 覆盖 commonLabels，保证路由时拿到正确的 tenant 等标签
        sub['commonLabels'] = {k: v for k, v in alert.get('labels', {}).items()}
        sub_payloads.append(sub)
    return sub_payloads


def _group_and_aggregate_by_alertname(sub_payloads: list) -> list:
    """将拆分后的子 payload 按 alertname 聚合，同 alertname 的 firing 子 payload 合并为一个批次。

    解决问题：Grafana 将同一告警规则下多个 pod 的实例打包到同一批次，
    拆分后每个 pod 独立路由导致发送多条卡片。聚合后同 alertname 的 firing
    实例合并为一张卡片发送。

    仅聚合 firing 子批次；resolved 子批次（原始顶层 status=resolved）保持独立。
    """
    firing_groups: dict[str, list] = {}   # alertname -> [sub_payload, ...]
    resolved_payloads: list = []

    for sub in sub_payloads:
        original_status = sub.get('_original_status', '')
        alert_status = sub.get('alerts', [{}])[0].get('status', '')

        # 只有原始顶层 status=resolved 的 resolved 子批次才走恢复通知路径，保持独立
        if original_status == 'resolved' and alert_status == 'resolved':
            resolved_payloads.append(sub)
            continue

        # firing 子批次（含原始顶层 firing 中的 resolved 实例）按 alertname + tenant 聚合
        # 仅 alertname 相同但 tenant 不同时不能合并，否则路由匹配会只取第一条 alert
        # 的 tenant，导致另一个 tenant 的告警被路由到错误的群组。
        alertname = sub.get('commonLabels', {}).get('alertname', '')
        tenant = sub.get('commonLabels', {}).get('tenant', '')
        if not alertname:
            # 无 alertname 无法聚合，保持独立
            firing_groups.setdefault(f'__no_name_{id(sub)}', []).append(sub)
        else:
            group_key = f'{alertname}||{tenant}'
            firing_groups.setdefault(group_key, []).append(sub)

    aggregated = []
    for name, group in firing_groups.items():
        if len(group) == 1:
            aggregated.append(group[0])
        else:
            # 合并同 alertname 的子 payload
            merged = dict(group[0])
            merged['alerts'] = []
            for sub in group:
                merged['alerts'].extend(sub.get('alerts', []))
            # 合并 commonLabels：取所有子批次的交集（相同 key 且相同 value 才保留），
            # 避免首个子批次的特有标签（如不同 model）被误当作公共标签
            sub_label_dicts = [sub.get('commonLabels', {}) for sub in group]
            merged_common = {}
            if sub_label_dicts:
                first = sub_label_dicts[0]
                for k, v in first.items():
                    if all(d.get(k) == v for d in sub_label_dicts[1:]):
                        merged_common[k] = v
            merged['commonLabels'] = merged_common
            # 标记为已聚合，防止递归调用 process_alert_request 时再次拆分
            merged['_aggregated'] = True
            # name 格式为 "alertname||tenant"，拆分后分别记录
            parts = name.split('||', 1)
            log_name = parts[0] if parts else name
            log_tenant = parts[1] if len(parts) > 1 else ''
            logger.info("聚合同 alertname '%s' tenant='%s' 的 %d 个子批次",
                        log_name, log_tenant, len(group))
            aggregated.append(merged)

    return aggregated + resolved_payloads


def process_alert_request(data, feishu_client):
    """
    处理告警请求
    
    Args:
        data: 告警请求数据
        feishu_client: 飞书客户端实例
    
    Returns:
        tuple: (response_dict, status_code)
    """
    try:
        active_dedup_key = None
        active_dedup_cache = None
        active_dedup_lock = None

        # 参数验证
        if not data:
            logger.error("请求体不能为空")
            return {"code": 400, "msg": "请求体不能为空"}, 400

        # 若批次中含多条 alert，拆分为单条子 payload 分别路由，再按 alertname 聚合
        # 已聚合的批次（_aggregated=True）跳过拆分，直接走单批次处理流程
        sub_payloads = _split_by_alert(data) if not data.get('_aggregated') else [data]
        if len(sub_payloads) > 1:
            # 按 alertname 聚合同名 firing 子批次，减少发送的卡片数量
            sub_payloads = _group_and_aggregate_by_alertname(sub_payloads)
            logger.info("批次含 %d 条 alert，拆分+聚合后 %d 个子批次独立路由",
                        len(data.get('alerts', [])), len(sub_payloads))
            all_responses = []
            all_failed = 0
            all_total = 0
            for sub in sub_payloads:
                resp, _ = process_alert_request(sub, feishu_client)
                summary = resp.get('summary', {})
                all_total += summary.get('total', 1)
                all_failed += summary.get('failed', 0)
                if 'data' in resp:
                    all_responses.extend(resp['data'])
            success_count = all_total - all_failed
            return {
                "code": 0,
                "msg": "success" if all_failed == 0 else f"部分成功 ({success_count}/{all_total})",
                "data": all_responses,
                "summary": {"total": all_total, "success": success_count, "failed": all_failed}
            }, 200

        # 去重逻辑（第一层：fingerprint 级别）：
        # - firing 批次：5 分钟内相同 fingerprint 组合只处理一次（防 Grafana repeat_interval 重复投递）
        # - resolved 批次：30 分钟内相同 fingerprint 组合只处理一次（防 Grafana repeat_interval 重复投递恢复通知）
        # - 双向清除：resolved 到来时清 firing 缓存（防 firing→resolved→firing 漏发），
        #   firing 到来时清 resolved 缓存（防 resolved→firing→resolved 漏发恢复通知）
        if _is_all_resolved(data):
            _clear_dedup_for_resolved(data)
            resolved_key = _make_resolved_dedup_key(data)
            if _is_duplicate(resolved_key, cache=_resolved_dedup_cache,
                             lock=_resolved_dedup_lock, ttl=_RESOLVED_DEDUP_TTL):
                logger.info("恢复告警重复，已跳过 (resolved_dedup_key=%s)", resolved_key)
                return {"code": 0, "msg": "duplicate, skipped"}, 200
            active_dedup_key = resolved_key
            active_dedup_cache = _resolved_dedup_cache
            active_dedup_lock = _resolved_dedup_lock
        else:
            # firing 到来时清除对应 fingerprint 的 resolved 缓存
            _clear_resolved_dedup_for_firing(data)
            dedup_key = _make_dedup_key(data)
            if _is_duplicate(dedup_key):
                logger.info("告警重复，已跳过 (dedup_key=%s)", dedup_key)
                return {"code": 0, "msg": "duplicate, skipped"}, 200
            active_dedup_key = dedup_key
            active_dedup_cache = _alert_dedup_cache
            active_dedup_lock = _alert_dedup_lock

        # 查找匹配的告警配置
        configs = _find_alert_configs(data)
        
        # 未找到任何配置，返回404
        if not configs:
            logger.error("未找到任何匹配的告警配置")
            _evict_dedup(active_dedup_key, cache=active_dedup_cache, lock=active_dedup_lock)
            return {
                "error": "未找到匹配的告警配置",
                "alertids": extract_alertids(data),
                "labels": extract_all_labels(data)
            }, 404
        
        # 提取 alertname 作为标题
        alertname = extract_alertname(data)

        # resolved 批次到来时，清除对应的语义去重缓存
        # 防止 firing→resolved→firing 中第二轮 firing 被语义去重拦截
        if _is_all_resolved(data):
            for config_row in configs:
                gid = config_row.get('group_id', '')
                label_key = _make_label_dedup_key(data, gid)
                _evict_dedup(label_key, cache=_alert_label_dedup_cache,
                             lock=_alert_label_dedup_lock)

        # 去重逻辑（第二层：alertname+group_id 语义级别，仅 firing）
        # fingerprint 去重只能拦截完全相同的重发，但 Grafana 不同评估周期
        # 因 value 变化导致 fingerprint 不同时，同一告警仍会重复发送。
        # 此处基于 alertname+group_id 做语义去重，确保同一告警规则在同一群组
        # 的冷却期内不重复发送。
        label_keys_to_evict = []
        label_dedup_skipped = set()  # 被语义去重跳过的 config 索引集合
        if not _is_all_resolved(data):
            for idx, config_row in enumerate(configs):
                gid = config_row.get('group_id', '')
                label_key = _make_label_dedup_key(data, gid)
                if _is_duplicate(label_key, cache=_alert_label_dedup_cache,
                                 lock=_alert_label_dedup_lock, ttl=_ALERT_DEDUP_TTL):
                    logger.info("告警语义重复 (alertname='%s', group_id='%s')，跳过该路由",
                                alertname, gid)
                    label_dedup_skipped.add(idx)
                else:
                    label_keys_to_evict.append(label_key)
            if len(label_dedup_skipped) == len(configs):
                # 所有路由都命中语义去重，跳过整个批次
                _evict_dedup(active_dedup_key, cache=active_dedup_cache, lock=active_dedup_lock)
                return {"code": 0, "msg": "duplicate (alertname), skipped"}, 200
        
        # 处理每个匹配的配置
        responses = []
        failed_count = 0
        
        # 有效路由数 = 总路由数 - 被语义去重跳过的路由数
        effective_total = len(configs) - len(label_dedup_skipped)
        logger.info("开始处理告警，共匹配 %d 个路由（%d 个被语义去重跳过）",
                    len(configs), len(label_dedup_skipped))
        
        for idx, config_row in enumerate(configs):
            if idx in label_dedup_skipped:
                continue
            try:
                logger.info("处理路由 [%d/%d]: group_id=%s", 
                           idx + 1, len(configs), config_row.get('group_id'))
                
                # 处理单个配置的告警
                response = _process_single_alert_config(
                    data, 
                    config_row, 
                    alertname, 
                    feishu_client
                )
                
                if response:
                    responses.append(response)
                else:
                    # 记录失败但继续处理其他路由
                    failed_count += 1
                    logger.error("路由 [%d/%d] 发送失败: group_id=%s", 
                               idx + 1, len(configs), config_row.get('group_id'))
                    responses.append({
                        'alert_id': config_row.get('alert_id'),
                        'group_id': config_row.get('group_id'),
                        'success': False,
                        'error': '发送失败'
                    })
                    
            except Exception as e:
                # 记录异常但继续处理其他路由
                failed_count += 1
                logger.error("路由 [%d/%d] 处理异常: %s", idx + 1, len(configs), str(e), exc_info=True)
                responses.append({
                    'alert_id': config_row.get('alert_id'),
                    'group_id': config_row.get('group_id'),
                    'success': False,
                    'error': str(e)
                })
        
        # 统计结果
        success_count = effective_total - failed_count
        logger.info("告警处理完成: 成功 %d/%d, 失败 %d/%d", 
                   success_count, effective_total, failed_count, effective_total)
        
        # 如果所有路由都失败，撤销所有去重缓存以允许 Grafana 重试
        if effective_total > 0 and failed_count == effective_total:
            logger.error("所有路由发送失败，撤销去重缓存以允许 Grafana 重试 (key=%s)", active_dedup_key)
            _evict_dedup(active_dedup_key, cache=active_dedup_cache, lock=active_dedup_lock)
            for lk in label_keys_to_evict:
                _evict_dedup(lk, cache=_alert_label_dedup_cache, lock=_alert_label_dedup_lock)
            return {
                "code": 500, 
                "msg": "所有路由发送失败", 
                "data": responses
            }, 500
        
        # 如果部分成功，返回200但包含失败信息
        return {
            "code": 0, 
            "msg": "success" if failed_count == 0 else f"部分成功 ({success_count}/{effective_total})",
            "data": responses,
            "summary": {
                "total": effective_total,
                "success": success_count,
                "failed": failed_count
            }
        }, 200
        
    except Exception as e:
        logger.error("处理告警请求失败: %s", e, exc_info=True)
        _evict_dedup(active_dedup_key, cache=active_dedup_cache, lock=active_dedup_lock)
        return {"code": 500, "msg": str(e)}, 500


def _find_alert_configs(data):
    """
    查找匹配的告警配置
    
    Args:
        data: 告警数据
    
    Returns:
        list: 匹配的配置列表
    """
    configs = []
    all_labels = extract_all_labels(data)
    
    # 1. 尝试通过标签匹配查询（现在返回所有匹配的配置）
    if all_labels:
        logger.info("尝试通过标签匹配查询，提取到的标签： %s", all_labels)
        matched_configs = get_alert_config_by_labels(all_labels)
        
        if matched_configs:
            configs.extend(matched_configs)
            logger.info("通过标签匹配查询，查询到 %d 个配置", len(matched_configs))
            for config in matched_configs:
                logger.info("  - 匹配路由: alert_id=%s, group_id=%s, label_rules=%s", 
                           config.get('alert_id'), 
                           config.get('group_id'),
                           config.get('label_rules'))
        else:
            logger.info("通过标签匹配查询，未查询到配置")
    
    # 2. 如果通过标签匹配未查询到配置，尝试通过alertid匹配查询配置
    if not configs:
        alertids = extract_alertids(data)
        logger.info("尝试通过alertid匹配查询，提取到的alertid： %s", alertids)
        
        if alertids:
            for alertid in alertids:
                config_row = get_alert_config_by_alertid(alertid)
                if config_row:
                    configs.append(config_row)
            
            if configs:
                logger.info("通过alertid匹配查询，查询到的配置： %s", configs)
            else:
                logger.info("通过alertid匹配查询，未查询到配置")
    
    return configs


def _process_single_alert_config(data, config_row, alertname, feishu_client):
    """
    处理单个告警配置

    Args:
        data: 告警数据
        config_row: 配置行
        alertname: 告警名称
        feishu_client: 飞书客户端实例

    Returns:
        dict: 处理结果
    """
    # 解包 4-tuple（新签名）
    alerts, severities, maid, grafana_urls = alert_data_api(
        data,
        config_row.get('project'),
        config_row.get('alertmanager_url'),
        group_id=config_row.get('group_id'),
    )

    # 判断是否为 resolved 告警（原始顶层 status=resolved 且所有 alert 都是 resolved）
    is_resolved = _is_all_resolved(data)

    # 判断是否符合 @ 条件（恢复通知不艾特任何人，只在 firing 时艾特）
    rank = config_row.get('rank', '')
    is_phone_alert = any(s == 'phone' for s in severities)
    severity_matches = any(
        severity in [str(r) for r in rank.split(',')]
        for severity in severities
    )
    if is_resolved:
        # 恢复通知不艾特任何人
        mentioned_user_list = []
    elif is_phone_alert:
        # 电话告警：强制获取 oncall 值班人进行艾特（仅 firing 艾特，resolved 不艾特）
        mentioned_user_list = _get_oncall_mentioned_users(config_row)
        logger.info("检测到 phone 级别告警，oncall 艾特用户数 %d", len(mentioned_user_list))
    elif severity_matches:
        if config_row.get('oncall_sync'):
            mentioned_user_list = _get_oncall_mentioned_users(config_row)
        else:
            mentioned_user_list = json.loads(config_row['users']) if config_row.get('users') else []
        logger.info("符合@条件的告警级别 %s | 此告警的级别 %s | 艾特用户数 %d",
                    rank, severities, len(mentioned_user_list))
    else:
        mentioned_user_list = []

    # 确定告警级别
    alert_severity = _determine_alert_severity(severities)

    # 判断模板类型（默认 ops）
    template_type = config_row.get('template_type', 'ops')
    group_id = config_row['group_id']

    # phone 级别仅 firing 时触发电话，resolved 不打电话
    # 创建 Flashcat incident 以触发电话通知，返回 incident_id 用于卡片认领按钮
    incident_id = None
    if is_phone_alert and not is_resolved:
        logger.info("📞 触发电话告警（firing），创建 Flashcat incident")
        incident_id = _create_phone_incident(data, maid)

    # ---------- resolved 告警：尝试在话题中回复 ----------
    if is_resolved:
        fingerprints = extract_fingerprints(data)
        thread_message_id = ''
        for fp in fingerprints:
            mid = get_message_id_by_fingerprint(fp, group_id=group_id)
            if mid:
                thread_message_id = mid
                break

        # 部分恢复检测：Grafana 按实例维度发送 resolved 通知，每批只含部分实例。
        # 通过当前批次所有 fingerprint 反查所有关联 firing 记录中的全部 fingerprint，
        # 若汇总后的原始 fingerprint 集合未全部包含在当前 resolved 批次中，
        # 说明仍有实例在 firing，应跳过恢复通知，避免在告警未完全恢复时发送误导性的"已恢复"卡片。
        if fingerprints:
            all_original_fps = get_all_fingerprints_by_fingerprint(fingerprints, group_id=group_id)
            resolved_set = set(fingerprints)
            if all_original_fps and not resolved_set.issuperset(all_original_fps):
                remaining = [fp for fp in all_original_fps if fp not in resolved_set]
                logger.info(
                    "⏸ 部分恢复检测：原始 %d 个实例，当前 resolved %d 个，"
                    "仍有 %d 个实例未恢复，跳过恢复通知 group_id=%s",
                    len(all_original_fps), len(fingerprints), len(remaining), group_id
                )
                return {
                    'alert_id': config_row.get('alert_id'),
                    'group_id': group_id,
                    'success': True,
                    'skipped': True,
                    'reason': f'部分恢复，仍有 {len(remaining)} 个实例未恢复',
                }

        if template_type == 'biz':
            raw_alerts = extract_alert_raw(data)
            # 用 DB 中存储的实际触发时间覆盖 Grafana resolved 包中的 startsAt
            # （Grafana 在 resolved 通知里会将 startsAt 重置为恢复时间，导致持续时长为 0）
            for ra in raw_alerts:
                fp = ra.get('fingerprint', '')
                if fp:
                    db_start = get_alerttime_by_fingerprint(fp, group_id=group_id)
                    if db_start:
                        ra['startsAt'] = db_start
            common_labels = data.get('commonLabels', {})
            content = build_biz_resolved_card(alertname, raw_alerts, grafana_urls, common_labels, mentioned_user_list)
        else:
            string_alert_info = _build_alert_message(alerts)
            content = _build_ops_resolved_content(string_alert_info, alertname, mentioned_user_list)

        if not thread_message_id:
            # 找不到源消息，无法在话题中回复，跳过不发送
            logger.warning("⚠️ 未找到源消息，跳过恢复告警通知 group_id=%s fingerprints=%s", group_id, fingerprints)
            return {
                'alert_id': config_row.get('alert_id'),
                'group_id': group_id,
                'success': True,
                'skipped': True,
                'reason': '未找到源消息，无法在话题中回复',
            }

        try:
            feishu_client.reply_message(thread_message_id, 'interactive', content, reply_in_thread=True)
            logger.info("✅ 已在话题中回复恢复通知，原消息: %s", thread_message_id)
            return {'alert_id': config_row.get('alert_id'), 'group_id': group_id, 'success': True}
        except Exception as e:
            # 话题回复失败，不降级为新消息，避免恢复通知脱离上下文
            logger.error("❌ 话题回复失败，跳过恢复告警通知: %s", e)
            return {
                'alert_id': config_row.get('alert_id'),
                'group_id': group_id,
                'success': False,
                'error': f'话题回复失败: {e}',
            }

    # ---------- firing 告警（含混合状态）----------
    # firing 告警永远发新消息，不回复旧话题。
    # 原因：混合状态时若复用旧 message_id，会导致"恢复后再触发"的新告警
    # 被错误地回复到上一轮已结束的话题中。
    if template_type == 'biz':
        raw_alerts = extract_alert_raw(data)
        common_labels = data.get('commonLabels', {})
        content = build_biz_firing_card(
            alertname, alert_severity, raw_alerts, grafana_urls, maid, common_labels, mentioned_user_list, incident_id
        )
        if not content:
            logger.info("biz firing 卡片无 firing 实例，跳过发送 group_id=%s", group_id)
            return {'alert_id': config_row.get('alert_id'), 'group_id': group_id, 'success': True}
        try:
            message_id = feishu_client.send("chat_id", group_id, "interactive", content)
        except Exception as e:
            logger.error("biz 卡片发送失败: %s", e)
            message_id = ''
    else:
        # ops 模板走原有 alert_to_feishu
        string_alert_info = _build_alert_message(alerts)
        message_id = alert_to_feishu(
            feishu_client,
            string_alert_info,
            mentioned_user_list,
            group_id,
            alertname=alertname,
            severity=alert_severity,
            maid=maid,
            incident_id=incident_id,
        )
        # ops 模板卡片在 alert_to_feishu 内部构建，无 content 变量
        content = None

    if message_id:
        # 保存 message_id 供后续 resolved/静默话题回复
        if maid:
            update_message_id(maid, message_id)
            # 保存原始卡片 JSON，认领时原地更新卡片使用（仅 biz 模板）
            if incident_id and content:
                save_card_content(maid, content)
        logger.info("✅ 发送告警信息成功，群组: %s，级别: %s", group_id, alert_severity)
        return {
            'alert_id': config_row.get('alert_id'),
            'group_id': group_id,
            'message_id': message_id,
            'success': True,
        }
    else:
        logger.error("❌ 发送告警信息失败，群组: %s", group_id)
        return None


def _get_oncall_mentioned_users(config_row: dict) -> list:
    """从 Flashcat 获取当前 oncall 人员的飞书 open_id 列表

    当 alert_config.oncall_sync=1 时调用此函数替换静态 users 列表。

    优先使用 config_row 中的 flashcat_schedule_id，否则回退到全局配置
    FLASHCAT_SCHEDULE_ID。

    Args:
        config_row: alert_config 数据库行

    Returns:
        list: 飞书 open_id 列表；配置缺失或 API 调用失败时返回空列表
    """
    app_key = Config.FLASHCAT_APP_KEY
    schedule_id_str = config_row.get('flashcat_schedule_id') or Config.FLASHCAT_SCHEDULE_ID

    if not app_key:
        logger.warning("oncall_sync 已启用但 FLASHCAT_APP_KEY 未配置，跳过 oncall 艾特")
        return []
    if not schedule_id_str:
        logger.warning("oncall_sync 已启用但未配置 flashcat_schedule_id 或 FLASHCAT_SCHEDULE_ID，跳过 oncall 艾特")
        return []

    try:
        schedule_id = int(schedule_id_str)
    except (ValueError, TypeError):
        logger.error("flashcat_schedule_id '%s' 不是有效整数，跳过 oncall 艾特", schedule_id_str)
        return []

    return get_oncall_open_ids(app_key, schedule_id)


def _create_phone_incident(data: dict, maid: str) -> str:
    """创建 Flashcat incident 以触发电话告警，并将 incident_id 入库

    通过 incident/create API 创建 Critical 级别 incident，Flashcat 会根据
    channel 通知策略触发电话通知。创建成功后将 incident_id 写入 alert_data 表。

    Args:
        data: 原始告警数据
        maid: 告警记录 ID，用于将 incident_id 写入 DB

    Returns:
        str: 创建成功返回 incident_id，失败返回 None
    """
    app_key = Config.FLASHCAT_APP_KEY
    channel_id = Config.FLASHCAT_CHANNEL_ID

    if not app_key:
        logger.error("FLASHCAT_APP_KEY 未配置，无法创建电话告警 incident")
        return None
    if not channel_id:
        logger.error("FLASHCAT_CHANNEL_ID 未配置，无法创建电话告警 incident")
        return None

    incident_id = create_phone_incident(data, app_key, channel_id)
    if incident_id:
        logger.info("📞 Flashcat incident 创建成功: incident_id=%s", incident_id)
        # 将 incident_id 入库，供后续认领按钮使用
        if maid:
            update_incident_id(maid, incident_id)
        return incident_id
    else:
        logger.error("📞 Flashcat incident 创建失败，回退到旧接口")
        # 回退到旧接口
        integration_key = Config.FLASHCAT_PHONE_INTEGRATION_KEY
        if integration_key:
            def _do_send_fallback():
                result = send_phone_alert(data, integration_key)
                if result:
                    logger.info("📞 电话告警（回退旧接口）已成功触发")
                else:
                    logger.error("📞 电话告警（回退旧接口）触发失败")
            t = threading.Thread(target=_do_send_fallback, daemon=True)
            t.start()
        return None


def _build_ops_resolved_content(string_alert_info: str, alertname: str, mentioned_user_list: list = None) -> str:
    """将 ops 格式告警信息包装成绿色恢复卡片 JSON"""
    import json as _json
    from feishu_utils.event_handler import _get_current_time
    elements = []
    if mentioned_user_list:
        mention_content = " ".join(f'<at id="{uid}"></at>' for uid in mentioned_user_list)
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": f"**📢 通知：** {mention_content}"}})
        elements.append({"tag": "hr"})
    elements += [
        {"tag": "div", "text": {"tag": "lark_md", "content": string_alert_info}},
        {"tag": "hr"},
        {"tag": "note", "elements": [{"tag": "plain_text", "content": f"⏰ 发送时间: {_get_current_time()}"}]},
    ]
    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"✅ {alertname} 已恢复"},
            "template": "green",
        },
        "elements": elements,
    }
    return _json.dumps(card, ensure_ascii=False)


def _build_alert_message(alerts):
    """
    构建告警消息
    
    Args:
        alerts: 告警列表
    
    Returns:
        str: 格式化的告警消息
    """
    string_alert_info = ""
    
    if alerts is not None:
        # 只过滤None值，保留空字符串作为空行分隔
        alert_lines = [
            alert for alert in alerts 
            if alert is not None and isinstance(alert, str)
        ]
        string_alert_info = "\n".join(alert_lines)
    
    if not string_alert_info.strip():
        string_alert_info = "No alert information available."
        logger.warning("No alert information available.")
    
    return string_alert_info


def _determine_alert_severity(severities):
    """
    确定告警级别（去重并取最高级别）
    
    Args:
        severities: 告警级别列表
    
    Returns:
        str: 最终告警级别
    """
    # 数字级别映射（1=最低，5=最高）
    numeric_severity_map = {
        "5": "critical",
        "4": "critical", 
        "3": "warning",
        "2": "info",
        "1": "info"
    }
    
    severity_priority = {
        "critical": 4, 
        "warning": 3, 
        "info": 2, 
        "success": 1,
        "resolved": 0,
        # P 级别：p0 最高，p3 最低
        "p0": 5,
        "p1": 4,
        "p2": 3,
        "p3": 2,
        # 电话告警，与 p0 同优先级
        "phone": 5,
    }
    
    alert_severity = "warning"  # 默认级别
    
    if severities:
        # 去重
        unique_severities = list(set(severities))
        logger.info("告警级别列表（去重后）: %s", unique_severities)
        
        # 从severities中选择优先级最高的级别
        max_priority = 0
        for sev in unique_severities:
            sev_str = str(sev)
            
            # 如果是数字，转换为对应的级别名称
            if sev_str.isdigit():
                sev_lower = numeric_severity_map.get(sev_str, "warning")
                logger.info("数字级别 %s 映射为 %s", sev_str, sev_lower)
            else:
                sev_lower = sev_str.lower()
            
            priority = severity_priority.get(sev_lower, 0)
            if priority > max_priority:
                max_priority = priority
                alert_severity = sev_lower
        
        logger.info("最终告警级别: %s (优先级: %d)", alert_severity, max_priority)
    
    return alert_severity

