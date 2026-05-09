#!/usr/bin/env python3
"""
告警处理模块
处理来自 Alertmanager 的告警请求
"""

import json
import logging

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
from alerts_format.savedb import update_message_id, get_message_id_by_fingerprint
from feishu_utils.event_handler import alert_to_feishu
from feishu_utils.alert_card_biz import build_biz_firing_card, build_biz_resolved_card

logger = logging.getLogger(__name__)


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
        # 参数验证
        if not data:
            logger.error("请求体不能为空")
            return {"code": 400, "msg": "请求体不能为空"}, 400
        
        # 查找匹配的告警配置
        configs = _find_alert_configs(data)
        
        # 未找到任何配置，返回404
        if not configs:
            logger.error("未找到任何匹配的告警配置")
            return {
                "error": "未找到匹配的告警配置",
                "alertids": extract_alertids(data),
                "labels": extract_all_labels(data)
            }, 404
        
        # 提取 alertname 作为标题
        alertname = extract_alertname(data)
        
        # 处理每个匹配的配置
        responses = []
        failed_count = 0
        
        logger.info("开始处理告警，共匹配 %d 个路由", len(configs))
        
        for idx, config_row in enumerate(configs, 1):
            try:
                logger.info("处理路由 [%d/%d]: group_id=%s", 
                           idx, len(configs), config_row.get('group_id'))
                
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
                               idx, len(configs), config_row.get('group_id'))
                    responses.append({
                        'alert_id': config_row.get('alert_id'),
                        'group_id': config_row.get('group_id'),
                        'success': False,
                        'error': '发送失败'
                    })
                    
            except Exception as e:
                # 记录异常但继续处理其他路由
                failed_count += 1
                logger.error("路由 [%d/%d] 处理异常: %s", idx, len(configs), str(e), exc_info=True)
                responses.append({
                    'alert_id': config_row.get('alert_id'),
                    'group_id': config_row.get('group_id'),
                    'success': False,
                    'error': str(e)
                })
        
        # 统计结果
        success_count = len(configs) - failed_count
        logger.info("告警处理完成: 成功 %d/%d, 失败 %d/%d", 
                   success_count, len(configs), failed_count, len(configs))
        
        # 如果所有路由都失败，返回500
        if failed_count == len(configs):
            return {
                "code": 500, 
                "msg": "所有路由发送失败", 
                "data": responses
            }, 500
        
        # 如果部分成功，返回200但包含失败信息
        return {
            "code": 0, 
            "msg": "success" if failed_count == 0 else f"部分成功 ({success_count}/{len(configs)})",
            "data": responses,
            "summary": {
                "total": len(configs),
                "success": success_count,
                "failed": failed_count
            }
        }, 200
        
    except Exception as e:
        logger.error("处理告警请求失败: %s", e, exc_info=True)
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
    )

    # 判断是否符合 @ 条件
    rank = config_row.get('rank', '')
    severity_matches = any(
        severity in [str(r) for r in rank.split(',')]
        for severity in severities
    )
    if severity_matches:
        mentioned_user_list = json.loads(config_row['users']) if config_row.get('users') else []
        logger.info("符合@条件的告警级别%s | 此告警的级别 %s", rank, severities)
    else:
        mentioned_user_list = []

    # 确定告警级别
    alert_severity = _determine_alert_severity(severities)

    # 判断模板类型（默认 ops）
    template_type = config_row.get('template_type', 'ops')
    group_id = config_row['group_id']
    grafana_url_cfg = config_row.get('grafana_url') or ''

    # 判断是否为 resolved 告警（所有 alert 都是 resolved）
    all_alerts = data.get('alerts', [])
    is_resolved = all_alerts and all(a.get('status') == 'resolved' for a in all_alerts)

    # ---------- resolved 告警：尝试在话题中回复 ----------
    if is_resolved:
        fingerprints = extract_fingerprints(data)
        thread_message_id = ''
        for fp in fingerprints:
            mid = get_message_id_by_fingerprint(fp)
            if mid:
                thread_message_id = mid
                break

        if template_type == 'biz':
            raw_alerts = extract_alert_raw(data)
            common_labels = data.get('commonLabels', {})
            content = build_biz_resolved_card(alertname, raw_alerts, grafana_urls, common_labels)
        else:
            string_alert_info = _build_alert_message(alerts)
            content = _build_ops_resolved_content(string_alert_info, alertname)

        if thread_message_id:
            try:
                feishu_client.reply_message(thread_message_id, 'interactive', content, reply_in_thread=True)
                logger.info("✅ 已在话题中回复恢复通知，原消息: %s", thread_message_id)
                return {'alert_id': config_row.get('alert_id'), 'group_id': group_id, 'success': True}
            except Exception as e:
                logger.warning("话题回复失败，降级为新消息: %s", e)

        # 无 thread_message_id 或回复失败时，发新消息
        message_id = feishu_client.send("chat_id", group_id, "interactive", content)
        logger.info("✅ 发送恢复通知（新消息）group_id=%s message_id=%s", group_id, message_id)
        return {'alert_id': config_row.get('alert_id'), 'group_id': group_id, 'success': bool(message_id)}

    # ---------- firing 告警 ----------
    if template_type == 'biz':
        raw_alerts = extract_alert_raw(data)
        common_labels = data.get('commonLabels', {})
        content = build_biz_firing_card(
            alertname, alert_severity, raw_alerts, grafana_urls, maid, common_labels, mentioned_user_list
        )
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
        )

    if message_id:
        # 保存 message_id 供后续 resolved/静默话题回复
        if maid:
            update_message_id(maid, message_id)
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


def _build_ops_resolved_content(string_alert_info: str, alertname: str) -> str:
    """将 ops 格式告警信息包装成绿色恢复卡片 JSON"""
    import json as _json
    from feishu_utils.event_handler import _get_current_time
    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"✅ {alertname} 已恢复"},
            "template": "green",
        },
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": string_alert_info}},
            {"tag": "hr"},
            {"tag": "note", "elements": [{"tag": "plain_text", "content": f"⏰ 发送时间: {_get_current_time()}"}]},
        ],
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
        "resolved": 0
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

