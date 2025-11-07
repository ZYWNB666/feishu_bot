#!/usr/bin/env python3
"""
飞书API客户端
用于发送消息到飞书
"""

import logging
import requests

logger = logging.getLogger(__name__)


class FeishuApiClient:
    """飞书API客户端"""
    
    TENANT_ACCESS_TOKEN_URI = "/open-apis/auth/v3/tenant_access_token/internal"
    MESSAGE_URI = "/open-apis/im/v1/messages"
    
    def __init__(self, app_id, app_secret, lark_host="https://open.feishu.cn"):
        """
        初始化飞书API客户端
        
        Args:
            app_id: 应用ID
            app_secret: 应用密钥
            lark_host: 飞书API地址
        """
        self._app_id = app_id
        self._app_secret = app_secret
        self._lark_host = lark_host
        self._tenant_access_token = ""
    
    @property
    def tenant_access_token(self):
        """获取tenant_access_token"""
        return self._tenant_access_token
    
    def send_text_with_open_id(self, open_id, content):
        """
        发送文本消息给用户
        
        Args:
            open_id: 用户的open_id
            content: 消息内容（JSON字符串格式）
        """
        self.send("open_id", open_id, "text", content)
    
    def send(self, receive_id_type, receive_id, msg_type, content):
        """
        发送消息
        
        Args:
            receive_id_type: 接收者ID类型 (open_id, chat_id, user_id等)
            receive_id: 接收者ID
            msg_type: 消息类型 (text, post, image, interactive等)
            content: 消息内容（JSON字符串格式）
        """
        # 获取access token
        self._authorize_tenant_access_token()
        
        # 构建请求URL
        url = f"{self._lark_host}{self.MESSAGE_URI}?receive_id_type={receive_id_type}"
        
        # 构建请求头
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.tenant_access_token}",
        }
        
        # 构建请求体
        req_body = {
            "receive_id": receive_id,
            "content": content,
            "msg_type": msg_type,
        }
        
        # 发送请求
        logger.info(f"发送消息: {receive_id_type}={receive_id}, msg_type={msg_type}")
        resp = requests.post(url=url, headers=headers, json=req_body, timeout=10)
        
        # 检查响应
        self._check_error_response(resp)
        
        logger.info("消息发送成功")
        return resp.json()
    
    def reply_message(self, message_id, msg_type, content):
        """
        回复消息（引用回复）
        
        Args:
            message_id: 要回复的消息ID
            msg_type: 消息类型 (text, post, image, interactive等)
            content: 消息内容（JSON字符串格式）
        """
        # 获取access token
        self._authorize_tenant_access_token()
        
        # 构建请求URL
        url = f"{self._lark_host}{self.MESSAGE_URI}/{message_id}/reply"
        
        # 构建请求头
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.tenant_access_token}",
        }
        
        # 构建请求体
        req_body = {
            "content": content,
            "msg_type": msg_type,
        }
        
        # 发送请求
        logger.info(f"回复消息: message_id={message_id}, msg_type={msg_type}")
        resp = requests.post(url=url, headers=headers, json=req_body, timeout=10)
        
        # 检查响应
        self._check_error_response(resp)
        
        logger.info("消息回复成功")
        return resp.json()
    
    def _authorize_tenant_access_token(self):
        """
        获取tenant_access_token
        文档: https://open.feishu.cn/document/ukTMukTMukTM/ukDNz4SO0MjL5QzM/auth-v3/auth/tenant_access_token_internal
        """
        url = f"{self._lark_host}{self.TENANT_ACCESS_TOKEN_URI}"
        
        req_body = {
            "app_id": self._app_id,
            "app_secret": self._app_secret
        }
        
        logger.debug("获取tenant_access_token...")
        response = requests.post(url, json=req_body, timeout=10)
        
        self._check_error_response(response)
        
        self._tenant_access_token = response.json().get("tenant_access_token")
        logger.debug("tenant_access_token获取成功")
    
    @staticmethod
    def _check_error_response(resp):
        """
        检查响应是否包含错误信息
        
        Args:
            resp: requests响应对象
        
        Raises:
            FeishuApiException: 当响应包含错误时
        """
        if resp.status_code != 200:
            logger.error(f"HTTP请求失败: {resp.status_code} - {resp.text}")
            resp.raise_for_status()
        
        response_dict = resp.json()
        code = response_dict.get("code", -1)
        
        if code != 0:
            msg = response_dict.get("msg", "未知错误")
            logger.error(f"飞书API错误: code={code}, msg={msg}")
            raise FeishuApiException(code=code, msg=msg)


class FeishuApiException(Exception):
    """飞书API异常"""
    
    def __init__(self, code=0, msg=None):
        self.code = code
        self.msg = msg
        super().__init__(f"飞书API错误 [{code}]: {msg}")
    
    def __str__(self):
        return f"飞书API错误 [{self.code}]: {self.msg}"
    
    __repr__ = __str__

