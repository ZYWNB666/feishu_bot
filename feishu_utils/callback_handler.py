#!/usr/bin/env python3
"""
飞书卡片交互回调处理模块
"""

import json
import logging
import threading
import time
from datetime import datetime
import pytz

from alerts_format.ma import macreate, madelete

logger = logging.getLogger(__name__)

# 用于去重的缓存（存储最近处理过的回调）
_callback_cache = {}
_callback_cache_lock = threading.Lock()


def _get_current_time():
    """获取当前时间字符串"""
    beijing_tz = pytz.timezone('Asia/Shanghai')
    now = datetime.now(beijing_tz)
    return now.strftime('%Y-%m-%d %H:%M:%S')


def create_silence_success_card(maid, duration):
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
                        "value": json.dumps({
                            "action": "cancel_silence",
                            "maid": maid
                        })
                    }
                ]
            }
        ]
    }
    
    return card_data


def create_cancel_silence_card(maid):
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
        content = f"**告警 {maid} {action_type}操作失败**\n\n❌ 错误信息: {error_message}\n\n💡 请检查以下配置：\n- Alertmanager URL 是否正确\n- Alertmanager 服务是否正常运行\n- 网络连接是否正常"
    else:
        content = f"**告警 {maid} {action_type}操作失败**\n\n💡 请检查以下配置：\n- Alertmanager URL 是否正确\n- Alertmanager 服务是否正常运行\n- 网络连接是否正常"
    
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


def handle_silence_action(maid, duration, open_message_id, feishu_client):
    """
    处理静默操作（异步执行）
    
    Args:
        maid: 告警ID
        duration: 静默时长（秒）
        open_message_id: 消息ID
        feishu_client: 飞书客户端实例
    """
    def process_silence():
        try:
            duration_hours = duration // 3600
            silence_result = macreate(maid, duration_hours)
            
            if silence_result.get('success'):
                # 成功：发送成功卡片
                silence_card = create_silence_success_card(maid, duration)
                feishu_client.reply_message(
                    open_message_id,
                    "interactive",
                    json.dumps(silence_card)
                )
                logger.info("静默操作完成")
            else:
                # 失败：发送失败卡片
                error_msg = silence_result.get('message', '未知错误')
                failure_card = create_failure_card(maid, "静默", error_msg)
                feishu_client.reply_message(
                    open_message_id,
                    "interactive",
                    json.dumps(failure_card)
                )
                logger.error("静默创建失败")
        except Exception as e:
            # 异常：发送失败卡片
            failure_card = create_failure_card(maid, "静默", str(e))
            try:
                feishu_client.reply_message(
                    open_message_id,
                    "interactive",
                    json.dumps(failure_card)
                )
            except:
                pass
            logger.error("处理静默时出错: %s", str(e))
    
    # 启动后台线程
    thread = threading.Thread(target=process_silence)
    thread.daemon = True
    thread.start()


def handle_cancel_silence_action(maid, open_message_id, feishu_client):
    """
    处理取消静默操作（异步执行）
    
    Args:
        maid: 告警ID
        open_message_id: 消息ID
        feishu_client: 飞书客户端实例
    """
    def process_cancel_silence():
        try:
            delete_result = madelete(maid)
            
            if delete_result.get('success'):
                # 成功：发送成功卡片
                cancel_card = create_cancel_silence_card(maid)
                feishu_client.reply_message(
                    open_message_id,
                    "interactive",
                    json.dumps(cancel_card)
                )
                logger.info("取消静默操作完成")
            else:
                # 失败：发送失败卡片
                error_msg = delete_result.get('message', '未知错误')
                failure_card = create_failure_card(maid, "取消静默", error_msg)
                feishu_client.reply_message(
                    open_message_id,
                    "interactive",
                    json.dumps(failure_card)
                )
                logger.error("取消静默失败")
        except Exception as e:
            # 异常：发送失败卡片
            failure_card = create_failure_card(maid, "取消静默", str(e))
            try:
                feishu_client.reply_message(
                    open_message_id,
                    "interactive",
                    json.dumps(failure_card)
                )
            except:
                pass
            logger.error("处理取消静默时出错: %s", str(e))
    
    # 启动后台线程
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
    
    action_value_str = action.get("value", "{}")
    
    # 解析 JSON 字符串（可能需要解析两次，因为飞书会双重转义）
    try:
        if isinstance(action_value_str, str):
            action_value = json.loads(action_value_str)
            # 如果解析后还是字符串，再解析一次
            if isinstance(action_value, str):
                action_value = json.loads(action_value)
        else:
            action_value = action_value_str
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
            handle_silence_action(maid, duration, open_message_id, feishu_client)
            return {}
        
        # 处理取消静默操作
        elif action_type == "cancel_silence":
            maid = action_value.get("maid")
            
            logger.info("执行取消静默操作")
            handle_cancel_silence_action(maid, open_message_id, feishu_client)
            return {}
        
        # 未知操作
        logger.warning("未知的操作类型")
        return {}
        
    except Exception as e:
        logger.error("处理卡片回调失败: %s", e, exc_info=True)
        # 即使失败也要返回空对象，避免用户看到错误提示
        return {}

