#!/usr/bin/env python3
"""
飞书用户管理路由 Blueprint

GET    /api/feishu_users           获取飞书用户列表
POST   /api/feishu_users           新增飞书用户（单个或批量）
PUT    /api/feishu_users/<id>      更新飞书用户
DELETE /api/feishu_users/<id>      删除飞书用户
"""

import logging

from flask import Blueprint, jsonify, request as flask_request

from db.pool import db_cursor

logger = logging.getLogger(__name__)

feishu_users_bp = Blueprint("feishu_users", __name__)


@feishu_users_bp.route("/api/feishu_users", methods=["GET"])
def list_feishu_users():
    """获取飞书用户列表"""
    try:
        with db_cursor(dictionary=True) as (conn, cursor):
            cursor.execute(
                "SELECT id, name, open_id, remark, created_at, updated_at "
                "FROM feishu_users ORDER BY id ASC"
            )
            rows = cursor.fetchall()
        for row in rows:
            if row.get("created_at"):
                row["created_at"] = str(row["created_at"])
            if row.get("updated_at"):
                row["updated_at"] = str(row["updated_at"])
        return jsonify({"code": 0, "data": rows})
    except Exception as e:
        logger.error("获取飞书用户列表失败: %s", e, exc_info=True)
        return jsonify({"code": 500, "msg": str(e)}), 500


@feishu_users_bp.route("/api/feishu_users", methods=["POST"])
def create_feishu_users():
    """新增飞书用户，支持单个对象或数组批量导入

    单个: {"name": "张三", "open_id": "ou_xxx", "remark": "可选"}
    批量: [{"name": "张三", "open_id": "ou_xxx"}, ...]
    """
    data = flask_request.get_json(silent=True)
    if not data:
        return jsonify({"code": 400, "msg": "请求体不能为空"}), 400

    # 统一为列表
    items = data if isinstance(data, list) else [data]

    results = {"success": 0, "failed": 0, "errors": []}
    try:
        with db_cursor() as (conn, cursor):
            for item in items:
                name = (item.get("name") or "").strip()
                open_id = (item.get("open_id") or "").strip()
                remark = (item.get("remark") or "").strip() or None
                if not name or not open_id:
                    results["failed"] += 1
                    results["errors"].append(f"name/open_id 不能为空: {item}")
                    continue
                try:
                    cursor.execute(
                        "INSERT INTO feishu_users (name, open_id, remark) VALUES (%s, %s, %s) "
                        "ON DUPLICATE KEY UPDATE open_id=VALUES(open_id), remark=VALUES(remark)",
                        (name, open_id, remark),
                    )
                    results["success"] += 1
                except Exception as row_err:
                    results["failed"] += 1
                    results["errors"].append(f"{name}: {row_err}")
            conn.commit()
    except Exception as e:
        logger.error("写入飞书用户失败: %s", e, exc_info=True)
        return jsonify({"code": 500, "msg": str(e)}), 500

    return jsonify({"code": 0, "msg": "操作完成", "data": results})


@feishu_users_bp.route("/api/feishu_users/<int:user_id>", methods=["PUT"])
def update_feishu_user(user_id):
    """更新飞书用户"""
    data = flask_request.get_json(silent=True) or {}
    fields, values = [], []
    for col in ("name", "open_id", "remark"):
        if col in data:
            fields.append(f"{col} = %s")
            values.append(data[col])
    if not fields:
        return jsonify({"code": 400, "msg": "没有可更新的字段"}), 400
    values.append(user_id)
    try:
        with db_cursor() as (conn, cursor):
            cursor.execute(f"UPDATE feishu_users SET {', '.join(fields)} WHERE id = %s", values)
            conn.commit()
        return jsonify({"code": 0, "msg": "更新成功"})
    except Exception as e:
        logger.error("更新飞书用户失败: %s", e, exc_info=True)
        return jsonify({"code": 500, "msg": str(e)}), 500


@feishu_users_bp.route("/api/feishu_users/<int:user_id>", methods=["DELETE"])
def delete_feishu_user(user_id):
    """删除飞书用户"""
    try:
        with db_cursor() as (conn, cursor):
            cursor.execute("DELETE FROM feishu_users WHERE id = %s", (user_id,))
            conn.commit()
        return jsonify({"code": 0, "msg": "删除成功"})
    except Exception as e:
        logger.error("删除飞书用户失败: %s", e, exc_info=True)
        return jsonify({"code": 500, "msg": str(e)}), 500
