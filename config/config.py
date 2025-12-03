#!/usr/bin/env python3
"""
配置管理模块
所有配置统一从环境变量读取
"""

import os
from dotenv import load_dotenv, find_dotenv

# 加载环境变量
load_dotenv(find_dotenv())


class Config:
    """配置类 - 统一管理所有配置项"""
    
    # ==================== 飞书应用配置 ====================
    APP_ID = os.getenv("APP_ID")
    APP_SECRET = os.getenv("APP_SECRET")
    LARK_HOST = os.getenv("LARK_HOST", "https://open.feishu.cn")
    
    # 事件订阅配置（可选）
    VERIFICATION_TOKEN = os.getenv("VERIFICATION_TOKEN", "")
    ENCRYPT_KEY = os.getenv("ENCRYPT_KEY", "")
    
    # ==================== MySQL数据库配置 ====================
    MYSQL_HOST = os.getenv("MYSQL_HOST", "localhost")
    MYSQL_PORT = int(os.getenv("MYSQL_PORT", "3306"))
    MYSQL_USER = os.getenv("MYSQL_USER", "root")
    MYSQL_PASSWORD = os.getenv("MYSQL_PASSWORD", "")
    MYSQL_DATABASE = os.getenv("MYSQL_DATABASE", "alert_db")
    MYSQL_CHARSET = os.getenv("MYSQL_CHARSET", "utf8mb4")
    
    # ==================== 服务配置 ====================
    HOST = os.getenv("HOST", "0.0.0.0")
    PORT = int(os.getenv("PORT", "3000"))
    DEBUG = os.getenv("DEBUG", "False").lower() == "true"
    
    # ==================== 日志配置 ====================
    LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
    
    # ==================== Jira配置 ====================
    JIRA_URL = os.getenv("JIRA_URL", "https://jira.magikcloud.cn")
    JIRA_USERNAME = os.getenv("JIRA_USERNAME", "admin")
    JIRA_PASSWORD = os.getenv("JIRA_PASSWORD", "XfJjKizUr7PSq6U")
    # 允许的邮箱后缀列表，多个后缀用逗号分隔，如 "@company.com,@example.com"
    # 留空则不限制邮箱后缀
    JIRA_ALLOWED_EMAIL_SUFFIXES = os.getenv("JIRA_ALLOWED_EMAIL_SUFFIXES", "")
    
    @classmethod
    def get_config_db_config(cls):
        """获取数据库连接配置"""
        return {
            "host": cls.MYSQL_HOST,
            "port": cls.MYSQL_PORT,
            "user": cls.MYSQL_USER,
            "password": cls.MYSQL_PASSWORD,
            "database": cls.MYSQL_DATABASE,
            "charset": cls.MYSQL_CHARSET
        }
    
    # 兼容旧代码
    get_alert_db_config = get_config_db_config
    
    @classmethod
    def validate(cls):
        """
        验证必需的配置项是否已设置
        """
        errors = []
        
        # 验证飞书配置
        if not cls.APP_ID:
            errors.append("APP_ID 未配置")
        if not cls.APP_SECRET:
            errors.append("APP_SECRET 未配置")
        
        # 验证MySQL配置
        if not cls.MYSQL_HOST:
            errors.append("MYSQL_HOST 未配置")
        if not cls.MYSQL_USER:
            errors.append("MYSQL_USER 未配置")
        if not cls.MYSQL_PASSWORD:
            errors.append("MYSQL_PASSWORD 未配置")
        
        if errors:
            error_msg = "\n".join(errors)
            raise ValueError(f"配置验证失败:\n{error_msg}\n\n请检查 .env 文件配置")
        
        return True
    
    @classmethod
    def show_config(cls):
        """显示当前配置（隐藏敏感信息）"""
        config_info = {
            "飞书配置": {
                "APP_ID": cls.APP_ID,
                "APP_SECRET": "***" if cls.APP_SECRET else None,
                "LARK_HOST": cls.LARK_HOST,
            },
            "数据库配置": {
                "host": cls.MYSQL_HOST,
                "port": cls.MYSQL_PORT,
                "user": cls.MYSQL_USER,
                "password": "***" if cls.MYSQL_PASSWORD else None,
                "database": cls.MYSQL_DATABASE,
            },
            "服务配置": {
                "host": cls.HOST,
                "port": cls.PORT,
                "debug": cls.DEBUG,
                "log_level": cls.LOG_LEVEL,
            }
        }
        return config_info


# 创建全局配置实例
config = Config()


if __name__ == "__main__":
    """测试配置"""
    import json
    
    print("=" * 60)
    print("配置信息:")
    print("=" * 60)
    print(json.dumps(config.show_config(), ensure_ascii=False, indent=2))
    print("=" * 60)
    
    try:
        config.validate()
        print("✅ 配置验证通过")
    except ValueError as e:
        print(f"❌ 配置验证失败:\n{e}")

