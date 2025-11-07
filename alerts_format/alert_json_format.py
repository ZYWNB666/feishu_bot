#!/usr/bin/env python
# -*- coding: utf-8 -*- 


from .savedb import save_dbdata

# 定义要过滤的label前缀
LABEL_FILTER_PREFIXES = [
    'feature_node_kubernetes_io_',
    'beta_kubernetes_io_',
    'nvidia_',
    'app_kubernetes_io_',
    'pod_template_hash',
    'pod_template_generation',
    'controller_revision_hash',
    'statefulset_kubernetes_io_pod_name'
]

def should_filter_label(label_key):
    """检查label是否应该被过滤"""
    for prefix in LABEL_FILTER_PREFIXES:
        if label_key.startswith(prefix):
            return True
    return False

def alert_data_api(alert_info_data, project, alertmanager_url):
    """
    处理告警数据并格式化
    :param alert_info_data: dict, alertmanager推送的json数据
    :param project: str, 项目名称
    :param alertmanager_url: str, alertmanager地址
    :return: tuple, (alerts列表, severities列表, maid)
    """
    dbid = save_dbdata(alert_info_data, project)
    alerts = []
    severities = []

    # 获取原始公共标签用于比较
    original_common_labels = alert_info_data.get('commonLabels', {})
    if isinstance(original_common_labels, str):
        original_common_labels = {}
    
    # 获取过滤后的公共标签，确保是字典类型，并排除alertid和过滤指定前缀
    common_labels = {k: v for k, v in original_common_labels.items() 
                     if k != 'alertid' and not should_filter_label(k)}

    # 添加公共信息 - 其他公共标签（alertname会作为标题，不在正文中显示）
    if common_labels:
        # 创建字典存放公共标签（除了alertname）
        other_labels = {k: v for k, v in common_labels.items() if k != 'alertname'}
        
        # 如果有其他公共标签，格式化为字符串
        if other_labels:
            for key, value in other_labels.items():
                alerts.append(f"{key}: {value}")

    # 添加Details标记
    alerts.append("✨✨✨✨✨✨✨✨✨✨✨✨")

    # 处理每个告警的特有信息
    for alert in alert_info_data.get('alerts', []):
        # 确保labels是字典类型
        alert_labels = alert.get('labels', {})
        if isinstance(alert_labels, str):
            alert_labels = {}

        # 获取该告警特有的标签（与原始commonLabels的差集），并排除alertid和过滤指定前缀
        specific_labels = {
            k: v for k, v in alert_labels.items()
            if k != 'alertid' and not should_filter_label(k) and (k not in original_common_labels or original_common_labels.get(k) != v)
        }

        # 添加状态
        alerts.append("🔥🔥🔥" if alert.get('status') == 'firing' else "✅✅✅")

        # 添加特有的标签
        for key, value in specific_labels.items():
            if value is not None:  # 只添加非None的值
                alerts.append(f"{key}: {value}")

        # 添加annotations中的信息
        alert_annotations = alert.get('annotations', {})
        if isinstance(alert_annotations, dict):
            # 添加description
            if 'description' in alert_annotations:
                alerts.append(f"description: {alert_annotations['description']}")
            # 添加summary
            if 'summary' in alert_annotations:
                alerts.append(f"summary: {alert_annotations['summary']}")

        # 根据告警状态添加时间信息
        if alert.get('status') == 'resolved' and alert.get('endsAt'):
            # 转换结束时间格式
            end_time = alert.get('endsAt').replace('T', ' ').replace('Z', '')
            alerts.append(f"endsAt: {end_time}")
        elif alert.get('startsAt'):
            # 转换开始时间格式
            start_time = alert.get('startsAt').replace('T', ' ').replace('Z', '')
            alerts.append(f"startsAt: {start_time}")

        severity = alert.get('labels', {}).get('severity')
        if severity:
            severities.append(severity)

    # # 添加告警链接
    if alertmanager_url and dbid:
        alerts.append(f"⚠️ **MAID:** {dbid}")
    #     alerts.append(f"🔗 **MAURL:** http://ma.***.com/ma/{dbid}")

    return alerts, severities, dbid

def extract_alertids(alert_info_data):
    """
    从alertmanager的json数据中提取所有alert的alertid
    :param alert_info_data: dict, alertmanager推送的json
    :return: list, 所有alertid（如果没有alertid则返回空列表）
    :去重
    """
    alertids = set()
    for alert in alert_info_data.get('alerts', []):
        labels = alert.get('labels', {})
        alertid = labels.get('alertid')
        if alertid:
            alertids.add(alertid)
    return list(alertids)


def extract_labrador_project(alert_info_data):
    """
    从alertmanager的json数据中提取labrador_project字段
    :param alert_info_data: dict, alertmanager推送的json
    :return: str, labrador_project值，如果没有则返回None
    """
    # 先从 commonLabels 中查找
    common_labels = alert_info_data.get('commonLabels', {})
    if isinstance(common_labels, dict):
        labrador_project = common_labels.get('labrador_project')
        if labrador_project:
            return labrador_project
    
    # 如果 commonLabels 中没有，从第一个 alert 的 labels 中查找
    alerts = alert_info_data.get('alerts', [])
    if alerts:
        first_alert = alerts[0]
        labels = first_alert.get('labels', {})
        if isinstance(labels, dict):
            labrador_project = labels.get('labrador_project')
            if labrador_project:
                return labrador_project
    
    return None


def extract_all_labels(alert_info_data):
    """
    从alertmanager的json数据中提取所有标签（包括commonLabels和每个alert的labels）
    用于标签路由匹配
    :param alert_info_data: dict, alertmanager推送的json
    :return: dict, 合并后的所有标签（排除alertid）
    """
    all_labels = {}
    
    # 首先从 commonLabels 中获取标签
    common_labels = alert_info_data.get('commonLabels', {})
    if isinstance(common_labels, dict):
        for key, value in common_labels.items():
            if key != 'alertid' and not should_filter_label(key):  # 排除alertid和过滤指定前缀
                all_labels[key] = value
    
    # 从第一个 alert 的 labels 中获取额外的标签（如果有）
    alerts = alert_info_data.get('alerts', [])
    if alerts:
        first_alert = alerts[0]
        labels = first_alert.get('labels', {})
        if isinstance(labels, dict):
            for key, value in labels.items():
                if key != 'alertid' and not should_filter_label(key) and key not in all_labels:  # 排除alertid和过滤指定前缀，且不覆盖commonLabels
                    all_labels[key] = value
    
    return all_labels


def extract_alertname(alert_info_data):
    """
    从alertmanager的json数据中提取alertname
    :param alert_info_data: dict, alertmanager推送的json
    :return: str, alertname值，如果没有则返回默认值
    """
    # 先从 commonLabels 中查找
    common_labels = alert_info_data.get('commonLabels', {})
    if isinstance(common_labels, dict):
        alertname = common_labels.get('alertname')
        if alertname:
            return alertname
    
    # 如果 commonLabels 中没有，从第一个 alert 的 labels 中查找
    alerts = alert_info_data.get('alerts', [])
    if alerts:
        first_alert = alerts[0]
        labels = first_alert.get('labels', {})
        if isinstance(labels, dict):
            alertname = labels.get('alertname')
            if alertname:
                return alertname
    
    return "告警通知"  # 默认值