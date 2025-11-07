import json
from datetime import datetime, timedelta
import pytz
import mysql.connector
import requests
import logging
from config import config

logger = logging.getLogger(__name__)


def madelete(maid):
    """
    删除告警静默
    :param maid: 告警ID
    :return: 响应结果
    """
    connection = None
    try:
        # 使用统一配置获取数据库连接
        db_config = config.get_alert_db_config()
        connection = mysql.connector.connect(**db_config)

        if connection.is_connected():
            cursor = connection.cursor()
            
            # 查询 silenceid 和 project
            select_query = "SELECT silenceid, project FROM alert_data WHERE id = %s"
            cursor.execute(select_query, (maid,))
            result = cursor.fetchone()
            
            if not result:
                logger.error(f"没有找到id为 {maid} 的记录")
                return {
                    "success": False,
                    "message": f"没有找到id为 {maid} 的记录"
                }
            
            silenceid_json = result[0]
            project = result[1]
            
            if not silenceid_json:
                logger.warning(f"告警 {maid} 没有关联的静默规则")
                return {
                    "success": False,
                    "message": "该告警没有关联的静默规则"
                }
            
            # 解析 silenceid 列表
            silence_ids = json.loads(silenceid_json)
            logger.info(f"开始删除 {len(silence_ids)} 个静默规则")
            
            # 获取 alertmanager_url
            config_db_config = config.get_config_db_config()
            config_conn = mysql.connector.connect(**config_db_config)
            config_cursor = config_conn.cursor(dictionary=True)
            
            select_query = "SELECT alertmanager_url FROM alert_config WHERE project = %s LIMIT 1"
            config_cursor.execute(select_query, (project,))
            config_result = config_cursor.fetchone()
            
            if not config_result:
                logger.error(f"未找到项目 {project} 的配置")
                config_cursor.close()
                config_conn.close()
                return {
                    "success": False,
                    "message": f"未找到项目 {project} 的配置"
                }
            
            alertma_config = config_result['alertmanager_url']
            config_cursor.close()
            config_conn.close()
            
            # 删除每个静默规则
            deleted_count = 0
            for silence_id in silence_ids:
                try:
                    url = f"{alertma_config}/api/v2/silence/{silence_id}"
                    response = requests.delete(url, timeout=30)
                    
                    if response.status_code in [200, 204]:
                        deleted_count += 1
                    else:
                        logger.error(f"删除静默规则失败，状态码: {response.status_code}")
                except Exception as e:
                    logger.error(f"删除静默规则时出错: {str(e)}")
            
            # 清空数据库中的 silenceid
            update_query = "UPDATE alert_data SET silenceid = NULL WHERE id = %s"
            cursor.execute(update_query, (maid,))
            connection.commit()
            
            logger.info(f"删除静默完成: {deleted_count}/{len(silence_ids)} 个成功")
            
            return {
                "success": True,
                "deleted_count": deleted_count,
                "total_count": len(silence_ids),
                "message": f"成功删除 {deleted_count} 个静默规则"
            }
            
    except mysql.connector.Error as e:
        logger.error(f"数据库操作出错：{e}", exc_info=True)
        return {
            "success": False,
            "message": f"数据库操作出错：{str(e)}"
        }
    except Exception as e:
        logger.error(f"删除静默失败：{e}", exc_info=True)
        return {
            "success": False,
            "message": f"删除静默失败：{str(e)}"
        }
    finally:
        if connection and connection.is_connected():
            cursor.close()
            connection.close()
            logger.debug("MySQL连接已关闭")


def macreate(maid, matime):
    """
    创建告警静默
    :param maid: 告警ID
    :param matime: 静默时长（小时）
    :return: 响应结果
    """
    connection = None
    try:
        matime_hours = int(matime)
        
        # 使用统一配置获取数据库连接
        db_config = config.get_alert_db_config()
        connection = mysql.connector.connect(**db_config)

        if connection.is_connected():
            cursor = connection.cursor()
            select_query = """
                SELECT alertlabels, project FROM alert_data WHERE id = %s
            """

            cursor.execute(select_query, (maid,))

            result = cursor.fetchone()

            if result:
                alertlabels_data = result[0]
                project = result[1]
                alertlabels_dict = json.loads(alertlabels_data)
                matchers_list = alertlabels_dict.get('matchers', [])
                logger.info(f"开始创建静默规则，共 {len(matchers_list)} 个告警")

                utc_now = datetime.now(pytz.utc)
                iso_format = utc_now.isoformat()
                startsAttime = iso_format[0:23] + "Z"

                end_utc_now = utc_now + timedelta(hours=matime_hours)
                end_iso_format = end_utc_now.isoformat()
                endsAttime = end_iso_format[0:23] + "Z"

                silence_id_list = []  # 创建一个列表来存储所有的silenceID

                # 从配置数据库获取 alertmanager_url
                config_db_config = config.get_config_db_config()
                config_conn = mysql.connector.connect(**config_db_config)
                config_cursor = config_conn.cursor(dictionary=True)
                
                select_query = "SELECT alertmanager_url FROM alert_config WHERE project = %s LIMIT 1"
                config_cursor.execute(select_query, (project,))
                config_result = config_cursor.fetchone()
                
                if not config_result:
                    logger.error(f"未找到项目 {project} 的 alertmanager_url 配置")
                    config_cursor.close()
                    config_conn.close()
                    return f"未找到项目 {project} 的配置"
                
                alertma_config = config_result['alertmanager_url']
                config_cursor.close()
                config_conn.close()

                for idx, matchers_item in enumerate(matchers_list, 1):
                    matchers = matchers_item.get('matchers', [])
                    if not matchers:
                        logger.warning(f"告警 {idx} 的 matchers 为空，跳过")
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
                        response = requests.post(url, data=alert_data, headers=headers, timeout=30)
                        
                        if response.status_code == 200:
                            silence_data = response.json()
                            if isinstance(silence_data, dict) and 'silenceID' in silence_data:
                                silence_id = silence_data['silenceID']
                                silence_id_list.append(silence_id)
                                logger.info(f"创建静默规则成功 [{idx}/{len(matchers_list)}]")
                            else:
                                logger.error(f"响应缺少 silenceID [{idx}/{len(matchers_list)}]")
                        else:
                            logger.error(f"创建静默失败 [{idx}/{len(matchers_list)}]，状态码: {response.status_code}")
                    except requests.exceptions.RequestException as e:
                        logger.error(f"网络请求失败 [{idx}/{len(matchers_list)}]: {str(e)}")
                    except json.JSONDecodeError:
                        logger.error(f"响应解析失败 [{idx}/{len(matchers_list)}]")

                # 检查是否成功获取到 silenceID
                if not silence_id_list:
                    logger.error("未能创建任何静默规则")
                    return {
                        "success": False,
                        "message": "未能创建静默规则：未获取到 silenceID"
                    }
                
                # 将所有silenceID转换为JSON字符串并保存到数据库
                silence_ids_json = json.dumps(silence_id_list)
                update_query = "UPDATE alert_data SET silenceid = %s WHERE id = %s"
                cursor.execute(update_query, (silence_ids_json, maid))
                affected_rows = cursor.rowcount
                connection.commit()
                
                if affected_rows == 0:
                    logger.warning("数据库更新未影响任何行")
                else:
                    logger.info(f"静默创建完成: {len(silence_id_list)} 个规则已保存")
                
                return {
                    "success": True,
                    "silence_ids": silence_id_list,
                    "message": f"成功创建 {len(silence_id_list)} 个静默规则"
                }

            else:
                logger.error(f"没有找到id为 {maid} 的记录。")
                return {
                    "success": False,
                    "message": f"没有找到id为 {maid} 的记录"
                }

    except mysql.connector.Error as e:
        logger.error(f"数据库操作出错：{e}", exc_info=True)
        return {
            "success": False,
            "message": f"数据库操作出错：{str(e)}"
        }
    except Exception as e:
        logger.error(f"创建静默失败：{e}", exc_info=True)
        return {
            "success": False,
            "message": f"创建静默失败：{str(e)}"
        }
    finally:
        # 关闭游标和连接
        if connection and connection.is_connected():
            cursor.close()
            connection.close()
            logger.debug("MySQL连接已关闭")