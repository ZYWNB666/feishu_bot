#!/usr/bin/env python3
"""
Grafana Alerting 静默 API 封装

Grafana 内置 Alertmanager 的 silence 接口路径：
  POST/DELETE  <grafana_url>/api/alertmanager/grafana/api/v2/silences
               <grafana_url>/api/alertmanager/grafana/api/v2/silence/<id>
"""

import json
import logging
from datetime import datetime, timedelta

import requests

from config.config import Config
from config.constants import SILENCE_API_TIMEOUT
from db.pool import db_cursor

logger = logging.getLogger(__name__)


def _get_alert_data(maid: str) -> dict:
    """从 alert_data 取 alertlabels / project / silenceid"""
    try:
        with db_cursor(dictionary=True) as (conn, cursor):
            cursor.execute(
                "SELECT alertlabels, project, silenceid FROM alert_data WHERE id = %s",
                (maid,)
            )
            return cursor.fetchone() or {}
    except Exception as e:
        logger.error("读取 alert_data 失败: %s", e)
        return {}


def _save_silence_ids(maid: str, silence_ids: list) -> None:
    """将 silence ID 列表写入 alert_data.silenceid"""
    try:
        with db_cursor() as (conn, cursor):
            cursor.execute(
                "UPDATE alert_data SET silenceid = %s WHERE id = %s",
                (json.dumps(silence_ids), maid)
            )
            conn.commit()
    except Exception as e:
        logger.error("保存 silence ID 失败: %s", e)


def _clear_silence_ids(maid: str) -> None:
    """清空 alert_data.silenceid"""
    try:
        with db_cursor() as (conn, cursor):
            cursor.execute(
                "UPDATE alert_data SET silenceid = NULL WHERE id = %s",
                (maid,)
            )
            conn.commit()
    except Exception as e:
        logger.error("清空 silence ID 失败: %s", e)


def grafana_create_silence(maid: str, duration_hours: int, grafana_url: str) -> dict:
    """
    向 Grafana 内置 Alertmanager 创建静默规则

    :param maid: 告警 MAID
    :param duration_hours: 静默时长（小时）
    :param grafana_url: Grafana 地址，如 https://grafana.example.com
    :return: {"success": bool, "message": str, ...}
    """
    api_key = Config.GRAFANA_API_KEY
    if not api_key:
        return {"success": False, "message": "未配置 GRAFANA_API_KEY"}
    if not grafana_url:
        return {"success": False, "message": "未配置 grafana_url"}

    row = _get_alert_data(maid)
    if not row:
        return {"success": False, "message": f"未找到 MAID={maid} 的记录"}

    alertlabels_data = row.get('alertlabels') or '{}'
    alertlabels_dict = json.loads(alertlabels_data) if isinstance(alertlabels_data, str) else alertlabels_data
    matchers_list = alertlabels_dict.get('matchers', [])

    if not matchers_list:
        return {"success": False, "message": "该告警无 matchers 数据"}

    now = datetime.now().astimezone()
    starts_at = now.isoformat(timespec='milliseconds')
    ends_at = (now + timedelta(hours=duration_hours)).isoformat(timespec='milliseconds')

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    url = f"{grafana_url.rstrip('/')}/api/alertmanager/grafana/api/v2/silences"

    silence_ids = []
    for matchers_item in matchers_list:
        matchers = matchers_item.get('matchers', [])
        if not matchers:
            continue

        # Grafana silence 的 matchers 格式：[{"name":"..","value":"..","isRegex":false,"isEqual":true}]
        body = {
            "matchers": matchers,
            "startsAt": starts_at,
            "endsAt": ends_at,
            "comment": f"Feishu Bot - MAID: {maid}",
            "createdBy": "feishu_bot",
        }
        try:
            resp = requests.post(url, headers=headers, json=body, timeout=SILENCE_API_TIMEOUT)
            if resp.status_code in (200, 201, 202):
                sid = resp.json().get('silenceID') or resp.json().get('id', '')
                if sid:
                    silence_ids.append(sid)
                    logger.info("Grafana 静默创建成功: %s", sid)
            else:
                logger.error("Grafana 静默创建失败: %s %s", resp.status_code, resp.text)
        except Exception as e:
            logger.error("调用 Grafana silence API 异常: %s", e)

    if silence_ids:
        _save_silence_ids(maid, silence_ids)
        return {
            "success": True,
            "silence_ids": silence_ids,
            "message": f"成功创建 {len(silence_ids)} 个 Grafana 静默规则",
        }
    return {"success": False, "message": "所有静默规则创建失败"}


def grafana_delete_silence(maid: str, grafana_url: str) -> dict:
    """
    删除 Grafana 内置 Alertmanager 中的静默规则

    :param maid: 告警 MAID
    :param grafana_url: Grafana 地址
    :return: {"success": bool, "message": str}
    """
    api_key = Config.GRAFANA_API_KEY
    if not api_key:
        return {"success": False, "message": "未配置 GRAFANA_API_KEY"}
    if not grafana_url:
        return {"success": False, "message": "未配置 grafana_url"}

    row = _get_alert_data(maid)
    if not row:
        return {"success": False, "message": f"未找到 MAID={maid} 的记录"}

    silenceid_raw = row.get('silenceid')
    if not silenceid_raw:
        return {"success": False, "message": "该告警没有关联的静默规则"}

    silence_ids = json.loads(silenceid_raw) if isinstance(silenceid_raw, str) else silenceid_raw

    headers = {
        "Authorization": f"Bearer {api_key}",
    }
    base_url = f"{grafana_url.rstrip('/')}/api/alertmanager/grafana/api/v2/silence"

    deleted = 0
    for sid in silence_ids:
        try:
            resp = requests.delete(f"{base_url}/{sid}", headers=headers, timeout=SILENCE_API_TIMEOUT)
            if resp.status_code in (200, 204):
                deleted += 1
            else:
                logger.error("Grafana 删除静默失败: %s %s", resp.status_code, resp.text)
        except Exception as e:
            logger.error("调用 Grafana delete silence 异常: %s", e)

    _clear_silence_ids(maid)
    return {
        "success": deleted > 0,
        "deleted_count": deleted,
        "total_count": len(silence_ids),
        "message": f"成功删除 {deleted}/{len(silence_ids)} 个 Grafana 静默规则",
    }
