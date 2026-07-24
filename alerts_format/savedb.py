import json
import logging
import random
import string
import datetime

from mysql.connector import Error

from config import config
from config.constants import MAID_LENGTH
from db.pool import db_cursor

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

    random_number = ''.join(random.choice(string.ascii_letters + string.digits) for _ in range(MAID_LENGTH))

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

    try:
        with db_cursor() as (conn, cursor):
            insert_query = """
                INSERT INTO alert_data (id, alertlabels, project, alerttime, fingerprints, group_id)
                VALUES (%s, %s, %s, %s, %s, %s)
            """
            cursor.execute(insert_query, (
                random_number, json_data_to_insert, project, startsAtTime, fingerprints_json, group_id
            ))
            conn.commit()
            logger.info(
                "告警数据已入库: maid=%s project=%s group_id=%s fingerprints=%s alerttime=%s",
                random_number, project, group_id, fingerprints, startsAtTime
            )
            return random_number
    except Error as e:
        logger.error("连接或插入告警数据时出错: maid=%s error=%s", random_number, e)
        return None


def update_message_id(maid: str, message_id: str) -> None:
    """将飞书消息 ID 写入 alert_data，用于话题回复"""
    if not maid or not message_id:
        return
    try:
        with db_cursor() as (conn, cursor):
            cursor.execute(
                "UPDATE alert_data SET message_id = %s WHERE id = %s",
                (message_id, maid)
            )
            conn.commit()
            logger.debug("已将 message_id=%s 写入 maid=%s", message_id, maid)
    except Error as e:
        logger.error("更新 message_id 失败: maid=%s error=%s", maid, e)


def update_incident_id(maid: str, incident_id: str) -> None:
    """将 Flashcat incident_id 写入 alert_data，用于后续认领操作"""
    if not maid or not incident_id:
        return
    try:
        with db_cursor() as (conn, cursor):
            cursor.execute(
                "UPDATE alert_data SET incident_id = %s WHERE id = %s",
                (incident_id, maid)
            )
            conn.commit()
            logger.debug("已将 incident_id=%s 写入 maid=%s", incident_id, maid)
    except Error as e:
        logger.error("更新 incident_id 失败: maid=%s error=%s", maid, e)


def get_incident_id_by_maid(maid: str) -> str:
    """通过 maid 查询 alert_data 中的 Flashcat incident_id"""
    if not maid:
        return ''
    try:
        with db_cursor() as (conn, cursor):
            cursor.execute(
                "SELECT incident_id FROM alert_data WHERE id = %s",
                (maid,)
            )
            row = cursor.fetchone()
            return row[0] if row and row[0] else ''
    except Error as e:
        logger.error("查询 incident_id 失败: maid=%s error=%s", maid, e)
        return ''


def save_card_content(maid: str, card_content: str) -> None:
    """将原始卡片 JSON 存入 alert_data，认领时原地更新卡片使用"""
    if not maid or not card_content:
        return
    try:
        with db_cursor() as (conn, cursor):
            cursor.execute(
                "UPDATE alert_data SET card_content = %s WHERE id = %s",
                (card_content, maid)
            )
            conn.commit()
            logger.debug("已将 card_content 写入 maid=%s (len=%d)", maid, len(card_content))
    except Error as e:
        logger.error("保存 card_content 失败: maid=%s error=%s", maid, e)


def get_card_content(maid: str) -> str:
    """从 alert_data 读取原始卡片 JSON"""
    if not maid:
        return ''
    try:
        with db_cursor() as (conn, cursor):
            cursor.execute(
                "SELECT card_content FROM alert_data WHERE id = %s",
                (maid,)
            )
            row = cursor.fetchone()
            return row[0] if row and row[0] else ''
    except Error as e:
        logger.error("查询 card_content 失败: maid=%s error=%s", maid, e)
        return ''


def get_maid_by_fingerprints(fingerprints: list, group_id: str = None) -> str:
    """通过 resolved 包的 fingerprint 反查最近一条 firing 告警的 MAID。"""
    if not fingerprints:
        return ''
    try:
        with db_cursor() as (conn, cursor):
            for fingerprint in fingerprints:
                if not fingerprint:
                    continue
                if group_id:
                    cursor.execute(
                        "SELECT id FROM alert_data "
                        "WHERE JSON_CONTAINS(fingerprints, %s) AND group_id = %s "
                        "ORDER BY created_at DESC, alerttime DESC LIMIT 1",
                        (json.dumps(fingerprint), group_id)
                    )
                else:
                    cursor.execute(
                        "SELECT id FROM alert_data "
                        "WHERE JSON_CONTAINS(fingerprints, %s) "
                        "ORDER BY created_at DESC, alerttime DESC LIMIT 1",
                        (json.dumps(fingerprint),)
                    )
                row = cursor.fetchone()
                if row and row[0]:
                    return row[0]
        return ''
    except Error as e:
        logger.error(
            "通过 fingerprint 反查 MAID 失败: group_id=%s fingerprints=%s error=%s",
            group_id, fingerprints, e
        )
        return ''


def get_alerttime_by_fingerprint(fingerprint: str, group_id: str = None) -> str:
    """通过 fingerprint（+可选 group_id）查找对应告警的 alerttime（ISO 字符串，取最早一条触发时间用于计算时长）"""
    if not fingerprint:
        return ''
    try:
        with db_cursor() as (conn, cursor):
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


def get_message_id_by_fingerprint(fingerprint: str, group_id: str = None) -> str:
    """通过 fingerprint（+可选 group_id）查找对应告警的飞书消息 ID（取最新记录，即最后一次告警对应的话题）"""
    if not fingerprint:
        return ''
    try:
        with db_cursor() as (conn, cursor):
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
    all_fps = set()
    try:
        with db_cursor() as (conn, cursor):
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
