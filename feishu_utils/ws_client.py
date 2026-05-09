#!/usr/bin/env python3
"""
飞书 WebSocket 长连接客户端模块

使用 lark-oapi SDK 建立长连接，替代 HTTP Webhook 接收飞书事件和卡片回调。
Flask 的 /webhook/event 和 /api/card_callback 路由作为备用保留。
"""

import json
import logging
import threading

import lark_oapi as lark
from lark_oapi.event.callback.model.p2_card_action_trigger import (
    P2CardActionTrigger,
    P2CardActionTriggerResponse,
)

from feishu_utils.event_handler import _process_event_async_wrapper
from feishu_utils.callback_handler import process_card_callback

logger = logging.getLogger(__name__)


def _make_event_bridge(feishu_client, event_label: str):
    """
    返回一个事件桥接函数，将强类型 SDK 对象转为 dict 后交给现有处理逻辑。
    """
    def bridge(data) -> None:
        try:
            raw = json.loads(lark.JSON.marshal(data))
            logger.debug("WS 收到事件 [%s]: %s", event_label, raw)
            _process_event_async_wrapper(feishu_client, raw)
        except Exception as e:
            logger.error("WS 事件 [%s] 处理失败: %s", event_label, e, exc_info=True)
    return bridge


def _make_card_action_bridge(feishu_client):
    """
    返回卡片回调桥接函数，将 P2CardActionTrigger 转为 dict 后交给现有回调处理逻辑。
    """
    def bridge(data: P2CardActionTrigger) -> P2CardActionTriggerResponse:
        try:
            raw = json.loads(lark.JSON.marshal(data))
            logger.debug("WS 收到卡片回调: %s", raw)
            process_card_callback(raw, feishu_client)
        except Exception as e:
            logger.error("WS 卡片回调处理失败: %s", e, exc_info=True)
        # 飞书要求必须返回响应对象，返回空 toast 即可
        return P2CardActionTriggerResponse({})
    return bridge


def start_ws_client(app_id: str, app_secret: str, feishu_client, log_level=lark.LogLevel.INFO):
    """
    构建 EventDispatcherHandler 并启动 WebSocket 长连接。
    该函数会阻塞当前线程，请在守护线程中调用。

    Args:
        app_id: 飞书应用 App ID
        app_secret: 飞书应用 App Secret
        feishu_client: FeishuApiClient 实例，用于发送消息
        log_level: SDK 日志级别
    """
    eb = _make_event_bridge
    cb = _make_card_action_bridge(feishu_client)

    event_handler = (
        lark.EventDispatcherHandler.builder("", "")
        # 消息事件
        .register_p2_im_message_receive_v1(eb(feishu_client, "im.message.receive_v1"))
        .register_p2_im_message_message_read_v1(eb(feishu_client, "im.message.message_read_v1"))
        .register_p2_im_message_recalled_v1(eb(feishu_client, "im.message.recalled_v1"))
        .register_p2_im_message_reaction_created_v1(eb(feishu_client, "im.message.reaction.created_v1"))
        .register_p2_im_message_reaction_deleted_v1(eb(feishu_client, "im.message.reaction.deleted_v1"))
        # 群成员事件
        .register_p2_im_chat_member_bot_added_v1(eb(feishu_client, "im.chat.member.bot.added_v1"))
        .register_p2_im_chat_member_bot_deleted_v1(eb(feishu_client, "im.chat.member.bot.deleted_v1"))
        .register_p2_im_chat_member_user_added_v1(eb(feishu_client, "im.chat.member.user.added_v1"))
        .register_p2_im_chat_member_user_withdrawn_v1(eb(feishu_client, "im.chat.member.user.withdrawn_v1"))
        # 卡片交互回调
        .register_p2_card_action_trigger(cb)
        .build()
    )

    ws_cli = lark.ws.Client(
        app_id,
        app_secret,
        event_handler=event_handler,
        log_level=log_level,
    )

    logger.info("🔌 飞书 WebSocket 长连接启动中...")
    ws_cli.start()  # 阻塞，内部自动重连


def start_ws_client_in_thread(app_id: str, app_secret: str, feishu_client) -> threading.Thread:
    """
    在守护线程中启动 WebSocket 长连接，不阻塞主线程（Flask 服务）。

    Returns:
        threading.Thread: 启动的守护线程
    """
    t = threading.Thread(
        target=start_ws_client,
        args=(app_id, app_secret, feishu_client),
        daemon=True,
        name="feishu-ws-client",
    )
    t.start()
    logger.info("✅ 飞书 WebSocket 长连接线程已启动 (线程名: %s)", t.name)
    return t
