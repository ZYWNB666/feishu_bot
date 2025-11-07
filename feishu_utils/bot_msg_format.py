

# 进群打招呼消息格式化函数


def bot_add_msg_to_group(data):
    """
    生成机器人进群打招呼消息
    
    Args:
        data: 飞书事件数据
        
    Returns:
        str: 打招呼消息内容
    """
    event = data.get("event", {})
    group_id = event.get("chat_id", "未知")
    # 飞书事件中群名称字段可能是name或chat_name
    group_name = event.get("name") or event.get("chat_name") or "本群"
    
    content = (
        f"👋 大家好！我是AlertBot，很高兴加入「{group_name}」群组\n\n"
        f"🤖 我的功能说明：\n\n"
        f"🔔 告警通知：\n"
        f"• 接收并转发Alertmanager告警\n"
        f"• 支持告警静默管理\n"
        f"🔇 静默管理：\n"
        f"• 可以通过点击告警卡片中的静默按钮进行静默管理\n"
        f"🆔 Group ID: {group_id}\n\n"
        f"如有问题，请联系管理员配置告警规则 📝"
    )
    return content

# 用户进群打招呼消息格式化函数
def user_add_msg_to_group(data):
    """
    生成用户进群打招呼消息
    
    Args:
        data: 飞书事件数据
        
    Returns:
        str: 打招呼消息内容
    """
    event = data.get("event", {})
    group_id = event.get("chat_id", "未知")
    group_name = event.get("name") or event.get("chat_name") or "本群"
    content = (
        f"👋 Hi！我是AlertBot，欢迎加入「{group_name}」群组！\n\n"
        f"🆔 Group ID: {group_id}"
    )
    return content















