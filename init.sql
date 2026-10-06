-- 创建数据库（如未存在）
CREATE DATABASE IF NOT EXISTS alert_db DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE alert_db;

-- Prometheus告警配置表
CREATE TABLE IF NOT EXISTS alert_config (
    id INT AUTO_INCREMENT PRIMARY KEY COMMENT '自增主键',
    group_id VARCHAR(128) NOT NULL COMMENT '群组ID',
    users JSON NOT NULL COMMENT '用户列表(JSON数组)',
    alert_id VARCHAR(64) NOT NULL COMMENT '告警ID',
    `rank` VARCHAR(64) NOT NULL COMMENT '告警级别',
    telephone_url VARCHAR(255) DEFAULT NULL COMMENT '电话告警URL',
    telephone_rank VARCHAR(64) DEFAULT NULL COMMENT '电话告警级别',
    alertmanager_url VARCHAR(255) NULL COMMENT 'Alertmanager地址',
    project VARCHAR(128) NOT NULL COMMENT '项目名',
    remark VARCHAR(300) DEFAULT NULL COMMENT '备注（最多100汉字）',
    label_rules JSON DEFAULT NULL COMMENT '标签匹配规则(JSON对象，键模糊匹配，值精准匹配)',
    template_type VARCHAR(16) NOT NULL DEFAULT 'ops' COMMENT '卡片模板类型: ops(运维) / biz(业务)',
    silence_type VARCHAR(16) NOT NULL DEFAULT 'alertmanager' COMMENT '静默方式: alertmanager / grafana',
    grafana_url VARCHAR(255) DEFAULT NULL COMMENT 'Grafana地址(静默类型为grafana时使用)',
    oncall_sync TINYINT(1) NOT NULL DEFAULT 0 COMMENT 'oncall同步开关: 0=使用静态users列表, 1=从Flashcat同步当前oncall人员',
    flashcat_schedule_id VARCHAR(64) DEFAULT NULL COMMENT 'Flashcat排班ID（覆盖全局FLASHCAT_SCHEDULE_ID配置）',
    trend_policy JSON DEFAULT NULL COMMENT '趋势告警策略(JSON)，NULL=该路由不参与趋势门控',
    UNIQUE KEY uq_alert_id (alert_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='Prometheus告警配置表';

-- 告警数据表
CREATE TABLE IF NOT EXISTS alert_data (
    id VARCHAR(32) PRIMARY KEY COMMENT '唯一ID',
    alertlabels JSON NOT NULL COMMENT '告警标签(JSON)',
    project VARCHAR(128) NOT NULL COMMENT '项目名',
    alerttime VARCHAR(32) NOT NULL COMMENT '告警时间(ISO格式)',
    silenceid JSON DEFAULT NULL COMMENT '静默ID列表(JSON)',
    message_id VARCHAR(64) DEFAULT NULL COMMENT '飞书消息 ID，用于话题回复',
    fingerprints JSON DEFAULT NULL COMMENT '告警指纹列表(JSON数组)，用于 resolved 反查',
    group_id VARCHAR(128) DEFAULT NULL COMMENT '发送目标群组ID',
    incident_id VARCHAR(64) DEFAULT NULL COMMENT 'Flashcat incident ID（电话告警认领用）',
    card_content MEDIUMTEXT DEFAULT NULL COMMENT '原始卡片JSON（认领时原地更新用）',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '记录创建时间，用于按插入顺序排序'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='告警数据表';

-- 指标趋势告警：待观察状态在服务重启后仍可恢复
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
    cancel_streak INT NOT NULL DEFAULT 0 COMMENT '连续 cancel 计数，用于恢复防抖',
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

-- 观察期汇总卡片：每群一张，结束观察后仍保留消息 ID
CREATE TABLE IF NOT EXISTS alert_trend_digest (
    group_id VARCHAR(128) NOT NULL,
    message_id VARCHAR(64) DEFAULT NULL,
    content_hash VARCHAR(64) DEFAULT NULL,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (group_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='趋势告警观察期汇总卡片（每群一张）';

-- 飞书用户表（姓名 → open_id 映射，供 oncall 艾特使用）
CREATE TABLE IF NOT EXISTS feishu_users (
    id INT AUTO_INCREMENT PRIMARY KEY COMMENT '自增主键',
    name VARCHAR(64) NOT NULL COMMENT '用户姓名',
    open_id VARCHAR(64) NOT NULL COMMENT '飞书 open_id',
    remark VARCHAR(128) DEFAULT NULL COMMENT '备注',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
    UNIQUE KEY uq_name (name),
    UNIQUE KEY uq_open_id (open_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='飞书用户 name→open_id 映射表';
