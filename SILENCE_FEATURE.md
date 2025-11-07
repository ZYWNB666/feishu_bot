# 告警静默功能说明

## 功能概述

告警静默功能允许用户通过交互式按钮对告警进行临时静默，支持多种静默时长，并可以随时取消静默。

## ✨ 功能特性

1. **多时长静默按钮**：在告警卡片中显示多个静默选项
   - 🔕 静默2小时
   - 🔕 静默12小时
   - 🔕 静默24小时
   - 🔕 静默3天

2. **静默确认**：点击后引用回复原消息，显示"✅ 静默成功"卡片，包含静默时长和到期时间

3. **取消静默按钮**：在静默成功卡片中显示"🔔 取消静默"按钮

4. **取消确认**：点击后引用回复静默消息，显示"🔔 已取消静默"卡片

## 🔄 实现流程

### 1. 告警发送流程

```
Alertmanager 发送告警
  ↓
POST /api/v1/alerts → main.py: alert_api()
  ↓
alert_handler.py: process_alert_request()
  ├─ 查询告警规则（从 alert_config 表）
  ├─ 格式化告警数据
  ├─ 保存告警记录（获取 maid）
  └─ 调用 event_handler.py: alert_to_feishu()
      ├─ 构建告警卡片（包含 @用户）
      ├─ 添加静默按钮组（2h/12h/24h/3天）
      └─ 发送到飞书群聊
```

### 2. 静默操作流程

```
用户点击"静默X小时"按钮
  ↓
飞书服务器 → POST /api/card_callback
  ↓
main.py: card_callback()
  ↓
callback_handler.py: process_card_callback()
  ├─ 解析回调数据
  │   - action: "silence"
  │   - maid: 告警唯一标识
  │   - duration: 静默时长（秒）
  │   - message_id: 原消息ID
  ├─ 计算到期时间
  ├─ 构建静默成功卡片
  │   - 显示静默时长
  │   - 显示到期时间
  │   - 添加"取消静默"按钮
  └─ 使用 reply_message() 引用回复原消息
```

### 3. 取消静默流程

```
用户点击"取消静默"按钮
  ↓
飞书服务器 → POST /api/card_callback
  ↓
main.py: card_callback()
  ↓
callback_handler.py: process_card_callback()
  ├─ 解析回调数据
  │   - action: "cancel_silence"
  │   - maid: 告警唯一标识
  │   - message_id: 静默消息ID
  ├─ 构建取消静默卡片
  └─ 使用 reply_message() 引用回复静默消息
```

## 🔑 关键模块说明

### 1. `feishu_utils/event_handler.py`

**`alert_to_feishu()` 函数**：
- 构建告警卡片消息
- 添加静默按钮组（支持多个时长选项）
- 使用 `lark_md` 标签支持 Markdown 格式
- 按钮 value 使用 JSON 字符串格式

```python
# 静默按钮示例
{
    "tag": "button",
    "text": {"tag": "plain_text", "content": "🔕 静默2小时"},
    "type": "primary",
    "value": json.dumps({
        "action": "silence",
        "maid": maid,
        "duration": 7200
    })
}
```

### 2. `feishu_utils/callback_handler.py`

**`process_card_callback()` 函数**：
- 处理所有卡片交互回调
- 解析按钮点击数据
- 根据 action 类型分发处理：
  - `silence`: 静默操作
  - `cancel_silence`: 取消静默操作

### 3. `feishu_utils/alert_handler.py`

**`process_alert_request()` 函数**：
- 接收 Alertmanager 告警
- 查询匹配的告警规则
- 格式化告警数据
- 保存到数据库并获取 maid
- 调用 `alert_to_feishu()` 发送告警

### 4. `feishu_utils/feishu_api.py`

**`FeishuApiClient` 类**：
- `send()`: 发送普通消息
- `reply_message()`: 引用回复消息（用于静默功能）
- 自动管理 access_token

### 5. `main.py`

**路由处理**：
- `/api/v1/alerts`: 接收告警（委托给 `alert_handler`）
- `/api/card_callback`: 处理卡片回调（委托给 `callback_handler`）
- `/webhook/event`: 处理飞书事件（委托给 `event_handler`）

## ⚙️ 配置说明

### 1. 飞书应用配置

在飞书开发者后台完成以下配置：

**权限要求：**
- `im:message` - 发送消息权限
- `im:message.group_at_msg` - 群消息@权限

**卡片回调地址：**
```
http://your-domain:3000/api/card_callback
```

在"应用功能-机器人"中配置消息卡片请求网址。

### 2. 数据库配置

**alert_config 表**（告警规则）：
```sql
CREATE TABLE alert_config (
  id INT PRIMARY KEY AUTO_INCREMENT,
  group_id VARCHAR(255),      -- 飞书群ID
  users JSON,                 -- @的用户列表
  alert_id VARCHAR(255),      -- 告警规则ID
  rank VARCHAR(50),           -- 告警等级
  project VARCHAR(255),       -- 项目名称
  alertmanager_url VARCHAR(500),
  label_rules JSON            -- 标签匹配规则
);
```

**alert_data 表**（告警记录）：
```sql
CREATE TABLE alert_data (
  dbid VARCHAR(255) PRIMARY KEY,  -- maid
  alertname VARCHAR(255),
  severity VARCHAR(50),
  status VARCHAR(50),
  data JSON,                      -- 完整告警数据
  created_at TIMESTAMP
);
```

## 📊 数据流转

```
┌─────────────┐
│Alertmanager │
└──────┬──────┘
       │ POST /api/v1/alerts
       ↓
┌─────────────────────────────────────────────┐
│ main.py: alert_api()                        │
│ → alert_handler.py: process_alert_request() │
│   ├─ 查询告警规则                            │
│   ├─ 格式化告警数据                          │
│   └─ 保存到数据库（获取 maid）                │
└──────┬──────────────────────────────────────┘
       │
       ↓
┌─────────────────────────────────┐
│ event_handler.py:               │
│ alert_to_feishu()              │
│ ├─ 构建告警卡片                 │
│ ├─ 添加静默按钮组               │
│ └─ 发送到飞书                   │
└──────┬──────────────────────────┘
       │
       ↓
┌─────────────────┐
│ 飞书群聊显示告警 │
└──────┬──────────┘
       │ 用户点击"静默2小时"
       ↓
┌─────────────────────────────────┐
│ POST /api/card_callback         │
│ → callback_handler.py:          │
│   process_card_callback()       │
│   ├─ 解析 action="silence"      │
│   ├─ 构建静默成功卡片            │
│   └─ reply_message() 引用回复   │
└──────┬──────────────────────────┘
       │
       ↓
┌──────────────────────────┐
│ 显示静默成功卡片          │
│ （包含"取消静默"按钮）     │
└──────┬───────────────────┘
       │ 用户点击"取消静默"
       ↓
┌─────────────────────────────────┐
│ POST /api/card_callback         │
│ → callback_handler.py:          │
│   process_card_callback()       │
│   ├─ 解析 action="cancel_silence"│
│   ├─ 构建取消静默卡片            │
│   └─ reply_message() 引用回复   │
└──────┬──────────────────────────┘
       │
       ↓
┌──────────────────┐
│ 显示取消静默成功  │
└──────────────────┘
```

## 📝 按钮数据格式

### 静默按钮 value

```json
{
  "action": "silence",
  "maid": "20231107_abc123xyz",
  "duration": 7200
}
```

**字段说明：**
- `action`: 操作类型，固定为 "silence"
- `maid`: 告警唯一标识（从数据库获取）
- `duration`: 静默时长（秒）
  - 7200 = 2小时
  - 43200 = 12小时
  - 86400 = 24小时
  - 259200 = 3天

### 取消静默按钮 value

```json
{
  "action": "cancel_silence",
  "maid": "20231107_abc123xyz"
}
```

**字段说明：**
- `action`: 操作类型，固定为 "cancel_silence"
- `maid`: 告警唯一标识

## 🧪 测试方法

### 1. 发送测试告警

使用 curl 直接调用告警接口：

```bash
curl -X POST http://localhost:3000/api/v1/alerts \
  -H "Content-Type: application/json" \
  -d '{
    "alerts": [
      {
        "status": "firing",
        "labels": {
          "alertname": "TestAlert",
          "severity": "critical",
          "instance": "test-server"
        },
        "annotations": {
          "summary": "这是一条测试告警",
          "description": "用于测试静默功能"
        }
      }
    ]
  }'
```

或使用 `example_send.py` 发送测试：

```bash
python example_send.py
```

### 2. 验证告警卡片

在飞书群聊中检查：
- ✅ 告警卡片正常显示
- ✅ 包含4个静默按钮（2h/12h/24h/3天）
- ✅ @用户功能正常

### 3. 测试静默功能

1. 点击"🔕 静默2小时"按钮
2. 验证：
   - ✅ 引用回复原告警消息
   - ✅ 显示静默成功卡片
   - ✅ 显示静默时长和到期时间
   - ✅ 包含"🔔 取消静默"按钮

### 4. 测试取消静默

1. 点击"🔔 取消静默"按钮
2. 验证：
   - ✅ 引用回复静默消息
   - ✅ 显示取消静默成功卡片

### 5. 查看日志

```bash
# 查看服务日志
tail -f logs/feishu_bot.log

# 或直接查看控制台输出
```

检查日志中的关键信息：
- 告警接收和处理日志
- 卡片回调处理日志
- 消息发送成功日志

## ⚠️ 注意事项

### 1. 必需条件

- ✅ **数据库连接**：告警必须成功保存到数据库才有 maid
- ✅ **回调地址**：飞书应用后台必须配置卡片回调地址
- ✅ **权限配置**：确保应用有发送消息和回复消息权限
- ✅ **机器人在群**：机器人必须已加入目标群聊

### 2. 常见问题

**问题：点击按钮没有反应**
- 检查飞书后台是否配置了卡片回调地址
- 检查服务日志是否收到回调请求
- 检查网络是否能从飞书服务器访问到你的服务

**问题：告警没有静默按钮**
- 检查告警是否成功保存到数据库（有 maid）
- 检查 `alert_to_feishu()` 函数是否传入了 maid 参数

**问题：按钮点击后报错**
- 检查按钮 value 是否为 JSON 字符串格式
- 检查回调数据解析是否正确
- 查看服务日志获取详细错误信息

### 3. 开发建议

- 使用内网穿透工具（如 ngrok）进行本地开发测试
- 开启 DEBUG 模式查看详细日志
- 使用 Postman 或 curl 测试 API 接口
- 在飞书开发者后台查看应用调用日志

## 🚀 未来扩展

可选的增强功能：

### 1. 持久化静默状态

```sql
CREATE TABLE alert_silence (
  id INT PRIMARY KEY AUTO_INCREMENT,
  maid VARCHAR(255),
  duration INT,
  start_time TIMESTAMP,
  end_time TIMESTAMP,
  status ENUM('active', 'cancelled', 'expired'),
  operator VARCHAR(255)
);
```

### 2. 智能告警屏蔽

在发送告警前检查静默状态：

```python
def is_silenced(maid):
    # 查询数据库，判断是否在静默期
    # 如果在静默期，不发送告警
    pass
```

### 3. 静默历史查询

添加 API 接口查询静默历史：

```bash
GET /api/silence_history?maid=xxx
```

### 4. 自定义静默时长

支持用户输入自定义时长：

```
┌───────────────────┐
│ 自定义静默时长     │
│ [输入框] 小时      │
│ [确认] [取消]      │
└───────────────────┘
```

### 5. 批量静默

支持对同类告警批量静默：

```
🔕 静默所有 HighCPU 告警 2小时
```

## 📚 相关文档

- [README.md](./README.md) - 项目主文档
- [飞书卡片搭建工具](https://open.feishu.cn/tool/cardbuilder) - 在线设计卡片
- [飞书消息卡片开发指南](https://open.feishu.cn/document/ukTMukTMukTM/uczM3QjL3MzN04yNzcDN) - 官方文档

