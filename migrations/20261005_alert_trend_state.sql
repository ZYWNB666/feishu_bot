CREATE TABLE IF NOT EXISTS alert_trend_state (
    rule_uid VARCHAR(64) NOT NULL,
    group_id VARCHAR(128) NOT NULL,
    fingerprint VARCHAR(128) NOT NULL,
    config_id INT NOT NULL,
    payload JSON NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'pending',
    first_seen DATETIME(6) NOT NULL,
    next_check DATETIME(6) NOT NULL,
    last_sent_at DATETIME(6) DEFAULT NULL,
    `last_value` DOUBLE DEFAULT NULL,
    reason VARCHAR(255) DEFAULT NULL,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (rule_uid, group_id, fingerprint),
    KEY idx_trend_due (status, next_check)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='趋势告警观察与发送状态';

CREATE TABLE IF NOT EXISTS alert_trend_decision_log (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    rule_uid VARCHAR(64) NOT NULL,
    group_id VARCHAR(128) NOT NULL,
    fingerprint VARCHAR(128) NOT NULL,
    action VARCHAR(16) NOT NULL,
    metric_value DOUBLE DEFAULT NULL,
    reason VARCHAR(255) NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    KEY idx_trend_decision_time (created_at),
    KEY idx_trend_decision_instance (rule_uid, group_id, fingerprint)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='趋势告警决策日志';
