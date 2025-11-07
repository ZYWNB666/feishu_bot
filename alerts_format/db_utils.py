import mysql.connector
import json
from config import config

def get_db_conn():
    """获取配置数据库连接"""
    db_config = config.get_config_db_config()
    return mysql.connector.connect(**db_config)

def get_alert_config_by_alertid(alertid: str) -> dict:
    """
    根据alertid查询alert_config表，返回该行的配置信息
    """
    conn = get_db_conn()
    try:
        cursor = conn.cursor(dictionary=True)
        sql = "SELECT * FROM alert_config WHERE alert_id = %s"
        cursor.execute(sql, (alertid,))
        result = cursor.fetchone()
        cursor.close()
        return result
    finally:
        conn.close()


def get_alert_config_by_project(project: str) -> dict:
    """
    根据project字段查询alert_config表，返回该行的配置信息
    如果有多个匹配项，返回第一个
    """
    conn = get_db_conn()
    try:
        cursor = conn.cursor(dictionary=True)
        sql = "SELECT * FROM alert_config WHERE project = %s LIMIT 1"
        cursor.execute(sql, (project,))
        result = cursor.fetchone()
        cursor.close()
        return result
    finally:
        conn.close()


def get_alert_config_by_labels(alert_labels: dict) -> list:
    """
    根据标签匹配规则查询alert_config表
    键模糊匹配（通配符），值精准匹配
    :param alert_labels: dict, 告警中的所有标签
    :return: list, 所有匹配的配置信息列表，如果没有匹配则返回空列表
    """
    if not alert_labels:
        return []
    
    conn = get_db_conn()
    try:
        cursor = conn.cursor(dictionary=True)
        # 查询所有有 label_rules 配置的记录，按优先级降序、id升序排序
        sql = "SELECT * FROM alert_config WHERE label_rules IS NOT NULL ORDER BY id ASC"
        cursor.execute(sql)
        configs = cursor.fetchall()
        cursor.close()
        
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
    finally:
        conn.close()


def _match_label_rules(alert_labels: dict, label_rules: dict) -> bool:
    """
    检查告警标签是否匹配规则
    规则说明：
    - 键正则匹配：规则中的键使用正则表达式匹配告警标签的键
    - 值正则匹配：规则中的值使用正则表达式匹配告警标签的值
    
    :param alert_labels: dict, 告警中的标签
    :param label_rules: dict, 数据库中配置的标签匹配规则（支持正则表达式）
    :return: bool, 是否所有规则都匹配
    """
    import re
    
    if not label_rules:
        return False
    
    # 遍历所有规则
    for rule_key, rule_value in label_rules.items():
        matched = False
        
        try:
            # 编译键的正则表达式（不区分大小写）
            key_pattern = re.compile(rule_key, re.IGNORECASE)
        except re.error:
            # 如果正则表达式无效，使用精确匹配
            key_pattern = None
        
        try:
            # 编译值的正则表达式
            value_pattern = re.compile(str(rule_value))
        except re.error:
            # 如果正则表达式无效，使用精确匹配
            value_pattern = None
        
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