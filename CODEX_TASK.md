# 任务书：趋势告警系统 V2（泛化 + 防抖 + 观察期汇总卡片）

> 给执行代理的说明：动手前先通读 ARCHITECTURE.md 和下面列出的必读文件，理解现有趋势告警（trend gate）机制的完整链路后再改代码。本任务书中的行为规格是精确要求，不是建议；与现有代码冲突时以本任务书为准。

## 背景

本仓库是飞书告警路由服务。已有"趋势告警试点"：对单条 Grafana 规则（Kimi-K3 TPOT P50，rule_uid `tfk3tpot5p0e3f`），webhook 告警到达后不立即发送，而是查 VictoriaMetrics 近期指标做 `send / observe / cancel` 决策，观察状态持久化在 MySQL（`alert_trend_state` / `alert_trend_decision_log`，见 `migrations/20261005_alert_trend_state.sql`）。

本次改造目标：
1. 把该机制从"单规则、全局常量参数"泛化为"多规则、按路由配置策略"（支持延迟类与成功率类指标）；
2. 恢复通知加连续确认防抖；
3. 观察期告警合并为每群一张汇总卡片（PATCH 原地更新，不刷屏）；
4. 决策日志清理 + 复盘 SQL。

### 必读文件

- `feishu_utils/trend_gate.py` — 决策核心：`rule_uid()`、`is_enabled_for()`、`_load_rule()`、`classify()`、`decide()`、`save_pending()/mark_sent()/mark_resolved()/schedule_next()/due_active()`、`oncall_mention_policy()`
- `feishu_utils/trend_worker.py` — 周期复查线程：`run_pending_once()`、`_process_pending_row()`
- `feishu_utils/alert_handler.py` — `process_alert_request()` 中的 trend 分流（`is_enabled_for` → `_process_trend_request` → `_process_trend_config`），其中 sent 状态的 1.25 倍恶化升级判断
- `alerts_format/db_utils.py` — alert_config 内存缓存模式（`_get_all_label_rule_configs` / `invalidate_alert_config_cache`）
- `routes/alert_rules.py` — 规则 CRUD 与 `_UPDATABLE_FIELDS`
- `config/config.py`、`config/constants.py` — `TREND_*` 配置项
- `init.sql`、`migrations/20261005_alert_trend_state.sql` — 表结构
- `test/trend_gate_regression.py` — 现有回归测试

---

## 任务 1：trend_policy 规则级配置化

### 1.1 数据库迁移

新建 `migrations/20261006_alert_trend_policy.sql`，并同步更新 `init.sql`：

```sql
ALTER TABLE alert_config
    ADD COLUMN trend_policy JSON DEFAULT NULL
    COMMENT '趋势告警策略(JSON)，NULL=该路由不参与趋势门控';

ALTER TABLE alert_trend_state
    ADD COLUMN cancel_streak INT NOT NULL DEFAULT 0
    COMMENT '连续 cancel 计数，用于恢复防抖';
```

`trend_policy` JSON 结构（所有字段可省略，省略时回退 `config/constants.py` 中对应 `TREND_*` 常量默认值）：

```json
{
  "enabled": true,
  "rule_uids": ["tfk3tpot5p0e3f"],
  "observe_seconds": 90,
  "rise_ratio": 0.15,
  "hard_ratio": 1.5,
  "hard_floor": 0.95,
  "min_requests": 20,
  "request_metric": "magik_model_tpot_ms_count",
  "slow_window_seconds": 600,
  "confirm_cycles": 2
}
```

字段语义：
- `rule_uids`：该路由启用趋势门控的 Grafana 规则 UID 列表（UID 取自告警 `generatorURL`）；
- `hard_floor`：仅"越低越严重"类指标（成功率）使用，绝对下限，跌破立即发送（见 1.3）；
- `slow_window_seconds`：仅"越低越严重"类指标使用，慢窗口时长（见 1.3）；
- `request_metric`：请求量下限检查用的 counter 指标名（见 1.4）；
- `confirm_cycles`：恢复防抖所需连续 cancel 次数（见任务 2）。

### 1.2 策略读取（trend_gate.py）

- 新增 `get_policy(config_row) -> TrendPolicy | None`：从 `config_row['trend_policy']` 解析校验（JSON 解析失败 / 字段类型错误 / `enabled != true` / `rule_uids` 为空 → 返回 None 并记日志）。返回冻结 dataclass，缺省字段取全局常量。
- 新增策略缓存（带 TTL，复用 `ALERT_CONFIG_CACHE_TTL`）：查询 `SELECT id, group_id, trend_policy FROM alert_config WHERE trend_policy IS NOT NULL`，实现模式参照 `db_utils._get_all_label_rule_configs`；`invalidate_alert_config_cache()` 中一并失效。
- `is_enabled_for(data)` 改为：`rule_uid(data)` ∈ 缓存中所有启用策略的 `rule_uids` 并集。
- **向后兼容（必须）**：若查询 trend_policy 列失败（迁移未执行）或无任何启用策略，回退旧行为（`Config.TREND_GATE_ENABLED` 且 `rule_uid == Config.TREND_RULE_UID`），回退时 `logger.warning` 一次并做模块级标记，避免每条告警刷屏。
- 环境变量补默认值：`TREND_CONFIRM_CYCLES`（默认 2）、`TREND_LOG_RETENTION_DAYS`（默认 90）加入 `config/constants.py` 与 `.env.example`。

### 1.3 方向自适应：classify 支持"越低越严重"

- `_load_rule(uid)` 目前对 `evaluator.type` 强制 `'gt'` 否则 raise。改为同时支持：
  - `'gt'` → direction=`higher_worse`（越高越严重，现状，行为零变化）
  - `'lt'` → direction=`lower_worse`（越低越严重，如成功率）
  - 其他类型仍 raise。返回值带上 direction（恢复阈值仍取 `unloadEvaluator`）。
- `classify()` 增加 direction（或直接接收 TrendPolicy）参数，按方向分支：
  - **higher_worse**：现有逻辑一行不改（硬上限 `hard_ratio`、median 恶化三条件、observe 到期、低于 `recovery_threshold` cancel）。
  - **lower_worse**（成功率类）：
    1. 样本不足 / 请求量 < `min_requests` → `send`（fail-open，与现状一致，reason='指标样本不足，按原流程发送'）；
    2. `latest <= hard_floor`（若配置了 hard_floor）→ `send`，urgent=True，reason='跌破绝对下限'；
    3. `latest >= recovery_threshold` → `cancel`；
    4. 双窗口确认：最近 `slow_window_seconds` 的 median 越过 threshold **且** 最近 60 秒的 median 也越过 threshold（对 lower_worse，"越过"指 <=）→ `send`，urgent=True，reason='快慢窗口同时越线'；
    5. 只有 60 秒快窗口越线 → `observe`；
    6. observe 到期（`now - first_seen >= observe_seconds`）仍越线 → `send`，urgent=False；到期已恢复 → `cancel`。
- `decide()` 的 query_range 起始时间改为 `max(180, slow_window_seconds + 60)` 秒前，保证慢窗口有足够样本；`TREND_MIN_REQUESTS` 等常量改为从策略读取（保留常量作为默认值）。

### 1.4 请求量下限泛化

`_request_count(labels)` 中写死的 `magik_model_tpot_ms_count` 改为使用策略的 `request_metric`。

### 1.5 恶化升级判断的方向修正（重要）

现有两处"较上次通知值恶化 25% 才再次通知"的比较写死了 `value >= prior * 1.25`，只对 higher_worse 正确。对 lower_worse（成功率）应为 `value <= prior * 0.75`。涉及：

- `alert_handler._process_trend_config` 中 sent 状态的 `worsened` 判断；
- `trend_worker._process_pending_row` 中 sent 状态的 `worsened` 判断，以及 pending 分支 `restore_sent` 的缓解判断（`decision.value < prior * 1.25` 的反向）；
- `trend_gate.oncall_mention_policy()` 中的 1.25 比较。

做法：在 trend_gate 新增辅助函数（如 `is_escalation(value, prior, direction) -> bool` 和对应的"已缓解"判断），`Decision` dataclass 增加 `direction` 字段由 classify 填充，两处调用统一走辅助函数，消除三处硬编码。

### 1.6 逐路由策略生效 + 接线

- `_process_trend_request` 遍历 configs 时：某条 `config_row` 的 `get_policy` 为 None 或该 rule_uid 不在其 `rule_uids` 中 → 该路由直接走 `_process_single_alert_config`（原有立即发送路径），不进趋势决策。
- `trend_worker._process_pending_row` 需在处理行时先按 `config_id` 查一次 `alert_config` 拿到策略（当前是发送时才查，改为入口处查询；查不到时用默认策略）。
- `decide(data, first_seen, policy)` 增加 policy 参数；HTTP 路径从 `config_row` 解析，worker 从查到的 config_row 解析；None 时用全局常量构造默认策略（等价旧行为）。
- `routes/alert_rules.py`：POST/PUT 支持 `trend_policy` 字段（加入 `_UPDATABLE_FIELDS`；写入前校验 JSON 可解析且为 dict，非法返回 400）。

---

## 任务 2：恢复防抖（cancel 连续确认）

仅改 `trend_worker._process_pending_row` 的 pending 分支（HTTP 首次决策 cancel 仍立即 `mark_resolved`，不改；sent 分支不动，sent 行的收敛依赖 Grafana resolved webhook）：

- `decision.action == 'cancel'` 时：`cancel_streak` +1；
  - 达到 `policy.confirm_cycles`（默认 2）→ `mark_resolved` 并将 streak 清零；
  - 未达到 → `schedule_next(reason=f'恢复待确认({streak}/{confirm_cycles})')`，streak 持久化。
- `decision.action != 'cancel'` 的所有路径（observe / send / restore_sent）需将 `cancel_streak` 归零（在 `schedule_next`/`save_pending`/`mark_sent` 等 UPDATE 中顺带置 0，或单独 UPDATE）。

---

## 任务 3：观察期汇总卡片（L0 digest）

- 新表（加入同一迁移文件与 init.sql）：

```sql
CREATE TABLE IF NOT EXISTS alert_trend_digest (
    group_id VARCHAR(128) NOT NULL,
    message_id VARCHAR(64) DEFAULT NULL,
    content_hash VARCHAR(64) DEFAULT NULL,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (group_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='趋势告警观察期汇总卡片（每群一张）';
```

- 新建 `feishu_utils/trend_digest.py`，worker 每轮 `run_pending_once()` 结束后调用更新逻辑：
  - 查询所有 `status='pending'` 的行（注意：不只 `due_active` 的到期行），按 `group_id` 分组；
  - 对每个有 pending 行的群构建一张"👀 趋势告警观察中"汇总卡片：每行展示 alertname、关键维度（tenant/model/ep）、当前值 vs 触发阈值、已观察时长、当前 reason；卡片不 @ 任何人；`config.update_multi=true`；带"更新于 HH:MM"时间戳；
  - `alert_trend_digest` 中已有 `message_id` → `feishu_client.patch_message` 原地更新；没有 → `feishu_client.send` 发一条并写入 message_id；
  - 新内容序列化后 md5 与 `content_hash` 相同 → 跳过 PATCH（避免每 15 秒无意义更新）；
  - 某群已无 pending 行但表中存在 message_id → PATCH 为"当前无观察中的趋势告警"，并保留卡片（不删除）；
  - PATCH/发送失败只记日志，绝不抛出影响复查主流程。

---

## 任务 4：决策日志清理 + 复盘 SQL

- `trend_worker` 内以模块级时间戳控制，每小时执行一次：
  `DELETE FROM alert_trend_decision_log WHERE created_at < UTC_TIMESTAMP() - INTERVAL {TREND_LOG_RETENTION_DAYS} DAY`。
- 新建 `scripts/trend_review.sql`，含三条带中文注释的复盘查询（口径写不精确处用近似实现并在注释中说明偏差）：
  1. **抑制错误率**：cancel 决策后 1 小时内，同 `(rule_uid, group_id, fingerprint)` 再次出现 pending/send 决策的比例；
  2. **骚扰信号**：send 决策后 30 分钟内，对应 `alert_data`（按 fingerprint 关联）`silenceid` 非空的比例；
  3. **电话告警有效率**：send 决策对应 `alert_data.incident_id` 非空的记录中，后续被认领的比例（认领状态未入库时，用可查到的口径并注明局限）。

---

## 全局约束（必须遵守）

1. **单副本假设保持**：不引入新线程/新进程，互斥仍用 `trend_gate.lock_for`，不把任何锁逻辑挪出进程。
2. **向后兼容（最重要）**：未执行迁移、`trend_policy` 全空时，系统行为必须与现在完全一致（旧行为回退 + 一次性 warning）。现有 TPOT 试点规则的默认参数值不许变。
3. **失败降级原则不变**：策略解析失败、指标查询失败一律 fail-open（按原流程立即发送），绝不因新代码导致告警丢失或异常中断复查循环。
4. **风格**：中文注释；日志格式与现有一致（logger.info/error/exception + maid/key 上下文）；新常量进 `config/constants.py` 并支持环境变量覆盖；DB 访问一律走 `db.pool.db_cursor`。
5. **不改动**静默、电话告警、Flashcat、事件处理等无关模块；不重构 `process_alert_request` 的去重与拆分聚合逻辑。
6. 分支：从 `feature/alert-routing` 新建 `feature/trend-gate-v2`；任务 1/2/3/4 各一个 commit，message 风格参照 git log（如 `feat(alerts): 趋势门控策略配置化，支持多规则与成功率类指标`）。
7. 同步更新 `.env.example`（新增环境变量）与 README 中趋势试点章节的行为描述。

## 测试与验收

- 扩展 `test/trend_gate_regression.py`，覆盖：
  - `classify` higher_worse 全场景回归（证明旧行为零变化）；
  - `classify` lower_worse 新场景：绝对下限立即发、快慢窗口同/不同时越线、恢复 cancel、样本不足 fail-open、observe 到期；
  - `get_policy`：完整策略 / 缺省字段回退常量 / 非法 JSON 返回 None / 旧环境变量回退路径；
  - `is_enabled_for`：多 rule_uid 并集、无策略时旧逻辑回退；
  - 恶化/缓解辅助函数在两个方向上的正确性（1.25 / 0.75）；
  - 恢复防抖 streak 的累加、达标清零、非 cancel 路径归零。
- 测试不得依赖真实 MySQL / Grafana / VictoriaMetrics（纯函数测试或 mock）。
- `python -m compileall .` 通过；现有 + 新增回归测试全部通过。
- 在 PR/交付说明中附手工验收清单：执行迁移 → 通过 PUT `/api/alert_rules` 给一条成功率规则配置 trend_policy → 模拟 webhook 验证分流、观察、digest 卡片、防抖日志、decision_log 写入。

## 明确不做（防止过度发挥）

- 不做动态基线 / 环比、不做任何 ML、不做多副本支持、不建评估平台 UI；
- 不改三层告警去重逻辑、不改静默/电话告警链路；
- 不引入新的第三方依赖。
