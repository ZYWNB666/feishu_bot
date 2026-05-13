# feishu_bot 代码逻辑文档

## 目录

1. [整体架构](#整体架构)
2. [启动流程](#启动流程)
3. [HTTP 路由](#http-路由)
4. [告警处理流程（核心）](#告警处理流程核心)
5. [标签路由匹配逻辑](#标签路由匹配逻辑)
6. [飞书事件处理](#飞书事件处理)
7. [卡片交互与静默](#卡片交互与静默)
8. [GitLab 消息处理](#gitlab-消息处理)
9. [数据库设计](#数据库设计)
10. [配置与环境变量](#配置与环境变量)

---

## 整体架构

```
Alertmanager / Grafana Alerting
        │ POST /api/v1/alerts
        ▼
  main.py (Flask)
        │
        ├─ alert_handler.py     → 告警路由、发送卡片到飞书群
        ├─ event_handler.py     → 飞书 Webhook 事件（进群等）
        ├─ callback_handler.py  → 卡片按钮回调（静默/取消静默）
        ├─ feishu_api.py        → 飞书 API 封装
        └─ ws_client.py         → WebSocket 长连接（接收飞书推送）

alerts_format/
  ├─ alert_json_format.py  → 从 Alertmanager payload 提取字段
  ├─ db_utils.py           → 路由规则查询与标签匹配
  ├─ savedb.py             → 告警记录写入 alert_data 表
  ├─ ma.py                 → 调用 Alertmanager API 创建/删除静默
  └─ grafana_silence.py    → 调用 Grafana API 创建/删除静默

feishu_utils/
  ├─ alert_card_biz.py     → biz 模板卡片构建（Grafana 格式）
  └─ bot_msg_format.py     → 机器人/用户进群欢迎消息
```

---

## 启动流程

```
python main.py
    │
    ├─ config.validate()         → 检查必要环境变量，缺少则 sys.exit(1)
    ├─ FeishuApiClient 初始化    → APP_ID + APP_SECRET
    ├─ start_ws_client_in_thread → 后台线程建立飞书 WebSocket 长连接
    └─ Flask app.run(port=3100)  → 开始监听 HTTP 请求
```

---

## HTTP 路由

| Method | Path | 功能 |
|--------|------|------|
| `POST` | `/webhook/event` | 接收飞书 Webhook 事件（进群等），立即返回 200，异步处理 |
| `POST` | `/api/card_callback` | 接收飞书卡片按钮点击回调 |
| `POST` | `/api/v1/alerts` | **接收 Alertmanager 告警**（核心入口） |
| `POST` | `/api/gitlab-pipeline-status` | 接收 GitLab Pipeline/Push Webhook |
| `POST` | `/api/send_message` | 主动发送卡片消息（调试/外部触发） |
| `POST` | `/api/send_text` | 主动发送文本消息 |
| `GET/POST/PUT/DELETE` | `/api/alert_rules` | 告警路由规则 CRUD（管理后台） |
| `GET` | `/` | 管理后台前端页面（`static/index.html`） |

---

## 告警处理流程（核心）

`POST /api/v1/alerts` → `alert_handler.process_alert_request()`

```
1. 参数校验（data 非空）

2. _find_alert_configs(data)  ← 查找匹配的路由配置
   │
   ├─ 提取告警所有标签（extract_all_labels）
   ├─ [优先] 标签路由匹配（get_alert_config_by_labels）
   │         → 返回所有命中的配置（多播，见下节）
   └─ [降级] 若标签匹配无结果，按 alert_id 精确匹配（get_alert_config_by_alertid）

3. 若 configs 为空 → 返回 404

4. 对每条命中的 config_row 并行处理（for 循环）：
   │
   ├─ alert_data_api()         → 格式化告警数据，写入 alert_data 表，生成 MAID
   ├─ 判断 template_type：
   │   ├─ "biz"  → build_biz_firing_card / build_biz_resolved_card
   │   └─ "ops"  → alert_to_feishu（Alertmanager 格式卡片）
   └─ feishu_client.send()     → 发送卡片到对应群组（group_id）

5. 汇总结果，全部失败返回 500，部分成功返回 200
```

### Resolved（告警恢复）逻辑

- `status == "resolved"` 时通过 `fingerprint` 反查 `alert_data` 表中原始消息的 `message_id`
- 找到 `message_id` → 以**线程回复**形式发送恢复卡片（不新开消息）
- 未找到 → 新发一条恢复消息

---

## 标签路由匹配逻辑

```python
# db_utils.py → _match_label_rules(alert_labels, label_rules)
```

### 规则

| 特性 | 说明 |
|------|------|
| **AND 关系** | `label_rules` 中的**所有** key-value 规则必须全部命中，才算匹配 |
| **多播** | 所有命中的路由规则都会触发，告警会发送到**每一个**匹配的群组 |
| **键匹配** | 正则表达式，不区分大小写 |
| **值匹配** | 正则表达式，区分大小写 |
| **降级** | 正则编译失败时退化为精确字符串匹配 |

### 匹配流程

```
label_rules = {"severity": "critical", "env": "prod.*"}

for rule_key, rule_value in label_rules.items():
    在 alert_labels 中查找 key 符合 rule_key 正则的标签
    若找到，再检查其 value 是否匹配 rule_value 正则
    若这一条规则未命中 → 整体返回 False（AND 关系）

全部命中 → 返回 True
```

### 实现 OR 逻辑

单条规则内是 AND 关系。要实现 OR，只需**配置多条规则**指向同一个 `group_id`：

| 规则 | label_rules | group_id |
|------|------------|----------|
| 规则 A | `{"severity": "critical"}` | 同一群 |
| 规则 B | `{"env": "prod"}` | 同一群 |

任一条件满足都会触发发送，等效于 OR。

### 匹配优先级

```
标签匹配（label_rules） > alert_id 精确匹配

注意：有标签匹配结果时，不再执行 alert_id 匹配（降级逻辑）
```

---

## 飞书事件处理

`POST /webhook/event` → `event_handler.feishu_event()`

```
1. 立即返回 HTTP 200（防止飞书超时重试）
2. 异步派发到后台线程 _process_event_async_wrapper()
3. 去重检查（_event_cache，TTL 1 小时）
4. 事件类型分发：
   - im.chat.member.bot.added_v1   → 机器人进群，发欢迎消息
   - im.chat.member.user.added_v1  → 新用户进群，发欢迎消息
   - im.message.receive_v1         → 收到消息（当前忽略或回复）
```

### WebSocket 长连接

`ws_client.py` 使用 `lark-oapi` SDK 建立持久 WebSocket 连接，接收飞书实时推送事件，与 HTTP Webhook 并行工作，两者都指向同一套事件处理逻辑。

---

## 卡片交互与静默

`POST /api/card_callback` → `callback_handler.process_card_callback()`

```
1. 去重检查（_callback_cache，TTL 5 秒）
2. 解析按钮 value（处理飞书双重 JSON 编码问题）
3. 提取 action / maid / duration / operator_id
4. 异步执行（后台线程）：
   ├─ action == "silence"
   │   ├─ 通过 maid 查询 silence_type（alertmanager / grafana）
   │   ├─ alertmanager → ma.macreate()  调用 /api/v2/silences
   │   ├─ grafana      → grafana_silence.grafana_create_silence()  调用 Grafana API
   │   └─ 更新卡片为"静默成功"，附带"取消静默"按钮
   └─ action == "cancel_silence"
       ├─ 从 alert_data 查出 silenceid（JSON 数组）
       ├─ 逐一调用删除接口
       └─ 更新卡片为"已取消静默"
```

### 静默按钮 value 结构

```json
{
  "action": "silence",
  "maid": "ABCD1234XYZ",
  "duration": 7200
}
```

---

## GitLab 消息处理

`POST /api/gitlab-pipeline-status` → `pipeline_msg_format.json_processing()`

```
事件类型：
  Pipeline Hook → 格式化为 Pipeline 状态卡片（含 branch/status/duration）
  Push Hook     → 格式化为 Push 事件卡片（含 commits 列表）

发送目标：从请求参数或 Header 中取 group_id，调用 feishu_client.send()
```

---

## 数据库设计

### alert_config（路由规则表）

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | INT | 自增主键 |
| `alert_id` | VARCHAR | 用于 alert_id 精确路由匹配 |
| `project` | VARCHAR | 项目名称标识 |
| `group_id` | VARCHAR | 飞书群 chat_id |
| `users` | JSON | 艾特的用户 open_id 列表 |
| `rank` | VARCHAR | 艾特触发级别，逗号分隔（如 `critical,warning`） |
| `label_rules` | JSON | 标签路由规则，key/value 均支持正则 |
| `template_type` | VARCHAR | 卡片模板：`ops`（Alertmanager）/ `biz`（Grafana） |
| `silence_type` | VARCHAR | 静默方式：`alertmanager` / `grafana` |
| `alertmanager_url` | VARCHAR | Alertmanager 地址（ops 模板使用） |
| `grafana_url` | VARCHAR | Grafana 地址（grafana 静默使用） |
| `remark` | TEXT | 备注 |

### alert_data（告警记录表）

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | VARCHAR(20) | 随机生成的 MAID（告警唯一标识，卡片中展示） |
| `project` | VARCHAR | 所属项目 |
| `alertlabels` | JSON | 告警 matchers（用于 Alertmanager 静默） |
| `alerttime` | VARCHAR | 告警时间（北京时间 ISO 格式） |
| `silenceid` | JSON | 静默 ID 数组（Alertmanager 返回的 silence UUID） |
| `message_id` | VARCHAR | 飞书消息 ID（用于 resolved 时线程回复） |
| `fingerprint` | VARCHAR | Alertmanager 告警指纹（用于 resolved 反查） |

---

## 配置与环境变量

通过 `.env` 文件或系统环境变量注入，`config/config.py` 在模块导入时加载。

| 变量 | 必须 | 说明 |
|------|------|------|
| `APP_ID` | ✅ | 飞书应用 App ID |
| `APP_SECRET` | ✅ | 飞书应用 App Secret |
| `MYSQL_HOST` | ✅ | MySQL 主机地址 |
| `MYSQL_PORT` | ✅ | MySQL 端口 |
| `MYSQL_USER` | ✅ | MySQL 用户名 |
| `MYSQL_PASSWORD` | ✅ | MySQL 密码 |
| `MYSQL_DATABASE` | ✅ | 数据库名称 |
| `GRAFANA_API_KEY` | ❌ | Grafana Service Account Token（使用 Grafana 静默时必填） |
| `LARK_HOST` | ❌ | 飞书 API 地址（默认 `https://open.feishu.cn`） |
| `LOG_LEVEL` | ❌ | 日志级别（默认 `INFO`） |
