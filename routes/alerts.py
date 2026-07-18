#!/usr/bin/env python3
"""
告警接入路由 Blueprint

POST /api/v1/alerts  接收 Alertmanager/Grafana 告警
"""

import logging

from flask import Blueprint, current_app, jsonify, request as flask_request

logger = logging.getLogger(__name__)

alerts_bp = Blueprint("alerts", __name__)


@alerts_bp.route("/api/v1/alerts", methods=["POST"])
def alert_api():
    """告警API，委托给 alert_handler 模块处理具体逻辑"""
    data = flask_request.get_json(silent=True)
    logger.debug("Received alert request: %s", data)
    if not data:
        return jsonify({"code": 400, "msg": "请求体不能为空或非JSON格式"}), 400

    # 延迟导入避免循环依赖
    from feishu_utils.alert_handler import process_alert_request
    feishu_client = current_app.config["FEISHU_CLIENT"]
    result, status_code = process_alert_request(data, feishu_client)
    return jsonify(result), status_code
