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

def save_dbdata(post_data, project):
    json_data = [post_data]

    # 获取UTC时间
    utc_now = datetime.datetime.now(pytz.utc)
    beijing_time = utc_now.astimezone(pytz.timezone('Asia/Shanghai'))
    iso_format = beijing_time.isoformat()
    startsAtTime = iso_format[:23] + "Z"
    random_number = ''.join(random.choice(string.ascii_letters + string.digits) for _ in range(20))

    # 初始化一个空列表来存储所有的matchers
    matchers = []

    # 遍历JSON数据中的alerts
    for alert_group in json_data:
        for alert in alert_group.get('alerts', []):
            if alert.get('status') == 'resolved':
                continue
            labels = alert.get('labels', {})
            # 为当前alert创建一个matchers对象
            matchers_object = {
                "matchers": []
            }
            # 将当前alert的labels转换为matchers格式并添加到当前alert的matchers对象中
            for label_name, label_value in labels.items():
                matchers_object["matchers"].append({
                    "name": label_name,
                    "value": label_value,
                    "isRegex": False,
                    "isEqual": True
                })
            # 将当前alert的matchers对象添加到总的matchers列表中
            matchers.append(matchers_object)

    if not matchers:
        logger.info("没有告警的数据，不写入数据库")
        return None

    output_mysql = {
        "matchers": matchers
    }
    json_data_to_insert = json.dumps(output_mysql)
    try:
        # 从config获取告警数据库配置
        db_config = config.get_alert_db_config()
        connection = mysql.connector.connect(**db_config)

        if connection.is_connected():
            db_Info = connection.get_server_info()
            logger.info(f"成功连接到MySQL数据库，版本：{db_Info}")

            cursor = connection.cursor()
            insert_query = """
                INSERT INTO alert_data (id, alertlabels, project, alerttime)
                VALUES (%s, %s, %s, %s)
            """

            id_value = random_number

            # 插入id和JSON数据
            cursor.execute(insert_query, (id_value, json_data_to_insert, project, startsAtTime))

            # 提交事务
            connection.commit()
            # print("数据插入成功。")
            return id_value

    except Error as e:
        logger.error(f"连接或插入数据时出错：{e}")
        return f"查询数据时出错：, {e}"

    finally:
        # 关闭游标和连接
        if connection.is_connected():
            cursor.close()
            connection.close()
            # print("MySQL连接已关闭。")