#!/usr/bin/env python3
"""
飞书Bot消息发送示例脚本
使用此脚本测试向群聊发送消息
"""

import requests
import json
from datetime import datetime

# Bot服务地址
BOT_API_URL = "http://localhost:3000"

def send_text_message(chat_id, text):
    """
    发送文本消息到群聊
    
    Args:
        chat_id: 群聊ID，格式如 oc_xxxxxxxxxxxxxxxx
        text: 要发送的文本内容
    
    Returns:
        dict: API响应结果
    """
    url = f"{BOT_API_URL}/api/send_text"
    data = {
        "chat_id": chat_id,
        "text": text
    }
    
    try:
        response = requests.post(url, json=data, timeout=10)
        result = response.json()
        
        if result.get("code") == 0:
            print(f"✅ 消息发送成功: {text}")
        else:
            print(f"❌ 消息发送失败: {result.get('msg')}")
        
        return result
    except requests.exceptions.RequestException as e:
        print(f"❌ 请求失败: {e}")
        return None


def send_rich_message(chat_id, title, content_items):
    """
    发送富文本消息到群聊
    
    Args:
        chat_id: 群聊ID
        title: 消息标题
        content_items: 内容列表，每项为一行文本
    
    Returns:
        dict: API响应结果
    """
    url = f"{BOT_API_URL}/api/send_message"
    
    # 构建富文本内容
    content_elements = []
    for item in content_items:
        content_elements.append([
            {
                "tag": "text",
                "text": item
            }
        ])
    
    data = {
        "receive_id": chat_id,
        "receive_id_type": "chat_id",
        "msg_type": "post",
        "content": {
            "zh_cn": {
                "title": title,
                "content": content_elements
            }
        }
    }
    
    try:
        response = requests.post(url, json=data, timeout=10)
        result = response.json()
        
        if result.get("code") == 0:
            print(f"✅ 富文本消息发送成功")
        else:
            print(f"❌ 富文本消息发送失败: {result.get('msg')}")
        
        return result
    except requests.exceptions.RequestException as e:
        print(f"❌ 请求失败: {e}")
        return None


def send_alert_card(chat_id, alert_title, alert_content, level="warning"):
    """
    发送告警卡片消息
    
    Args:
        chat_id: 群聊ID
        alert_title: 告警标题
        alert_content: 告警详细内容
        level: 告警级别 (red/orange/yellow/green/blue)
    
    Returns:
        dict: API响应结果
    """
    url = f"{BOT_API_URL}/api/send_message"
    
    # 根据级别选择颜色
    color_map = {
        "critical": "red",
        "warning": "orange",
        "info": "blue",
        "success": "green"
    }
    template_color = color_map.get(level, "orange")
    
    data = {
        "receive_id": chat_id,
        "receive_id_type": "chat_id",
        "msg_type": "interactive",
        "content": {
            "config": {
                "wide_screen_mode": True
            },
            "header": {
                "title": {
                    "tag": "plain_text",
                    "content": alert_title
                },
                "template": template_color
            },
            "elements": [
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": alert_content
                    }
                },
                {
                    "tag": "note",
                    "elements": [
                        {
                            "tag": "plain_text",
                            "content": f"告警时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                        }
                    ]
                }
            ]
        }
    }
    
    try:
        response = requests.post(url, json=data, timeout=10)
        result = response.json()
        
        if result.get("code") == 0:
            print(f"✅ 告警卡片发送成功")
        else:
            print(f"❌ 告警卡片发送失败: {result.get('msg')}")
        
        return result
    except requests.exceptions.RequestException as e:
        print(f"❌ 请求失败: {e}")
        return None


def check_service_health():
    """检查Bot服务健康状态"""
    url = f"{BOT_API_URL}/api/health"
    
    try:
        response = requests.get(url, timeout=5)
        result = response.json()
        
        if result.get("code") == 0:
            print("✅ Bot服务运行正常")
            print(f"   APP_ID: {result['data']['app_id']}")
            print(f"   LARK_HOST: {result['data']['lark_host']}")
            return True
        else:
            print("❌ Bot服务异常")
            return False
    except requests.exceptions.RequestException as e:
        print(f"❌ 无法连接到Bot服务: {e}")
        print(f"   请确保服务已启动: python main.py")
        return False


def main():
    """主函数 - 示例用法"""
    
    print("=" * 60)
    print("飞书Bot消息发送测试")
    print("=" * 60)
    print()
    
    # 1. 检查服务健康状态
    print("1. 检查服务状态...")
    if not check_service_health():
        return
    print()
    
    # 2. 配置你的群聊ID（需要修改为实际的群聊ID）
    CHAT_ID = "oc_550ce8d6930977facb3153b4d27c772c"  # ⚠️ 请替换为你的实际群聊ID
    
    if CHAT_ID == "oc_xxxxxxxxxxxxxxxx":
        print("⚠️  请先修改脚本中的 CHAT_ID 为你的实际群聊ID")
        print("   如何获取chat_id:")
        print("   1. 在飞书网页版进入群聊，URL中包含chat_id")
        print("   2. 或让机器人收到消息后，从日志中查看")
        return
    
    # 3. 发送简单文本消息
    print("2. 发送文本消息...")
    send_text_message(CHAT_ID, "🤖 这是来自AlertBot的测试消息")
    print()
    
    # 4. 发送富文本消息
    print("3. 发送富文本消息...")
    send_rich_message(
        CHAT_ID,
        "系统状态报告",
        [
            "服务器: server-01",
            "状态: 运行正常 ✅",
            f"检查时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        ]
    )
    print()
    
    # 5. 发送告警卡片
    print("4. 发送告警卡片...")
    send_alert_card(
        CHAT_ID,
        "系统告警通知",
        "**告警级别**: 警告\n**告警内容**: CPU使用率达到85%\n**服务器**: server-01",
        level="warning"
    )
    print()
    
    print("=" * 60)
    print("测试完成！")
    print("=" * 60)


if __name__ == "__main__":
    main()

