#!/usr/bin/env python3
"""
系统路由 Blueprint

GET  /api/health         健康检查
POST /api/card_callback  飞书卡片交互回调
POST /webhook/event      飞书事件回调接口
GET  /                   前端管理页面
"""

import logging

from flask import Blueprint, current_app, jsonify, request as flask_request, send_from_directory

from config import config

logger = logging.getLogger(__name__)

system_bp = Blueprint("system", __name__)


@system_bp.route("/api/health", methods=["GET"])
def health_check():
    """健康检查接口"""
    return jsonify({
        "code": 0,
        "msg": "service is running",
        "data": {
            "app_id": config.APP_ID,
            "lark_host": config.LARK_HOST,
            "config": config.show_config()
        }
    })


@system_bp.route("/api/card_callback", methods=["POST"])
def card_callback():
    """处理飞书卡片交互回调，委托给 callback_handler 模块处理具体逻辑"""
    data = flask_request.get_json(silent=True)
    if not data:
        return jsonify({})
    # 延迟导入避免循环依赖
    from feishu_utils.callback_handler import process_card_callback
    feishu_client = current_app.config["FEISHU_CLIENT"]
    result = process_card_callback(data, feishu_client)
    return jsonify(result)


@system_bp.route("/webhook/event", methods=["POST"])
def webhook_event():
    """飞书事件回调接口

    用于处理URL验证和接收飞书事件。
    配置地址: http://your-domain/webhook/event
    委托给 event_handler 模块处理具体逻辑。
    """
    data = flask_request.get_json(silent=True)
    if not data:
        return jsonify({"code": 400, "msg": "请求体不能为空或非JSON格式"}), 400
    # 延迟导入避免循环依赖
    from feishu_utils.event_handler import feishu_event
    feishu_client = current_app.config["FEISHU_CLIENT"]
    result, status_code = feishu_event(feishu_client, data)
    return jsonify(result), status_code


@system_bp.route("/")
@system_bp.route("/index.html")
def index():
    """前端管理页面"""
    return send_from_directory('static', 'index.html')
