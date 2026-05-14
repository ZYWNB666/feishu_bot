import json
import random
import mysql.connector
from mysql.connector import Error
import string
import datetime
import pytz
import logging

from config import config

logger = logging.getLogger(__name__)


def save_dbdata(post_data, project, group_id=None):
    json_data = [post_data]

    utc_now = datetime.datetime.now(pytz.utc)
    beijing_time = utc_now.astimezone(pytz.timezone('Asia/Shanghai'))
    iso_format = beijing_time.isoformat()
    startsAtTime = iso_format[:23] + "Z"
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
    """通过 fingerprint（+可选 group_id）查找对应告警的 alerttime（ISO 字符串，取最新一条）"""
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
                "ORDER BY alerttime ASC LIMIT 1",
                (json.dumps(fingerprint), group_id)
            )
        else:
            cursor.execute(
                "SELECT alerttime FROM alert_data "
                "WHERE JSON_CONTAINS(fingerprints, %s) "
                "ORDER BY alerttime ASC LIMIT 1",
                (json.dumps(fingerprint),)
            )
        row = cursor.fetchone()
        if row and row[0]:
            val = row[0]
            # DB 存储的是北京时间（save_dbdata 用 beijing_time 写入）
            # Grafana endsAt 同样是北京时间（Grafana 时区为 Asia/Shanghai，但加了假 Z 后缀）
            # 两边都是 naive 北京时间，直接返回不做时区转换，保持一致
            if hasattr(val, 'strftime'):
                return val.strftime('%Y-%m-%dT%H:%M:%SZ')
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
    """通过 fingerprint（+可选 group_id）查找对应告警的飞书消息 ID（取最新一条）"""
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
                "ORDER BY alerttime DESC LIMIT 1",
                (json.dumps(fingerprint), group_id)
            )
        else:
            cursor.execute(
                "SELECT message_id FROM alert_data "
                "WHERE JSON_CONTAINS(fingerprints, %s) AND message_id IS NOT NULL "
                "ORDER BY alerttime DESC LIMIT 1",
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
