import json
import logging
from datetime import datetime, timedelta

import requests
from mysql.connector import Error

from config import config
from config.constants import SILENCE_API_TIMEOUT
from db.pool import db_cursor

logger = logging.getLogger(__name__)


def _get_alert_data_and_alertmanager_url(maid: str):
    """查询 alert_data（alertlabels/project/silenceid）与 alert_config（alertmanager_url）。

    合并 ma.py 中 macreate/madelete 重复的两次 DB 查询为一次，减少连接获取次数。
    Returns:
        (alertlabels_data, project, silenceid_json, alertmanager_url) 或 None（未找到记录）
    """
    try:
        with db_cursor(dictionary=True) as (conn, cursor):
            cursor.execute(
                "SELECT alertlabels, project, silenceid FROM alert_data WHERE id = %s",
                (maid,)
            )
            row = cursor.fetchone()
            if not row:
                return None
            project = row.get('project')
            cursor.execute(
                "SELECT alertmanager_url FROM alert_config WHERE project = %s LIMIT 1",
                (project,)
            )
            cfg_row = cursor.fetchone()
            alertmanager_url = cfg_row['alertmanager_url'] if cfg_row else None
            return row.get('alertlabels'), project, row.get('silenceid'), alertmanager_url
    except Error as e:
        logger.error("查询 alert_data/alert_config 失败: maid=%s error=%s", maid, e)
        return None


def madelete(maid):
    """
    删除告警静默
    :param maid: 告警ID
    :return: 响应结果
    """
    try:
        logger.info("Alertmanager silence delete started: maid=%s", maid)
        result = _get_alert_data_and_alertmanager_url(maid)
        if not result:
            logger.error("没有找到告警记录: maid=%s", maid)
            return {
                "success": False,
                "message": f"没有找到id为 {maid} 的记录"
            }
        alertlabels_data, project, silenceid_json, alertma_config = result

        if not silenceid_json:
            logger.warning("告警没有关联的静默规则: maid=%s", maid)
            return {
                "success": False,
                "message": "该告警没有关联的静默规则"
            }

        if not alertma_config:
            logger.error("未找到项目的 alertmanager_url 配置: maid=%s project=%s", maid, project)
            return {
                "success": False,
                "message": f"未找到项目 {project} 的配置"
            }

        # 解析 silenceid 列表
        silence_ids = json.loads(silenceid_json)
        logger.info("Alertmanager silence delete batch: maid=%s total=%d", maid, len(silence_ids))

        # 删除每个静默规则
        deleted_count = 0
        for silence_id in silence_ids:
            try:
                url = f"{alertma_config}/api/v2/silence/{silence_id}"
                response = requests.delete(url, timeout=SILENCE_API_TIMEOUT)

                if response.status_code in [200, 204]:
                    deleted_count += 1
                    logger.info("Alertmanager silence deleted: maid=%s silence_id=%s", maid, silence_id)
                else:
                    logger.error(
                        "删除静默规则失败: maid=%s silence_id=%s status=%s",
                        maid, silence_id, response.status_code
                    )
            except Exception as e:
                logger.error(
                    "删除静默规则时出错: maid=%s silence_id=%s error=%s",
                    maid, silence_id, e
                )

        # 清空数据库中的 silenceid
        with db_cursor() as (conn, cursor):
            cursor.execute("UPDATE alert_data SET silenceid = NULL WHERE id = %s", (maid,))
            conn.commit()

        logger.info("Alertmanager silence delete completed: maid=%s success=%d total=%d", maid, deleted_count, len(silence_ids))

        return {
            "success": True,
            "deleted_count": deleted_count,
            "total_count": len(silence_ids),
            "message": f"成功删除 {deleted_count} 个静默规则"
        }

    except Error as e:
        logger.error("删除静默数据库操作出错: maid=%s error=%s", maid, e, exc_info=True)
        return {
            "success": False,
            "message": f"数据库操作出错：{str(e)}"
        }
    except Exception as e:
        logger.error("删除静默失败: maid=%s error=%s", maid, e, exc_info=True)
        return {
            "success": False,
            "message": f"删除静默失败：{str(e)}"
        }


def macreate(maid, matime):
    """
    创建告警静默
    :param maid: 告警ID
    :param matime: 静默时长（小时）
    :return: 响应结果
    """
    try:
        matime_hours = int(matime)
        logger.info("Alertmanager silence create started: maid=%s duration_hours=%s", maid, matime)
        result = _get_alert_data_and_alertmanager_url(maid)
        if not result:
            logger.error("没有找到告警记录: maid=%s", maid)
            return {
                "success": False,
                "message": f"没有找到id为 {maid} 的记录"
            }
        alertlabels_data, project, silenceid_json, alertma_config = result

        if not alertma_config:
            logger.error("未找到项目的 alertmanager_url 配置: maid=%s project=%s", maid, project)
            return {
                "success": False,
                "message": f"未找到项目 {project} 的配置"
            }

        alertlabels_dict = json.loads(alertlabels_data) if isinstance(alertlabels_data, str) else alertlabels_data
        matchers_list = alertlabels_dict.get('matchers', [])
        logger.info("Alertmanager silence create batch: maid=%s total=%d", maid, len(matchers_list))

        now = datetime.now().astimezone()
        startsAttime = now.isoformat()

        end_now = now + timedelta(hours=matime_hours)
        endsAttime = end_now.isoformat()

        silence_id_list = []  # 创建一个列表来存储所有的silenceID

        for idx, matchers_item in enumerate(matchers_list, 1):
            matchers = matchers_item.get('matchers', [])
            if not matchers:
                logger.warning("告警 matchers 为空，跳过: maid=%s index=%d", maid, idx)
                continue

            # 根据 Alertmanager OpenAPI 规范构建请求
            final_output = {
                "matchers": matchers,
                "startsAt": startsAttime,
                "endsAt": endsAttime,
                "comment": f"Feishu Bot - MAID: {maid}",
                "createdBy": "feishu_bot"
            }

            url = f"{alertma_config}/api/v2/silences"
            alert_data = json.dumps(final_output)
            headers = {"Content-Type": "application/json"}

            try:
                response = requests.post(url, data=alert_data, headers=headers, timeout=SILENCE_API_TIMEOUT)

                if response.status_code == 200:
                    silence_data = response.json()
                    if isinstance(silence_data, dict) and 'silenceID' in silence_data:
                        silence_id = silence_data['silenceID']
                        silence_id_list.append(silence_id)
                        logger.info("Alertmanager silence created: maid=%s silence_id=%s index=%d total=%d", maid, silence_id, idx, len(matchers_list))
                    else:
                        logger.error(
                            "响应缺少 silenceID: maid=%s index=%d total=%d",
                            maid, idx, len(matchers_list)
                        )
                else:
                    logger.error(
                        "创建静默失败: maid=%s index=%d total=%d status=%s",
                        maid, idx, len(matchers_list), response.status_code
                    )
            except requests.exceptions.RequestException as e:
                logger.error(
                    "创建静默网络请求失败: maid=%s index=%d total=%d error=%s",
                    maid, idx, len(matchers_list), e
                )
            except json.JSONDecodeError:
                logger.error(
                    "创建静默响应解析失败: maid=%s index=%d total=%d",
                    maid, idx, len(matchers_list)
                )

        # 检查是否成功获取到 silenceID
        if not silence_id_list:
            logger.error("未能创建任何静默规则: maid=%s", maid)
            return {
                "success": False,
                "message": "未能创建静默规则：未获取到 silenceID"
            }

        # 将所有silenceID转换为JSON字符串并保存到数据库
        silence_ids_json = json.dumps(silence_id_list)
        with db_cursor() as (conn, cursor):
            cursor.execute("UPDATE alert_data SET silenceid = %s WHERE id = %s", (silence_ids_json, maid))
            affected_rows = cursor.rowcount
            conn.commit()

            if affected_rows == 0:
                logger.warning("静默数据库更新未影响任何行: maid=%s", maid)
            else:
                logger.info(
                    "静默创建完成: maid=%s saved=%d",
                    maid, len(silence_id_list)
                )

        return {
            "success": True,
            "silence_ids": silence_id_list,
            "message": f"成功创建 {len(silence_id_list)} 个静默规则"
        }

    except Error as e:
        logger.error("创建静默数据库操作出错: maid=%s error=%s", maid, e, exc_info=True)
        return {
            "success": False,
            "message": f"数据库操作出错：{str(e)}"
        }
    except Exception as e:
        logger.error("创建静默失败: maid=%s error=%s", maid, e, exc_info=True)
        return {
            "success": False,
            "message": f"创建静默失败：{str(e)}"
        }
