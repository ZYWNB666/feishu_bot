#!/usr/bin/env python3
"""
GitLab Webhook 路由 Blueprint

POST /api/gitlab-pipeline-status  接收 GitLab Pipeline/Push Webhook
"""

import logging

from flask import Blueprint, current_app, jsonify, request as flask_request

logger = logging.getLogger(__name__)

gitlab_bp = Blueprint("gitlab", __name__)


@gitlab_bp.route("/api/gitlab-pipeline-status", methods=["POST"])
def gitlab_pipeline_status():
    data = flask_request.get_json()
    try:
        if not data:
            return jsonify({"code": 400, "msg": "No data provided"}), 400
        headers = flask_request.headers
        group_id = headers.get("X-Gitlab-Token") if headers else None

        # 延迟导入避免循环依赖
        from gitlab_utils.pipeline_msg_format import json_processing
        feishu_client = current_app.config["FEISHU_CLIENT"]
        result, status_code = json_processing(group_id, data, feishu_client)
        return result, status_code
    except Exception as e:
        logger.error("展示Gitlab pipeline status失败: %s", e, exc_info=True)
        return jsonify({"code": 500, "msg": str(e)}), 500


@gitlab_bp.route("/api/json", methods=["GET", "POST"])
def json_api():
    if flask_request.method == "POST":
        data = flask_request.get_json()
        headers = flask_request.headers
        if headers:
            logger.info("Received headers: %s", headers)
        logger.info("Received data: %s", data)
        if not data:
            logger.error("Received empty data")
            return jsonify({"error": "No data provided"})
        return jsonify({"message": "Test API received data!", "data": data})
    return jsonify({"message": "Test API is working!"})
