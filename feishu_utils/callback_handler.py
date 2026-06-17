#!/usr/bin/env python3
"""
飞书卡片交互回调处理模块
"""

import json
import logging
import threading
import time
from datetime import datetime
import mysql.connector

from alerts_format.ma import macreate, madelete
from alerts_format.grafana_silence import grafana_create_silence, grafana_delete_silence

logger = logging.getLogger(__name__)

# 用于去重的缓存（存储最近处理过的回调）
_callback_cache = {}
_callback_cache_lock = threading.Lock()


def _get_current_time():
    """获取当前时间字符串（容器已配置上海时区，直接用本地时间）"""
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def _get_silence_config_by_maid(maid: str) -> dict:
    """
    通过 maid 查找对应的 silence_type 和 grafana_url
    先从 alert_data 查 project，再从 alert_config 查路由配置
    """
    from config import config
    connection = None
    try:
        db_config = config.get_alert_db_config()
        connection = mysql.connector.connect(**db_config)
        cursor = connection.cursor(dictionary=True)
        cursor.execute("SELECT project FROM alert_data WHERE id = %s", (maid,))
        row = cursor.fetchone()
        if not row or not row.get('project'):
            return {}
        project = row['project']
        cursor.close()

        # 查 alert_config
        cfg_conn = mysql.connector.connect(**config.get_config_db_config())
        cfg_cursor = cfg_conn.cursor(dictionary=True)
        cfg_cursor.execute(
            "SELECT silence_type, grafana_url FROM alert_config WHERE project = %s LIMIT 1",
            (project,)
        )
        cfg_row = cfg_cursor.fetchone()
        cfg_cursor.close()
        cfg_conn.close()
        return cfg_row or {}
    except Exception as e:
        logger.error("查询 silence_config 失败: %s", e)
        return {}
    finally:
        if connection and connection.is_connected():
            connection.close()


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
    检查是否为重复的回调请求（5秒内）
    
    Args:
        action_type: 操作类型
        action_value: 操作值
        open_message_id: 消息ID
    
    Returns:
        bool: True 表示重复，False 表示不重复
    """
    callback_key = f"{open_message_id}_{action_type}_{action_value.get('maid')}"
    current_time = time.time()
    
    with _callback_cache_lock:
        # 清理5秒前的缓存
        expired_keys = [k for k, v in _callback_cache.items() if current_time - v > 5]
        for k in expired_keys:
            del _callback_cache[k]
        
        # 检查是否重复
        if callback_key in _callback_cache:
            logger.info("重复回调已忽略")
            return True
        
        # 记录此次回调
        _callback_cache[callback_key] = current_time
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
            duration = action_value.get("duration", 7200)
            
            logger.info("执行静默操作")
            handle_silence_action(maid, duration, open_message_id, feishu_client, open_id)
            return {}
        
        # 处理取消静默操作
        elif action_type == "cancel_silence":
            maid = action_value.get("maid")
            
            logger.info("执行取消静默操作")
            handle_cancel_silence_action(maid, open_message_id, feishu_client, open_id)
            return {}
        
        # 未知操作
        logger.warning("未知的操作类型")
        return {}
        
    except Exception as e:
        logger.error("处理卡片回调失败: %s", e, exc_info=True)
        # 即使失败也要返回空对象，避免用户看到错误提示
        return {}

