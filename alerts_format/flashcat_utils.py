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

logger = logging.getLogger(__name__)

FLASHCAT_API_BASE = "https://api.flashcat.cloud"


def get_oncall_person_ids(app_key: str, schedule_id: int) -> list:
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
        resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        cur_oncall = data.get("data", {}).get("cur_oncall", {})
        members = cur_oncall.get("group", {}).get("members", [])
        person_ids = []
        for member in members:
            person_ids.extend(member.get("person_ids", []))

        logger.info("Flashcat oncall person_ids: %s", person_ids)
        return person_ids
    except Exception as e:
        logger.error("获取 Flashcat oncall person_ids 失败: %s", e)
        return []




def get_person_names(app_key: str, person_ids: list) -> list:
    """根据 person_id 列表获取姓名列表"""
    if not person_ids:
        return []
    url = f"{FLASHCAT_API_BASE}/person/infos?app_key={app_key}"
    try:
        resp = requests.post(url, json={"person_ids": person_ids}, timeout=10)
        resp.raise_for_status()
        items = resp.json().get("data", {}).get("items", [])
        names = [item["person_name"] for item in items if item.get("person_name")]
        logger.info("Flashcat oncall 人员姓名: %s", names)
        return names
    except Exception as e:
        logger.error("获取 Flashcat person names 失败: %s", e)
        return []


def get_oncall_open_ids(app_key: str, schedule_id: int) -> list:
    """完整流程：从 Flashcat 获取 oncall 人员，查询本地 feishu_users 表转换为 open_id

    不依赖任何飞书 API token，直接查库。

    Args:
        app_key: Flashcat API key
        schedule_id: Flashcat 排班 ID

    Returns:
        list: 飞书 open_id 列表
    """
    from alerts_format.db_utils import get_open_ids_by_names

    person_ids = get_oncall_person_ids(app_key, schedule_id)
    if not person_ids:
        logger.warning("未获取到 oncall person_ids，跳过 oncall 艾特")
        return []

    names = get_person_names(app_key, person_ids)
    if not names:
        logger.warning("未获取到 oncall 人员姓名，跳过 oncall 艾特")
        return []

    name_to_id = get_open_ids_by_names(names)
    open_ids = []
    for name in names:
        oid = name_to_id.get(name, "")
        if oid:
            logger.info("用户 '%s' -> open_id: %s", name, oid)
            open_ids.append(oid)
        else:
            logger.warning(
                "用户 '%s' 不在 feishu_users 表中，已跳过（请在管理页面添加该用户）", name
            )

    logger.info("oncall 艾特 open_id 列表: %s", open_ids)
    return open_ids


def send_phone_alert(data: dict, integration_key: str) -> bool:
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
        logger.error("FLASHCAT_PHONE_INTEGRATION_KEY 未配置，无法发送电话告警")
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
        resp = requests.post(url, json=phone_data, timeout=10)
        resp.raise_for_status()
        logger.info("电话告警发送成功: status=%s body=%s", resp.status_code, resp.text[:200])
        return True
    except Exception as e:
        logger.error("电话告警发送失败: %s", e)
        return False


def create_phone_incident(data: dict, app_key: str, channel_id: str) -> str:
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
        logger.error("FLASHCAT_APP_KEY 未配置，无法创建 incident")
        return ""
    if not channel_id:
        logger.error("FLASHCAT_CHANNEL_ID 未配置，无法创建 incident")
        return ""

    # 从告警数据中提取标题和描述
    alertname = ""
    common_labels = data.get("commonLabels", {})
    if isinstance(common_labels, dict):
        alertname = common_labels.get("alertname", "")
    if not alertname:
        alerts = data.get("alerts", [])
        if alerts:
            alertname = alerts[0].get("labels", {}).get("alertname", "告警")

    # 提取描述（取第一条 alert 的 description 或 summary）
    description = ""
    for alert in data.get("alerts", []):
        annotations = alert.get("annotations", {})
        description = annotations.get("description") or annotations.get("summary", "")
        if description:
            break
    if not description:
        description = f"告警 {alertname} 已触发，请立即处理"

    # 尝试将 channel_id 转为整数（Flashcat API 要求数字类型）
    try:
        channel_id_int = int(channel_id)
    except (ValueError, TypeError):
        logger.error("FLASHCAT_CHANNEL_ID '%s' 不是有效数字", channel_id)
        return ""

    payload = {
        "incident_severity": "Critical",
        "title": alertname,
        "description": description,
        "channel_id": channel_id_int,
    }

    url = f"{FLASHCAT_API_BASE}/incident/create?app_key={app_key}"
    try:
        resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        result = resp.json()
        incident_id = result.get("data", {}).get("incident_id", "")
        if incident_id:
            logger.info("Flashcat incident 创建成功: incident_id=%s title=%s", incident_id, alertname)
            return incident_id
        else:
            logger.error("Flashcat incident 创建失败: 响应中无 incident_id, body=%s", resp.text[:200])
            return ""
    except Exception as e:
        logger.error("创建 Flashcat incident 失败: %s", e)
        return ""


def ack_incident(app_key: str, incident_id: str) -> bool:
    """认领（ack）Flashcat incident

    Args:
        app_key: Flashcat API key
        incident_id: Flashcat incident ID

    Returns:
        bool: 认领成功返回 True，否则返回 False
    """
    if not app_key:
        logger.error("FLASHCAT_APP_KEY 未配置，无法认领 incident")
        return False
    if not incident_id:
        logger.error("incident_id 为空，无法认领")
        return False

    url = f"{FLASHCAT_API_BASE}/incident/ack?app_key={app_key}"
    payload = {"incident_ids": [incident_id]}
    max_retries = 3
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.post(url, json=payload, timeout=10)
            resp.raise_for_status()
            logger.info("Flashcat incident 认领成功: incident_id=%s (attempt %d/%d)", incident_id, attempt, max_retries)
            return True
        except Exception as e:
            if attempt < max_retries:
                wait = attempt * 2
                logger.warning("认领 Flashcat incident 失败 (attempt %d/%d): %s, %d秒后重试", attempt, max_retries, e, wait)
                time.sleep(wait)
            else:
                logger.error("认领 Flashcat incident 失败 (已重试 %d 次): %s", max_retries, e)
                return False
