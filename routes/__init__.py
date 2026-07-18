"""路由 Blueprint 模块"""

from .alerts import alerts_bp
from .messages import messages_bp
from .gitlab import gitlab_bp
from .alert_rules import alert_rules_bp
from .feishu_users import feishu_users_bp
from .alert_stats import alert_stats_bp
from .system import system_bp

__all__ = [
    "alerts_bp",
    "messages_bp",
    "gitlab_bp",
    "alert_rules_bp",
    "feishu_users_bp",
    "alert_stats_bp",
    "system_bp",
]
