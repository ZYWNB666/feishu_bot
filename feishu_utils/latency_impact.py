"""延迟 histogram 的短长窗口证据；单位、维度和桶计数均须可校验。"""

import math
import re
from dataclasses import dataclass
from config.constants import (
    TREND_IMPACT_SHORT_SECONDS, TREND_IMPACT_LONG_SECONDS, TREND_IMPACT_MAX_ERROR_RATIO,
)

SHORT_SECONDS = TREND_IMPACT_SHORT_SECONDS
LONG_SECONDS = TREND_IMPACT_LONG_SECONDS
MAX_ERROR_RATIO = TREND_IMPACT_MAX_ERROR_RATIO
# 避开 VM 最新采集尚未全部可见的区间；窗口宽度和观察期限保持原值。
SNAPSHOT_LAG_SECONDS = 30
EPSILON = 1e-6
# P0 要求明显扩大影响：P99=3%、P95=15%、P50=60%。
# 高分位使用3倍规则边界；中位数最多再允许20%的正常观测转慢，避免边界超过100%。
P0_RATIO_MULTIPLIER = 3
P0_EXCESS_FRACTION = 0.20


@dataclass(frozen=True)
class HistogramSpec:
    quantile: float
    metric: str
    selector: str
    groups: tuple[str, ...]
    divisor: float

    @property
    def source(self):
        return self.metric + '{' + self.selector + '}'

    @property
    def allowed_ratio(self):
        # 分位数边界来自现有规则；不把它解释为对外承诺的 SLO。
        return round(1 - self.quantile, 12)


@dataclass(frozen=True)
class HistogramWindow:
    counts: dict[float, float]

    @property
    def total(self):
        return self.counts[math.inf]

    def slow_bounds(self, raw_threshold):
        """缺少精确桶时返回慢观测比例上下界，不插值猜测实际分布。"""
        lower = [le for le in self.counts if le <= raw_threshold]
        upper = [le for le in self.counts if le >= raw_threshold]
        if not lower or not upper:
            raise ValueError('缺少可界定延迟阈值的桶')
        return ((self.total - self.counts[min(upper)]) / self.total,
                (self.total - self.counts[max(lower)]) / self.total)


def parse_histogram(expression, labels):
    expression = re.sub(r'\s*>=\s*0\s*$', '', expression).strip()
    if expression.startswith('(') and expression.endswith(')'):
        expression = expression[1:-1].strip()
    match = re.fullmatch(
        r'histogram_quantile\(\s*(?P<q>0?\.\d+)\s*,\s*'
        r'sum\s+by\s*\((?P<groups>[\w,\s]+)\)\s*\(\s*rate\(\s*'
        r'(?P<metric>magik_model_(?:ttft|tpot)_ms_bucket)'
        r'\{(?P<selector>(?:[^"{}]|"(?:\\.|[^"\\])*")*)\}'
        r'\[\d+[smhdw]\]\s*\)\s*\)\s*\)\s*'
        r'(?:/\s*(?P<divisor>1000(?:\.0+)?|1(?:\.0+)?))?', expression)
    if not match:
        raise ValueError('不支持此延迟表达式或单位转换')
    groups = tuple(name.strip() for name in match['groups'].split(','))
    dimensions = {key: labels[key] for key in ('tenant', 'model', 'ep') if labels.get(key)}
    if (not dimensions or 'le' not in groups or len(groups) != len(set(groups))
            or any(not re.fullmatch(r'[a-zA-Z_][a-zA-Z0-9_]*', name) for name in groups)
            or not all(key in groups for key in dimensions)):
        raise ValueError('延迟聚合维度无法唯一匹配告警')
    quantile = float(match['q'])
    if not 0 < quantile < 1:
        raise ValueError('延迟分位数无效')
    return HistogramSpec(quantile, match['metric'], match['selector'], groups,
                         float(match['divisor'] or 1))


def read_windows(spec, labels, now, min_samples, query, windows=(SHORT_SECONDS, LONG_SECONDS)):
    if not 15 <= SHORT_SECONDS < LONG_SECONDS:
        raise ValueError('影响判断窗口必须满足 15秒≤短窗口<长窗口')
    grouping = ','.join(spec.groups)
    snapshot_time = now - SNAPSHOT_LAG_SECONDS
    parts = []
    for seconds in windows:
        # 对源样本检查新鲜度；遇到 counter 重置保守回退，不据此降低等级。
        expression = (f'sum by({grouping})(increase({spec.source}[{seconds}s])) '
                      f'and (min by({grouping})(timestamp({spec.source})) >= {now - 45}) '
                      f'and (sum by({grouping})(resets({spec.source}[{max(windows)}s])) == 0)')
        parts.append(f'label_replace(({expression}), "impact_window", "{seconds}", "", "")')
    results = query(' or '.join(parts), start=snapshot_time, end=snapshot_time, step=15)
    dimensions = {key: labels[key] for key in ('tenant', 'model', 'ep') if labels.get(key)}
    buckets = {seconds: {} for seconds in windows}
    for series in results:
        metric = series.get('metric') or {}
        if not all(metric.get(key) == value for key, value in dimensions.items()):
            continue
        seconds = int(metric.get('impact_window', windows[0] if len(windows) == 1 else -1))
        values = series.get('values') or []
        if seconds not in buckets or len(values) != 1:
            raise ValueError('延迟窗口或取值不唯一')
        timestamp, value = values[0]
        boundary, count = float(metric['le']), float(value)
        if (boundary in buckets[seconds] or math.isnan(boundary) or boundary < 0
                or not math.isfinite(count) or count < 0 or abs(float(timestamp) - snapshot_time) > 5):
            raise ValueError('延迟桶数据无效或匹配不唯一')
        buckets[seconds][boundary] = count
    parsed = {}
    for seconds, counts in buckets.items():
        total = counts.get(math.inf)
        if total is None or total <= 0 or total < min_samples:
            raise ValueError(f'{seconds}秒窗口观测不足或缺少总量桶')
        ordered = sorted(counts)
        for a, b in zip(ordered, ordered[1:]):
            if counts[b] + EPSILON < counts[a]:
                raise ValueError(f'{seconds}秒累计桶计数不一致: '
                                 f'le={a:g} count={counts[a]:g}, le={b:g} count={counts[b]:g}')
        parsed[seconds] = HistogramWindow(counts)
    if len(windows) > 1:
        short, long = parsed[min(windows)], parsed[max(windows)]
        if (set(short.counts) != set(long.counts)
                or any(short.counts[le] > long.counts[le] + EPSILON for le in short.counts)):
            raise ValueError('短长窗口桶不完整或计数不一致')
    return parsed


def response_health(spec, labels, now, min_samples, query):
    """读取降级用的响应总量和5xx/408计数；校验成功样本，不推断在途请求。"""
    grouping = ','.join((*[name for name in spec.groups if name != 'le'], 'code'))
    snapshot_time = now - SNAPSHOT_LAG_SECONDS
    source = 'magik_model_response_total{' + spec.selector + '}'
    expression = (f'sum by({grouping})(increase({source}[{SHORT_SECONDS}s])) '
                  f'and (min by({grouping})(timestamp({source})) >= {now - 45}) '
                  f'and (sum by({grouping})(resets({source}[{SHORT_SECONDS}s])) == 0)')
    results = query(expression, start=snapshot_time, end=snapshot_time, step=15)
    dimensions = {key: labels[key] for key in ('tenant', 'model', 'ep') if labels.get(key)}
    counts = {}
    for series in results:
        metric = series.get('metric') or {}
        if not all(metric.get(key) == value for key, value in dimensions.items()):
            continue
        code = str(metric.get('code', ''))
        values = series.get('values') or []
        if code in counts or not re.fullmatch(r'[1-5]\d{2}', code) or len(values) != 1:
            raise ValueError('响应状态码或匹配不唯一')
        timestamp, value = values[0]
        count = float(value)
        if not math.isfinite(count) or count < 0 or abs(float(timestamp) - snapshot_time) > 5:
            raise ValueError('响应计数无效')
        counts[code] = count
    total = sum(counts.values())
    success = sum(n for code, n in counts.items() if code.startswith('2'))
    errors = sum(n for code, n in counts.items() if code.startswith('5') or code == '408')
    if total < min_samples or success < min_samples or total <= 0:
        raise ValueError('响应样本不足，无法确认当前服务响应正常')
    return total, errors


def slow_ratio_lower_bound(ratio, total):
    """单侧95% Wilson 下限，作为近边界噪声缓冲，不保证观测独立。"""
    # read_windows 接受 EPSILON 内的累计计数舍入误差，避免极小负值进入平方根。
    ratio = max(0.0, min(1.0, ratio))
    z = 1.6448536269514722
    correction = z * z / total
    return max(0.0, (ratio + correction / 2
                    - z * math.sqrt(ratio * (1 - ratio) / total + correction / (4 * total)))
               / (1 + correction))


def impact_level(spec, windows, threshold, recovery_threshold, hard_ratio, min_samples=20):
    """返回 P0/P1/不确定；严重尾部不确定时由调用方保留紧急兜底。"""
    raw_threshold = threshold * spec.divisor
    raw_recovery = recovery_threshold * spec.divisor
    if not (math.isfinite(raw_threshold) and math.isfinite(raw_recovery)
            and 0 < raw_recovery <= raw_threshold and hard_ratio > 0):
        raise ValueError('延迟阈值或恢复阈值无效')
    short, long = windows[SHORT_SECONDS], windows[LONG_SECONDS]
    short_low, short_high = short.slow_bounds(raw_threshold)
    long_low, long_high = long.slow_bounds(raw_threshold)
    _, recovery_high = short.slow_bounds(raw_recovery)
    hard_low, hard_high = short.slow_bounds(raw_threshold * hard_ratio)
    allowed = spec.allowed_ratio
    urgent_ratio = min(P0_RATIO_MULTIPLIER * allowed,
                       allowed + P0_EXCESS_FRACTION * (1 - allowed))
    # 用计数比较消除 1-0.99 的浮点边界误差。
    severe = hard_low * short.total > allowed * short.total + EPSILON
    sustained_observed = (short_low * short.total > allowed * short.total + EPSILON
                          and long_low * long.total > allowed * long.total + EPSILON)
    short_confidence_low = slow_ratio_lower_bound(short_low, short.total)
    long_confidence_low = slow_ratio_lower_bound(long_low, long.total)
    # 长窗口包含短窗口，同一轮突刺不能成为两份持续证据。
    # 去掉最近短窗口，校验此前区间的累计桶；不对桶精度不足的数据插值。
    prior_counts = {le: long.counts[le] - short.counts[le] for le in short.counts}
    ordered = sorted(prior_counts)
    prior_valid = not (any(n < -EPSILON for n in prior_counts.values())
                      or any(prior_counts[b] + EPSILON < prior_counts[a]
                             for a, b in zip(ordered, ordered[1:])))
    # 差分不满足累计关系时不把它用作持续证据；原窗口严重判定仍可独立触发。
    prior = HistogramWindow({le: max(0.0, n) for le, n in prior_counts.items()})
    prior_minimum = max(min_samples, math.ceil(1 / allowed))
    prior_low = prior_confidence_low = None
    if prior_valid and prior.total >= prior_minimum:
        prior_low, _ = prior.slow_bounds(raw_threshold)
        prior_confidence_low = slow_ratio_lower_bound(prior_low, prior.total)
    sustained = (short_confidence_low > urgent_ratio + EPSILON / short.total
                 and long_confidence_low > urgent_ratio + EPSILON / long.total
                 and prior_confidence_low is not None
                 and prior_confidence_low > urgent_ratio + EPSILON / prior.total)
    recovered = recovery_high * short.total <= allowed * short.total + EPSILON
    hard_normal = hard_high * short.total <= allowed * short.total + EPSILON
    long_normal = long_high * long.total <= allowed * long.total + EPSILON
    # 短窗口可疑、长窗口上界正常且无严重尾部；不插值猜测短窗口的实际比例。
    transient = (short_high * short.total > allowed * short.total + EPSILON
                 and long_normal and hard_normal)
    below_trigger = (short_high * short.total <= allowed * short.total + EPSILON
                     and long_normal and hard_normal)
    # 原始占比略越线但样本证据弱时只降为 P1；严重尾部不确定仍保守兜底。
    borderline = sustained_observed and not sustained and hard_normal
    # 触发边界精度不足不等同严重故障。只有严重尾部上界正常时允许 P1，
    # 调用方还须校验足够成功响应和错误率；不得把这个结果当恢复。
    uncertain_nonsevere = (not (severe or sustained or recovered or transient or below_trigger or borderline)
                           and hard_normal)
    level = ('p0' if severe or sustained else
             'p1' if recovered or transient or below_trigger or borderline or uncertain_nonsevere else None)
    return level, {'quantile': spec.quantile, 'allowed_ratio': allowed,
                   'urgent_ratio': urgent_ratio, 'prior_seconds': LONG_SECONDS - SHORT_SECONDS,
                   'prior_valid': prior_valid,
                   'prior_total': prior.total, 'prior_min_samples': prior_minimum,
                   'prior_slow_low': prior_low, 'prior_slow_confidence_low': prior_confidence_low,
                   'short_total': short.total, 'long_total': long.total,
                   'short_slow_low': short_low, 'short_slow_high': short_high,
                   'long_slow_low': long_low, 'long_slow_high': long_high,
                   'short_slow_confidence_low': short_confidence_low,
                   'long_slow_confidence_low': long_confidence_low,
                   'recovery_slow_high': recovery_high, 'hard_slow_low': hard_low,
                   'hard_slow_high': hard_high, 'recovered': recovered, 'transient': transient,
                   'below_trigger': below_trigger, 'borderline': borderline,
                   'uncertain_nonsevere': uncertain_nonsevere,
                   'severe': severe, 'sustained': sustained, 'sustained_observed': sustained_observed}
