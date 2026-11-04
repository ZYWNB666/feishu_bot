"""在原告警卡片上展示恢复状态，不创建话题或重新 @ 用户。"""
import copy
import html
import json
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from db.pool import db_cursor

_AT = re.compile(r'<at\s+id=["\']([^"\']+)["\'][^>]*>(.*?)</at>', re.DOTALL)
_SHANGHAI = ZoneInfo('Asia/Shanghai')


def mention_names(card):
    """只读取本地通讯录；缺失的姓名在卡片中保留为普通用户 ID。"""
    ids = set()

    def collect(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key == 'content' and isinstance(value, str):
                    ids.update(m.group(1) for m in _AT.finditer(value))
                else:
                    collect(value)
        elif isinstance(node, list):
            for item in node:
                collect(item)

    collect(card)
    ids = sorted(ids)
    if not ids:
        return {}
    with db_cursor() as (_, cursor):
        placeholders = ','.join(['%s'] * len(ids))
        cursor.execute(f'SELECT open_id, name FROM feishu_users WHERE open_id IN ({placeholders})', tuple(ids))
        return dict(cursor.fetchall())


def _timestamp(value):
    if not isinstance(value, str) or not value or value.startswith('0001-'):
        return None
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return result.replace(tzinfo=_SHANGHAI) if result.tzinfo is None else result
    except (TypeError, ValueError):
        return None


def build_resolved_card(original, raw_alerts, names):
    """保留触发快照；聚合卡片展示首个触发到最后恢复的持续时长。"""
    card = copy.deepcopy(original)
    if not isinstance(card, dict) or not isinstance(card.get('elements'), list) or not card.get('header'):
        raise ValueError('原告警卡片结构无效')
    title = card['header']['title']['content']
    # PATCH 成功但后续持久化/重试时避免重复追加恢复区域。
    if title.startswith('✅ 已恢复｜'):
        return json.dumps(card, ensure_ascii=False)

    def plain_mentions(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key == 'content' and isinstance(value, str):
                    value = _AT.sub(lambda m: html.escape(names.get(m.group(1)) or m.group(2) or m.group(1)), value)
                    node[key] = (value.replace('📢 通知人员：', '原通知人员：')
                                 .replace('📢 通知：', '原通知人员：')
                                 .replace('📝 描述：', '📝 触发时描述：')
                                 .replace('当前成功率', '触发时成功率'))
                else:
                    plain_mentions(value)
        elif isinstance(node, list):
            for item in node:
                plain_mentions(item)

    plain_mentions(card['elements'])
    card['header']['template'] = 'green'
    card['header']['title']['content'] = '✅ 已恢复｜' + title.removeprefix('🔔 ').strip()
    card.setdefault('config', {})['update_multi'] = True

    # 只保留链接按钮，移除静默、日期选择器及认领等过期操作。
    elements = []
    for element in card['elements']:
        if element.get('tag') == 'action':
            element['actions'] = [a for a in element.get('actions', []) if a.get('url') or a.get('multi_url')]
            if not element['actions']:
                continue
        elements.append(element)

    starts = [_timestamp(a.get('startsAt', '')) for a in raw_alerts]
    ends = [_timestamp(a.get('endsAt', '')) for a in raw_alerts]
    # 不用发送时间冒充恢复时间；任一实例缺失时间则不计算聚合时长。
    end = max(ends) if ends and all(ends) else None
    start = min(starts) if starts and all(starts) else None
    end_text = end.astimezone(_SHANGHAI).strftime('%Y-%m-%d %H:%M:%S') if end else '未提供'
    lines = [f'✅ **恢复时间：** {end_text}']
    if start and end and end >= start:
        seconds = int((end.astimezone(timezone.utc) - start.astimezone(timezone.utc)).total_seconds())
        hours, seconds = divmod(seconds, 3600)
        minutes, seconds = divmod(seconds, 60)
        duration = (f'{hours} 小时 ' if hours else '') + f'{minutes} 分 {seconds} 秒'
        lines.append(f'⌛ **持续时长：** {duration}')
    if len(raw_alerts) > 1:
        lines.append(f'全部 {len(raw_alerts)} 个实例已恢复（持续时长从最早触发至最后恢复）')
    recovery = {'tag': 'div', 'text': {'tag': 'lark_md', 'content': '\n'.join(lines)}}
    # biz 紧接最后一条触发时间；ops 放在告警详情之后、页脚之前。
    index = next((i + 1 for i in reversed(range(len(elements)))
                  if '触发时间' in elements[i].get('text', {}).get('content', '')), None)
    if index is None:
        index = next((i for i, e in enumerate(elements) if e.get('tag') in ('action', 'note')), len(elements))
    elements.insert(index, recovery)
    card['elements'] = elements
    return json.dumps(card, ensure_ascii=False)
