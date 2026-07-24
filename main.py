#!/usr/bin/env python3
"""
飞书Bot AlertBot - 主服务
提供HTTP API接口，支持向飞书群聊发送消息

优化点（P1/P2）：
- Blueprint 拆分：路由逻辑从 main.py 迁移到 routes/ 包，main.py 仅负责应用装配
- 连接池：DB 访问统一走 db.pool，消除每次请求新建/关闭连接的开销
- token 缓存：FeishuApiClient 缓存 tenant_access_token，避免每次发送都重新获取
- 日志统一：统一日志格式与第三方库日志抑制
- 魔法数字：统一引用 config.constants
"""

import logging
import sys

from flask import Flask, jsonify, request as flask_request

# 导入配置和API客户端
from config import config
from feishu_utils.feishu_api import FeishuApiClient, FeishuApiException
from feishu_utils.ws_client import start_ws_client_in_thread

# 配置日志（统一格式）
logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL.upper(), logging.INFO),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# 始终抑制 websockets 协议层心跳日志（keepalive ping/pong 对业务无意义）
logging.getLogger("websockets").setLevel(logging.WARNING)
logging.getLogger("websockets.client").setLevel(logging.WARNING)
# urllib3 DEBUG 日志会输出带 app_key/integration_key 的完整请求 URL。
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("urllib3.connectionpool").setLevel(logging.WARNING)

# 验证配置
try:
    config.validate()
    logger.info("✅ 配置验证通过")
except ValueError as e:
    logger.error("❌ %s", e)
    sys.exit(1)

app = Flask(__name__, static_folder='static', static_url_path='/static')

# 初始化飞书API客户端，存入 app.config 供各 Blueprint 共享
feishu_client = FeishuApiClient(config.APP_ID, config.APP_SECRET, config.LARK_HOST)
app.config["FEISHU_CLIENT"] = feishu_client


# ── 全局错误处理 ──
@app.errorhandler(404)
def handle_404(error):
    """处理404错误"""
    # favicon.ico不需要记录日志
    if flask_request.path == '/favicon.ico':
        return '', 204

    logger.warning("404 Not Found: %s", flask_request.path)
    return jsonify({"code": 404, "msg": "资源不存在"}), 404


@app.errorhandler(Exception)
def handle_error(error):
    """全局错误处理"""
    logger.error("发生错误: %s", error, exc_info=True)

    if isinstance(error, FeishuApiException):
        return jsonify({"code": error.code, "msg": error.msg}), 500

    return jsonify({"code": 500, "msg": str(error)}), 500


# ── 注册 Blueprint ──
from routes import (
    alerts_bp, messages_bp, gitlab_bp,
    alert_rules_bp, feishu_users_bp, alert_stats_bp, system_bp,
)

app.register_blueprint(alerts_bp)
app.register_blueprint(messages_bp)
app.register_blueprint(gitlab_bp)
app.register_blueprint(alert_rules_bp)
app.register_blueprint(feishu_users_bp)
app.register_blueprint(alert_stats_bp)
app.register_blueprint(system_bp)


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
