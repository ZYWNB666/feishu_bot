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
    template_type VARCHAR(16) NOT NULL DEFAULT 'ops' COMMENT '卡片模板类型: ops(运维) / biz(业务)',
    silence_type VARCHAR(16) NOT NULL DEFAULT 'alertmanager' COMMENT '静默方式: alertmanager / grafana',
    grafana_url VARCHAR(255) DEFAULT NULL COMMENT 'Grafana地址(静默类型为grafana时使用)',
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
    fingerprints JSON DEFAULT NULL COMMENT '告警指纹列表(JSON数组)，用于 resolved 反查'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='告警数据表';

-- 升级已存在表（如果表已存在则运行以下语句添加新列）
ALTER TABLE alert_config
    ADD COLUMN IF NOT EXISTS template_type VARCHAR(16) NOT NULL DEFAULT 'ops' COMMENT '卡片模板类型: ops / biz',
    ADD COLUMN IF NOT EXISTS silence_type VARCHAR(16) NOT NULL DEFAULT 'alertmanager' COMMENT '静默方式: alertmanager / grafana',
    ADD COLUMN IF NOT EXISTS grafana_url VARCHAR(255) DEFAULT NULL COMMENT 'Grafana地址';

ALTER TABLE alert_data
    ADD COLUMN IF NOT EXISTS message_id VARCHAR(64) DEFAULT NULL COMMENT '飞书消息 ID',
    ADD COLUMN IF NOT EXISTS fingerprints JSON DEFAULT NULL COMMENT '告警指纹列表';