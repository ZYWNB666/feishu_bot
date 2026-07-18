#!/usr/bin/env python3
"""
消息发送路由 Blueprint

POST /api/send_message  主动发送完整消息
POST /api/send_text     快捷发送文本消息
"""

import json
import logging

from flask import Blueprint, current_app, jsonify, request as flask_request

logger = logging.getLogger(__name__)

messages_bp = Blueprint("messages", __name__)


@messages_bp.route("/api/send_message", methods=["POST"])
def send_message_api():
    """主动发送消息API

    请求示例:
    {
        "receive_id": "oc_xxx",
        "receive_id_type": "chat_id",
        "msg_type": "text",
        "content": {"text": "你好，这是一条测试消息"}
    }
    """
    try:
        data = flask_request.get_json(silent=True)

        if not data:
            return jsonify({"code": 400, "msg": "请求体不能为空"}), 400

        receive_id = data.get("receive_id")
        receive_id_type = data.get("receive_id_type", "chat_id")
        msg_type = data.get("msg_type", "text")
        content = data.get("content")

        if not receive_id:
            return jsonify({"code": 400, "msg": "receive_id不能为空"}), 400

        if not content:
            return jsonify({"code": 400, "msg": "content不能为空"}), 400

        # 将content转换为JSON字符串
        if isinstance(content, dict):
            content_str = json.dumps(content)
        else:
            content_str = content

        feishu_client = current_app.config["FEISHU_CLIENT"]
        logger.info("发送消息到 %s:%s", receive_id_type, receive_id)
        feishu_client.send(receive_id_type, receive_id, msg_type, content_str)

        return jsonify({
            "code": 0,
            "msg": "success",
            "data": {
                "receive_id": receive_id,
                "receive_id_type": receive_id_type,
                "msg_type": msg_type
            }
        })

    except Exception as e:
        logger.error("发送消息失败: %s", e)
        return jsonify({"code": 500, "msg": str(e)}), 500


@messages_bp.route("/api/send_text", methods=["POST"])
def send_text_api():
    """快捷发送文本消息API

    请求示例:
    {
        "chat_id": "oc_xxx",
        "text": "你好，这是一条测试消息"
    }
    或发送给个人:
    {
        "open_id": "ou_xxx",
        "text": "你好，这是一条测试消息"
    }
    """
    try:
        data = flask_request.get_json(silent=True)

        if not data:
            return jsonify({"code": 400, "msg": "请求体不能为空"}), 400

        text = data.get("text")
        if not text:
            return jsonify({"code": 400, "msg": "text不能为空"}), 400

        chat_id = data.get("chat_id")
        open_id = data.get("open_id")

        content = json.dumps({"text": text})
        feishu_client = current_app.config["FEISHU_CLIENT"]

        if chat_id:
            logger.info("发送文本消息到群聊: %s", chat_id)
            feishu_client.send("chat_id", chat_id, "text", content)
            return jsonify({
                "code": 0,
                "msg": "success",
                "data": {"chat_id": chat_id, "text": text}
            })
        elif open_id:
            logger.info("发送文本消息到用户: %s", open_id)
            feishu_client.send("open_id", open_id, "text", content)
            return jsonify({
                "code": 0,
                "msg": "success",
                "data": {"open_id": open_id, "text": text}
            })
        else:
            return jsonify({"code": 400, "msg": "chat_id和open_id至少提供一个"}), 400

    except Exception as e:
        logger.error("发送文本消息失败: %s", e)
        return jsonify({"code": 500, "msg": str(e)}), 500
