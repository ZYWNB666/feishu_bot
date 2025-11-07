# 飞书Bot AlertBot

通过API调用向飞书群聊主动发送消息的机器人服务。

## 快速开始

### 1. 环境准备

```bash
# 安装Python依赖
pip install -r requirements.txt
```

### 2. 配置环境变量

创建 `.env` 文件并配置你的飞书应用信息：

```env
APP_ID=cli_a986aaa325ba500b
APP_SECRET=J5X8MpkBhQNdVNIIYJE5pek02n2WHNny
VERIFICATION_TOKEN=your_verification_token
ENCRYPT_KEY=
LARK_HOST=https://open.feishu.cn
```

### 3. 启动服务

```bash
python main.py
```

服务将运行在 `http://localhost:3000`

### 4. 测试发送消息

修改 `example_send.py` 中的 `CHAT_ID` 为你的群聊ID，然后运行：

```bash
python example_send.py
```

## API接口

### 1. 快速发送文本消息

```bash
POST http://localhost:3000/api/send_text
Content-Type: application/json

{
  "chat_id": "oc_xxxxxxxxxxxxxxxx",
  "text": "这是一条测试消息"
}
```

### 2. 发送完整消息

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

### 3. 健康检查

```bash
GET http://localhost:3000/api/health
```

## 使用示例

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

### 监控告警示例

```python
def send_alert(message):
    """发送告警到飞书群"""
    import requests
    
    url = "http://localhost:3000/api/send_text"
    data = {
        "chat_id": "oc_xxxxxxxxxxxxxxxx",  # 你的群聊ID
        "text": f"[告警] {message}"
    }
    
    response = requests.post(url, json=data)
    return response.json()

# 使用
send_alert("数据库连接失败")
```

## 如何获取 chat_id

**方法一：飞书网页版**
1. 打开飞书网页版
2. 进入目标群聊
3. 查看URL中的ID: `https://xxx.feishu.cn/messenger/chat/oc_xxxx`

**方法二：通过机器人**
1. 在群聊中@机器人发送消息
2. 查看服务日志，会显示 `chat_id`

## 更多文档

详细的API使用文档请查看 [API_USAGE.md](./API_USAGE.md)

## 功能特性

- ✅ 主动向群聊发送消息
- ✅ 支持文本、富文本、卡片等多种消息类型
- ✅ 支持发送给群聊或个人
- ✅ 接收并回复用户消息
- ✅ 完整的错误处理和日志记录

## 注意事项

1. 确保机器人已加入目标群聊
2. 确保在飞书开发者后台配置了必要的权限：
   - 获取与发送单聊、群组消息
   - 获取用户发给机器人的单聊消息
   - 接收群聊中@机器人消息事件
3. 生产环境建议使用HTTPS

## 项目结构

```
feishu_bot/
├── main.py              # 主服务文件
├── example_send.py      # 使用示例
├── API_USAGE.md         # 详细API文档
├── requirements.txt     # Python依赖
├── .env                 # 环境变量配置（需自行创建）
└── demo/                # 官方demo代码
    └── python/
        ├── api.py       # API客户端
        ├── event.py     # 事件处理
        └── ...
```

## License

MIT

