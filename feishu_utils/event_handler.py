#!/usr/bin/env python3
"""
飞书事件处理模块
统一处理各类飞书事件，包括机器人进群、用户进群等
"""

import json
import logging
from .bot_msg_format import bot_add_msg_to_group, user_add_msg_to_group

logger = logging.getLogger(__name__)


def handle_bot_added_to_group(feishu_client, event_data):
    """
    处理机器人进群事件
    
    Args:
        feishu_client: 飞书API客户端实例
        event_data: 飞书事件数据
        
    Returns:
        bool: 处理是否成功
    """
    try:
        chat_id = event_data.get("event", {}).get("chat_id")
        if not chat_id:
            logger.warning("机器人进群事件中没有chat_id")
            return False
        
        # 获取打招呼消息内容
        welcome_text = bot_add_msg_to_group(event_data)
        
        # 发送消息到群聊
        content = json.dumps({"text": welcome_text})
        feishu_client.send("chat_id", chat_id, "text", content)
        
        logger.info("✅ 已向群聊 %s 发送机器人打招呼消息", chat_id)
        return True
        
    except Exception as e:
        logger.error("发送机器人进群打招呼消息失败: %s", e, exc_info=True)
        return False


def handle_user_added_to_group(feishu_client, event_data):
    """
    处理用户进群事件
    
    Args:
        feishu_client: 飞书API客户端实例
        event_data: 飞书事件数据
        
    Returns:
        bool: 处理是否成功
    """
    try:
        chat_id = event_data.get("event", {}).get("chat_id")
        if not chat_id:
            logger.warning("用户进群事件中没有chat_id")
            return False
        
        # 获取打招呼消息内容
        welcome_text = user_add_msg_to_group(event_data)
        
        # 发送消息到群聊
        content = json.dumps({"text": welcome_text})
        feishu_client.send("chat_id", chat_id, "text", content)
        
        logger.info("✅ 已向群聊 %s 发送用户打招呼消息", chat_id)
        return True
        
    except Exception as e:
        logger.error("发送用户进群打招呼消息失败: %s", e, exc_info=True)
        return False


def handle_message_received(feishu_client, event_data):
    """
    处理接收到的消息事件
    
    Args:
        feishu_client: 飞书API客户端实例
        event_data: 飞书事件数据
        
    Returns:
        bool: 处理是否成功
    """
    try:
        # 获取事件数据
        event = event_data.get("event", {})
        message = event.get("message", {})
        sender = event.get("sender", {})
        
        # 获取消息内容
        content_str = message.get("content")
        if not content_str:
            logger.warning("消息内容为空")
            return False
        
        # 解析消息内容
        try:
            content = json.loads(content_str)
        except json.JSONDecodeError as e:
            logger.error(f"解析消息内容失败: {e}")
            return False
        
        # 获取消息文本
        text = content.get("text", "").strip()
        if not text:
            logger.debug("消息文本为空")
            return True
        
        # 获取发送者信息
        sender_id = sender.get("sender_id", {}).get("open_id")
        
        # 检查是否有@机器人
        mentions = message.get("mentions", [])
        
        # 解析命令（去除@内容）
        # 如果消息以@开头，提取实际的命令文本
        command_text = text
        if mentions:
            # 去除@标记，获取纯命令
            for mention in mentions:
                mention_key = mention.get("key", "")
                if mention_key:
                    command_text = command_text.replace(mention_key, "").strip()
        
        # 分割命令和参数
        parts = command_text.split()
        if not parts:
            return True
        
        command = parts[0].lower()
        
        # 获取消息ID（用于引用回复）
        message_id = message.get("message_id")
        
        # 定义命令处理
        if command == "help":
            # 构建卡片消息（支持 Markdown）
            card_data = {
                "config": {
                    "wide_screen_mode": True
                },
                "header": {
                    "title": {
                        "tag": "plain_text",
                        "content": "📖 可用命令列表"
                    },
                    "template": "blue"
                },
                "elements": [
                    {
                        "tag": "div",
                        "text": {
                            "tag": "lark_md",
                            "content": "**help** - 显示此帮助信息\n**myuid** - 查看你的用户ID"
                        }
                    }
                ]
            }
            
            # 使用引用回复（卡片消息）
            reply_content = json.dumps(card_data)
            feishu_client.reply_message(message_id, "interactive", reply_content)
            logger.info(f"已回复help命令给用户 {sender_id}")
            
        elif command == "myuid":
            # 构建卡片消息（支持 Markdown）
            if sender_id:
                uid_text = f"**您的用户ID：**\n{sender_id}"
            else:
                uid_text = "**提示：** 抱歉，无法获取您的用户ID"
            
            card_data = {
                "config": {
                    "wide_screen_mode": True
                },
                "header": {
                    "title": {
                        "tag": "plain_text",
                        "content": "🆔 用户信息"
                    },
                    "template": "green"
                },
                "elements": [
                    {
                        "tag": "div",
                        "text": {
                            "tag": "lark_md",
                            "content": uid_text
                        }
                    }
                ]
            }
            
            # 使用引用回复（卡片消息）
            reply_content = json.dumps(card_data)
            feishu_client.reply_message(message_id, "interactive", reply_content)
            logger.info(f"已回复myuid命令给用户 {sender_id}")
            
        else:
            logger.debug(f"收到未知命令: {command}")
        
        return True
        
    except Exception as e:
        logger.error(f"处理消息失败: {e}", exc_info=True)
        return False



def alert_to_feishu(feishu_client, alert_data, mentioned_user_list, group_id, alertname="告警通知", severity="warning", maid=None):
    """
    处理告警信息发送到飞书（卡片格式）
    
    Args:
        feishu_client: 飞书API客户端实例
        alert_data: 告警信息内容
        mentioned_user_list: 被@的用户ID列表（open_id）
        group_id: 群组ID
        alertname: 告警名称，用作卡片标题
        severity: 告警级别 (critical/warning/info/success)，默认warning
        maid: 告警MAID，用于静默功能
        
    Returns:
        int: HTTP状态码
    """
    try:
        # 告警级别对应的卡片颜色
        color_map = {
            "critical": "red",
            "warning": "orange", 
            "info": "blue",
            "success": "green"
        }
        template_color = color_map.get(severity.lower(), "orange")
        
        # 构建标题（使用 alertname）
        title_content = f"🔔 {alertname}"
        
        # 构建卡片元素列表
        elements = []
        
        # 如果有艾特人员，在最前面添加艾特区域（显眼位置）
        if mentioned_user_list:
            mention_content = ""
            for user_id in mentioned_user_list:
                mention_content += f'<at id="{user_id}"></at> '
            
            elements.append({
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"**📢 通知人员：** {mention_content}"
                }
            })
            elements.append({
                "tag": "hr"
            })
        
        # 添加告警详细信息
        elements.append({
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": alert_data
            }
        })
        
        # 添加分隔线和时间戳
        elements.append({
            "tag": "hr"
        })
        elements.append({
            "tag": "note",
            "elements": [
                {
                    "tag": "plain_text",
                    "content": f"⏰ 发送时间: {_get_current_time()}"
                }
            ]
        })
        
        # 如果有MAID，添加静默时间选择按钮
        if maid:
            elements.append({
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {
                            "tag": "plain_text",
                            "content": "🔕 静默2小时"
                        },
                        "type": "primary",
                        "value": json.dumps({
                            "action": "silence",
                            "maid": maid,
                            "duration": 7200  # 2小时
                        })
                    },
                    {
                        "tag": "button",
                        "text": {
                            "tag": "plain_text",
                            "content": "🔕 静默12小时"
                        },
                        "type": "primary",
                        "value": json.dumps({
                            "action": "silence",
                            "maid": maid,
                            "duration": 43200  # 12小时
                        })
                    },
                    {
                        "tag": "button",
                        "text": {
                            "tag": "plain_text",
                            "content": "🔕 静默24小时"
                        },
                        "type": "primary",
                        "value": json.dumps({
                            "action": "silence",
                            "maid": maid,
                            "duration": 86400  # 24小时
                        })
                    },
                    {
                        "tag": "button",
                        "text": {
                            "tag": "plain_text",
                            "content": "🔕 静默3天"
                        },
                        "type": "primary",
                        "value": json.dumps({
                            "action": "silence",
                            "maid": maid,
                            "duration": 259200  # 3天
                        })
                    }
                ]
            })
        
        # 构建飞书卡片消息
        card_data = {
            "config": {
                "wide_screen_mode": True
            },
            "header": {
                "title": {
                    "tag": "plain_text",
                    "content": title_content
                },
                "template": template_color
            },
            "elements": elements
        }
        
        # 发送卡片消息
        content = json.dumps(card_data)
        feishu_client.send("chat_id", group_id, "interactive", content)
        
        logger.info("✅ 已向群聊 %s 发送告警卡片消息", group_id)
        if mentioned_user_list:
            logger.info("📢 艾特用户: %s", ", ".join(mentioned_user_list))
        
        return 200
        
    except Exception as e:
        logger.error("发送告警信息失败: %s", e, exc_info=True)
        return 500


def _get_current_time():
    """获取当前时间字符串"""
    from datetime import datetime
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def feishu_event(feishu_client, data):
    """
    统一处理飞书事件回调
    用于处理URL验证和接收飞书各类事件
    
    Args:
        feishu_client: 飞书API客户端实例
        data: 飞书事件数据
        
    Returns:
        tuple: (response_dict, status_code)
    """
    try:
        if not data:
            logger.warning("收到空的webhook请求")
            return {"code": 400, "msg": "空请求"}, 400
        
        # 处理URL验证（飞书配置事件订阅时会发送验证请求）
        if data.get("type") == "url_verification":
            challenge = data.get("challenge")
            logger.info("✅ URL验证请求，返回challenge: %s", challenge)
            
            # 返回challenge进行验证
            return {"challenge": challenge}, 200
        
        # 处理其他事件
        event_type = data.get("header", {}).get("event_type")
        logger.info("收到飞书事件: %s", event_type)

        # 根据事件类型分发处理
        if event_type == "im.message.receive_v1":
            logger.info("收到飞书文字消息")
            logger.debug("收到飞书文字消息内容: %s", data)
            try:
                handle_message_received(feishu_client, data)
            except Exception as e:
                logger.error(f"处理飞书文字消息失败: {e}", exc_info=True)
        elif event_type == "im.message.message_read_v1":
            logger.info("收到飞书消息阅读事件")
            logger.debug("收到飞书消息阅读事件内容: %s", data)
        elif event_type == "im.message.recalled_v1":
            logger.info("收到飞书消息撤回事件")
            logger.debug("收到飞书消息撤回事件内容: %s", data)
        elif event_type == "im.message.reaction.created_v1":
            logger.info("收到飞书表情回复事件")
            logger.debug("收到飞书表情回复事件内容: %s", data)
        elif event_type == "im.message.reaction.deleted_v1":
            logger.info("收到飞书表情删除事件")
            logger.debug("收到飞书表情删除事件内容: %s", data)
        elif event_type == "im.chat.disbanded_v1":
            logger.info("收到飞书群聊解散事件")
            logger.debug("收到飞书群聊解散事件内容: %s", data)
        elif event_type == "im.chat.updated_v1":
            logger.info("收到飞书群配置修改事件")
            logger.debug("收到飞书群配置修改事件内容: %s", data)
        elif event_type == "im.chat.member.user.added_v1":
            logger.info("收到飞书用户进群事件")
            logger.debug("收到飞书用户进群事件内容: %s", data)
            # 用户进群后发送打招呼消息
            try:
                handle_user_added_to_group(feishu_client, data)
            except Exception as e:
                logger.error(f"发送用户进群打招呼消息失败: {e}", exc_info=True)
        elif event_type == "im.chat.member.user.withdrawn_v1":
            logger.info("收到飞书撤销入群事件")
            logger.debug("收到飞书撤销入群事件内容: %s", data)
        elif event_type == "im.chat.member.bot.added_v1":
            logger.info("收到飞书机器人进群事件")
            logger.debug("收到飞书机器人进群事件内容: %s", data)
            # 机器人进群后发送打招呼消息
            try:
                handle_bot_added_to_group(feishu_client, data)
            except Exception as e:
                logger.error(f"发送机器人进群打招呼消息失败: {e}", exc_info=True)
        elif event_type == "im.chat.member.bot.deleted_v1":
            logger.info("收到飞书机器人退群事件")
            logger.debug("收到飞书机器人退群事件内容: %s", data)
        elif event_type == "p2p_chat_create":
            logger.info("收到飞书用户和机器人的会话首次被创建事件")
            logger.debug("收到飞书用户和机器人的会话首次被创建事件内容: %s", data)
        elif event_type == "im.chat.member.user.status_updated_v1":
            logger.info("收到飞书用户状态修改事件")
            logger.debug("收到飞书用户状态修改事件内容: %s", data)
        
        return {"code": 0, "msg": "success"}, 200
        
    except Exception as e:
        logger.error("处理webhook事件失败: %s", e, exc_info=True)
        return {"code": 500, "msg": str(e)}, 500
