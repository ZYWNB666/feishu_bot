#!/usr/bin/env python3
"""
Flashcat oncall 工具模块

流程：
1. 调用 Flashcat schedule/info API 获取当前 oncall 的 person_id 列表
2. 调用 Flashcat person/infos API 获取对应的人员姓名
3. 查询本地 feishu_users 表，将姓名转换为飞书 open_id
"""

import copy
import time
import logging
import requests

from config.constants import FLASHCAT_API_TIMEOUT, MAX_RETRIES, RETRY_BACKOFF_BASE

logger = logging.getLogger(__name__)

FLASHCAT_API_BASE = "https://api.flashcat.cloud"


def _extract_alert_title_and_description(data: dict) -> tuple[str, str]:
    """Extract a compact title and description from an Alertmanager/Grafana payload."""
    alertname = ""
    common_labels = data.get("commonLabels", {})
    if isinstance(common_labels, dict):
        alertname = common_labels.get("alertname", "")
    if not alertname:
        alerts = data.get("alerts", [])
        if alerts:
            alertname = alerts[0].get("labels", {}).get("alertname", "告警")

    description = ""
    for alert in data.get("alerts", []):
        annotations = alert.get("annotations", {})
        description = annotations.get("description") or annotations.get("summary", "")
        if description:
            break
    if not description:
        description = f"告警 {alertname} 已触发，请立即处理"

    return alertname, description


def _flatten_alert_labels(data: dict) -> dict:
    """Build Flashcat event labels, keeping only string values and within API limits."""
    labels = {}
    for source in (data.get("commonLabels", {}),):
        if not isinstance(source, dict):
            continue
        for key, value in source.items():
            if key and value is not None:
                labels[str(key)[:128]] = str(value)[:2048]
    for alert in data.get("alerts", []):
        alert_labels = alert.get("labels", {})
        if not isinstance(alert_labels, dict):
            continue
        for key, value in alert_labels.items():
            if key and key not in labels and value is not None:
                labels[str(key)[:128]] = str(value)[:2048]
        break
    return dict(list(labels.items())[:49])


def _get_incident_id_by_alert_key(app_key: str, alert_key: str) -> str:
    """Look up the incident_id produced for an alert_key."""
    if not app_key or not alert_key:
        return ""

    url = f"{FLASHCAT_API_BASE}/alert/list?app_key={app_key}"
    payload = {
        "p": 1,
        "limit": 1,
        "alert_keys": [alert_key],
    }

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.post(url, json=payload, timeout=FLASHCAT_API_TIMEOUT)
            resp.raise_for_status()
            result = resp.json()
            items = result.get("data", {}).get("items", [])
            if items:
                incident = items[0].get("incident") or {}
                incident_id = incident.get("incident_id", "")
                if incident_id:
                    logger.info(
                        "Flashcat incident lookup succeeded: maid=%s incident_id=%s",
                        alert_key, incident_id
                    )
                    return incident_id

            if attempt < MAX_RETRIES:
                wait = attempt * RETRY_BACKOFF_BASE
                logger.info(
                    "Flashcat incident not visible yet: maid=%s attempt=%d/%d retry_in=%ds",
                    alert_key, attempt, MAX_RETRIES, wait
                )
                time.sleep(wait)
        except Exception as e:
            if attempt < MAX_RETRIES:
                wait = attempt * RETRY_BACKOFF_BASE
                logger.warning(
                    "Flashcat incident lookup failed: maid=%s attempt=%d/%d error=%s retry_in=%ds",
                    alert_key, attempt, MAX_RETRIES, e, wait
                )
                time.sleep(wait)
            else:
                logger.error(
                    "Flashcat incident lookup failed: maid=%s attempts=%d error=%s",
                    alert_key, MAX_RETRIES, e
                )

    return ""


def create_phone_incident_from_event(data: dict, app_key: str, integration_key: str, alert_key: str) -> str:
    """Create a phone alert through Flashcat Event API and return its incident_id."""
    if not app_key:
        logger.error("FLASHCAT_APP_KEY 未配置，无法查询 incident_id: maid=%s", alert_key)
        return ""
    if not integration_key:
        logger.error(
            "FLASHCAT_PHONE_INTEGRATION_KEY 未配置，无法通过 Event API 创建电话告警: maid=%s",
            alert_key
        )
        return ""
    if not alert_key:
        logger.error("maid 为空，无法稳定查询 Flashcat incident")
        return ""

    title, description = _extract_alert_title_and_description(data)
    labels = _flatten_alert_labels(data)
    labels["maid"] = alert_key

    payload = {
        "title_rule": title,
        "event_status": "Critical",
        "alert_key": alert_key,
        "description": description,
        "labels": labels,
    }
    url = f"{FLASHCAT_API_BASE}/event/push/alert?integration_key={integration_key}"

    try:
        resp = requests.post(url, json=payload, timeout=FLASHCAT_API_TIMEOUT)
        resp.raise_for_status()
        result = resp.json()
        returned_alert_key = result.get("data", {}).get("alert_key") or alert_key
        logger.info(
            "Flashcat Event API 推送成功: maid=%s request_id=%s",
            returned_alert_key, result.get("request_id", "")
        )
        return _get_incident_id_by_alert_key(app_key, returned_alert_key)
    except Exception as e:
        body = ""
        if hasattr(e, "response") and e.response is not None:
            body = e.response.text[:500]
        logger.error("Flashcat Event API 推送失败: maid=%s error=%s body=%s", alert_key, e, body)
        return ""


def get_oncall_person_ids(app_key: str, schedule_id: int, maid: str = None) -> list:
    """从 Flashcat 获取当前 oncall 的 person_id 列表

    Args:
        app_key: Flashcat API key
        schedule_id: 排班 ID

    Returns:
        list: person_id 列表，失败返回空列表
    """
    now = int(time.time())
    url = f"{FLASHCAT_API_BASE}/schedule/info?app_key={app_key}"
    payload = {
        "schedule_id": schedule_id,
        "start": now,
        "end": now + 86400,
    }
    try:
        resp = requests.post(url, json=payload, timeout=FLASHCAT_API_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()

        cur_oncall = data.get("data", {}).get("cur_oncall", {})
        members = cur_oncall.get("group", {}).get("members", [])
        person_ids = []
        for member in members:
            person_ids.extend(member.get("person_ids", []))

        logger.info("Flashcat oncall person_ids: maid=%s person_ids=%s", maid, person_ids)
        return person_ids
    except Exception as e:
        logger.error("获取 Flashcat oncall person_ids 失败: maid=%s error=%s", maid, e)
        return []




def get_person_names(app_key: str, person_ids: list, maid: str = None) -> list:
    """根据 person_id 列表获取姓名列表"""
    if not person_ids:
        return []
    url = f"{FLASHCAT_API_BASE}/person/infos?app_key={app_key}"
    try:
        resp = requests.post(url, json={"person_ids": person_ids}, timeout=FLASHCAT_API_TIMEOUT)
        resp.raise_for_status()
        items = resp.json().get("data", {}).get("items", [])
        names = [item["person_name"] for item in items if item.get("person_name")]
        logger.info("Flashcat oncall 人员姓名: maid=%s names=%s", maid, names)
        return names
    except Exception as e:
        logger.error("获取 Flashcat person names 失败: maid=%s error=%s", maid, e)
        return []


def get_oncall_open_ids(app_key: str, schedule_id: int, maid: str = None) -> list:
    """完整流程：从 Flashcat 获取 oncall 人员，查询本地 feishu_users 表转换为 open_id

    不依赖任何飞书 API token，直接查库。

    Args:
        app_key: Flashcat API key
        schedule_id: Flashcat 排班 ID

    Returns:
        list: 飞书 open_id 列表
    """
    from alerts_format.db_utils import get_open_ids_by_names

    person_ids = get_oncall_person_ids(app_key, schedule_id, maid=maid)
    if not person_ids:
        logger.warning("未获取到 oncall person_ids，跳过 oncall 艾特: maid=%s", maid)
        return []

    names = get_person_names(app_key, person_ids, maid=maid)
    if not names:
        logger.warning("未获取到 oncall 人员姓名，跳过 oncall 艾特: maid=%s", maid)
        return []

    name_to_id = get_open_ids_by_names(names)
    open_ids = []
    for name in names:
        oid = name_to_id.get(name, "")
        if oid:
            logger.info("oncall 用户映射成功: maid=%s name=%s open_id=%s", maid, name, oid)
            open_ids.append(oid)
        else:
            logger.warning(
                "oncall 用户未配置，已跳过: maid=%s name=%s", maid, name
            )

    logger.info("oncall 艾特 open_id 列表: maid=%s open_ids=%s", maid, open_ids)
    return open_ids


def send_phone_alert(data: dict, integration_key: str, maid: str = None) -> bool:
    """发送电话告警到 Flashcat（Grafana 兼容接口）

    将告警数据中所有 severity 标签替换为 Critical 后 POST 到
    Flashcat 电话告警接口，以触发电话通知。

    Args:
        data: 原始告警数据（Grafana/Alertmanager webhook 格式）
        integration_key: Flashcat integration key

    Returns:
        bool: 发送成功返回 True，否则返回 False
    """
    if not integration_key:
        logger.error("FLASHCAT_PHONE_INTEGRATION_KEY 未配置，无法发送电话告警: maid=%s", maid)
        return False

    # 深拷贝，不修改原始数据
    phone_data = copy.deepcopy(data)

    # 替换各 alert 的 severity 标签为 Critical
    for alert in phone_data.get("alerts", []):
        labels = alert.get("labels", {})
        if isinstance(labels, dict):
            labels["severity"] = "Critical"

    # 替换 commonLabels 中的 severity
    common_labels = phone_data.get("commonLabels", {})
    if isinstance(common_labels, dict) and "severity" in common_labels:
        common_labels["severity"] = "Critical"

    url = f"{FLASHCAT_API_BASE}/event/push/alert/grafana?integration_key={integration_key}"
    try:
        resp = requests.post(url, json=phone_data, timeout=FLASHCAT_API_TIMEOUT)
        resp.raise_for_status()
        logger.info(
            "电话告警发送成功: maid=%s status=%s body=%s",
            maid, resp.status_code, resp.text[:200]
        )
        return True
    except Exception as e:
        logger.error("电话告警发送失败: maid=%s error=%s", maid, e)
        return False


def create_phone_incident(data: dict, app_key: str, channel_id: str, maid: str = None) -> str:
    """创建 Flashcat incident 以触发电话告警

    通过 incident/create API 创建一个 Critical 级别的 incident，
    Flashcat 会根据 channel 的通知策略触发电话通知。

    Args:
        data: 原始告警数据（Grafana/Alertmanager webhook 格式）
        app_key: Flashcat API key
        channel_id: Flashcat channel_id（字符串或整数）

    Returns:
        str: 创建成功返回 incident_id，失败返回空字符串
    """
    if not app_key:
        logger.error("FLASHCAT_APP_KEY 未配置，无法创建 incident: maid=%s", maid)
        return ""
    if not channel_id:
        logger.error("FLASHCAT_CHANNEL_ID 未配置，无法创建 incident: maid=%s", maid)
        return ""

    # 从告警数据中提取标题和描述
    alertname, description = _extract_alert_title_and_description(data)

    # 尝试将 channel_id 转为整数（Flashcat API 要求数字类型）
    try:
        channel_id_int = int(channel_id)
    except (ValueError, TypeError):
        logger.error("FLASHCAT_CHANNEL_ID 不是有效数字: maid=%s channel_id=%s", maid, channel_id)
        return ""

    payload = {
        "incident_severity": "Critical",
        "title": alertname,
        "description": description,
        "channel_id": channel_id_int,
    }

    url = f"{FLASHCAT_API_BASE}/incident/create?app_key={app_key}"
    try:
        resp = requests.post(url, json=payload, timeout=FLASHCAT_API_TIMEOUT)
        resp.raise_for_status()
        result = resp.json()
        incident_id = result.get("data", {}).get("incident_id", "")
        if incident_id:
            logger.info(
                "Flashcat incident 创建成功: maid=%s incident_id=%s title=%s",
                maid, incident_id, alertname
            )
            return incident_id
        else:
            logger.error(
                "Flashcat incident 创建失败: maid=%s 响应中无 incident_id body=%s",
                maid, resp.text[:200]
            )
            return ""
    except Exception as e:
        body = ""
        if hasattr(e, "response") and e.response is not None:
            body = e.response.text[:500]
        logger.error("创建 Flashcat incident 失败: maid=%s error=%s body=%s", maid, e, body)
        return ""


def ack_incident(app_key: str, incident_id: str, maid: str = None) -> bool:
    """认领（ack）Flashcat incident

    Args:
        app_key: Flashcat API key
        incident_id: Flashcat incident ID

    Returns:
        bool: 认领成功返回 True，否则返回 False
    """
    if not app_key:
        logger.error("FLASHCAT_APP_KEY 未配置，无法认领 incident: maid=%s", maid)
        return False
    if not incident_id:
        logger.error("incident_id 为空，无法认领: maid=%s", maid)
        return False

    url = f"{FLASHCAT_API_BASE}/incident/ack?app_key={app_key}"
    payload = {"incident_ids": [incident_id]}
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.post(url, json=payload, timeout=FLASHCAT_API_TIMEOUT)
            resp.raise_for_status()
            logger.info(
                "Flashcat incident 认领成功: maid=%s incident_id=%s attempt=%d/%d",
                maid, incident_id, attempt, MAX_RETRIES
            )
            return True
        except Exception as e:
            if attempt < MAX_RETRIES:
                wait = attempt * RETRY_BACKOFF_BASE
                logger.warning(
                    "认领 Flashcat incident 失败: maid=%s incident_id=%s attempt=%d/%d "
                    "error=%s retry_in=%ds",
                    maid, incident_id, attempt, MAX_RETRIES, e, wait
                )
                time.sleep(wait)
            else:
                logger.error(
                    "认领 Flashcat incident 失败: maid=%s incident_id=%s attempts=%d error=%s",
                    maid, incident_id, MAX_RETRIES, e
                )
                return False
