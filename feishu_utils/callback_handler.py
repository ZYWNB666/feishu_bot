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

from alerts_format.ma import macreate, madelete
from alerts_format.grafana_silence import grafana_create_silence, grafana_delete_silence
from alerts_format.flashcat_utils import ack_incident
from config.constants import (
    CALLBACK_CACHE_TTL,
    CALLBACK_CACHE_MAXSIZE,
    MAX_RETRIES,
    RETRY_BACKOFF_BASE,
    DEFAULT_SILENCE_DURATION,
)
from db.pool import db_cursor
from utils.bounded_cache import BoundedTTLCache

logger = logging.getLogger(__name__)

# 用于去重的缓存（存储最近处理过的回调）
# 使用带容量上限的 TTL 缓存，防止长时间运行后内存无限增长
_callback_cache = BoundedTTLCache(maxsize=CALLBACK_CACHE_MAXSIZE, ttl=CALLBACK_CACHE_TTL)
_callback_cache_lock = threading.Lock()  # 保留用于跨缓存原子操作


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
        logger.error("查询 silence_config 失败: %s", e)
        return {}


def create_silence_success_card(maid, duration, operator_id=None):
    """
    创建静默成功的卡片
    
    Args:
        maid: 告警ID
        duration: 静默时长（秒）
    
    Returns:
        dict: 飞书卡片数据
    """
    # 智能显示时间单位
    duration_hours = duration // 3600
    if duration_hours >= 24:
        duration_days = duration_hours // 24
        duration_text = f"{duration_days} 天"
    else:
        duration_text = f"{duration_hours} 小时"
    
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
                    "content": f"**告警 {maid} 已静默 {duration_text}**\n在此期间不会发送此告警通知"
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
            },
            {
                "tag": "action",
                "actions": [
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

            if silence_type == 'grafana':
                grafana_url = silence_cfg.get('grafana_url', '')
                silence_result = grafana_create_silence(maid, duration_hours, grafana_url)
            else:
                silence_result = macreate(maid, duration_hours)

            if silence_result.get('success'):
                silence_card = create_silence_success_card(maid, duration, operator_id)
                feishu_client.reply_message(
                    open_message_id,
                    "interactive",
                    json.dumps(silence_card),
                    reply_in_thread=True,
                )
                logger.info("静默操作完成（%s）", silence_type)
            else:
                error_msg = silence_result.get('message', '未知错误')
                failure_card = create_failure_card(maid, "静默", error_msg)
                feishu_client.reply_message(
                    open_message_id,
                    "interactive",
                    json.dumps(failure_card),
                    reply_in_thread=True,
                )
                logger.error("静默创建失败")
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
            logger.error("处理静默时出错: %s", e)

    thread = threading.Thread(target=process_silence)
    thread.daemon = True
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
                logger.info("取消静默操作完成（%s）", silence_type)
            else:
                error_msg = delete_result.get('message', '未知错误')
                failure_card = create_failure_card(maid, "取消静默", error_msg)
                feishu_client.reply_message(
                    open_message_id,
                    "interactive",
                    json.dumps(failure_card),
                    reply_in_thread=True,
                )
                logger.error("取消静默失败")
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
            logger.error("处理取消静默时出错: %s", e)

    thread = threading.Thread(target=process_cancel_silence)
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
                logger.error("FLASHCAT_APP_KEY 未配置，无法认领 incident")
                failure_card = create_failure_card(maid, "认领", "FLASHCAT_APP_KEY 未配置")
                feishu_client.reply_message(
                    open_message_id,
                    "interactive",
                    json.dumps(failure_card),
                    reply_in_thread=True,
                )
                return

            success = ack_incident(app_key, incident_id)
            if success:
                # ── 原地更新原告警卡片（认领按钮改为禁用+已认领）──
                _update_card_after_ack(feishu_client, open_message_id, operator_id, maid)
                logger.info("认领告警完成: maid=%s incident_id=%s", maid, incident_id)
            else:
                logger.error("认领告警失败: maid=%s incident_id=%s", maid, incident_id)
        except Exception as e:
            logger.error("处理认领告警时出错: %s", e)

    thread = threading.Thread(target=process_ack)
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
    from alerts_format.savedb import get_card_content
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

    logger.debug("原始卡片 elements 数量: %d", len(card.get('elements', [])))

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
                logger.info("认领按钮已改为禁用状态: %s", json.dumps(action, ensure_ascii=False))

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
            logger.info("原卡片已原地更新为已认领状态: message_id=%s (attempt %d/%d)", open_message_id, attempt, MAX_RETRIES)
            return
        except Exception as e:
            if attempt < MAX_RETRIES:
                wait = attempt * RETRY_BACKOFF_BASE
                logger.warning("原地更新卡片失败 (attempt %d/%d): %s, %d秒后重试", attempt, MAX_RETRIES, e, wait)
                time.sleep(wait)
            else:
                logger.error("原地更新卡片失败（已重试 %d 次）: %s（不影响认领流程）", MAX_RETRIES, e)


def parse_callback_data(data):
    """
    解析飞书卡片回调数据
    
    Args:
        data: 飞书回调的原始数据
    
    Returns:
        tuple: (action_type, action_value, open_message_id, open_id)
    """
    logger.info("收到卡片回调")
    
    # 验证回调（URL验证）
    if "challenge" in data:
        logger.info("URL验证请求")
        return "challenge", data["challenge"], None, None
    
    # 兼容两种回调格式
    if "event" in data and "action" in data["event"]:
        # 事件订阅 2.0 格式
        action = data["event"]["action"]
        open_message_id = data["event"]["context"]["open_message_id"]
        open_id = data["event"]["operator"]["open_id"]
    else:
        # 旧版格式
        action = data.get("action", {})
        open_message_id = data.get("open_message_id")
        open_id = data.get("open_id")
    
    action_value_raw = action.get("value", {})
    
    # SDK 传来的 value 可能是 dict（新版）或 JSON 字符串（旧版/双重转义）
    try:
        if isinstance(action_value_raw, dict):
            action_value = action_value_raw
        elif isinstance(action_value_raw, str):
            action_value = json.loads(action_value_raw)
            if isinstance(action_value, str):
                action_value = json.loads(action_value)
        else:
            action_value = {}
    except json.JSONDecodeError:
        logger.error("解析回调数据失败")
        return None, None, None, None
    
    # 确保 action_value 是字典
    if not isinstance(action_value, dict):
        logger.error("回调数据格式错误")
        return None, None, None, None
    
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
    callback_key = f"{open_message_id}_{action_type}_{action_value.get('maid')}"

    if _callback_cache.mark(callback_key):
        logger.info("重复回调已忽略")
        return True
    return False


def process_card_callback(data, feishu_client):
    """
    处理飞书卡片交互回调
    
    Args:
        data: 飞书回调数据
        feishu_client: 飞书客户端实例
    
    Returns:
        dict: 响应数据
    """
    try:
        # 解析回调数据
        action_type, action_value, open_message_id, open_id = parse_callback_data(data)
        
        # 处理 URL 验证
        if action_type == "challenge":
            return {"challenge": action_value}
        
        # 解析失败
        if action_type is None:
            return {}
        
        # 去重检查
        if is_duplicate_callback(action_type, action_value, open_message_id):
            return {}
        
        # 处理静默操作
        if action_type == "silence":
            maid = action_value.get("maid")
            duration = action_value.get("duration", DEFAULT_SILENCE_DURATION)
            
            logger.info("执行静默操作")
            handle_silence_action(maid, duration, open_message_id, feishu_client, open_id)
            return {}
        
        # 处理取消静默操作
        elif action_type == "cancel_silence":
            maid = action_value.get("maid")
            
            logger.info("执行取消静默操作")
            handle_cancel_silence_action(maid, open_message_id, feishu_client, open_id)
            return {}
        
        # 处理认领告警操作
        elif action_type == "ack_incident":
            maid = action_value.get("maid")
            incident_id = action_value.get("incident_id")
            
            logger.info("执行认领告警操作: maid=%s incident_id=%s", maid, incident_id)
            handle_ack_incident_action(maid, incident_id, open_message_id, feishu_client, open_id)
            return {}
        
        # 未知操作
        logger.warning("未知的操作类型: %s", action_type)
        return {}
        
    except Exception as e:
        logger.error("处理卡片回调失败: %s", e, exc_info=True)
        # 即使失败也要返回空对象，避免用户看到错误提示
        return {}

