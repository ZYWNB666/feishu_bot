# 告警静默功能说明

## 功能概述

告警静默功能允许用户通过交互式按钮对告警进行临时静默，并可以随时取消静默。

## 功能特性

1. **静默按钮**：在告警卡片中显示"🔕 静默2小时"按钮
2. **静默确认**：点击后引用回复原消息，显示"✅ 静默成功"卡片
3. **取消静默按钮**：在静默成功卡片中显示"🔔 取消静默"按钮
4. **取消确认**：点击后引用回复静默消息，显示"🔔 已取消静默"卡片

## 实现流程

### 1. 告警发送
```
main.py: alert_api()
  ├─ 提取 alertname 和 maid
  ├─ 调用 alert_to_feishu() 传递 maid
  └─ 在卡片中添加"静默2小时"按钮
```

### 2. 静默操作
```
用户点击"静默2小时"按钮
  ↓
飞书回调 → POST /api/card_callback
  ↓
main.py: card_callback()
  ├─ 解析回调数据（action: "silence", maid, duration）
  ├─ 调用 create_silence_success_card() 创建卡片
  └─ 调用 feishu_client.reply_message() 引用回复
```

### 3. 取消静默
```
用户点击"取消静默"按钮
  ↓
飞书回调 → POST /api/card_callback
  ↓
main.py: card_callback()
  ├─ 解析回调数据（action: "cancel_silence", maid）
  ├─ 调用 create_cancel_silence_card() 创建卡片
  └─ 调用 feishu_client.reply_message() 引用回复
```

## 关键代码修改

### 1. `alerts_format/alert_json_format.py`
- 修改 `alert_data_api()` 返回值：`return alerts, severities, dbid`
- dbid (maid) 现在会一起返回

### 2. `main.py`
- 新增 `create_silence_success_card()` 函数：创建静默成功卡片
- 新增 `create_cancel_silence_card()` 函数：创建取消静默卡片
- 新增 `_get_current_time()` 函数：获取当前时间
- 新增 `/api/card_callback` 接口：处理卡片交互回调
- 修改 `alert_api()`: 接收 maid 并传递给 alert_to_feishu()

### 3. `feishu_utils/event_handler.py`
- 修改 `alert_to_feishu()`: 新增 `maid` 参数
- 添加静默按钮到告警卡片

### 4. `feishu_utils/feishu_api.py`
- 新增 `reply_message()` 方法：实现引用回复功能

## 配置说明

### 飞书应用配置

1. **事件订阅**
   - 需要配置卡片回调地址：`http://your-domain/api/card_callback`
   - 确保应用有发送消息的权限

2. **权限要求**
   - `im:message`：发送消息权限
   - `im:message.group_at_msg`：群消息@权限

## 数据流转

```
Alertmanager
  ↓ (POST /api/v1/alerts)
Main.py (alert_api)
  ↓
alert_data_api() → 生成 maid (保存到数据库)
  ↓
alert_to_feishu() → 发送告警卡片（包含静默按钮）
  ↓
飞书群聊显示告警
  ↓
用户点击"静默2小时"按钮
  ↓ (POST /api/card_callback)
Main.py (card_callback)
  ↓
创建静默成功卡片 → reply_message() → 引用回复原消息
  ↓
飞书群聊显示静默成功卡片（包含取消静默按钮）
  ↓
用户点击"取消静默"按钮
  ↓ (POST /api/card_callback)
Main.py (card_callback)
  ↓
创建取消静默卡片 → reply_message() → 引用回复静默消息
  ↓
飞书群聊显示取消静默成功卡片
```

## 按钮值格式

按钮的 `value` 字段使用 JSON 字符串格式：

### 静默按钮
```json
{
  "action": "silence",
  "maid": "abc123xyz",
  "duration": 7200
}
```

### 取消静默按钮
```json
{
  "action": "cancel_silence",
  "maid": "abc123xyz"
}
```

## 测试方法

1. **发送测试告警**
   ```bash
   # 通过 Alertmanager 发送告警
   # 或使用 curl 直接调用接口
   curl -X POST http://your-domain/api/v1/alerts \
     -H "Content-Type: application/json" \
     -d @test_alert.json
   ```

2. **查看告警卡片**
   - 检查是否显示"🔕 静默2小时"按钮

3. **点击静默按钮**
   - 检查是否引用回复原消息
   - 检查静默成功卡片是否包含"🔔 取消静默"按钮

4. **点击取消静默按钮**
   - 检查是否引用回复静默消息
   - 检查取消静默成功卡片显示

## 注意事项

1. **maid 必须有效**：只有成功保存到数据库的告警才会有 maid
2. **回调地址配置**：确保飞书应用后台配置了正确的回调地址
3. **权限检查**：确保应用有足够的权限发送和回复消息
4. **JSON 格式**：按钮 value 必须是 JSON 字符串，不是 JSON 对象

## 未来扩展

可选的扩展功能：
- 在数据库中存储静默状态
- 实现真正的告警屏蔽逻辑（在发送前检查静默状态）
- 支持自定义静默时长
- 静默历史记录查询

