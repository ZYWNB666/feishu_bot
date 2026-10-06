-- 先执行 20261005_alert_trend_state.sql；以下 ALTER 仅执行一次。
ALTER TABLE alert_config
    ADD COLUMN trend_policy JSON DEFAULT NULL
    COMMENT '趋势告警策略(JSON)，NULL=该路由不参与趋势门控';

ALTER TABLE alert_trend_state
    ADD COLUMN cancel_streak INT NOT NULL DEFAULT 0
    COMMENT '连续 cancel 计数，用于恢复防抖';

CREATE TABLE IF NOT EXISTS alert_trend_digest (
    group_id VARCHAR(128) NOT NULL,
    message_id VARCHAR(64) DEFAULT NULL,
    content_hash VARCHAR(64) DEFAULT NULL,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (group_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='趋势告警观察期汇总卡片（每群一张）';
