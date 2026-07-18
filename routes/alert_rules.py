#!/usr/bin/env python3
"""
告警规则 CRUD 路由 Blueprint

GET    /api/alert_rules           获取所有告警规则
POST   /api/alert_rules           创建告警规则
PUT    /api/alert_rules/<id>      更新告警规则
DELETE /api/alert_rules/<id>      删除告警规则
"""

import json
import logging

from flask import Blueprint, jsonify, request as flask_request
from mysql.connector import Error as MySQLError

from db.pool import db_cursor
from alerts_format.db_utils import invalidate_alert_config_cache

logger = logging.getLogger(__name__)

alert_rules_bp = Blueprint("alert_rules", __name__)

# 允许更新的字段白名单（防止 SQL 注入与意外字段写入）
_UPDATABLE_FIELDS = (
    'group_id', 'users', 'alert_id', 'rank', 'alertmanager_url', 'project',
    'remark', 'label_rules', 'template_type', 'silence_type', 'grafana_url',
    'oncall_sync', 'flashcat_schedule_id',
)


@alert_rules_bp.route("/api/alert_rules", methods=["GET"])
def get_alert_rules():
    """获取所有告警规则"""
    try:
        with db_cursor(dictionary=True) as (conn, cursor):
            cursor.execute("SELECT * FROM alert_config ORDER BY id DESC")
            rules = cursor.fetchall()
        return jsonify({"code": 0, "msg": "success", "data": rules})
    except Exception as e:
        logger.error("获取告警规则失败: %s", e, exc_info=True)
        return jsonify({"code": 500, "msg": str(e)}), 500


@alert_rules_bp.route("/api/alert_rules", methods=["POST"])
def create_alert_rule():
    """创建告警规则"""
    data = flask_request.get_json(silent=True)
    if not data:
        return jsonify({"code": 400, "msg": "请求体不能为空"}), 400

    # 参数验证
    required_fields = ['group_id', 'users', 'alert_id', 'rank', 'project']
    for field in required_fields:
        if field not in data:
            return jsonify({"code": 400, "msg": f"缺少必填字段: {field}"}), 400

    # 将users和label_rules转换为JSON字符串
    users_json = json.dumps(data['users']) if isinstance(data['users'], list) else data['users']
    label_rules_json = json.dumps(data.get('label_rules')) if data.get('label_rules') else None

    sql = """
        INSERT INTO alert_config
        (group_id, users, alert_id, `rank`, alertmanager_url, project, remark, label_rules,
         template_type, silence_type, grafana_url, oncall_sync, flashcat_schedule_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """

    values = (
        data['group_id'],
        users_json,
        data['alert_id'],
        data['rank'],
        data.get('alertmanager_url') or None,
        data['project'],
        data.get('remark'),
        label_rules_json,
        data.get('template_type', 'ops'),
        data.get('silence_type', 'alertmanager'),
        data.get('grafana_url') or None,
        int(data.get('oncall_sync', 0)),
        data.get('flashcat_schedule_id') or None
    )

    try:
        with db_cursor() as (conn, cursor):
            cursor.execute(sql, values)
            conn.commit()
            rule_id = cursor.lastrowid
        # 配置变更，失效静态缓存
        invalidate_alert_config_cache()
        return jsonify({"code": 0, "msg": "创建成功", "data": {"id": rule_id}})
    except MySQLError as e:
        if e.errno == 1062:  # 重复键错误
            return jsonify({"code": 400, "msg": "alert_id已存在"}), 400
        logger.error("创建告警规则失败: %s", e, exc_info=True)
        return jsonify({"code": 500, "msg": str(e)}), 500
    except Exception as e:
        logger.error("创建告警规则失败: %s", e, exc_info=True)
        return jsonify({"code": 500, "msg": str(e)}), 500


@alert_rules_bp.route("/api/alert_rules/<int:rule_id>", methods=["PUT"])
def update_alert_rule(rule_id):
    """更新告警规则"""
    data = flask_request.get_json(silent=True)
    if not data:
        return jsonify({"code": 400, "msg": "请求体不能为空"}), 400

    # 构建更新SQL（仅允许白名单字段）
    update_fields = []
    values = []

    for field in _UPDATABLE_FIELDS:
        if field not in data:
            continue
        # rank 是保留字需反引号
        col = '`rank`' if field == 'rank' else field
        update_fields.append(f'{col} = %s')

        if field == 'users':
            values.append(json.dumps(data['users']) if isinstance(data['users'], list) else data['users'])
        elif field == 'label_rules':
            values.append(json.dumps(data['label_rules']) if data['label_rules'] else None)
        elif field in ('alertmanager_url', 'grafana_url', 'flashcat_schedule_id'):
            values.append(data.get(field) or None)
        elif field == 'oncall_sync':
            values.append(int(data.get('oncall_sync', 0)))
        else:
            values.append(data[field])

    if not update_fields:
        return jsonify({"code": 400, "msg": "没有可更新的字段"}), 400

    values.append(rule_id)
    sql = f"UPDATE alert_config SET {', '.join(update_fields)} WHERE id = %s"

    try:
        with db_cursor() as (conn, cursor):
            cursor.execute(sql, values)
            conn.commit()
        # 配置变更，失效静态缓存
        invalidate_alert_config_cache()
        return jsonify({"code": 0, "msg": "更新成功"})
    except Exception as e:
        logger.error("更新告警规则失败: %s", e, exc_info=True)
        return jsonify({"code": 500, "msg": str(e)}), 500


@alert_rules_bp.route("/api/alert_rules/<int:rule_id>", methods=["DELETE"])
def delete_alert_rule(rule_id):
    """删除告警规则"""
    try:
        with db_cursor() as (conn, cursor):
            cursor.execute("DELETE FROM alert_config WHERE id = %s", (rule_id,))
            conn.commit()
        # 配置变更，失效静态缓存
        invalidate_alert_config_cache()
        return jsonify({"code": 0, "msg": "删除成功"})
    except Exception as e:
        logger.error("删除告警规则失败: %s", e, exc_info=True)
        return jsonify({"code": 500, "msg": str(e)}), 500
