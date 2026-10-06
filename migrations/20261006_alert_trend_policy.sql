-- 先执行 20261005_alert_trend_state.sql；以下 ALTER 仅执行一次。
ALTER TABLE alert_config
    ADD COLUMN trend_policy JSON DEFAULT NULL
    COMMENT '趋势告警策略(JSON)，NULL=该路由不参与趋势门控';
