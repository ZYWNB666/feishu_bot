#!/usr/bin/env python3
"""
飞书Bot AlertBot - 主服务
提供HTTP API接口，支持向飞书群聊发送消息
"""

import json
import logging
import sys
from flask import Flask, jsonify, request as flask_request, send_from_directory
import mysql.connector

# 导入配置和API客户端
from config import config
from feishu_utils.feishu_api import FeishuApiClient, FeishuApiException
from feishu_utils.event_handler import feishu_event
from feishu_utils.callback_handler import process_card_callback
from feishu_utils.alert_handler import process_alert_request

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

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
    logger.error(f"发生错误: {error}", exc_info=True)
    
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
        logger.info(f"发送消息到 {receive_id_type}:{receive_id}")
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
            logger.info(f"发送文本消息到群聊: {chat_id}")
            feishu_client.send("chat_id", chat_id, "text", content)
            return jsonify({
                "code": 0,
                "msg": "success",
                "data": {"chat_id": chat_id, "text": text}
            })
        elif open_id:
            # 发送给个人
            logger.info(f"发送文本消息到用户: {open_id}")
            feishu_client.send("open_id", open_id, "text", content)
            return jsonify({
                "code": 0,
                "msg": "success",
                "data": {"open_id": open_id, "text": text}
            })
        else:
            return jsonify({"code": 400, "msg": "chat_id和open_id至少提供一个"}), 400
            
    except Exception as e:
        logger.error(f"发送文本消息失败: {e}")
        return jsonify({"code": 500, "msg": str(e)}), 500


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
        required_fields = ['group_id', 'users', 'alert_id', 'rank', 'alertmanager_url', 'project']
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
            (group_id, users, alert_id, `rank`, alertmanager_url, project, remark, label_rules)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """
        
        values = (
            data['group_id'],
            users_json,
            data['alert_id'],
            data['rank'],
            data['alertmanager_url'],
            data['project'],
            data.get('remark'),
            label_rules_json
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
    logger.info("  - POST /webhook/event      飞书事件回调（URL验证）")
    logger.info("=" * 60)
    logger.info("🌐 服务地址: http://%s:%s", config.HOST, config.PORT)
    logger.info("🎨 管理页面: http://%s:%s/", config.HOST, config.PORT)
    logger.info("=" * 60)
    
    app.run(host=config.HOST, port=config.PORT, debug=config.DEBUG)
