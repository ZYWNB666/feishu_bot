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
    # 配置数据库（存储alert_config表）
    MYSQL_CONFIG_HOST = os.getenv("MYSQL_CONFIG_HOST", "localhost")
    MYSQL_CONFIG_PORT = int(os.getenv("MYSQL_CONFIG_PORT", "3306"))
    MYSQL_CONFIG_USER = os.getenv("MYSQL_CONFIG_USER", "root")
    MYSQL_CONFIG_PASSWORD = os.getenv("MYSQL_CONFIG_PASSWORD", "")
    MYSQL_CONFIG_DATABASE = os.getenv("MYSQL_CONFIG_DATABASE", "alert_db")
    MYSQL_CONFIG_CHARSET = os.getenv("MYSQL_CONFIG_CHARSET", "utf8mb4")
    
    # 告警数据库（存储alert_data表）
    MYSQL_ALERT_HOST = os.getenv("MYSQL_ALERT_HOST", None)  # 默认与配置库相同
    MYSQL_ALERT_PORT = int(os.getenv("MYSQL_ALERT_PORT", "3306"))
    MYSQL_ALERT_USER = os.getenv("MYSQL_ALERT_USER", None)  # 默认与配置库相同
    MYSQL_ALERT_PASSWORD = os.getenv("MYSQL_ALERT_PASSWORD", None)  # 默认与配置库相同
    MYSQL_ALERT_DATABASE = os.getenv("MYSQL_ALERT_DATABASE", "alert_db")
    MYSQL_ALERT_CHARSET = os.getenv("MYSQL_ALERT_CHARSET", "utf8mb4")
    
    # ==================== 服务配置 ====================
    HOST = os.getenv("HOST", "0.0.0.0")
    PORT = int(os.getenv("PORT", "3000"))
    DEBUG = os.getenv("DEBUG", "False").lower() == "true"
    
    # ==================== 日志配置 ====================
    LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
    
    @classmethod
    def get_config_db_config(cls):
        """
        获取配置数据库连接配置
        用于连接存储alert_config配置的数据库
        """
        return {
            "host": cls.MYSQL_CONFIG_HOST,
            "port": cls.MYSQL_CONFIG_PORT,
            "user": cls.MYSQL_CONFIG_USER,
            "password": cls.MYSQL_CONFIG_PASSWORD,
            "database": cls.MYSQL_CONFIG_DATABASE,
            "charset": cls.MYSQL_CONFIG_CHARSET
        }
    
    @classmethod
    def get_alert_db_config(cls):
        """
        获取告警数据库连接配置
        用于连接存储alert_data告警记录的数据库
        如果未单独配置，则使用配置数据库的连接信息
        """
        return {
            "host": cls.MYSQL_ALERT_HOST or cls.MYSQL_CONFIG_HOST,
            "port": cls.MYSQL_ALERT_PORT,
            "user": cls.MYSQL_ALERT_USER or cls.MYSQL_CONFIG_USER,
            "password": cls.MYSQL_ALERT_PASSWORD or cls.MYSQL_CONFIG_PASSWORD,
            "database": cls.MYSQL_ALERT_DATABASE,
            "charset": cls.MYSQL_ALERT_CHARSET
        }
    
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
        if not cls.MYSQL_CONFIG_HOST:
            errors.append("MYSQL_CONFIG_HOST 未配置")
        if not cls.MYSQL_CONFIG_USER:
            errors.append("MYSQL_CONFIG_USER 未配置")
        if not cls.MYSQL_CONFIG_PASSWORD:
            errors.append("MYSQL_CONFIG_PASSWORD 未配置")
        
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
            "配置数据库": {
                "host": cls.MYSQL_CONFIG_HOST,
                "port": cls.MYSQL_CONFIG_PORT,
                "user": cls.MYSQL_CONFIG_USER,
                "password": "***" if cls.MYSQL_CONFIG_PASSWORD else None,
                "database": cls.MYSQL_CONFIG_DATABASE,
            },
            "告警数据库": {
                "host": cls.MYSQL_ALERT_HOST or cls.MYSQL_CONFIG_HOST,
                "port": cls.MYSQL_ALERT_PORT,
                "user": cls.MYSQL_ALERT_USER or cls.MYSQL_CONFIG_USER,
                "password": "***" if (cls.MYSQL_ALERT_PASSWORD or cls.MYSQL_CONFIG_PASSWORD) else None,
                "database": cls.MYSQL_ALERT_DATABASE,
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

