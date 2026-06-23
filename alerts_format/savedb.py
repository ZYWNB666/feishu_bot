import json
import random
import mysql.connector
from mysql.connector import Error
import string
import datetime
import logging

from config import config

logger = logging.getLogger(__name__)


def save_dbdata(post_data, project, group_id=None):
    json_data = [post_data]

    # 使用 Grafana 发送的最早 startsAt（已配置为上海时区），直接使用原始值不做转换
    min_starts_at = None
    for alert in post_data.get('alerts', []):
        if alert.get('status') == 'resolved':
            continue
        sa = alert.get('startsAt', '')
        # 过滤无效占位时间
        if sa and sa != '0001-01-01T00:00:00Z':
            if min_starts_at is None or sa < min_starts_at:
                min_starts_at = sa
    if min_starts_at:
        startsAtTime = min_starts_at
    else:
        # 备用：如果 Grafana 没有发 startsAt 则用当前本地时间（容器已配置上海时区）
        startsAtTime = datetime.datetime.now().astimezone().isoformat()

    random_number = ''.join(random.choice(string.ascii_letters + string.digits) for _ in range(20))

    matchers = []
    fingerprints = []

    for alert_group in json_data:
        for alert in alert_group.get('alerts', []):
            if alert.get('status') == 'resolved':
                continue
            labels = alert.get('labels', {})
            matchers_object = {"matchers": []}
            for label_name, label_value in labels.items():
                matchers_object["matchers"].append({
                    "name": label_name,
                    "value": label_value,
                    "isRegex": False,
                    "isEqual": True
                })
            matchers.append(matchers_object)
            fp = alert.get('fingerprint')
            if fp and fp not in fingerprints:
                fingerprints.append(fp)

    if not matchers:
        logger.info("没有告警的数据，不写入数据库")
        return None

    output_mysql = {"matchers": matchers}
    json_data_to_insert = json.dumps(output_mysql)
    fingerprints_json = json.dumps(fingerprints)

    connection = None
    try:
        db_config = config.get_alert_db_config()
        connection = mysql.connector.connect(**db_config)

        if connection.is_connected():
            logger.info("成功连接到MySQL数据库")
            cursor = connection.cursor()
            insert_query = """
                INSERT INTO alert_data (id, alertlabels, project, alerttime, fingerprints, group_id)
                VALUES (%s, %s, %s, %s, %s, %s)
            """
            cursor.execute(insert_query, (
                random_number, json_data_to_insert, project, startsAtTime, fingerprints_json, group_id
            ))
            connection.commit()
            return random_number

    except Error as e:
        logger.error("连接或插入数据时出错：%s", e)
        return None

    finally:
        if connection and connection.is_connected():
            cursor.close()
            connection.close()


def update_message_id(maid: str, message_id: str) -> None:
    """将飞书消息 ID 写入 alert_data，用于话题回复"""
    if not maid or not message_id:
        return
    connection = None
    try:
        db_config = config.get_alert_db_config()
        connection = mysql.connector.connect(**db_config)
        cursor = connection.cursor()
        cursor.execute(
            "UPDATE alert_data SET message_id = %s WHERE id = %s",
            (message_id, maid)
        )
        connection.commit()
        logger.debug("已将 message_id=%s 写入 maid=%s", message_id, maid)
    except Error as e:
        logger.error("更新 message_id 失败: %s", e)
    finally:
        if connection and connection.is_connected():
            cursor.close()
            connection.close()


def get_alerttime_by_fingerprint(fingerprint: str, group_id: str = None) -> str:
    """通过 fingerprint（+可选 group_id）查找对应告警的 alerttime（ISO 字符串，取最早一条触发时间用于计算时长）"""
    if not fingerprint:
        return ''
    connection = None
    try:
        db_config = config.get_alert_db_config()
        connection = mysql.connector.connect(**db_config)
        cursor = connection.cursor()
        if group_id:
            cursor.execute(
                "SELECT alerttime FROM alert_data "
                "WHERE JSON_CONTAINS(fingerprints, %s) AND group_id = %s "
                "ORDER BY created_at DESC, alerttime DESC LIMIT 1",
                (json.dumps(fingerprint), group_id)
            )
        else:
            cursor.execute(
                "SELECT alerttime FROM alert_data "
                "WHERE JSON_CONTAINS(fingerprints, %s) "
                "ORDER BY created_at DESC, alerttime DESC LIMIT 1",
                (json.dumps(fingerprint),)
            )
        row = cursor.fetchone()
        if row and row[0]:
            val = row[0]
            # DB alerttime 存的是 Grafana 原始 startsAt（上海时区）
            # MySQL DATETIME 返回 naive datetime，直接格式化返回即可
            if hasattr(val, 'strftime'):
                return val.strftime('%Y-%m-%dT%H:%M:%S')
            return str(val)
        return ''
    except Error as e:
        logger.error("查询 alerttime 失败: %s", e)
        return ''
    finally:
        if connection and connection.is_connected():
            cursor.close()
            connection.close()


def get_message_id_by_fingerprint(fingerprint: str, group_id: str = None) -> str:
    """通过 fingerprint（+可选 group_id）查找对应告警的飞书消息 ID（取最新记录，即最后一次告警对应的话题）"""
    if not fingerprint:
        return ''
    connection = None
    try:
        db_config = config.get_alert_db_config()
        connection = mysql.connector.connect(**db_config)
        cursor = connection.cursor()
        if group_id:
            cursor.execute(
                "SELECT message_id FROM alert_data "
                "WHERE JSON_CONTAINS(fingerprints, %s) AND message_id IS NOT NULL AND group_id = %s "
                "ORDER BY created_at DESC, alerttime DESC LIMIT 1",
                (json.dumps(fingerprint), group_id)
            )
        else:
            cursor.execute(
                "SELECT message_id FROM alert_data "
                "WHERE JSON_CONTAINS(fingerprints, %s) AND message_id IS NOT NULL "
                "ORDER BY created_at DESC, alerttime DESC LIMIT 1",
                (json.dumps(fingerprint),)
            )
        row = cursor.fetchone()
        return row[0] if row else ''
    except Error as e:
        logger.error("查询 message_id 失败: %s", e)
        return ''
    finally:
        if connection and connection.is_connected():
            cursor.close()
            connection.close()


def get_all_fingerprints_by_fingerprint(fingerprints: list, group_id: str = None) -> list:
    """通过当前 resolved 批次中的 fingerprint 集合，反查所有关联 firing 记录中的全部 fingerprint。

    用于 resolved 时的部分恢复检测：Grafana 按实例维度发送 resolved 通知，
    每批只含部分实例。由于 firing 时可能因拆分+聚合产生多条 DB 记录
   （每条记录只含部分实例的 fingerprint），需要用当前批次所有 fingerprint
    逐个反查，汇总所有命中记录中的全部 fingerprint，才能正确判断是否还有
    实例未恢复。

    Args:
        fingerprints: 当前 resolved 批次中所有实例的 fingerprint 列表
        group_id: 可选，限定群组范围，避免跨群组误匹配
    Returns:
        list: 所有关联 firing 记录中的全部 fingerprint 列表（去重）；查不到时返回空列表
    """
    if not fingerprints:
        return []
    connection = None
    try:
        db_config = config.get_alert_db_config()
        connection = mysql.connector.connect(**db_config)
        cursor = connection.cursor()
        all_fps = set()
        for fp in fingerprints:
            if not fp:
                continue
            if group_id:
                cursor.execute(
                    "SELECT fingerprints FROM alert_data "
                    "WHERE JSON_CONTAINS(fingerprints, %s) AND group_id = %s "
                    "ORDER BY created_at DESC, alerttime DESC",
                    (json.dumps(fp), group_id)
                )
            else:
                cursor.execute(
                    "SELECT fingerprints FROM alert_data "
                    "WHERE JSON_CONTAINS(fingerprints, %s) "
                    "ORDER BY created_at DESC, alerttime DESC",
                    (json.dumps(fp),)
                )
            for row in cursor.fetchall():
                if row and row[0]:
                    fps = row[0]
                    if isinstance(fps, str):
                        fps = json.loads(fps)
                    if fps:
                        all_fps.update(fps)
        return list(all_fps)
    except Error as e:
        logger.error("查询全部 fingerprint 失败: %s", e)
        return []
    finally:
        if connection and connection.is_connected():
            cursor.close()
            connection.close()
