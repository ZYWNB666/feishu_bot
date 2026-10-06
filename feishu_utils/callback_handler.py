#!/usr/bin/env python3
"""
飞书卡片交互回调处理模块

优化点：
- 回调去重缓存改用 BoundedTTLCache（带容量上限，防内存无限增长）
- 魔法数字统一引用 config.constants
- DB 访问改用连接池
- 重试次数/退避使用常量
"""

import json
import logging
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from alerts_format.ma import macreate, madelete
from alerts_format.grafana_silence import grafana_create_silence, grafana_delete_silence
from alerts_format.flashcat_utils import ack_incident
from config.constants import (
    CALLBACK_CACHE_TTL,
    CALLBACK_CACHE_MAXSIZE,
    MAX_RETRIES,
    RETRY_BACKOFF_BASE,
    DEFAULT_SILENCE_DURATION,
    SILENCE_DURATION_6H,
    SILENCE_DURATION_12H,
    SILENCE_DURATION_24H,
    SILENCE_DURATION_3D,
    SILENCE_DURATION_7D,
)
from db.pool import db_cursor
from utils.bounded_cache import BoundedTTLCache
from utils.alert_trace import traced_request, set_maid, contextual_target

logger = logging.getLogger(__name__)

# 用于去重的缓存（存储最近处理过的回调）
# 使用带容量上限的 TTL 缓存，防止长时间运行后内存无限增长
_callback_cache = BoundedTTLCache(maxsize=CALLBACK_CACHE_MAXSIZE, ttl=CALLBACK_CACHE_TTL)
_callback_cache_lock = threading.Lock()
_silence_action_lock = threading.Lock()  # 保留用于跨缓存原子操作


def _get_current_time():
    """获取当前时间字符串（容器已配置上海时区，直接用本地时间）"""
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def _get_silence_config_by_maid(maid: str) -> dict:
    """
    通过 maid 查找对应的 silence_type 和 grafana_url
    先从 alert_data 查 project，再从 alert_config 查路由配置

    使用连接池，两次查询复用同一连接，减少连接建立开销。
    """
    try:
        with db_cursor(dictionary=True) as (conn, cursor):
            cursor.execute("SELECT project FROM alert_data WHERE id = %s", (maid,))
            row = cursor.fetchone()
            if not row or not row.get('project'):
                return {}
            project = row['project']

            # 查 alert_config（复用同一连接）
            cursor.execute(
                "SELECT silence_type, grafana_url FROM alert_config WHERE project = %s LIMIT 1",
                (project,)
            )
            cfg_row = cursor.fetchone()
            return cfg_row or {}
    except Exception as e:
        logger.error("查询 silence_config 失败: maid=%s error=%s", maid, e)
        return {}


def _format_silence_duration(duration_seconds):
    """将累计静默秒数格式化为易读的天/小时，避免网络耗时导致少显示1小时。"""
    seconds = max(0, int(duration_seconds or 0))
    total_hours = max(1, (seconds + 3599) // 3600)
    days, hours = divmod(total_hours, 24)
    parts = []
    if days:
        parts.append(f"{days} 天")
    if hours:
        parts.append(f"{hours} 小时")
    return " ".join(parts) or "1 小时"


def _silence_extension_button(maid, text, duration):
    return {
        "tag": "button",
        "text": {"tag": "plain_text", "content": text},
        "type": "primary",
        "value": {
            "action": "silence",
            "maid": maid,
            "duration": duration,
        },
    }


def _decode_action_value(raw_value):
    """兼容 dict、JSON 字符串和双重 JSON 字符串形式的按钮 value。"""
    try:
        value = raw_value
        while isinstance(value, str):
            value = json.loads(value)
        return value if isinstance(value, dict) else {}
    except (TypeError, json.JSONDecodeError):
        return {}


def _custom_silence_picker(maid):
    return {
        "tag": "picker_datetime",
        "name": "silence_until",
        "placeholder": {
            "tag": "plain_text",
            "content": "选择静默截止时间",
        },
        "width": "default",
        "value": {"action": "silence_until", "maid": maid},
    }


def create_custom_silence_picker_card(card, maid):
    """只把同一操作行里的“自定义时间”按钮替换为日期时间选择器。"""
    if not isinstance(card, dict):
        return None

    updated = json.loads(json.dumps(card, ensure_ascii=False))
    elements = updated.get("elements")
    if not isinstance(elements, list):
        return None

    for element in elements:
        if not isinstance(element, dict) or element.get("tag") != "action":
            continue
        actions = element.get("actions") or []
        for index, action in enumerate(actions):
            if not isinstance(action, dict):
                continue
            if (
                _decode_action_value(action.get("value", {})).get("action")
                == "show_custom_silence"
            ):
                actions[index] = _custom_silence_picker(maid)
                updated.setdefault("config", {})["update_multi"] = True
                return updated
    return None


def _load_original_card(maid, open_message_id, feishu_client):
    """读取原始告警卡片；选完时间后用它去掉临时日期选择框。"""
    from alerts_format.savedb import get_card_content

    content = get_card_content(maid)
    if not content and open_message_id and hasattr(feishu_client, "get_message"):
        message = feishu_client.get_message(open_message_id)
        content = ((message or {}).get("body") or {}).get("content")
    try:
        return json.loads(content) if isinstance(content, str) else content
    except (TypeError, json.JSONDecodeError):
        return None


def _parse_silence_until(option, timezone_name=None):
    """解析飞书 picker_datetime 返回值，例如 2026-08-28 23:15 +0800。"""
    if not option:
        return None
    text = str(option).strip()
    for fmt in ("%Y-%m-%d %H:%M %z", "%Y-%m-%d %H:%M:%S %z"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        try:
            parsed = parsed.replace(tzinfo=ZoneInfo(timezone_name or "Asia/Shanghai"))
        except ZoneInfoNotFoundError:
            return None
    return parsed


def create_silence_success_card(maid, duration, operator_id=None, ends_at=None):
    """
    创建静默成功的卡片
    
    Args:
        maid: 告警ID
        duration: 静默时长（秒）
    
    Returns:
        dict: 飞书卡片数据
    """
    duration_text = _format_silence_duration(duration)
    if ends_at:
        utc_offset = ends_at.strftime("%z")
        if len(utc_offset) == 5:
            utc_offset = f"{utc_offset[:3]}:{utc_offset[3:]}"
        end_text = ends_at.strftime("%Y-%m-%d %H:%M") + f" (UTC{utc_offset})"
        summary = f"**告警 {maid} 已静默至 {end_text}**\n在此期间不会发送此告警通知"
    else:
        summary = f"**告警 {maid} 已静默 {duration_text}**\n在此期间不会发送此告警通知"
    
    card_data = {
        "config": {
            "wide_screen_mode": True
        },
        "header": {
            "title": {
                "tag": "plain_text",
                "content": "✅ 静默成功"
            },
            "template": "green"
        },
        "elements": [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": summary
                }
            },
            {
                "tag": "hr"
            },
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": (
                        "**继续延长静默**\n"
                        "下方时长会在当前静默结束时间基础上继续累加。"
                    )
                }
            },
            {
                "tag": "action",
                "actions": [
                    _silence_extension_button(
                        maid, "🔕 静默6小时", SILENCE_DURATION_6H
                    ),
                    _silence_extension_button(
                        maid, "🔕 静默12小时", SILENCE_DURATION_12H
                    ),
                    _silence_extension_button(
                        maid, "🔕 静默24小时", SILENCE_DURATION_24H
                    ),
                ]
            },
            {
                "tag": "action",
                "actions": [
                    _silence_extension_button(
                        maid, "🔕 静默3天", SILENCE_DURATION_3D
                    ),
                    _silence_extension_button(
                        maid, "🔕 静默7天", SILENCE_DURATION_7D
                    ),
                    {
                        "tag": "button",
                        "text": {
                            "tag": "plain_text",
                            "content": "🔔 取消静默"
                        },
                        "type": "danger",
                        "value": {
                            "action": "cancel_silence",
                            "maid": maid
                        }
                    }
                ]
            },
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": (
                        "⚠️ **操作提示：静默操作请根据实际业务场景谨慎选择。**\n"
                        "长时间静默可能掩盖持续故障，请确认影响范围和处置计划。"
                    )
                }
            },
            {
                "tag": "note",
                "elements": [
                    {
                        "tag": "plain_text",
                        "content": f"⏰ 操作时间: {_get_current_time()}"
                    },
                    {
                        "tag": "lark_md",
                        "content": f"👤 操作人: <at id=\"{operator_id}\"></at>" if operator_id else ""
                    }
                ]
            }
        ]
    }
    
    return card_data


def create_cancel_silence_card(maid, operator_id=None):
    """
    创建取消静默成功的卡片
    
    Args:
        maid: 告警ID
    
    Returns:
        dict: 飞书卡片数据
    """
    card_data = {
        "config": {
            "wide_screen_mode": True
        },
        "header": {
            "title": {
                "tag": "plain_text",
                "content": "🔔 已取消静默"
            },
            "template": "blue"
        },
        "elements": [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"**告警 {maid} 的静默已取消**\n将继续接收此告警通知"
                }
            },
            {
                "tag": "hr"
            },
            {
                "tag": "note",
                "elements": [
                    {
                        "tag": "plain_text",
                        "content": f"⏰ 操作时间: {_get_current_time()}"
                    },
                    {
                        "tag": "lark_md",
                        "content": f"👤 操作人: <at id=\"{operator_id}\"></at>" if operator_id else ""
                    }
                ]
            }
        ]
    }
    
    return card_data


def create_failure_card(maid, action_type="静默", error_message=None):
    """
    创建操作失败的卡片
    
    Args:
        maid: 告警ID
        action_type: 操作类型（静默/取消静默）
        error_message: 错误信息
    
    Returns:
        dict: 飞书卡片数据
    """
    # 构建错误详情
    if error_message:
        content = f"**告警 {maid} {action_type}操作失败**\n\n❌ 错误信息: {error_message}\n\n💡 请检查以下配置：\n- Grafana API Key 是否有效（GRAFANA_API_KEY）\n- Grafana 地址是否正确（grafana_url）\n- Grafana Alertmanager 是否启用\n- 网络连接是否正常"
    else:
        content = f"**告警 {maid} {action_type}操作失败**\n\n💡 请检查以下配置：\n- Grafana API Key 是否有效（GRAFANA_API_KEY）\n- Grafana 地址是否正确（grafana_url）\n- Grafana Alertmanager 是否启用\n- 网络连接是否正常"
    
    card_data = {
        "config": {
            "wide_screen_mode": True
        },
        "header": {
            "title": {
                "tag": "plain_text",
                "content": "❌ 操作失败"
            },
            "template": "red"
        },
        "elements": [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": content
                }
            },
            {
                "tag": "hr"
            },
            {
                "tag": "note",
                "elements": [
                    {
                        "tag": "plain_text",
                        "content": f"⏰ 操作时间: {_get_current_time()}"
                    }
                ]
            }
        ]
    }
    
    return card_data


def handle_silence_action(maid, duration, open_message_id, feishu_client, operator_id=None):
    """
    处理静默操作（异步执行）

    Args:
        maid: 告警ID
        duration: 静默时长（秒）
        open_message_id: 消息ID（用于话题回复）
        feishu_client: 飞书客户端实例
    """
    def process_silence():
        try:
            duration_hours = duration // 3600

            # 查询 silence_type
            silence_cfg = _get_silence_config_by_maid(maid)
            silence_type = silence_cfg.get('silence_type', 'grafana')

            with _silence_action_lock:
                if silence_type == 'grafana':
                    grafana_url = silence_cfg.get('grafana_url', '')
                    silence_result = grafana_create_silence(maid, duration_hours, grafana_url)
                else:
                    silence_result = macreate(maid, duration_hours)

            if silence_result.get('success'):
                display_duration = silence_result.get("duration_seconds", duration)
                silence_card = create_silence_success_card(
                    maid, display_duration, operator_id
                )
                feishu_client.reply_message(
                    open_message_id,
                    "interactive",
                    json.dumps(silence_card),
                    reply_in_thread=True,
                )
                logger.info("silence action completed: maid=%s silence_type=%s duration=%s operator_id=%s", maid, silence_type, duration, operator_id)
            else:
                error_msg = silence_result.get('message', '未知错误')
                failure_card = create_failure_card(maid, "静默", error_msg)
                feishu_client.reply_message(
                    open_message_id,
                    "interactive",
                    json.dumps(failure_card),
                    reply_in_thread=True,
                )
                logger.error(
                    "静默创建失败: maid=%s silence_type=%s error=%s",
                    maid, silence_type, error_msg
                )
        except Exception as e:
            failure_card = create_failure_card(maid, "静默", str(e))
            try:
                feishu_client.reply_message(
                    open_message_id,
                    "interactive",
                    json.dumps(failure_card),
                    reply_in_thread=True,
                )
            except Exception:
                pass
            logger.error("处理静默时出错: maid=%s error=%s", maid, e)

    thread = threading.Thread(target=contextual_target(process_silence))
    thread.daemon = True
    thread.start()


def handle_silence_until_action(
    maid,
    ends_at,
    open_message_id,
    feishu_client,
    operator_id=None,
):
    """按用户选择的绝对截止时间创建或更新静默。"""
    def process_silence_until():
        try:
            silence_cfg = _get_silence_config_by_maid(maid)
            silence_type = silence_cfg.get("silence_type", "grafana")

            with _silence_action_lock:
                if silence_type == "grafana":
                    silence_result = grafana_create_silence(
                        maid,
                        None,
                        silence_cfg.get("grafana_url", ""),
                        ends_at=ends_at,
                    )
                else:
                    silence_result = macreate(maid, ends_at=ends_at)

            if silence_result.get("success"):
                remaining_seconds = silence_result.get(
                    "duration_seconds",
                    max(0, int((ends_at - datetime.now().astimezone()).total_seconds())),
                )
                silence_card = create_silence_success_card(
                    maid,
                    remaining_seconds,
                    operator_id,
                    ends_at=ends_at,
                )
                feishu_client.reply_message(
                    open_message_id,
                    "interactive",
                    json.dumps(silence_card),
                    reply_in_thread=True,
                )
                logger.info(
                    "custom silence action completed: maid=%s silence_type=%s ends_at=%s operator_id=%s",
                    maid, silence_type, ends_at.isoformat(), operator_id,
                )
            else:
                error_msg = silence_result.get("message", "未知错误")
                failure_card = create_failure_card(maid, "自定义静默", error_msg)
                feishu_client.reply_message(
                    open_message_id,
                    "interactive",
                    json.dumps(failure_card),
                    reply_in_thread=True,
                )
                logger.error(
                    "自定义静默创建失败: maid=%s silence_type=%s error=%s",
                    maid, silence_type, error_msg,
                )
        except Exception as exc:
            logger.error("处理自定义静默时出错: maid=%s error=%s", maid, exc, exc_info=True)
            try:
                failure_card = create_failure_card(maid, "自定义静默", str(exc))
                feishu_client.reply_message(
                    open_message_id,
                    "interactive",
                    json.dumps(failure_card),
                    reply_in_thread=True,
                )
            except Exception:
                pass

    thread = threading.Thread(target=contextual_target(process_silence_until), daemon=True)
    thread.start()


def handle_cancel_silence_action(maid, open_message_id, feishu_client, operator_id=None):
    """
    处理取消静默操作（异步执行）

    Args:
        maid: 告警ID
        open_message_id: 消息ID（用于话题回复）
        feishu_client: 飞书客户端实例
    """
    def process_cancel_silence():
        try:
            # 查询 silence_type
            silence_cfg = _get_silence_config_by_maid(maid)
            silence_type = silence_cfg.get('silence_type', 'grafana')

            if silence_type == 'grafana':
                grafana_url = silence_cfg.get('grafana_url', '')
                delete_result = grafana_delete_silence(maid, grafana_url)
            else:
                delete_result = madelete(maid)

            if delete_result.get('success'):
                cancel_card = create_cancel_silence_card(maid, operator_id)
                feishu_client.reply_message(
                    open_message_id,
                    "interactive",
                    json.dumps(cancel_card),
                    reply_in_thread=True,
                )
                logger.info("cancel silence action completed: maid=%s silence_type=%s operator_id=%s", maid, silence_type, operator_id)
            else:
                error_msg = delete_result.get('message', '未知错误')
                failure_card = create_failure_card(maid, "取消静默", error_msg)
                feishu_client.reply_message(
                    open_message_id,
                    "interactive",
                    json.dumps(failure_card),
                    reply_in_thread=True,
                )
                logger.error(
                    "取消静默失败: maid=%s silence_type=%s error=%s",
                    maid, silence_type, error_msg
                )
        except Exception as e:
            failure_card = create_failure_card(maid, "取消静默", str(e))
            try:
                feishu_client.reply_message(
                    open_message_id,
                    "interactive",
                    json.dumps(failure_card),
                    reply_in_thread=True,
                )
            except Exception:
                pass
            logger.error("处理取消静默时出错: maid=%s error=%s", maid, e)

    thread = threading.Thread(target=contextual_target(process_cancel_silence))
    thread.daemon = True
    thread.start()


def create_ack_success_card(maid, incident_id, operator_id=None):
    """
    创建认领成功的卡片

    Args:
        maid: 告警ID
        incident_id: Flashcat incident ID
        operator_id: 操作人 open_id

    Returns:
        dict: 飞书卡片数据
    """
    card_data = {
        "config": {
            "wide_screen_mode": True
        },
        "header": {
            "title": {
                "tag": "plain_text",
                "content": "✅ 告警已认领"
            },
            "template": "green"
        },
        "elements": [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"**告警 {maid} 已认领**\nFlashcat incident: `{incident_id}`\n电话通知将停止"
                }
            },
            {
                "tag": "hr"
            },
            {
                "tag": "note",
                "elements": [
                    {
                        "tag": "plain_text",
                        "content": f"⏰ 操作时间: {_get_current_time()}"
                    },
                    {
                        "tag": "lark_md",
                        "content": f"👤 操作人: <at id=\"{operator_id}\"></at>" if operator_id else ""
                    }
                ]
            }
        ]
    }

    return card_data


def handle_ack_incident_action(maid, incident_id, open_message_id, feishu_client, operator_id=None):
    """
    处理认领告警操作（异步执行）

    1. 调用 Flashcat incident/ack API 认领 incident
    2. 认领成功后，原地更新原告警卡片：标题改为"已认领"、移除认领按钮、追加认领人信息
    3. 同时在话题中回复一条认领确认消息

    Args:
        maid: 告警ID
        incident_id: Flashcat incident ID
        open_message_id: 消息ID（触发回调的卡片消息ID，用于原地更新和话题回复）
        feishu_client: 飞书客户端实例
        operator_id: 操作人 open_id
    """
    def process_ack():
        try:
            from config.config import Config
            app_key = Config.FLASHCAT_APP_KEY
            if not app_key:
                logger.error("FLASHCAT_APP_KEY 未配置，无法认领 incident: maid=%s", maid)
                failure_card = create_failure_card(maid, "认领", "FLASHCAT_APP_KEY 未配置")
                feishu_client.reply_message(
                    open_message_id,
                    "interactive",
                    json.dumps(failure_card),
                    reply_in_thread=True,
                )
                return

            success = ack_incident(app_key, incident_id, maid=maid)
            if success:
                # ── 原地更新原告警卡片（认领按钮改为禁用+已认领）──
                _update_card_after_ack(feishu_client, open_message_id, operator_id, maid)
                logger.info("认领告警完成: maid=%s incident_id=%s", maid, incident_id)
            else:
                logger.error("认领告警失败: maid=%s incident_id=%s", maid, incident_id)
        except Exception as e:
            logger.error(
                "处理认领告警时出错: maid=%s incident_id=%s error=%s",
                maid, incident_id, e
            )

    thread = threading.Thread(target=contextual_target(process_ack))
    thread.daemon = True
    thread.start()


def _update_card_after_ack(feishu_client, open_message_id, operator_id=None, maid=None):
    """原地更新告警卡片：认领按钮改为禁用、追加认领人信息

    从数据库读取发送时保存的原始卡片 JSON，修改后 PATCH。
    不使用飞书 GET API（会剥离按钮 value 导致静默按钮失效）。

    Args:
        feishu_client: 飞书客户端实例
        open_message_id: 原告警卡片的消息ID
        operator_id: 认领人 open_id
        maid: 告警ID，用于从数据库读取原始卡片 JSON
    """
    if not maid:
        logger.warning("maid 为空，跳过原地更新")
        return

    # 从数据库读取发送时保存的原始卡片 JSON
    from alerts_format.savedb import get_card_content, save_card_content
    content_str = get_card_content(maid)
    if not content_str:
        logger.warning("数据库中无原始卡片 JSON，跳过原地更新: maid=%s", maid)
        return

    try:
        card = json.loads(content_str)
    except (json.JSONDecodeError, TypeError):
        logger.warning("原始卡片 JSON 解析失败，跳过原地更新: maid=%s", maid)
        return

    if not isinstance(card, dict):
        logger.warning("原始卡片内容不是 dict，跳过原地更新: maid=%s", maid)
        return

    logger.debug("原始卡片 elements 数量: maid=%s count=%d", maid, len(card.get('elements', [])))

    # ── 遍历 elements，将认领按钮改为禁用 ──
    operator_line = f"👤 认领人: <at id=\"{operator_id}\"></at>" if operator_id else "👤 认领人: 未知"
    elements = card.get('elements', [])
    for elem in elements:
        if not isinstance(elem, dict) or elem.get('tag') != 'action':
            continue
        actions = elem.get('actions', [])
        for action in actions:
            if not isinstance(action, dict):
                continue

            # 通过 value 识别认领按钮（原始卡片 JSON 中 value 完整保留）
            value = action.get('value', {})
            is_ack_button = False
            if isinstance(value, dict) and value.get('action') == 'ack_incident':
                is_ack_button = True
            elif isinstance(value, str):
                try:
                    parsed_val = json.loads(value)
                    if parsed_val.get('action') == 'ack_incident':
                        is_ack_button = True
                except (json.JSONDecodeError, TypeError):
                    pass

            if is_ack_button:
                # 禁用按钮、清空 value 使其不可点击，文案保持不变
                action['type'] = "default"
                action['disabled'] = True
                action['value'] = {}
                action.pop('url', None)
                action.pop('multi_url', None)
                action.pop('behaviors', None)
                logger.info(
                    "认领按钮已改为禁用状态: maid=%s action=%s",
                    maid, json.dumps(action, ensure_ascii=False)
                )

    # ── 在卡片末尾追加认领人信息 ──
    ack_note = {
        "tag": "note",
        "elements": [
            {"tag": "plain_text", "content": f"✅ 已认领 | {_get_current_time()}"},
            {"tag": "lark_md", "content": operator_line},
        ],
    }
    elements.append({"tag": "hr"})
    elements.append(ack_note)

    # 确保 config 中有 update_multi: True（飞书 PATCH 要求）
    config = card.get('config', {})
    if not isinstance(config, dict):
        config = {}
    config['update_multi'] = True
    card['config'] = config

    # ── 调用 PATCH 接口原地更新（带 retry）──
    card_json = json.dumps(card, ensure_ascii=False)
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            feishu_client.patch_message(open_message_id, card_json)
            save_card_content(maid, card_json)
            logger.info(
                "原卡片已原地更新为已认领状态: maid=%s message_id=%s attempt=%d/%d",
                maid, open_message_id, attempt, MAX_RETRIES
            )
            return
        except Exception as e:
            if attempt < MAX_RETRIES:
                wait = attempt * RETRY_BACKOFF_BASE
                logger.warning(
                    "原地更新卡片失败: maid=%s message_id=%s attempt=%d/%d "
                    "error=%s retry_in=%ds",
                    maid, open_message_id, attempt, MAX_RETRIES, e, wait
                )
                time.sleep(wait)
            else:
                logger.error(
                    "原地更新卡片失败: maid=%s message_id=%s attempts=%d error=%s（不影响认领流程）",
                    maid, open_message_id, MAX_RETRIES, e
                )


def parse_callback_data(data):
    """
    解析飞书卡片回调数据
    
    Args:
        data: 飞书回调的原始数据
    
    Returns:
        tuple: (action_type, action_value, open_message_id, open_id)
    """
    # 验证回调（URL验证）
    if "challenge" in data:
        logger.info("URL验证请求")
        return "challenge", data["challenge"], None, None
    
    # 兼容两种回调格式
    if "event" in data and "action" in data["event"]:
        # 事件订阅 2.0 格式
        event = data["event"]
        action = event["action"]
        context = event.get("context") or data.get("context") or {}
        open_message_id = context.get("open_message_id")
        open_id = (event.get("operator") or {}).get("open_id")
    else:
        # 旧版格式
        action = data.get("action", {})
        open_message_id = data.get("open_message_id")
        open_id = data.get("open_id")
    
    action_value_raw = action.get("value", {})
    
    # SDK 传来的 value 可能是 dict（新版）或 JSON 字符串（旧版/双重转义）
    action_value = _decode_action_value(action_value_raw)
    if not action_value:
        logger.error("解析回调数据失败")
        return None, None, None, None
    
    # 确保 action_value 是字典
    if not isinstance(action_value, dict):
        logger.error("回调数据格式错误")
        return None, None, None, None
    
    action_value["_option"] = action.get("option")
    action_value["_timezone"] = action.get("timezone")
    action_type = action_value.get("action")
    
    return action_type, action_value, open_message_id, open_id


def is_duplicate_callback(action_type, action_value, open_message_id):
    """
    检查是否为重复的回调请求（TTL 内）

    使用 BoundedTTLCache.mark() 原子化"检查并标记"操作，自带 TTL 过期清理
    与容量上限淘汰，无需手动维护过期清理逻辑。

    Args:
        action_type: 操作类型
        action_value: 操作值
        open_message_id: 消息ID
    
    Returns:
        bool: True 表示重复，False 表示不重复
    """
    callback_key = (
        f"{open_message_id}_{action_type}_{action_value.get('maid')}_"
        f"{action_value.get('_option') or ''}"
    )

    if _callback_cache.mark(callback_key):
        logger.info("duplicate callback ignored: maid=%s action=%s message_id=%s", action_value.get('maid'), action_type, open_message_id)
        return True
    return False


@traced_request
def process_card_callback(data, feishu_client):
    """
    处理飞书卡片交互回调
    
    Args:
        data: 飞书回调数据
        feishu_client: 飞书客户端实例
    
    Returns:
        dict: 响应数据
    """
    maid = None
    try:
        # 解析回调数据
        action_type, action_value, open_message_id, open_id = parse_callback_data(data)
        
        # 处理 URL 验证
        if action_type == "challenge":
            return {"challenge": action_value}
        
        # 解析失败
        if action_type is None:
            return {}

        maid = action_value.get("maid")
        set_maid(maid)
        logger.info('event=alert.callback action=%s message_id=%s', action_type, open_message_id)
        
        # 去重检查
        if action_type != "silence" and is_duplicate_callback(action_type, action_value, open_message_id):
            return {}

        if action_type == "show_custom_silence":
            original_card = _load_original_card(maid, open_message_id, feishu_client)
            picker_card = create_custom_silence_picker_card(original_card, maid)
            if not picker_card:
                logger.error("无法展开自定义时间选择器: maid=%s message_id=%s", maid, open_message_id)
                return {
                    "toast": {
                        "type": "error",
                        "content": "无法加载原告警卡片，请在新告警卡片上重试",
                    }
                }
            return {
                "toast": {"type": "info", "content": "请选择静默截止时间"},
                "card": {"type": "raw", "data": picker_card},
            }

        if action_type == "silence_until":
            ends_at = _parse_silence_until(
                action_value.get("_option"),
                action_value.get("_timezone"),
            )
            if ends_at is None:
                return {
                    "toast": {"type": "error", "content": "无法识别所选时间，请重新选择"}
                }
            if ends_at <= datetime.now().astimezone():
                return {
                    "toast": {"type": "error", "content": "静默截止时间必须晚于当前时间"}
                }

            logger.info(
                "custom silence action received: maid=%s ends_at=%s timezone=%s message_id=%s operator_id=%s",
                maid, ends_at.isoformat(), action_value.get("_timezone"), open_message_id, open_id,
            )
            handle_silence_until_action(
                maid, ends_at, open_message_id, feishu_client, open_id
            )

            # 选中有效时间后立即恢复原卡片，去掉临时日期选择框。
            response = {
                "toast": {
                    "type": "success",
                    "content": f"正在设置静默至 {ends_at.strftime('%Y-%m-%d %H:%M')}",
                }
            }
            original_card = _load_original_card(maid, open_message_id, feishu_client)
            if isinstance(original_card, dict):
                original_card.setdefault("config", {})["update_multi"] = True
                response["card"] = {"type": "raw", "data": original_card}
            return response
        
        # 处理静默操作
        if action_type == "silence":
            duration = action_value.get("duration", DEFAULT_SILENCE_DURATION)
            
            logger.info("silence action received: maid=%s duration=%s message_id=%s operator_id=%s", maid, duration, open_message_id, open_id)
            handle_silence_action(maid, duration, open_message_id, feishu_client, open_id)
            return {}
        
        # 处理取消静默操作
        elif action_type == "cancel_silence":
            logger.info("cancel silence action received: maid=%s message_id=%s operator_id=%s", maid, open_message_id, open_id)
            handle_cancel_silence_action(maid, open_message_id, feishu_client, open_id)
            return {}
        
        # 处理认领告警操作
        elif action_type == "ack_incident":
            incident_id = action_value.get("incident_id")
            
            logger.info("ack action received: maid=%s incident_id=%s message_id=%s operator_id=%s", maid, incident_id, open_message_id, open_id)
            handle_ack_incident_action(maid, incident_id, open_message_id, feishu_client, open_id)
            return {}
        
        # 未知操作
        logger.warning("未知的操作类型: maid=%s action=%s", maid, action_type)
        return {}
        
    except Exception as e:
        logger.error("处理卡片回调失败: maid=%s error=%s", maid, e, exc_info=True)
        # 即使失败也要返回空对象，避免用户看到错误提示
        return {}
