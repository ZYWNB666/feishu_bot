#!/usr/bin/env python3
"""
飞书Bot AlertBot - 主服务
提供HTTP API接口，支持向飞书群聊发送消息
"""

import json
import logging
import re
import sys
from flask import Flask, jsonify, request as flask_request, send_from_directory
import mysql.connector

# 导入配置和API客户端
from config import config
from feishu_utils.feishu_api import FeishuApiClient, FeishuApiException
from feishu_utils.event_handler import feishu_event
from feishu_utils.callback_handler import process_card_callback
from feishu_utils.alert_handler import process_alert_request
from feishu_utils.ws_client import start_ws_client_in_thread

# gitlab webhook 消息处理
from gitlab_utils.pipeline_msg_format import json_processing

# 配置日志
logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL.upper(), logging.INFO),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# 始终抑制 websockets 协议层心跳日志（keepalive ping/pong 对业务无意义）
logging.getLogger("websockets").setLevel(logging.WARNING)
logging.getLogger("websockets.client").setLevel(logging.WARNING)

# 验证配置
try:
    config.validate()
    logger.info("✅ 配置验证通过")
except ValueError as e:
    logger.error(f"❌ {e}")
    sys.exit(1)

app = Flask(__name__, static_folder='static', static_url_path='/static')

# 初始化飞书API客户端
feishu_client = FeishuApiClient(config.APP_ID, config.APP_SECRET, config.LARK_HOST)


@app.errorhandler(404)
def handle_404(error):
    """处理404错误"""
    # favicon.ico不需要记录日志
    if flask_request.path == '/favicon.ico':
        return '', 204
    
    logger.warning("404 Not Found: %s", flask_request.path)
    return jsonify({
        "code": 404,
        "msg": "资源不存在"
    }), 404


@app.errorhandler(Exception)
def handle_error(error):
    """全局错误处理"""
    logger.error("发生错误: %s", error, exc_info=True)
    
    if isinstance(error, FeishuApiException):
        return jsonify({
            "code": error.code,
            "msg": error.msg
        }), 500
    
    return jsonify({
        "code": 500,
        "msg": str(error)
    }), 500


@app.route("/api/v1/alerts", methods=["POST"])
def alert_api():
    """
    告警API
    委托给 alert_handler 模块处理具体逻辑
    """
    logger.debug("Received alert request: %s", flask_request.json)
    data = flask_request.json
    result, status_code = process_alert_request(data, feishu_client)
    return jsonify(result), status_code


@app.route("/api/send_message", methods=["POST"])
def send_message_api():
    """
    主动发送消息API
    
    请求示例:
    {
        "receive_id": "oc_xxx",  # 群聊ID或用户open_id
        "receive_id_type": "chat_id",  # chat_id(群聊), open_id(用户), user_id, union_id, email
        "msg_type": "text",  # text, post, image, interactive等
        "content": {
            "text": "你好，这是一条测试消息"
        }
    }
    """
    try:
        data = flask_request.json
        
        # 参数验证
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
        
        # 发送消息
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
        logger.error(f"发送消息失败: {e}")
        return jsonify({"code": 500, "msg": str(e)}), 500


@app.route("/api/send_text", methods=["POST"])
def send_text_api():
    """
    快捷发送文本消息API
    
    请求示例:
    {
        "chat_id": "oc_xxx",  # 群聊ID
        "text": "你好，这是一条测试消息"
    }
    
    或者发送给个人:
    {
        "open_id": "ou_xxx",  # 用户open_id
        "text": "你好，这是一条测试消息"
    }
    """
    try:
        data = flask_request.json
        
        if not data:
            return jsonify({"code": 400, "msg": "请求体不能为空"}), 400
        
        text = data.get("text")
        if not text:
            return jsonify({"code": 400, "msg": "text不能为空"}), 400
        
        # 判断是发送给群聊还是个人
        chat_id = data.get("chat_id")
        open_id = data.get("open_id")
        
        content = json.dumps({"text": text})
        
        if chat_id:
            # 发送到群聊
            logger.info("发送文本消息到群聊: %s", chat_id)
            feishu_client.send("chat_id", chat_id, "text", content)
            return jsonify({
                "code": 0,
                "msg": "success",
                "data": {"chat_id": chat_id, "text": text}
            })
        elif open_id:
            # 发送给个人
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


@app.route('/api/gitlab-pipeline-status', methods=['POST'])
def gitlab_pipeline_status():
    data = flask_request.get_json()
    try:
        if not data:
            return jsonify({"code": 400, "msg": "No data provided"}), 400
        headers = flask_request.headers
        group_id = headers.get("X-Gitlab-Token") if headers else None
        result, status_code = json_processing(group_id, data, feishu_client)
        return result, status_code
    except Exception as e:
        logger.error("展示Gitlab pipeline status失败: %s", e, exc_info=True)
        return jsonify({"code": 500, "msg": str(e)}), 500

@app.route('/api/json', methods=['GET', 'POST'])
def json_api():
    if flask_request.method == 'POST':
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

@app.route("/")
@app.route("/index.html")
def index():
    """前端管理页面"""
    return send_from_directory('static', 'index.html')


@app.route("/api/health", methods=["GET"])
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


@app.route("/api/alert_rules", methods=["GET"])
def get_alert_rules():
    """获取所有告警规则"""
    try:
        db_config = config.get_config_db_config()
        conn = mysql.connector.connect(**db_config)
        cursor = conn.cursor(dictionary=True)
        
        cursor.execute("SELECT * FROM alert_config ORDER BY id DESC")
        rules = cursor.fetchall()
        
        cursor.close()
        conn.close()
        
        return jsonify({
            "code": 0,
            "msg": "success",
            "data": rules
        })
    except Exception as e:
        logger.error("获取告警规则失败: %s", e, exc_info=True)
        return jsonify({"code": 500, "msg": str(e)}), 500


@app.route("/api/alert_rules", methods=["POST"])
def create_alert_rule():
    """创建告警规则"""
    try:
        data = flask_request.json
        
        # 参数验证
        required_fields = ['group_id', 'users', 'alert_id', 'rank', 'project']
        for field in required_fields:
            if field not in data:
                return jsonify({"code": 400, "msg": f"缺少必填字段: {field}"}), 400
        
        db_config = config.get_config_db_config()
        conn = mysql.connector.connect(**db_config)
        cursor = conn.cursor()
        
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
        
        cursor.execute(sql, values)
        conn.commit()
        
        rule_id = cursor.lastrowid
        
        cursor.close()
        conn.close()
        
        return jsonify({
            "code": 0,
            "msg": "创建成功",
            "data": {"id": rule_id}
        })
        
    except mysql.connector.Error as e:
        if e.errno == 1062:  # 重复键错误
            return jsonify({"code": 400, "msg": "alert_id已存在"}), 400
        logger.error("创建告警规则失败: %s", e, exc_info=True)
        return jsonify({"code": 500, "msg": str(e)}), 500
    except Exception as e:
        logger.error("创建告警规则失败: %s", e, exc_info=True)
        return jsonify({"code": 500, "msg": str(e)}), 500


@app.route("/api/alert_rules/<int:rule_id>", methods=["PUT"])
def update_alert_rule(rule_id):
    """更新告警规则"""
    try:
        data = flask_request.json
        
        db_config = config.get_config_db_config()
        conn = mysql.connector.connect(**db_config)
        cursor = conn.cursor()
        
        # 构建更新SQL
        update_fields = []
        values = []
        
        if 'group_id' in data:
            update_fields.append('group_id = %s')
            values.append(data['group_id'])
        if 'users' in data:
            update_fields.append('users = %s')
            values.append(json.dumps(data['users']) if isinstance(data['users'], list) else data['users'])
        if 'alert_id' in data:
            update_fields.append('alert_id = %s')
            values.append(data['alert_id'])
        if 'rank' in data:
            update_fields.append('`rank` = %s')
            values.append(data['rank'])
        if 'alertmanager_url' in data:
            update_fields.append('alertmanager_url = %s')
            values.append(data['alertmanager_url'])
        if 'project' in data:
            update_fields.append('project = %s')
            values.append(data['project'])
        if 'remark' in data:
            update_fields.append('remark = %s')
            values.append(data['remark'])
        if 'label_rules' in data:
            update_fields.append('label_rules = %s')
            values.append(json.dumps(data['label_rules']) if data['label_rules'] else None)
        if 'template_type' in data:
            update_fields.append('template_type = %s')
            values.append(data['template_type'])
        if 'silence_type' in data:
            update_fields.append('silence_type = %s')
            values.append(data['silence_type'])
        if 'grafana_url' in data:
            update_fields.append('grafana_url = %s')
            values.append(data.get('grafana_url') or None)
        if 'oncall_sync' in data:
            update_fields.append('oncall_sync = %s')
            values.append(int(data.get('oncall_sync', 0)))
        if 'flashcat_schedule_id' in data:
            update_fields.append('flashcat_schedule_id = %s')
            values.append(data.get('flashcat_schedule_id') or None)
        
        if not update_fields:
            return jsonify({"code": 400, "msg": "没有可更新的字段"}), 400
        
        values.append(rule_id)
        sql = f"UPDATE alert_config SET {', '.join(update_fields)} WHERE id = %s"
        
        cursor.execute(sql, values)
        conn.commit()
        
        cursor.close()
        conn.close()
        
        return jsonify({
            "code": 0,
            "msg": "更新成功"
        })
        
    except Exception as e:
        logger.error("更新告警规则失败: %s", e, exc_info=True)
        return jsonify({"code": 500, "msg": str(e)}), 500


@app.route("/api/alert_rules/<int:rule_id>", methods=["DELETE"])
def delete_alert_rule(rule_id):
    """删除告警规则"""
    try:
        db_config = config.get_config_db_config()
        conn = mysql.connector.connect(**db_config)
        cursor = conn.cursor()
        
        cursor.execute("DELETE FROM alert_config WHERE id = %s", (rule_id,))
        conn.commit()
        
        cursor.close()
        conn.close()
        
        return jsonify({
            "code": 0,
            "msg": "删除成功"
        })
        
    except Exception as e:
        logger.error("删除告警规则失败: %s", e, exc_info=True)
        return jsonify({"code": 500, "msg": str(e)}), 500




# ──────────────────────────────────────────────
# 飞书用户管理 /api/feishu_users
# ──────────────────────────────────────────────

@app.route("/api/feishu_users", methods=["GET"])
def list_feishu_users():
    """获取飞书用户列表"""
    try:
        db_config = config.get_config_db_config()
        conn = mysql.connector.connect(**db_config)
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT id, name, open_id, remark, created_at, updated_at FROM feishu_users ORDER BY id ASC")
        rows = cursor.fetchall()
        cursor.close()
        conn.close()
        for row in rows:
            if row.get("created_at"):
                row["created_at"] = str(row["created_at"])
            if row.get("updated_at"):
                row["updated_at"] = str(row["updated_at"])
        return jsonify({"code": 0, "data": rows})
    except Exception as e:
        logger.error("获取飞书用户列表失败: %s", e, exc_info=True)
        return jsonify({"code": 500, "msg": str(e)}), 500


@app.route("/api/feishu_users", methods=["POST"])
def create_feishu_users():
    """新增飞书用户，支持单个对象或数组批量导入
    
    单个: {"name": "张三", "open_id": "ou_xxx", "remark": "可选"}
    批量: [{"name": "张三", "open_id": "ou_xxx"}, ...]
    """
    data = flask_request.json
    if not data:
        return jsonify({"code": 400, "msg": "请求体不能为空"}), 400

    # 统一为列表
    items = data if isinstance(data, list) else [data]

    results = {"success": 0, "failed": 0, "errors": []}
    try:
        db_config = config.get_config_db_config()
        conn = mysql.connector.connect(**db_config)
        cursor = conn.cursor()
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
        cursor.close()
        conn.close()
    except Exception as e:
        logger.error("写入飞书用户失败: %s", e, exc_info=True)
        return jsonify({"code": 500, "msg": str(e)}), 500

    return jsonify({"code": 0, "msg": "操作完成", "data": results})


@app.route("/api/feishu_users/<int:user_id>", methods=["PUT"])
def update_feishu_user(user_id):
    """更新飞书用户"""
    data = flask_request.json or {}
    fields, values = [], []
    for col in ("name", "open_id", "remark"):
        if col in data:
            fields.append(f"{col} = %s")
            values.append(data[col])
    if not fields:
        return jsonify({"code": 400, "msg": "没有可更新的字段"}), 400
    values.append(user_id)
    try:
        db_config = config.get_config_db_config()
        conn = mysql.connector.connect(**db_config)
        cursor = conn.cursor()
        cursor.execute(f"UPDATE feishu_users SET {', '.join(fields)} WHERE id = %s", values)
        conn.commit()
        cursor.close()
        conn.close()
        return jsonify({"code": 0, "msg": "更新成功"})
    except Exception as e:
        logger.error("更新飞书用户失败: %s", e, exc_info=True)
        return jsonify({"code": 500, "msg": str(e)}), 500


@app.route("/api/feishu_users/<int:user_id>", methods=["DELETE"])
def delete_feishu_user(user_id):
    """删除飞书用户"""
    try:
        db_config = config.get_config_db_config()
        conn = mysql.connector.connect(**db_config)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM feishu_users WHERE id = %s", (user_id,))
        conn.commit()
        cursor.close()
        conn.close()
        return jsonify({"code": 0, "msg": "删除成功"})
    except Exception as e:
        logger.error("删除飞书用户失败: %s", e, exc_info=True)
        return jsonify({"code": 500, "msg": str(e)}), 500


# ──────────────────────────────────────────────
# 告警统计 /api/alert_stats
# ──────────────────────────────────────────────

def _extract_alertnames_from_labels(alertlabels_json):
    """从 alert_data.alertlabels JSON 中提取所有 alertname 标签值。

    alertlabels 结构: {"matchers": [{"matchers": [{"name":"alertname","value":"Xx"}, ...]}, ...]}
    每条记录可能包含多个告警实例，返回去重后的 alertname 列表。
    """
    names = set()
    if not alertlabels_json:
        return names
    try:
        data = json.loads(alertlabels_json) if isinstance(alertlabels_json, str) else alertlabels_json
    except (json.JSONDecodeError, TypeError):
        return names
    for group in data.get('matchers', []):
        for m in group.get('matchers', []):
            if m.get('name') == 'alertname' and m.get('value'):
                names.add(m['value'])
    return names


@app.route("/api/alert_stats/top", methods=["GET"])
def alert_stats_top():
    """Top 告警统计

    GET /api/alert_stats/top?start=2026-01-01&end=2026-07-07&limit=20
    返回指定时间范围内出现次数最多的 alertname 列表。
    """
    try:
        from datetime import datetime, timedelta

        # 默认查询最近 7 天
        end_str = flask_request.args.get('end')
        start_str = flask_request.args.get('start')
        limit = flask_request.args.get('limit', 20, type=int)

        if end_str:
            try:
                end_dt = datetime.strptime(end_str, '%Y-%m-%d')
            except ValueError:
                return jsonify({"code": 400, "msg": "end 参数格式应为 YYYY-MM-DD"}), 400
        else:
            end_dt = datetime.now()

        if start_str:
            try:
                start_dt = datetime.strptime(start_str, '%Y-%m-%d')
            except ValueError:
                return jsonify({"code": 400, "msg": "start 参数格式应为 YYYY-MM-DD"}), 400
        else:
            start_dt = end_dt - timedelta(days=7)

        # alerttime 是 VARCHAR 存储的 ISO 格式字符串，可以直接做字符串比较
        start_iso = start_dt.strftime('%Y-%m-%d')
        end_iso = (end_dt + timedelta(days=1)).strftime('%Y-%m-%d')

        db_config = config.get_config_db_config()
        conn = mysql.connector.connect(**db_config)
        cursor = conn.cursor(dictionary=True)

        cursor.execute(
            "SELECT id, alertlabels, project, alerttime "
            "FROM alert_data "
            "WHERE alerttime >= %s AND alerttime < %s "
            "ORDER BY alerttime DESC",
            (start_iso, end_iso)
        )
        rows = cursor.fetchall()
        cursor.close()
        conn.close()

        # 在 Python 层聚合 alertname 计数
        stats = {}  # alertname -> count
        for row in rows:
            names = _extract_alertnames_from_labels(row.get('alertlabels'))
            for name in names:
                stats[name] = stats.get(name, 0) + 1

        # 排序取 top N
        result = sorted(stats.items(), key=lambda x: x[1], reverse=True)[:limit]
        data = [{"alertname": name, "count": cnt} for name, cnt in result]

        return jsonify({
            "code": 0,
            "msg": "success",
            "data": data,
            "total_records": len(rows),
            "start": start_dt.strftime('%Y-%m-%d'),
            "end": end_dt.strftime('%Y-%m-%d')
        })
    except Exception as e:
        logger.error("获取Top告警统计失败: %s", e, exc_info=True)
        return jsonify({"code": 500, "msg": str(e)}), 500


@app.route("/api/alert_stats/details", methods=["GET"])
def alert_stats_details():
    """告警详情列表

    GET /api/alert_stats/details?alertname=XXX&start=2026-01-01&end=2026-07-07&limit=100
    返回指定 alertname 在时间范围内的每条告警详情。
    """
    try:
        from datetime import datetime, timedelta

        alertname = flask_request.args.get('alertname')
        if not alertname:
            return jsonify({"code": 400, "msg": "alertname 参数不能为空"}), 400

        end_str = flask_request.args.get('end')
        start_str = flask_request.args.get('start')
        limit = flask_request.args.get('limit', 200, type=int)

        if end_str:
            try:
                end_dt = datetime.strptime(end_str, '%Y-%m-%d')
            except ValueError:
                return jsonify({"code": 400, "msg": "end 参数格式应为 YYYY-MM-DD"}), 400
        else:
            end_dt = datetime.now()

        if start_str:
            try:
                start_dt = datetime.strptime(start_str, '%Y-%m-%d')
            except ValueError:
                return jsonify({"code": 400, "msg": "start 参数格式应为 YYYY-MM-DD"}), 400
        else:
            start_dt = end_dt - timedelta(days=7)

        start_iso = start_dt.strftime('%Y-%m-%d')
        end_iso = (end_dt + timedelta(days=1)).strftime('%Y-%m-%d')

        db_config = config.get_config_db_config()
        conn = mysql.connector.connect(**db_config)
        cursor = conn.cursor(dictionary=True)

        # 先用 MySQL JSON_SEARCH 做粗筛：alertlabels 中包含 alertname 标签值匹配的记录
        # 再在 Python 层精确过滤
        cursor.execute(
            "SELECT id, alertlabels, project, alerttime, silenceid, group_id "
            "FROM alert_data "
            "WHERE alerttime >= %s AND alerttime < %s "
            "AND JSON_SEARCH(alertlabels, 'one', %s, NULL, '$**.value') IS NOT NULL "
            "ORDER BY alerttime DESC",
            (start_iso, end_iso, alertname)
        )
        rows = cursor.fetchall()
        cursor.close()
        conn.close()

        # 过滤出包含该 alertname 的记录，并提取完整标签
        details = []
        for row in rows:
            labels_map = _extract_all_labels(row.get('alertlabels'), alertname)
            if labels_map is None:
                continue

            # 解析 silenceid
            silence_ids = []
            if row.get('silenceid'):
                try:
                    silence_ids = json.loads(row['silenceid']) if isinstance(row['silenceid'], str) else row['silenceid']
                except (json.JSONDecodeError, TypeError):
                    silence_ids = []

            details.append({
                "id": row['id'],
                "project": row.get('project', ''),
                "alerttime": row.get('alerttime', ''),
                "labels": labels_map,
                "silenced": len(silence_ids) > 0,
                "silence_ids": silence_ids,
                "group_id": row.get('group_id', ''),
            })
            if len(details) >= limit:
                break

        return jsonify({
            "code": 0,
            "msg": "success",
            "data": details,
            "alertname": alertname,
            "count": len(details),
            "start": start_dt.strftime('%Y-%m-%d'),
            "end": end_dt.strftime('%Y-%m-%d')
        })
    except Exception as e:
        logger.error("获取告警详情失败: %s", e, exc_info=True)
        return jsonify({"code": 500, "msg": str(e)}), 500


def _extract_all_labels(alertlabels_json, target_alertname=None):
    """从 alertlabels JSON 中提取标签字典。

    如果 target_alertname 不为 None，则只在该条记录的任一告警实例中
    包含匹配 alertname 时返回合并后的标签，否则返回 None。

    返回: {label_name: label_value} 或 None
    """
    if not alertlabels_json:
        return None
    try:
        data = json.loads(alertlabels_json) if isinstance(alertlabels_json, str) else alertlabels_json
    except (json.JSONDecodeError, TypeError):
        return None

    merged_labels = {}
    found = False
    for group in data.get('matchers', []):
        instance_labels = {}
        instance_has_target = False
        for m in group.get('matchers', []):
            name = m.get('name')
            value = m.get('value')
            if name and value is not None:
                instance_labels[name] = value
                if name == 'alertname' and value == target_alertname:
                    instance_has_target = True
        if instance_labels:
            # 如果没有指定 target，或者该实例包含 target alertname，则合并标签
            if target_alertname is None or instance_has_target:
                merged_labels.update(instance_labels)
                if instance_has_target:
                    found = True

    if target_alertname is not None and not found:
        return None
    return merged_labels if merged_labels else None


@app.route("/api/card_callback", methods=["POST"])
def card_callback():
    """
    处理飞书卡片交互回调
    委托给 callback_handler 模块处理具体逻辑
    """
    data = flask_request.json
    result = process_card_callback(data, feishu_client)
    return jsonify(result)


@app.route("/webhook/event", methods=["POST"])
def webhook_event():
    """
    飞书事件回调接口
    用于处理URL验证和接收飞书事件
    配置地址: http://your-domain/webhook/event
    委托给 event_handler 模块处理具体逻辑
    """
    data = flask_request.json
    result, status_code = feishu_event(feishu_client, data)
    return jsonify(result), status_code


if __name__ == "__main__":
    logger.info("=" * 60)
    logger.info("飞书Bot AlertBot 启动中...")
    logger.info("APP_ID: %s", config.APP_ID)
    logger.info("LARK_HOST: %s", config.LARK_HOST)
    logger.info("=" * 60)
    
    # 显示配置信息
    logger.info("数据库配置:")
    logger.info("  MySQL: %s:%s/%s", 
                config.MYSQL_HOST, 
                config.MYSQL_PORT, 
                config.MYSQL_DATABASE)
    logger.info("=" * 60)
    
    logger.info("WEB界面:")
    logger.info("  - GET  /                   前端管理页面")
    logger.info("")
    logger.info("API接口:")
    logger.info("  - GET  /api/health         健康检查")
    logger.info("  - GET  /api/alert_rules    获取告警规则列表")
    logger.info("  - POST /api/alert_rules    创建告警规则")
    logger.info("  - PUT  /api/alert_rules/:id 更新告警规则")
    logger.info("  - DEL  /api/alert_rules/:id 删除告警规则")
    logger.info("  - POST /api/v1/alerts      接收告警")
    logger.info("  - POST /api/send_text      发送文本消息")
    logger.info("  - POST /api/send_message   发送完整消息")
    logger.info("  - POST /webhook/event      飞书事件回调（备用，长连接模式下无需暴露）")
    logger.info("=" * 60)
    logger.info("🌐 服务地址: http://%s:%s", config.HOST, config.PORT)
    logger.info("🎨 管理页面: http://%s:%s/", config.HOST, config.PORT)
    logger.info("=" * 60)

    # 启动飞书 WebSocket 长连接（守护线程，自动重连）
    start_ws_client_in_thread(config.APP_ID, config.APP_SECRET, feishu_client, debug=config.DEBUG)

    app.run(host=config.HOST, port=config.PORT, debug=config.DEBUG)
