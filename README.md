# 飞书Bot AlertBot

功能完整的飞书机器人服务，支持告警管理、消息推送、事件处理、Web管理界面等功能。

## ✨ 功能特性

- 🚨 **告警管理**
  - 接收 Alertmanager 告警并推送到飞书群聊
  - 支持告警规则配置和标签匹配
  - 告警静默功能（快捷时长 + 自定义静默截止日期时间）
  - 告警数据库存储和查询

- 📨 **消息推送**
  - 支持文本、富文本、卡片等多种消息类型
  - 支持发送给群聊或个人
  - 支持 @用户通知
  - 支持引用回复

- 🤖 **事件处理**
  - 机器人进群自动打招呼
  - 用户进群欢迎消息
  - 接收并处理用户消息
  - 支持命令交互（help、myuid、groupid 等）

- 🎛️ **Web管理界面**
  - 可视化告警规则管理
  - 规则增删改查
  - 健康检查

- 💾 **数据库支持**
  - MySQL 配置库（存储告警规则）
  - MySQL 告警库（存储告警记录）
  - 支持数据库分离配置

### 告警发送功能消息效果

![告警消息](./img/1.png)
![静默功能](./img/2.png)
![命令功能](./img/3.png)
![路由管理](./img/4.png)
![路由添加](./img/5.png)

## 🚀 快速开始

### 1. 环境准备

```bash
# 克隆项目
git clone <repository_url>
cd feishu_bot

# 安装Python依赖
pip install -r requirements.txt
```

### 2. 数据库初始化

```bash
# 导入数据库结构
mysql -u root -p < init.sql
```

### 3. 配置环境变量

创建 `.env` 文件：

```env
# 飞书应用配置
APP_ID=your_app_id
APP_SECRET=your_app_secret
LARK_HOST=https://open.feishu.cn

# MySQL配置数据库
MYSQL_CONFIG_HOST=localhost
MYSQL_CONFIG_PORT=3306
MYSQL_CONFIG_USER=root
MYSQL_CONFIG_PASSWORD=your_password
MYSQL_CONFIG_DATABASE=alert_db

# 服务配置
HOST=0.0.0.0
PORT=3000
DEBUG=False
```

### 4. 启动服务

```bash
python main.py
```

服务将运行在 `http://localhost:3000`

### 5. 测试发送消息

修改 `example_send.py` 中的 `CHAT_ID` 为你的群聊ID：

```bash
python example_send.py
```

## 📡 API接口

### 1. Web 管理界面

```bash
GET http://localhost:3000/
```

访问 Web 管理页面，可视化管理告警规则。

### 2. 健康检查

```bash
GET http://localhost:3000/api/health
```

### 3. 告警接口（接收 Alertmanager 告警）

```bash
POST http://localhost:3000/api/v1/alerts
Content-Type: application/json

{
  "alerts": [
    {
      "status": "firing",
      "labels": {
        "alertname": "HighCPU",
        "severity": "critical"
      },
      "annotations": {
        "summary": "CPU使用率过高"
      }
    }
  ]
}
```

### 4. 发送文本消息

```bash
POST http://localhost:3000/api/send_text
Content-Type: application/json

{
  "chat_id": "oc_xxxxxxxxxxxxxxxx",
  "text": "这是一条测试消息"
}
```

### 5. 发送完整消息

```bash
POST http://localhost:3000/api/send_message
Content-Type: application/json

{
  "receive_id": "oc_xxxxxxxxxxxxxxxx",
  "receive_id_type": "chat_id",
  "msg_type": "text",
  "content": {
    "text": "这是一条测试消息"
  }
}
```

### 6. 告警规则管理

```bash
# 获取所有规则
GET http://localhost:3000/api/alert_rules

# 创建规则
POST http://localhost:3000/api/alert_rules
Content-Type: application/json

{
  "group_id": "oc_xxx",
  "users": ["ou_xxx"],
  "alert_id": "HighCPU",
  "rank": "P0",
  "alertmanager_url": "http://alertmanager:9093",
  "project": "production"
}

# 更新规则
PUT http://localhost:3000/api/alert_rules/1

# 删除规则
DELETE http://localhost:3000/api/alert_rules/1
```

### 7. 飞书事件回调

```bash
POST http://localhost:3000/webhook/event
```

用于接收飞书事件（机器人进群、用户消息等），需在飞书开发者后台配置。

### 8. 卡片交互回调

```bash
POST http://localhost:3000/api/card_callback
```

用于处理卡片交互（如静默按钮点击），需在飞书开发者后台配置。

## 💡 使用示例

### 趋势告警 V2：按路由配置

部署保持单副本。先依次执行 `migrations/20261005_alert_trend_state.sql` 和
`migrations/20261006_alert_trend_policy.sql`，再通过 POST `/api/alert_rules` 或
PUT `/api/alert_rules/<id>` 设置 `trend_policy`（JSON 对象或对象的 JSON 字符串，
传 null 清空）。配置变更会失效策略与路由缓存，缓存 TTL 为 `ALERT_CONFIG_CACHE_TTL`。

```json
{
  "trend_policy": {
    "enabled": true,
    "rule_uids": ["成功率规则的UID"],
    "observe_seconds": 90,
    "hard_floor": 0.95,
    "request_metric": "业务实际使用的请求counter指标名",
    "slow_window_seconds": 600,
    "min_requests": 20,
    "confirm_cycles": 2
  }
}
```

必须填写非空的 `rule_uids`；`enabled` 缺省为 true，其他参数默认值见 `.env.example`。
`request_metric` 必须是合法指标名，示例中的中文占位内容需替换。成功率的阈值与
`hard_floor` 使用原查询单位：0–1 比例可配置 0.95，0–100 百分数应配置 95。
硬下限缺省不启用。查询指标时按 tenant/model/ep 匹配唯一序列，无法唯一匹配则直接发送。

Grafana `gt` 条件表示越高越严重，保留原 TPOT 行为：轻微越线观察 90 秒，
达到阈值 1.5 倍或最近一分钟明显恶化时立即发送。`lt` 表示越低越严重：
先检查硬下限，再确认恢复；快窗口（60 秒）和慢窗口（默认 600 秒）的中位数均
低于或等于触发阈值时立即发送；仅快窗口越线则观察，到期仍越线也发送。
指标样本不足、规则或指标查询失败时直接发送，沿用原有 @ 配置。

状态按规则 UID、群组、fingerprint 隔离。已通知的同一轮普通重复告警被抑制；
数值比上次通知恶化 25% 才升级（越高越严重为 >=1.25 倍，越低越严重为 <=0.75 倍）。
硬阈值、明显恶化和升级通知可 @ 当前值班人；观察到期的普通越线消息不 @。
恢复后再次越线属于新一轮，当前版本仍不提供跨轮次冷却。

新策略的 pending 复查需要连续 `confirm_cycles` 次 cancel 才结束观察（默认 2 次，
约间隔 15 秒）。中途 observe/send 或升级缓解会清零计数，重启后计数从数据库恢复。
HTTP 首次判断 cancel 仍立即结束；sent 状态仍等待 Grafana resolved webhook 收敛。
旧环境变量试点不启用此防抖，保持原有立即取消行为。

未执行迁移或没有任何启用策略时，回退 `TREND_GATE_ENABLED` / `TREND_RULE_UID`
旧试点（默认关闭，默认 UID `tfk3tpot5p0e3f`），回退只警告一次。
只要存在启用策略，便按每条路由的 UID 列表启用，无需打开旧试点环境开关；
无策略、无匹配 UID 或策略非法的路由直接发送。复用原有单个后台复查线程，
运行时经 CRUD 新增策略也会自动生效。

查询配置需要 `VM_QUERY_URL`（以 `/api/v1/query` 或 `/api/v1/query_range` 结尾），
Grafana 使用 `GRAFANA_RULES_READ_KEY` 或现有 `GRAFANA_API_KEY`。
内网 VM 无认证时省略 `VM_USER` / `VM_PASSWORD`；凭据仅存放在运行环境或 Secret。


### Python调用示例

```python
import requests

# 发送文本消息
url = "http://localhost:3000/api/send_text"
data = {
    "chat_id": "oc_xxxxxxxxxxxxxxxx",
    "text": "告警：服务器CPU使用率超过90%"
}

response = requests.post(url, json=data)
print(response.json())
```

### Alertmanager 集成

在 Alertmanager 配置文件中添加：

```yaml
receivers:
  - name: 'feishu-bot'
    webhook_configs:
      - url: 'http://your-domain:3000/api/v1/alerts'
        send_resolved: true
```

### 机器人命令

在飞书群聊中@机器人：

```
@机器人 help      # 查看帮助
@机器人 myuid     # 查看你的用户ID
@机器人 groupid   # 查看当前群组ID
```

## 🔧 飞书应用配置

### 1. 创建飞书应用

1. 访问[飞书开发者后台](https://open.feishu.cn/app)
2. 创建企业自建应用
3. 获取 `App ID` 和 `App Secret`

### 2. 配置权限

在"权限管理"中开通以下权限：

- `im:message` - 获取与发送单聊、群组消息
- `im:message.group_at_msg` - 获取群组中所有消息
- `im:message.p2p_msg` - 获取用户发给机器人的单聊消息
- `im:chat` - 获取群组信息

### 3. 配置事件订阅

在"事件订阅"中配置：

**请求地址：** `http://your-domain:3000/webhook/event`

**订阅事件：**
- `im.chat.member.bot.added_v1` - 机器人进群
- `im.chat.member.user.added_v1` - 用户进群
- `im.message.receive_v1` - 接收消息

![事件订阅配置](./img/shi_jian_jian_ting.png)

### 4. 配置卡片回调

在"应用功能-机器人"中配置：

**消息卡片请求网址：** `http://your-domain:3000/api/card_callback`

![卡片回调配置](./img/hui_diao_ding_yue.png)

### 5. 发布版本

配置完成后，创建并发布应用版本。

## 📁 项目结构

```
feishu_bot/
├── main.py                      # 主服务入口
├── config/
│   └── config.py               # 配置管理
├── feishu_utils/               # 飞书工具模块
│   ├── feishu_api.py          # 飞书API客户端
│   ├── event_handler.py       # 事件处理器
│   ├── alert_handler.py       # 告警处理器
│   ├── callback_handler.py    # 回调处理器
│   └── bot_msg_format.py      # 消息格式化
│   ├── __init__.py            # 模块初始化
├── alerts_format/              # 告警格式化模块
│   ├── alert_json_format.py   # 告警JSON处理
│   ├── db_utils.py            # 数据库工具
│   ├── ma.py                  # Alertmanager适配
│   └── savedb.py              # 数据库保存
├── static/
│   └── index.html             # Web管理界面
├── init.sql                   # 数据库初始化脚本
├── example_send.py            # 使用示例
├── requirements.txt           # Python依赖
├── .env                       # 环境变量（需自行创建）
├── .gitignore                # Git忽略文件
└── README.md                 # 项目文档
```

## 🎯 如何获取 chat_id

**方法一：通过机器人命令（推荐）**
1. 让机器人进入目标群聊
2. 在群聊中@机器人发送：`@机器人 groupid`
3. 机器人会直接回复当前群组的 chat_id

**方法二：飞书网页版**
1. 打开飞书网页版
2. 进入目标群聊
3. 查看URL中的ID: `https://xxx.feishu.cn/messenger/chat/oc_xxxx`

**方法三：通过日志**
1. 让机器人进入群聊
2. 查看服务日志中的进群事件或消息事件，包含 `chat_id`

## ⚙️ 高级配置

### 数据库分离

如果告警数据量大，可以将告警数据库和配置数据库分离：

```env
# 配置数据库
MYSQL_CONFIG_HOST=config-db.example.com
MYSQL_CONFIG_DATABASE=alert_config

# 告警数据库
MYSQL_ALERT_HOST=alert-db.example.com
MYSQL_ALERT_DATABASE=alert_data
```

### 日志级别

```env
LOG_LEVEL=DEBUG  # DEBUG, INFO, WARNING, ERROR
```

## ⚠️ 注意事项

1. **权限配置**：确保机器人已加入目标群聊，并配置了必要的权限
2. **网络访问**：飞书服务器和 外部依赖服务需要能访问你的回调地址（公网或内网穿透）
3. **HTTPS**：生产环境建议使用 HTTPS
4. **数据库**：确保 MySQL 数据库已启动并可访问
5. **环境变量**：不要将 `.env` 文件提交到版本控制系统
## 📚 更多文档

- [告警静默功能说明](./SILENCE_FEATURE.md) - 详细的静默功能实现文档
- [飞书开发者文档](https://open.feishu.cn/document/) - 官方API文档

## 🤝 贡献

欢迎提交 Issue 和 Pull Request！

## 📄 License

MIT

## 🔗 相关链接

- [飞书开放平台](https://open.feishu.cn/)
- [Alertmanager 文档](https://prometheus.io/docs/alerting/latest/alertmanager/)
- [Flask 文档](https://flask.palletsprojects.com/)
