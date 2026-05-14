#!/usr/bin/env python3
"""
业务告警卡片模板（biz 模板）

设计目标：
- firing: 橙色/红色标题，双列 label 展示，关键字段高亮，Grafana 跳转按钮，静默按钮
- resolved: 绿色标题，时长展示，无静默按钮
"""

import json
from datetime import datetime


def _parse_ts(ts: str) -> datetime | None:
    """解析 alertmanager ISO 时间字符串"""
    if not ts:
        return None
    for fmt in ('%Y-%m-%dT%H:%M:%S.%fZ', '%Y-%m-%dT%H:%M:%SZ', '%Y-%m-%dT%H:%M:%S%z',
                '%Y-%m-%dT%H:%M:%S.%f', '%Y-%m-%d %H:%M:%S.%f', '%Y-%m-%d %H:%M:%S'):
        try:
            # 处理带毫秒且末尾有 Z 的格式：直接去掉末尾 Z 再加回来，避免双 Z 问题
            if fmt.endswith('.%fZ'):
                clean = ts.rstrip('Z')
                dt = datetime.strptime(clean[:26], fmt[:-1])
            else:
                dt = datetime.strptime(ts, fmt)
            return dt
        except ValueError:
            continue
    return None


def _duration_str(start_ts: str, end_ts: str) -> str:
    """计算触发时长字符串"""
    st = _parse_ts(start_ts)
    et = _parse_ts(end_ts)
    if not st or not et:
        return ''
    try:
        delta = abs((et - st).total_seconds())
        h, rem = divmod(int(delta), 3600)
        m, s = divmod(rem, 60)
        if h:
            return f"{h}h{m}m"
        if m:
            return f"{m}m{s}s"
        return f"{s}s"
    except Exception:
        return ''


def _silence_buttons(maid: str) -> dict:
    """构建静默操作按钮组（默认2小时）"""
    return {
        "tag": "action",
        "actions": [
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "🔕 静默2小时"},
                "type": "primary",
                "value": {"action": "silence", "maid": maid, "duration": 7200},
            },
        ],
    }


def _grafana_buttons(grafana_urls: dict, maid: str = None) -> dict | None:
    """构建 Grafana 跳转按钮 + 静默按钮（同一行）"""
    actions = []
    if grafana_urls.get('panelURL'):
        actions.append({
            "tag": "button",
            "text": {"tag": "plain_text", "content": "📈 Panel"},
            "type": "default",
            "url": grafana_urls['panelURL'],
        })
    if grafana_urls.get('generatorURL'):
        actions.append({
            "tag": "button",
            "text": {"tag": "plain_text", "content": "🔗 告警源"},
            "type": "default",
            "url": grafana_urls['generatorURL'],
        })
    if maid:
        actions.append({
            "tag": "button",
            "text": {"tag": "plain_text", "content": "🔕 静默2小时"},
            "type": "primary",
            "value": {"action": "silence", "maid": maid, "duration": 7200},
        })
    return {"tag": "action", "actions": actions} if actions else None


def _label_fields(labels: dict) -> list:
    """将 labels dict 转换为双列 fields 元素"""
    if not labels:
        return []
    fields = []
    for k, v in labels.items():
        fields.append({
            "tag": "plain_text",
            "content": f"{k}: {v}",
        })
    # 双列布局
    return [{"tag": "div", "fields": fields}]


def build_biz_firing_card(
    alertname: str,
    severity: str,
    raw_alerts: list,
    grafana_urls: dict,
    maid: str,
    common_labels: dict,
    mentioned_user_list: list,
) -> str:
    """
    构建业务告警（firing）飞书卡片 JSON 字符串

    :param alertname: 告警名称
    :param severity: 告警级别 critical/warning/info
    :param raw_alerts: extract_alert_raw() 返回的原始 alert 列表
    :param grafana_urls: extract_grafana_urls() 返回的 URL 字典
    :param maid: 告警记录 ID
    :param common_labels: 公共标签字典（alertname 除外）
    :param mentioned_user_list: 需要 @ 的 open_id 列表
    :return: str, 序列化好的卡片 JSON
    """
    color_map = {
        "critical": "red", "warning": "orange", "info": "blue",
        # P 级别
        "p0": "red", "p1": "orange", "p2": "yellow", "p3": "blue",
        # 电话告警，与 P0 同级
        "phone": "red",
    }
    template_color = color_map.get(severity.lower(), "orange")

    elements = []

    # @ 用户区域
    if mentioned_user_list:
        mention_content = " ".join(f'<at id="{uid}"></at>' for uid in mentioned_user_list)
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": f"**📢 通知：** {mention_content}"},
        })
        elements.append({"tag": "hr"})

    # 公共标签（过滤 alertname / grafana_folder / severity / alertid）
    _label_blacklist = {'alertname', 'grafana_folder', 'severity', 'alertid'}
    common_display = {k: v for k, v in (common_labels or {}).items() if k not in _label_blacklist}
    if common_display:
        field_items = [{"is_short": True, "text": {"tag": "lark_md", "content": f"**{k}**\n{v}"}}
                       for k, v in common_display.items()]
        elements.append({"tag": "div", "fields": field_items})
        elements.append({"tag": "hr"})

    # 每条 alert 详情
    for alert in raw_alerts:
        status_icon = "🔥" if alert.get('status') == 'firing' else "✅"
        # 特有标签
        spec_labels = alert.get('labels', {})
        if spec_labels:
            field_items = [{"is_short": True, "text": {"tag": "lark_md", "content": f"**{k}**\n{v}"}}
                           for k, v in spec_labels.items()]
            elements.append({
                "tag": "div",
                "text": {"tag": "lark_md", "content": f"{status_icon} **告警实例**"},
            })
            elements.append({"tag": "div", "fields": field_items})
        # annotations
        annotations = alert.get('annotations', {})
        desc = annotations.get('description') or annotations.get('summary', '')
        if desc:
            elements.append({
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"**📝 描述：**\n<font color='orange'>{desc}</font>",
                },
            })
        # 触发时间
        starts_at = (alert.get('startsAt') or '').replace('T', ' ').replace('Z', '')[:19]
        if starts_at:
            elements.append({
                "tag": "div",
                "text": {"tag": "lark_md", "content": f"⏱ **触发时间：** {starts_at}"},
            })
        elements.append({"tag": "hr"})

    # Grafana URL 按钮 + 静默按钮（同一行）
    grafana_btn = _grafana_buttons(grafana_urls, maid)
    if grafana_btn:
        elements.append(grafana_btn)
    elif maid:
        # 没有 Grafana URL 时单独显示静默按钮
        elements.append(_silence_buttons(maid))

    # MAID 展示
    if maid:
        elements.append({
            "tag": "note",
            "elements": [{"tag": "plain_text", "content": f"⚠️ MAID: {maid}"}],
        })

    # 标题中显示的级别标签（P 级别显示为更直观的名称）
    severity_label_map = {
        "p0": "P0", "p1": "P1", "p2": "P2", "p3": "P3",
        "critical": "critical", "warning": "warning", "info": "info",
        "phone": "P0",
    }
    severity_label = severity_label_map.get(severity.lower(), severity) if severity else ""

    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"🔔 {alertname}" + (f"  [{severity_label}]" if severity_label else "")},
            "template": template_color,
        },
        "elements": elements,
    }
    return json.dumps(card, ensure_ascii=False)


def build_biz_resolved_card(
    alertname: str,
    raw_alerts: list,
    grafana_urls: dict,
    common_labels: dict,
    mentioned_user_list: list = None,
) -> str:
    """
    构建告警恢复（resolved）飞书卡片 JSON 字符串

    :param alertname: 告警名称
    :param raw_alerts: extract_alert_raw() 返回的原始 alert 列表
    :param grafana_urls: extract_grafana_urls() 返回的 URL 字典
    :param common_labels: 公共标签字典
    :param mentioned_user_list: 需要 @ 的 open_id 列表
    :return: str, 序列化好的卡片 JSON
    """
    elements = []

    # @ 用户区域
    if mentioned_user_list:
        mention_content = " ".join(f'<at id="{uid}"></at>' for uid in mentioned_user_list)
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": f"**📢 通知：** {mention_content}"},
        })
        elements.append({"tag": "hr"})

    # 公共标签（过滤 alertname / alertid）
    common_display = {k: v for k, v in (common_labels or {}).items() if k not in ('alertname', 'alertid')}
    if common_display:
        field_items = [{"is_short": True, "text": {"tag": "lark_md", "content": f"**{k}**\n{v}"}}
                       for k, v in common_display.items()]
        elements.append({"tag": "div", "fields": field_items})
        elements.append({"tag": "hr"})

    for alert in raw_alerts:
        spec_labels = alert.get('labels', {})
        if spec_labels:
            field_items = [{"is_short": True, "text": {"tag": "lark_md", "content": f"**{k}**\n{v}"}}
                           for k, v in spec_labels.items()]
            elements.append({"tag": "div", "fields": field_items})

        annotations = alert.get('annotations', {})
        desc = annotations.get('description') or annotations.get('summary', '')
        if desc:
            elements.append({
                "tag": "div",
                "text": {"tag": "lark_md", "content": f"**📝 描述：**\n{desc}"},
            })

        # 持续时长
        dur = _duration_str(alert.get('startsAt', ''), alert.get('endsAt', ''))
        ends_at = (alert.get('endsAt') or '').replace('T', ' ').replace('Z', '')[:19]
        time_line = f"✅ **恢复时间：** {ends_at}"
        if dur:
            time_line += f"  **持续时长：** {dur}"
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": time_line},
        })
        elements.append({"tag": "hr"})

    # Grafana URL
    grafana_btn = _grafana_buttons(grafana_urls)
    if grafana_btn:
        elements.append(grafana_btn)

    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"✅ {alertname} 已恢复"},
            "template": "green",
        },
        "elements": elements,
    }
    return json.dumps(card, ensure_ascii=False)
