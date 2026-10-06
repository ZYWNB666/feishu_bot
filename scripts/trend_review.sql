-- MySQL 5.7+；请在 time_zone='+00:00' 的会话执行，TIMESTAMP 显示值须与 UTC 对齐。
-- 默认复盘最近 7 天；三条查询均不修改数据。

-- 1. 抑制错误率（近似）：cancel 决策后 1 小时内，同实例再次 observe/pending/send 的比例。
-- 当前日志 action='observe' 表示进入/继续 pending，兼容可能存在的历史 pending action。
-- 按决策行计数（不是独立事件数）；恢复防抖未确认的 cancel 也计入，因此只是复查信号。
-- 排除最近 1 小时尚未观察完整的样本。同秒日志用自增 id 确定先后。
SELECT COUNT(*) AS cancel_decisions,
       COALESCE(SUM(retriggered), 0) AS retriggered_within_1h,
       ROUND(SUM(retriggered) / NULLIF(COUNT(*), 0), 4) AS suppression_error_ratio
FROM (
    SELECT EXISTS (
        SELECT 1 FROM alert_trend_decision_log later
        WHERE later.rule_uid = canceled.rule_uid
          AND later.group_id = canceled.group_id
          AND later.fingerprint = canceled.fingerprint
          AND later.id > canceled.id
          AND later.created_at >= canceled.created_at
          AND later.created_at <= canceled.created_at + INTERVAL 1 HOUR
          AND later.action IN ('observe', 'pending', 'send')
    ) AS retriggered
    FROM alert_trend_decision_log canceled
    WHERE canceled.action = 'cancel'
      AND canceled.created_at >= UTC_TIMESTAMP() - INTERVAL 7 DAY
      AND canceled.created_at <= UTC_TIMESTAMP() - INTERVAL 1 HOUR
) samples;

-- 2. 骚扰信号（近似）：send 后 30 分钟内创建的匹配群/指纹记录，当前 silenceid 非空的比例。
-- 没有静默操作时间字段，不能证明静默是在 30 分钟内发生；撤销静默会低估此比例。
-- send 是发送候选决策，可能被后续去重抑制；同一消息也可能对应多条 send 决策。
-- 使用 EXISTS 避免多条 alert_data 关联把同一决策重复计数，排除未满 30 分钟的样本。
SELECT COUNT(*) AS send_decisions,
       COALESCE(SUM(silenced), 0) AS currently_silenced_matches,
       ROUND(SUM(silenced) / NULLIF(COUNT(*), 0), 4) AS nuisance_signal_ratio
FROM (
    SELECT EXISTS (
        SELECT 1 FROM alert_data a
        WHERE a.group_id = sent.group_id
          AND JSON_CONTAINS(a.fingerprints, JSON_QUOTE(sent.fingerprint))
          AND a.created_at >= sent.created_at
          AND a.created_at <= sent.created_at + INTERVAL 30 MINUTE
          AND a.silenceid IS NOT NULL
          AND JSON_TYPE(a.silenceid) = 'ARRAY'
          AND JSON_LENGTH(a.silenceid) > 0
    ) AS silenced
    FROM alert_trend_decision_log sent
    WHERE sent.action = 'send'
      AND sent.created_at >= UTC_TIMESTAMP() - INTERVAL 7 DAY
      AND sent.created_at <= UTC_TIMESTAMP() - INTERVAL 30 MINUTE
) samples;

-- 3. 电话告警有效率（近似）：匹配 send 决策且 incident_id 非空的 alert_data 中，
-- 持久化卡片含“✅ 已认领 | …”回执的比例。回执写在 $.elements[*].elements[*].content。
-- 无独立认领状态字段；Flashcat 外部认领、卡片 PATCH/写库失败不可见，因此不是权威认领率。
-- 分母按 alert_data.id 计数；用前 30 分钟的同群/指纹 send 关联（无直接 decision_id 外键）。
-- 没有可用快照仍计入分母，同时输出 observable_card_records 供判断偏差。
SELECT COUNT(*) AS phone_alert_records,
       COALESCE(SUM(JSON_VALID(a.card_content)), 0) AS observable_card_records,
       COALESCE(SUM(CASE WHEN JSON_VALID(a.card_content) THEN
           JSON_SEARCH(a.card_content, 'one', '✅ 已认领 | %', NULL,
                       '$.elements[*].elements[*].content') IS NOT NULL
           ELSE 0 END), 0) AS acknowledged_card_records,
       ROUND(SUM(CASE WHEN JSON_VALID(a.card_content) THEN
           JSON_SEARCH(a.card_content, 'one', '✅ 已认领 | %', NULL,
                       '$.elements[*].elements[*].content') IS NOT NULL
           ELSE 0 END) / NULLIF(COUNT(*), 0), 4) AS phone_ack_ratio
FROM alert_data a
WHERE a.incident_id IS NOT NULL AND a.incident_id <> ''
  AND a.created_at >= UTC_TIMESTAMP() - INTERVAL 7 DAY
  AND a.created_at <= UTC_TIMESTAMP() - INTERVAL 30 MINUTE
  AND EXISTS (
      SELECT 1 FROM alert_trend_decision_log sent
      WHERE sent.action = 'send'
        AND sent.group_id = a.group_id
        AND JSON_CONTAINS(a.fingerprints, JSON_QUOTE(sent.fingerprint))
        AND sent.created_at <= a.created_at
        AND sent.created_at >= a.created_at - INTERVAL 30 MINUTE
  );
