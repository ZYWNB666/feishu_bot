import json
import logging
import re
import threading
import time

from db.pool import db_cursor
from utils.regex_cache import compile_pattern

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# alert_config 静态缓存（P2 优化）
# ──────────────────────────────────────────────
# get_alert_config_by_labels 每次都全表扫描 alert_config（含 label_rules 的所有行），
# 在高频告警场景下数据库压力较大。这里对"全表 label_rules 配置"做短 TTL 内存缓存，
# 避免每次告警都打 DB。配置变更通过管理接口的 invalidate_alert_config_cache() 主动失效。
from config.constants import ALERT_CONFIG_CACHE_TTL

_alert_config_cache_lock = threading.Lock()
_alert_config_cache: list = []  # 缓存所有含 label_rules 的配置行
_alert_config_cache_expire_at: float = 0.0


def _get_all_label_rule_configs() -> list:
    """获取所有含 label_rules 的 alert_config 行（带 TTL 内存缓存）。

    缓存命中时直接返回内存副本，避免高频告警下重复全表扫描。
    """
    global _alert_config_cache, _alert_config_cache_expire_at
    now = time.time()
    if _alert_config_cache and now < _alert_config_cache_expire_at:
        # 返回浅拷贝，避免调用方误改缓存
        return list(_alert_config_cache)

    with _alert_config_cache_lock:
        # double-check
        if _alert_config_cache and time.time() < _alert_config_cache_expire_at:
            return list(_alert_config_cache)
        try:
            with db_cursor(dictionary=True) as (conn, cursor):
                cursor.execute(
                    "SELECT * FROM alert_config WHERE label_rules IS NOT NULL ORDER BY id ASC"
                )
                rows = cursor.fetchall()
        except Exception:
            if _alert_config_cache:
                logger.exception("加载 alert_config label_rules 失败，继续使用旧缓存")
                return list(_alert_config_cache)
            raise

        _alert_config_cache = rows
        _alert_config_cache_expire_at = now + ALERT_CONFIG_CACHE_TTL
        logger.debug("alert_config label_rules 缓存已刷新: %d 行", len(rows))
        return list(rows)


def invalidate_alert_config_cache() -> None:
    """主动失效 alert_config 静态缓存。

    在 alert_config 表发生 CRUD（创建/更新/删除规则）后调用，
    确保后续告警路由能读到最新配置。
    """
    global _alert_config_cache, _alert_config_cache_expire_at
    with _alert_config_cache_lock:
        _alert_config_cache = []
        _alert_config_cache_expire_at = 0.0
    logger.info("alert_config 静态缓存已失效")


def get_alert_config_by_alertid(alertid: str) -> dict:
    """
    根据alertid查询alert_config表，返回该行的配置信息
    """
    with db_cursor(dictionary=True) as (conn, cursor):
        sql = "SELECT * FROM alert_config WHERE alert_id = %s"
        cursor.execute(sql, (alertid,))
        return cursor.fetchone()


def get_alert_config_by_project(project: str) -> dict:
    """
    根据project字段查询alert_config表，返回该行的配置信息
    如果有多个匹配项，返回第一个
    """
    with db_cursor(dictionary=True) as (conn, cursor):
        sql = "SELECT * FROM alert_config WHERE project = %s LIMIT 1"
        cursor.execute(sql, (project,))
        return cursor.fetchone()


def get_alert_config_by_labels(alert_labels: dict) -> list:
    """
    根据标签匹配规则查询alert_config表
    键模糊匹配（通配符），值精准匹配
    :param alert_labels: dict, 告警中的所有标签
    :return: list, 所有匹配的配置信息列表，如果没有匹配则返回空列表
    """
    if not alert_labels:
        return []

    # 使用静态缓存获取所有含 label_rules 的配置（避免每次全表扫描）
    configs = _get_all_label_rule_configs()

    matched_configs = []

    # 遍历每个配置，检查是否匹配
    for config_item in configs:
        label_rules = config_item.get('label_rules')
        if not label_rules:
            continue

        # 如果 label_rules 是字符串，尝试解析为JSON
        if isinstance(label_rules, str):
            try:
                label_rules = json.loads(label_rules)
            except (json.JSONDecodeError, ValueError):
                continue

        # 检查是否所有规则都匹配
        if _match_label_rules(alert_labels, label_rules):
            matched_configs.append(config_item)

    return matched_configs


def _match_label_rules(alert_labels: dict, label_rules: dict) -> bool:
    """
    检查告警标签是否匹配规则
    规则说明：
    - 键正则匹配：规则中的键使用正则表达式匹配告警标签的键
    - 值正则匹配：规则中的值使用正则表达式匹配告警标签的值
    
    优化：正则编译结果通过 utils.regex_cache 缓存，避免每次请求重新编译。

    :param alert_labels: dict, 告警中的标签
    :param label_rules: dict, 数据库中配置的标签匹配规则（支持正则表达式）
    :return: bool, 是否所有规则都匹配
    """
    if not label_rules:
        return False
    
    # 遍历所有规则
    for rule_key, rule_value in label_rules.items():
        matched = False

        # 编译键的正则表达式（不区分大小写），结果缓存
        key_pattern = compile_pattern(rule_key, re.IGNORECASE)

        # 编译值的正则表达式，结果缓存
        value_pattern = compile_pattern(str(rule_value))
        
        # 在告警标签中查找匹配的键
        for alert_key, alert_value in alert_labels.items():
            key_matched = False
            
            # 键匹配（支持正则表达式）
            if key_pattern:
                key_matched = key_pattern.search(alert_key) is not None
            else:
                # 降级到精确匹配
                key_matched = rule_key.lower() == alert_key.lower()
            
            if key_matched:
                # 值匹配（支持正则表达式）
                if value_pattern:
                    if value_pattern.search(str(alert_value)):
                        matched = True
                        break
                else:
                    # 降级到精确匹配
                    if str(rule_value) == str(alert_value):
                        matched = True
                        break
        
        # 如果任何一个规则不匹配，返回False
        if not matched:
            return False
    
    # 所有规则都匹配，返回True
    return True


# ──────────────────────────────────────────────
# feishu_users 表：姓名 → open_id 本地映射
# ──────────────────────────────────────────────

def get_open_id_by_name(name: str) -> str:
    """根据姓名从 feishu_users 表查询 open_id

    Args:
        name: 用户姓名（精确匹配）

    Returns:
        str: open_id，未找到返回空字符串
    """
    with db_cursor(dictionary=True) as (conn, cursor):
        cursor.execute("SELECT open_id FROM feishu_users WHERE name = %s LIMIT 1", (name,))
        row = cursor.fetchone()
        return row["open_id"] if row else ""


def get_open_ids_by_names(names: list) -> dict:
    """批量根据姓名查询 open_id

    Args:
        names: 姓名列表

    Returns:
        dict: name -> open_id，未找到的 name 不在字典中
    """
    if not names:
        return {}
    with db_cursor(dictionary=True) as (conn, cursor):
        placeholders = ",".join(["%s"] * len(names))
        cursor.execute(f"SELECT name, open_id FROM feishu_users WHERE name IN ({placeholders})", names)
        rows = cursor.fetchall()
        return {row["name"]: row["open_id"] for row in rows}
