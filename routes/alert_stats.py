#!/usr/bin/env python3
"""
告警统计路由 Blueprint

GET /api/alert_stats/top      Top 告警统计
GET /api/alert_stats/details  告警详情列表

优化点（P2 SQL 聚合）：
- Top 统计改用 MySQL JSON_TABLE + GROUP BY 聚合，避免拉全表到 Python 层计数
- 详情查询保留 JSON_SEARCH 粗筛 + Python 精确过滤
"""

import json
import logging
from datetime import datetime, timedelta

from flask import Blueprint, jsonify, request as flask_request

from config.constants import (
    ALERT_STATS_DEFAULT_DAYS,
    ALERT_STATS_DEFAULT_TOP_LIMIT,
    ALERT_STATS_DEFAULT_DETAILS_LIMIT,
)
from db.pool import db_cursor

logger = logging.getLogger(__name__)

alert_stats_bp = Blueprint("alert_stats", __name__)


def _parse_date_range():
    """解析 start/end 查询参数，返回 (start_dt, end_dt, error_response)。

    error_response 为 None 表示解析成功。
    """
    end_str = flask_request.args.get('end')
    start_str = flask_request.args.get('start')

    if end_str:
        try:
            end_dt = datetime.strptime(end_str, '%Y-%m-%d')
        except ValueError:
            return None, None, (jsonify({"code": 400, "msg": "end 参数格式应为 YYYY-MM-DD"}), 400)
    else:
        end_dt = datetime.now()

    if start_str:
        try:
            start_dt = datetime.strptime(start_str, '%Y-%m-%d')
        except ValueError:
            return None, None, (jsonify({"code": 400, "msg": "start 参数格式应为 YYYY-MM-DD"}), 400)
    else:
        start_dt = end_dt - timedelta(days=ALERT_STATS_DEFAULT_DAYS)

    return start_dt, end_dt, None


def _extract_alertnames_from_labels(alertlabels_json):
    """从 alert_data.alertlabels JSON 中提取所有 alertname 标签值。

    alertlabels 结构: {"matchers": [{"matchers": [{"name":"alertname","value":"Xx"}, ...]}, ...]}
    每条记录可能包含多个告警实例，返回去重后的 alertname 列表。
    """
    names = set()
    if not alertlabels_json:
        return names
    try:
        data = json.loads(alertlabels_json) if isinstance(alertlabels_json, str) else alertlabels_json
    except (json.JSONDecodeError, TypeError):
        return names
    for group in data.get('matchers', []):
        for m in group.get('matchers', []):
            if m.get('name') == 'alertname' and m.get('value'):
                names.add(m['value'])
    return names


@alert_stats_bp.route("/api/alert_stats/top", methods=["GET"])
def alert_stats_top():
    """Top 告警统计

    GET /api/alert_stats/top?start=2026-01-01&end=2026-07-07&limit=20
    返回指定时间范围内出现次数最多的 alertname 列表。

    优化：优先尝试 MySQL 8 JSON_TABLE 聚合（SQL 层完成 GROUP BY），
    若数据库版本不支持则降级到 Python 层聚合。
    """
    start_dt, end_dt, err = _parse_date_range()
    if err:
        return err

    limit = flask_request.args.get('limit', ALERT_STATS_DEFAULT_TOP_LIMIT, type=int)

    # alerttime 是 VARCHAR 存储的 ISO 格式字符串，可以直接做字符串比较
    start_iso = start_dt.strftime('%Y-%m-%d')
    end_iso = (end_dt + timedelta(days=1)).strftime('%Y-%m-%d')

    try:
        with db_cursor(dictionary=True) as (conn, cursor):
            # 优先尝试 MySQL 8 JSON_TABLE 聚合（SQL 层完成，避免拉全表到 Python）
            data = _try_sql_aggregate_top(cursor, start_iso, end_iso, limit)
            if data is not None:
                total_records = _count_records(cursor, start_iso, end_iso)
            else:
                # 降级：JSON_TABLE 不可用，拉取记录在 Python 层聚合
                data, total_records = _python_aggregate_top(cursor, start_iso, end_iso, limit)

        return jsonify({
            "code": 0,
            "msg": "success",
            "data": data,
            "total_records": total_records,
            "start": start_dt.strftime('%Y-%m-%d'),
            "end": end_dt.strftime('%Y-%m-%d')
        })
    except Exception as e:
        logger.error("获取Top告警统计失败: %s", e, exc_info=True)
        return jsonify({"code": 500, "msg": str(e)}), 500


def _try_sql_aggregate_top(cursor, start_iso, end_iso, limit):
    """尝试用 MySQL 8 JSON_TABLE 在 SQL 层聚合 alertname 计数。

    Returns:
        list: 聚合结果（[{alertname, count}]），或 None（JSON_TABLE 不可用时降级）
    """
    sql = """
        SELECT jt.`value` AS alertname, COUNT(DISTINCT alert_data.id) AS cnt
        FROM alert_data,
        JSON_TABLE(
            alertlabels,
            '$.matchers[*].matchers[*]' COLUMNS (
                `name` VARCHAR(128) PATH '$.name',
                `value` VARCHAR(255) PATH '$.value'
            )
        ) AS jt
        WHERE alerttime >= %s AND alerttime < %s
          AND jt.`name` = 'alertname' AND jt.`value` IS NOT NULL AND jt.`value` != ''
        GROUP BY jt.`value`
        ORDER BY cnt DESC
        LIMIT %s
    """
    try:
        cursor.execute(sql, (start_iso, end_iso, limit))
        rows = cursor.fetchall()
        return [{"alertname": r["alertname"], "count": r["cnt"]} for r in rows]
    except Exception as e:
        # JSON_TABLE 仅 MySQL 8.0.4+ 支持，不支持时降级
        logger.info("JSON_TABLE 聚合不可用，降级到 Python 层聚合: %s", e)
        return None


def _count_records(cursor, start_iso, end_iso):
    """统计时间范围内的记录总数"""
    cursor.execute(
        "SELECT COUNT(*) AS cnt FROM alert_data WHERE alerttime >= %s AND alerttime < %s",
        (start_iso, end_iso)
    )
    row = cursor.fetchone()
    return row["cnt"] if row else 0


def _python_aggregate_top(cursor, start_iso, end_iso, limit):
    """降级方案：拉取记录在 Python 层聚合 alertname 计数"""
    cursor.execute(
        "SELECT id, alertlabels, project, alerttime "
        "FROM alert_data "
        "WHERE alerttime >= %s AND alerttime < %s "
        "ORDER BY alerttime DESC",
        (start_iso, end_iso)
    )
    rows = cursor.fetchall()

    stats = {}  # alertname -> count
    for row in rows:
        names = _extract_alertnames_from_labels(row.get('alertlabels'))
        for name in names:
            stats[name] = stats.get(name, 0) + 1

    result = sorted(stats.items(), key=lambda x: x[1], reverse=True)[:limit]
    data = [{"alertname": name, "count": cnt} for name, cnt in result]
    return data, len(rows)


@alert_stats_bp.route("/api/alert_stats/details", methods=["GET"])
def alert_stats_details():
    """告警详情列表

    GET /api/alert_stats/details?alertname=XXX&start=2026-01-01&end=2026-07-07&limit=100
    返回指定 alertname 在时间范围内的每条告警详情。
    """
    alertname = flask_request.args.get('alertname')
    if not alertname:
        return jsonify({"code": 400, "msg": "alertname 参数不能为空"}), 400

    start_dt, end_dt, err = _parse_date_range()
    if err:
        return err

    limit = flask_request.args.get('limit', ALERT_STATS_DEFAULT_DETAILS_LIMIT, type=int)

    start_iso = start_dt.strftime('%Y-%m-%d')
    end_iso = (end_dt + timedelta(days=1)).strftime('%Y-%m-%d')

    try:
        with db_cursor(dictionary=True) as (conn, cursor):
            # 先用 MySQL JSON_SEARCH 做粗筛：alertlabels 中包含 alertname 标签值匹配的记录
            # 再在 Python 层精确过滤
            cursor.execute(
                "SELECT id, alertlabels, project, alerttime, silenceid, group_id "
                "FROM alert_data "
                "WHERE alerttime >= %s AND alerttime < %s "
                "AND JSON_SEARCH(alertlabels, 'one', %s, NULL, '$**.value') IS NOT NULL "
                "ORDER BY alerttime DESC",
                (start_iso, end_iso, alertname)
            )
            rows = cursor.fetchall()

        # 过滤出包含该 alertname 的记录，并提取完整标签
        details = []
        for row in rows:
            labels_map = _extract_all_labels(row.get('alertlabels'), alertname)
            if labels_map is None:
                continue

            # 解析 silenceid
            silence_ids = []
            if row.get('silenceid'):
                try:
                    silence_ids = json.loads(row['silenceid']) if isinstance(row['silenceid'], str) else row['silenceid']
                except (json.JSONDecodeError, TypeError):
                    silence_ids = []

            details.append({
                "id": row['id'],
                "project": row.get('project', ''),
                "alerttime": row.get('alerttime', ''),
                "labels": labels_map,
                "silenced": len(silence_ids) > 0,
                "silence_ids": silence_ids,
                "group_id": row.get('group_id', ''),
            })
            if len(details) >= limit:
                break

        return jsonify({
            "code": 0,
            "msg": "success",
            "data": details,
            "alertname": alertname,
            "count": len(details),
            "start": start_dt.strftime('%Y-%m-%d'),
            "end": end_dt.strftime('%Y-%m-%d')
        })
    except Exception as e:
        logger.error("获取告警详情失败: %s", e, exc_info=True)
        return jsonify({"code": 500, "msg": str(e)}), 500


def _extract_all_labels(alertlabels_json, target_alertname=None):
    """从 alertlabels JSON 中提取标签字典。

    如果 target_alertname 不为 None，则只在该条记录的任一告警实例中
    包含匹配 alertname 时返回合并后的标签，否则返回 None。

    返回: {label_name: label_value} 或 None
    """
    if not alertlabels_json:
        return None
    try:
        data = json.loads(alertlabels_json) if isinstance(alertlabels_json, str) else alertlabels_json
    except (json.JSONDecodeError, TypeError):
        return None

    merged_labels = {}
    found = False
    for group in data.get('matchers', []):
        instance_labels = {}
        instance_has_target = False
        for m in group.get('matchers', []):
            name = m.get('name')
            value = m.get('value')
            if name and value is not None:
                instance_labels[name] = value
                if name == 'alertname' and value == target_alertname:
                    instance_has_target = True
        if instance_labels:
            # 如果没有指定 target，或者该实例包含 target alertname，则合并标签
            if target_alertname is None or instance_has_target:
                merged_labels.update(instance_labels)
                if instance_has_target:
                    found = True

    if target_alertname is not None and not found:
        return None
    return merged_labels if merged_labels else None
