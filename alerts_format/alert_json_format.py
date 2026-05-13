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


def is_grafana_alert(alert_info_data: dict) -> bool:
    """检测是否为 Grafana Alerting 格式（含 generatorURL 字段）"""
    for alert in alert_info_data.get('alerts', []):
        if alert.get('generatorURL'):
            return True
    return False


def extract_grafana_urls(alert_info_data: dict) -> dict:
    """
    提取 Grafana 告警的相关 URL（取第一条告警的值）
    :return: dict, 键包含 dashboardURL / panelURL / generatorURL / silenceURL
    """
    urls = {
        'dashboardURL': '',
        'panelURL': '',
        'generatorURL': '',
        'silenceURL': '',
    }
    alerts = alert_info_data.get('alerts', [])
    if alerts:
        first = alerts[0]
        urls['dashboardURL'] = first.get('dashboardURL', '')
        urls['panelURL'] = first.get('panelURL', '')
        urls['generatorURL'] = first.get('generatorURL', '')
        urls['silenceURL'] = first.get('silenceURL', '')
    return urls


def extract_fingerprints(alert_info_data: dict) -> list:
    """提取所有 alert 的 fingerprint，用于 resolved 时反查原始消息"""
    fps = []
    for alert in alert_info_data.get('alerts', []):
        fp = alert.get('fingerprint')
        if fp and fp not in fps:
            fps.append(fp)
    return fps


def alert_data_api(alert_info_data, project, alertmanager_url, group_id=None):
    """
    处理告警数据并格式化（ops 模板使用）
    :param alert_info_data: dict, alertmanager推送的json数据
    :param project: str, 项目名称
    :param alertmanager_url: str, alertmanager地址
    :param group_id: str, 发送目标群组ID（用于 resolved 反查）
    :return: tuple, (alerts列表, severities列表, maid, grafana_urls)
    """
    dbid = save_dbdata(alert_info_data, project, group_id=group_id)
    alerts = []
    severities = []

    # 提取 Grafana URL 信息
    grafana_urls = extract_grafana_urls(alert_info_data)

    # 获取原始公共标签用于比较
    original_common_labels = alert_info_data.get('commonLabels', {})
    if isinstance(original_common_labels, str):
        original_common_labels = {}

    # 获取过滤后的公共标签
    common_labels = {k: v for k, v in original_common_labels.items()
                     if k != 'alertid' and not should_filter_label(k)}

    if common_labels:
        other_labels = {k: v for k, v in common_labels.items() if k != 'alertname'}
        if other_labels:
            for key, value in other_labels.items():
                alerts.append(f"{key}: {value}")

    alerts.append("✨✨✨✨✨✨✨✨✨✨✨✨")

    for alert in alert_info_data.get('alerts', []):
        alert_labels = alert.get('labels', {})
        if isinstance(alert_labels, str):
            alert_labels = {}

        specific_labels = {
            k: v for k, v in alert_labels.items()
            if k != 'alertid' and not should_filter_label(k)
            and (k not in original_common_labels or original_common_labels.get(k) != v)
        }

        alerts.append("🔥🔥🔥" if alert.get('status') == 'firing' else "✅✅✅")

        for key, value in specific_labels.items():
            if value is not None:
                alerts.append(f"{key}: {value}")

        alert_annotations = alert.get('annotations', {})
        if isinstance(alert_annotations, dict):
            if 'description' in alert_annotations:
                alerts.append(f"description: {alert_annotations['description']}")
            if 'summary' in alert_annotations:
                alerts.append(f"summary: {alert_annotations['summary']}")

        if alert.get('status') == 'resolved' and alert.get('endsAt'):
            end_time = alert.get('endsAt').replace('T', ' ').replace('Z', '')
            alerts.append(f"endsAt: {end_time}")
        elif alert.get('startsAt'):
            start_time = alert.get('startsAt').replace('T', ' ').replace('Z', '')
            alerts.append(f"startsAt: {start_time}")

        severity = alert.get('labels', {}).get('severity')
        if severity:
            severities.append(severity)

    if alertmanager_url and dbid:
        alerts.append(f"⚠️ **MAID:** {dbid}")

    return alerts, severities, dbid, grafana_urls


def extract_alert_raw(alert_info_data: dict) -> list:
    """
    提取原始 alert 列表（供 biz 模板使用），包含 labels/annotations/startsAt/endsAt/status
    只过滤噪声 label，不做文本格式化。
    """
    original_common_labels = alert_info_data.get('commonLabels', {})
    if isinstance(original_common_labels, str):
        original_common_labels = {}

    raw_alerts = []
    for alert in alert_info_data.get('alerts', []):
        labels = alert.get('labels', {})
        if isinstance(labels, str):
            labels = {}

        specific = {
            k: v for k, v in labels.items()
            if not should_filter_label(k)
            and k not in ('alertid', 'alertname', 'severity')
            and (k not in original_common_labels or original_common_labels.get(k) != v)
        }

        annotations = alert.get('annotations', {})
        if isinstance(annotations, str):
            annotations = {}

        raw_alerts.append({
            'status': alert.get('status', ''),
            'labels': specific,
            'annotations': annotations,
            'startsAt': alert.get('startsAt', ''),
            'endsAt': alert.get('endsAt', ''),
            'fingerprint': alert.get('fingerprint', ''),
        })
    return raw_alerts


def extract_alertids(alert_info_data):
    """
    从alertmanager的json数据中提取所有alert的alertid
    :param alert_info_data: dict, alertmanager推送的json
    :return: list, 所有alertid（如果没有alertid则返回空列表）
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
    common_labels = alert_info_data.get('commonLabels', {})
    if isinstance(common_labels, dict):
        labrador_project = common_labels.get('labrador_project')
        if labrador_project:
            return labrador_project

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

    common_labels = alert_info_data.get('commonLabels', {})
    if isinstance(common_labels, dict):
        for key, value in common_labels.items():
            if key != 'alertid' and not should_filter_label(key):
                all_labels[key] = value

    alerts = alert_info_data.get('alerts', [])
    if alerts:
        first_alert = alerts[0]
        labels = first_alert.get('labels', {})
        if isinstance(labels, dict):
            for key, value in labels.items():
                if key != 'alertid' and not should_filter_label(key) and key not in all_labels:
                    all_labels[key] = value

    return all_labels


def extract_alertname(alert_info_data):
    """
    从alertmanager的json数据中提取alertname
    :param alert_info_data: dict, alertmanager推送的json
    :return: str, alertname值，如果没有则返回默认值
    """
    common_labels = alert_info_data.get('commonLabels', {})
    if isinstance(common_labels, dict):
        alertname = common_labels.get('alertname')
        if alertname:
            return alertname

    alerts = alert_info_data.get('alerts', [])
    if alerts:
        first_alert = alerts[0]
        labels = first_alert.get('labels', {})
        if isinstance(labels, dict):
            alertname = labels.get('alertname')
            if alertname:
                return alertname

    return "告警通知"
