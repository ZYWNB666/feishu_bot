"""
Gitlab Webhook 消息处理模块
包含Gitlab API客户端、消息格式化等功能
"""

from .pipeline_msg_format import json_processing

__all__ = [
    'json_processing',
]