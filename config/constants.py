#!/usr/bin/env python3
"""
全局常量定义模块

集中管理项目中散落的魔法数字/魔法字符串，统一命名，便于维护与调优。
按功能域分组组织。
"""

import os


# ==================== 告警去重（alert_handler） ====================
# firing 批次 fingerprint 级别去重 TTL（秒）：防止 Grafana repeat_interval 重复投递同一 firing 告警
ALERT_DEDUP_TTL = int(os.getenv("ALERT_DEDUP_TTL", "300"))

# resolved 批次 fingerprint 级别去重 TTL（秒）：覆盖多个 Grafana repeat_interval 周期
RESOLVED_DEDUP_TTL = int(os.getenv("RESOLVED_DEDUP_TTL", "1800"))

# alertname+group_id 语义级去重 TTL（秒），与 ALERT_DEDUP_TTL 一致
ALERT_LABEL_DEDUP_TTL = ALERT_DEDUP_TTL

# 各类去重缓存的最大条目数（防止内存无限增长）
ALERT_DEDUP_CACHE_MAXSIZE = int(os.getenv("ALERT_DEDUP_CACHE_MAXSIZE", "50000"))


# ==================== 飞书事件/回调去重 ====================
# 飞书事件 event_id 去重 TTL（秒），默认 1 小时
EVENT_CACHE_TTL = int(os.getenv("EVENT_CACHE_TTL", "3600"))
EVENT_CACHE_MAXSIZE = int(os.getenv("EVENT_CACHE_MAXSIZE", "20000"))

# 飞书卡片回调去重 TTL（秒），默认 5 秒（拦截快速重复点击）
CALLBACK_CACHE_TTL = int(os.getenv("CALLBACK_CACHE_TTL", "5"))
CALLBACK_CACHE_MAXSIZE = int(os.getenv("CALLBACK_CACHE_MAXSIZE", "10000"))


# ==================== HTTP / 网络超时 ====================
# 飞书 OpenAPI 请求超时（秒）
FEISHU_API_TIMEOUT = int(os.getenv("FEISHU_API_TIMEOUT", "10"))

# Flashcat API 请求超时（秒）
FLASHCAT_API_TIMEOUT = int(os.getenv("FLASHCAT_API_TIMEOUT", "10"))

# Alertmanager / Grafana 静默 API 请求超时（秒）
SILENCE_API_TIMEOUT = int(os.getenv("SILENCE_API_TIMEOUT", "30"))


# ==================== 重试策略 ====================
# 通用最大重试次数
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))

# 重试退避基数（秒），第 n 次重试等待 n * RETRY_BACKOFF_BASE
RETRY_BACKOFF_BASE = int(os.getenv("RETRY_BACKOFF_BASE", "2"))


# ==================== 飞书 tenant_access_token 缓存 ====================
# token 过期前预留的安全刷新余量（秒），避免临界点使用已过期 token
TOKEN_REFRESH_BUFFER = int(os.getenv("TOKEN_REFRESH_BUFFER", "300"))


# ==================== 静默时长选项（秒） ====================
SILENCE_DURATION_2H = 7200
SILENCE_DURATION_12H = 43200
SILENCE_DURATION_24H = 86400
SILENCE_DURATION_3D = 259200

# 默认静默时长（秒），用于按钮缺省值
DEFAULT_SILENCE_DURATION = SILENCE_DURATION_2H


# ==================== 告警记录（alert_data） ====================
# MAID 随机字符串长度
MAID_LENGTH = int(os.getenv("MAID_LENGTH", "20"))


# ==================== 告警统计（alert_stats） ====================
# 默认统计时间范围（天）
ALERT_STATS_DEFAULT_DAYS = int(os.getenv("ALERT_STATS_DEFAULT_DAYS", "7"))

# Top 告警默认返回条数
ALERT_STATS_DEFAULT_TOP_LIMIT = int(os.getenv("ALERT_STATS_DEFAULT_TOP_LIMIT", "20"))

# 告警详情默认返回条数
ALERT_STATS_DEFAULT_DETAILS_LIMIT = int(os.getenv("ALERT_STATS_DEFAULT_DETAILS_LIMIT", "200"))


# ==================== 路由配置缓存（静态缓存） ====================
# alert_config（含 label_rules）内存缓存 TTL（秒），减少高频告警下的全表扫描
ALERT_CONFIG_CACHE_TTL = int(os.getenv("ALERT_CONFIG_CACHE_TTL", "30"))


# ==================== 数据库连接池 ====================
# 连接池大小
MYSQL_POOL_SIZE = int(os.getenv("MYSQL_POOL_SIZE", "10"))

# 连接池获取连接超时（秒）
MYSQL_POOL_ACQUIRE_TIMEOUT = int(os.getenv("MYSQL_POOL_ACQUIRE_TIMEOUT", "30"))
