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
    alertmanager_url VARCHAR(255) NOT NULL COMMENT 'Alertmanager地址',
    project VARCHAR(128) NOT NULL COMMENT '项目名',
    remark VARCHAR(300) DEFAULT NULL COMMENT '备注（最多100汉字）',
    label_rules JSON DEFAULT NULL COMMENT '标签匹配规则(JSON对象，键模糊匹配，值精准匹配)',
    UNIQUE KEY uq_alert_id (alert_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='Prometheus告警配置表';

-- 告警数据表
CREATE TABLE IF NOT EXISTS alert_data (
    id VARCHAR(32) PRIMARY KEY COMMENT '唯一ID',
    alertlabels JSON NOT NULL COMMENT '告警标签(JSON)',
    project VARCHAR(128) NOT NULL COMMENT '项目名',
    alerttime VARCHAR(32) NOT NULL COMMENT '告警时间(ISO格式)',
    silenceid JSON DEFAULT NULL COMMENT '静默ID列表(JSON)'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='告警数据表';