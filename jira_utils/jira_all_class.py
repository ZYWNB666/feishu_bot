import logging
from config.config import Config
import requests

logger = logging.getLogger(__name__)

config = Config()
url = config.JIRA_URL
username = config.JIRA_USERNAME
password = config.JIRA_PASSWORD

class JiraClient:
    """Jira API客户端，支持session和basic auth两种认证方式，以及Web界面操作"""
    
    def __init__(self, base_url):
        self.base_url = base_url
        self.session = requests.Session()
        self.authenticated = False
        self.csrf_token = None
        self.xsrf_token = None
    
    def login(self, username, password):
        """
        使用session方式登录Jira
        
        Args:
            username: Jira用户名
            password: Jira密码
            
        Returns:
            dict: 登录响应信息
            
        Raises:
            requests.exceptions.RequestException: 登录失败时抛出异常
        """
        headers = {
            "Content-Type": "application/json"
        }
        data = {
            "username": username,
            "password": password
        }
        
        try:
            response = self.session.post(
                f"{self.base_url}/rest/auth/1/session",
                headers=headers,
                json=data
            )
            response.raise_for_status()  # 检查HTTP错误
            
            result = response.json()
            # 检查登录是否成功
            if 'session' in result:
                self.authenticated = True
                # print(f"登录成功！Session ID: {result['session']['name']}")
                
                # 获取CSRF token（用于Web界面操作）
                self._get_csrf_token()
                
                return result
            else:
                raise ValueError("登录失败：未返回session信息")
                
        except requests.exceptions.HTTPError as e:
            logger.warning(f"登录失败：HTTP错误 {e.response.status_code}")
            logger.warning(f"错误信息：{e.response.text}")
            raise
        except (ValueError, requests.exceptions.RequestException) as e:
            logger.warning(f"登录失败：{str(e)}")
            raise
    
    def _get_csrf_token(self):
        """
        获取CSRF token（用于Web界面操作）
        从cookie中获取atlassian.xsrf.token，使用完整值
        """
        try:
            # 从cookie中获取，使用完整的token值（不要去掉后缀）
            if 'atlassian.xsrf.token' in self.session.cookies:
                self.xsrf_token = self.session.cookies.get('atlassian.xsrf.token')
                # 直接使用完整的cookie值作为CSRF token
                self.csrf_token = self.xsrf_token
                logger.debug(f"从Cookie获取CSRF Token: {self.csrf_token[:30]}...")
                return
            
            logger.warning("未能从Cookie获取CSRF token")
        except (requests.exceptions.RequestException, AttributeError) as e:
            logger.warning(f"获取CSRF token时出错：{str(e)}")
    
    def _get_page_csrf_token(self, page_url):
        """
        访问指定页面获取最新的CSRF token
        Jira的CSRF token每次请求都会变化，必须从目标页面获取最新的token
        
        Args:
            page_url: 要访问的页面URL
            
        Returns:
            str: CSRF token，获取失败返回None
        """
        import re
        try:
            response = self.session.get(page_url, timeout=30)
            response.raise_for_status()
            
            # 从页面中提取 atlassian-token
            # 格式: <meta id="atlassian-token" name="atlassian-token" content="TOKEN_VALUE">
            token_match = re.search(
                r'<meta[^>]*name=["\']atlassian-token["\'][^>]*content=["\']([^"\']+)["\']',
                response.text
            )
            if token_match:
                token = token_match.group(1)
                logger.debug(f"从页面获取CSRF Token: {token[:40]}...")
                return token
            
            # 尝试另一种格式
            token_match = re.search(
                r'<meta[^>]*content=["\']([^"\']+)["\'][^>]*name=["\']atlassian-token["\']',
                response.text
            )
            if token_match:
                token = token_match.group(1)
                logger.debug(f"从页面获取CSRF Token: {token[:40]}...")
                return token
                
            logger.warning("无法从页面提取CSRF token")
            return None
            
        except requests.exceptions.RequestException as e:
            logger.warning(f"访问页面获取CSRF token失败: {e}")
            return None
    
    def invite_user(self, email, applications=None):
        """
        邀请用户加入Jira（发送邀请邮件）
        
        Args:
            email: 被邀请用户的邮箱地址
            applications: 应用列表，默认为['jira-software']
            
        Returns:
            dict: 包含成功状态和消息的字典
                - success (bool): 是否成功
                - message (str): 结果消息
            
        Raises:
            RuntimeError: 未登录或缺少CSRF token
            requests.exceptions.RequestException: 请求失败
        """
        if not self.authenticated:
            raise RuntimeError("请先登录！")
        
        if applications is None:
            applications = ['jira-software']
        
        # 关键：先访问邀请页面获取最新的CSRF token
        invite_page_url = f"{self.base_url}/secure/admin/user/InviteUser!default.jspa"
        fresh_token = self._get_page_csrf_token(invite_page_url)
        
        if not fresh_token:
            return {"success": False, "message": "邀请失败：无法获取CSRF token"}
        
        # 准备请求头
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": self.base_url,
            "Referer": invite_page_url,
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        }
        
        # 准备表单数据，使用最新的token
        data = {
            "email": email,
            "selectedApplications": applications[0] if isinstance(applications, list) else applications,
            "atl_token": fresh_token,
            "Invite": "邀请用户"
        }
        
        try:
            response = self.session.post(
                f"{self.base_url}/secure/admin/user/InviteUser.jspa",
                headers=headers,
                data=data,
                timeout=30,
                allow_redirects=False  # 禁用自动重定向，以便捕获302状态码
            )
            
            # 只有 302 Found 才表示邮件发送成功
            if response.status_code == 302:
                logger.info(f"邀请邮件已成功发送到 {email}（状态码：302）")
                return {"success": True, "message": f"✅ 邀请邮件已成功发送到 {email}"}
            
            # 失败时打印调试信息
            logger.warning(f"邀请用户失败，状态码: {response.status_code}")
            logger.debug(f"响应内容前500字符: {response.text[:500]}")
            
            # 其他状态码，分析响应内容判断具体错误
            response_text = response.text.lower()
            
            # 检查具体错误类型
            if "already exists" in response_text or "已存在用户" in response.text:
                return {"success": False, "message": f"邀请失败：邮箱 {email} 对应的用户已存在"}
            elif "invalid email" in response_text or "邮箱地址无效" in response.text:
                return {"success": False, "message": f"邀请失败：邮箱地址 {email} 格式无效"}
            elif "会话过期" in response.text or "session" in response_text:
                return {"success": False, "message": "邀请失败：会话过期，请重试"}
            else:
                return {"success": False, "message": f"邀请失败：状态码 {response.status_code}，请联系管理员"}
            
        except requests.exceptions.HTTPError as e:
            error_msg = f"邀请用户失败：HTTP错误 {e.response.status_code}"
            logger.warning(error_msg)
            return {"success": False, "message": error_msg}
        except (RuntimeError, requests.exceptions.RequestException) as e:
            error_msg = f"邀请用户失败：{str(e)}"
            logger.warning(error_msg)
            return {"success": False, "message": error_msg}
