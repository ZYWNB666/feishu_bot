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
from alerts_format.silence_extension import extend_existing_silences
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
        logger.error("读取 alert_data 失败: maid=%s error=%s", maid, e)
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
        logger.error("保存 silence ID 失败: maid=%s error=%s", maid, e)


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
        logger.error("清空 silence ID 失败: maid=%s error=%s", maid, e)


def grafana_create_silence(
    maid: str,
    duration_hours: int | None,
    grafana_url: str,
    ends_at: datetime | None = None,
) -> dict:
    """
    向 Grafana 内置 Alertmanager 创建静默规则

    :param maid: 告警 MAID
    :param duration_hours: 静默时长（小时），与 ends_at 二选一
    :param grafana_url: Grafana 地址，如 https://grafana.example.com
    :param ends_at: 绝对静默截止时间（带时区的 datetime）
    :return: {"success": bool, "message": str, ...}
    """
    api_key = Config.GRAFANA_API_KEY
    if not api_key:
        return {"success": False, "message": "未配置 GRAFANA_API_KEY"}
    if not grafana_url:
        return {"success": False, "message": "未配置 grafana_url"}

    now = datetime.now().astimezone()
    if ends_at is not None:
        target_end = ends_at.astimezone()
        if target_end <= now:
            return {"success": False, "message": "静默截止时间必须晚于当前时间"}
    else:
        duration_hours = int(duration_hours or 0)
        if duration_hours <= 0:
            return {"success": False, "message": "静默时长必须大于 0"}
        target_end = now + timedelta(hours=duration_hours)

    logger.info(
        "Grafana silence create started: maid=%s duration_hours=%s ends_at=%s grafana_url=%s",
        maid, duration_hours, target_end.isoformat(), grafana_url,
    )
    row = _get_alert_data(maid)
    if not row:
        return {"success": False, "message": f"未找到 MAID={maid} 的记录"}

    alertlabels_data = row.get('alertlabels') or '{}'
    alertlabels_dict = json.loads(alertlabels_data) if isinstance(alertlabels_data, str) else alertlabels_data
    matchers_list = alertlabels_dict.get('matchers', [])

    if not matchers_list:
        return {"success": False, "message": "该告警无 matchers 数据"}

    existing_silence_ids = []
    silenceid_raw = row.get("silenceid")
    if silenceid_raw:
        try:
            parsed_silence_ids = (
                json.loads(silenceid_raw)
                if isinstance(silenceid_raw, str)
                else silenceid_raw
            )
            if isinstance(parsed_silence_ids, list):
                existing_silence_ids = [str(sid) for sid in parsed_silence_ids if sid]
        except (TypeError, json.JSONDecodeError):
            logger.warning("Invalid stored silence IDs, creating a fresh silence: maid=%s", maid)

    if existing_silence_ids:
        extension_result = extend_existing_silences(
            f"{grafana_url.rstrip('/')}/api/alertmanager/grafana/api/v2/silence",
            existing_silence_ids,
            duration_hours,
            headers={"Authorization": f"Bearer {api_key}"},
            backend="Grafana",
            ends_at=target_end if ends_at is not None else None,
        )
        if extension_result and extension_result.get("success"):
            updated_silence_ids = extension_result.get("silence_ids", existing_silence_ids)
            _save_silence_ids(maid, updated_silence_ids)
            extension_result["message"] = (
                f"成功更新 {len(existing_silence_ids)} 个 Grafana 静默规则的截止时间"
                if ends_at is not None
                else f"成功延长 {len(existing_silence_ids)} 个 Grafana 静默规则"
            )
            return extension_result
        if extension_result and not extension_result.get("not_found"):
            return extension_result

    starts_at = now.isoformat(timespec='milliseconds')
    ends_at_text = target_end.isoformat(timespec='milliseconds')

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
            "endsAt": ends_at_text,
            "comment": f"Feishu Bot - MAID: {maid}",
            "createdBy": "feishu_bot",
        }
        try:
            resp = requests.post(url, headers=headers, json=body, timeout=SILENCE_API_TIMEOUT)
            if resp.status_code in (200, 201, 202):
                sid = resp.json().get('silenceID') or resp.json().get('id', '')
                if sid:
                    silence_ids.append(sid)
                    logger.info("Grafana silence created: maid=%s silence_id=%s", maid, sid)
            else:
                logger.error(
                    "Grafana 静默创建失败: maid=%s status=%s body=%s",
                    maid, resp.status_code, resp.text
                )
        except Exception as e:
            logger.error("调用 Grafana silence API 异常: maid=%s error=%s", maid, e)

    if silence_ids:
        _save_silence_ids(maid, silence_ids)
        return {
            "success": True,
            "silence_ids": silence_ids,
            "duration_seconds": max(0, int((target_end - now).total_seconds())),
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

    logger.info("Grafana silence delete started: maid=%s grafana_url=%s", maid, grafana_url)
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
                logger.info("Grafana silence deleted: maid=%s silence_id=%s", maid, sid)
            else:
                logger.error(
                    "Grafana 删除静默失败: maid=%s silence_id=%s status=%s body=%s",
                    maid, sid, resp.status_code, resp.text
                )
        except Exception as e:
            logger.error(
                "调用 Grafana delete silence 异常: maid=%s silence_id=%s error=%s",
                maid, sid, e
            )

    _clear_silence_ids(maid)
    return {
        "success": deleted > 0,
        "deleted_count": deleted,
        "total_count": len(silence_ids),
        "message": f"成功删除 {deleted}/{len(silence_ids)} 个 Grafana 静默规则",
    }
