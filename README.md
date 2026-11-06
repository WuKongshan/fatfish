# 大肥鱼 · fatfish

**把 DeepSeek 接到 QQ 和微信。** 一个自带工具的聊天机器人：能聊天、查天气、画图、设提醒、联网搜索。

> 名字来自那条刷屏的「蓝色大肥鱼」——它在 QQ 里替 ChatBot 说话，这里把它搬到了自己的服务器上。

```
QQ 私聊 / QQ 群 / 微信公众号
            ↓
       fatfish（本仓库）
            ↓
   DeepSeek + 工具（天气 / 生图 / 搜索 / 提醒）
```

## 特性

| | 说明 |
|---|---|
| **双平台** | QQ（长连接）+ 微信（公众号回调），同一个进程 |
| **工具调用** | 天气（免 key）、生图（智谱/豆包/硅基）、联网搜索、定时提醒 |
| **多轮记忆** | 每个用户/群独立上下文，默认 10 条 |
| **时间感知** | 每次请求注入当前时间，能理解「一分钟后提醒我」 |
| **提醒持久化** | SQLite，容器重建不丢；到期主动推送 |
| **零依赖部署** | 一个 Docker 容器，不需要数据库 |

## 快速开始

### Docker（推荐）

```bash
cp .env.example .env
# 至少填 DEEPSEEK_API_KEY，要连 QQ 再填 QQ_APP_ID/SECRET
docker compose up -d --build
docker compose logs -f
```

### 本地开发

```bash
uv sync
cp .env.example .env
uv run python -m fatfish.main
```

看日志出现这几行就说明起来了：

```
INFO  fatfish: 大肥鱼 启动（生图 已配置，联网搜索 未配置）
INFO  fatfish.qq: 已获取 access_token（7200 秒有效）
INFO  fatfish.wechat: 微信回调已启动，监听 :80/wechat
```

## 配置

| 变量 | 必填 | 说明 |
|---|---|---|
| `DEEPSEEK_API_KEY` | ✅ | 模型 key |
| `QQ_APP_ID` / `QQ_APP_SECRET` | 连 QQ 时必填 | q.qq.com → 开发设置 |
| `WECHAT_TOKEN` | 连微信时必填 | 公众号后台「接口配置」里填同一个值 |
| `WECHAT_APP_ID` / `WECHAT_APP_SECRET` | 连微信时必填 | 有 Secret 才能用客服消息异步推答案 |
| `IMAGE_PROVIDER` | | `zhipu`（默认）/ `ark`（豆包）/ `siliconflow` |
| `IMAGE_API_KEY` | 生图时必填 | 对应平台的 key |
| `WEB_SEARCH_PROVIDER` | | `off`（默认）/ `tavily` / `bocha` |
| `BOT_NAME` | | 机器人自称，默认「大肥鱼」 |
| `BOT_TIMEZONE` | | 默认 `Asia/Shanghai` |

完整列表见 `.env.example`。

## 加一个工具

三步，都在 `fatfish/tools.py`：

```python
# 1. 写 handler（必须返回字符串，不能抛异常）
async def get_crypto_price(symbol: str) -> str: ...


# 2. 声明给模型（OpenAI / DeepSeek 通用的 function calling 格式）
TOOLS.append(
    {
        "type": "function",
        "function": {
            "name": "get_crypto_price",
            "description": "查询币价",
            "parameters": {
                "type": "object",
                "properties": {"symbol": {"type": "string"}},
                "required": ["symbol"],
            },
        },
    }
)

# 3. 注册
HANDLERS["get_crypto_price"] = get_crypto_price
```

工具想给用户发图片，调 `context.add_image(url)` 即可；想设提醒，从 `context.get_context()` 取当前用户。

## 目录

```
fatfish/
├── main.py        入口：QQ 长连接 + 微信 HTTP + 提醒调度
├── qq.py          QQ 协议（鉴权、心跳、事件、富媒体）
├── wechat.py      公众号回调（验签、XML、客服消息）
├── deepseek.py    模型调用 + 工具循环 + 会话记忆
├── tools.py       工具实现（天气/搜索/生图/提醒/时间）
├── push.py        主动消息分发（QQ / 微信）
├── scheduler.py   到期提醒扫描
├── store.py       提醒的 SQLite 存储
└── context.py     当前用户上下文（contextvars）
```

## 文档

- [接入指南](docs/接入指南.md)：QQ 机器人、公众号测试号从零配置
- [踩坑记](docs/踩坑记.md)：7 个只有踩过才知道的问题

## 已知限制

- **微信 5 秒窗口**：靠「先回执 + 客服消息异步推」绕过，需要认证或测试号的客服消息权限
- **QQ 主动消息配额**：提醒推送受官方频率限制，密了会被限流
- **记忆在内存**：进程重启后上下文丢失（提醒不受影响，存在 SQLite）
- **不做鉴权隔离**：所有能用这个机器人的人共享同一套工具权限，别放敏感操作

## License

MIT
