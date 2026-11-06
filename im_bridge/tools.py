"""机器人的「手」：查天气、联网搜索、生图、设提醒。

加新工具只要三步：写 handler → 加进 TOOLS 声明 → 加进 HANDLERS。
工具只负责"做事"，不负责"说话"——说什么由模型决定。

几个约定：
- 工具永远返回字符串，哪怕是错误说明。抛异常会让整轮对话挂掉。
- 工具不接收 user_id 之类的参数——从 context 里取，避免模型瞎编。
"""

import logging
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from im_bridge import context
from im_bridge.config import get_settings
from im_bridge.store import ReminderStore

log = logging.getLogger(__name__)

GEO_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
TAVILY_URL = "https://api.tavily.com/search"
BOCHA_URL = "https://api.bochaai.com/v1/web-search"

# WMO 天气代码 → 中文。完整表见 open-meteo 文档，这里覆盖常见值
WMO = {
    0: "晴",
    1: "多云间晴",
    2: "多云",
    3: "阴",
    45: "雾",
    48: "雾凇",
    51: "小毛毛雨",
    53: "毛毛雨",
    55: "大毛毛雨",
    61: "小雨",
    63: "中雨",
    65: "大雨",
    71: "小雪",
    73: "中雪",
    75: "大雪",
    80: "阵雨",
    81: "强阵雨",
    82: "暴雨",
    95: "雷阵雨",
    96: "雷阵雨伴冰雹",
    99: "强雷暴伴冰雹",
}

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_current_time",
            "description": "获取当前日期和时间。需要知道现在几点、今天几号，或计算提醒时间时调用。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "查询某城市今天的天气。问到天气、气温、带不带伞时调用。",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string", "description": "城市名，如「杭州」"}},
                "required": ["city"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "联网搜索。问新闻、实时信息、或你不确定的事实时调用。",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "搜索关键词"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "generate_image",
            "description": "根据描述生成一张图片。用户说「画一张」「来张图」时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "画面描述，英文效果更好，可保留中文主体",
                    }
                },
                "required": ["prompt"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_reminder",
            "description": "设置定时提醒。用户说「X 分钟后提醒我」「明天几点提醒我」时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "提醒内容，如「喝水」"},
                    "due_at": {
                        "type": "string",
                        "description": "ISO 8601 带时区，如 2026-11-05T21:30:00+08:00",
                    },
                },
                "required": ["content", "due_at"],
            },
        },
    },
]

_STORE: ReminderStore | None = None


def init_store(store: ReminderStore) -> None:
    global _STORE
    _STORE = store


# ---------------- 时间 ----------------
def format_now(now: datetime | None = None) -> str:
    """给模型读的时间串。带时区和星期，模型算「明天下午三点」才不容易出错。"""
    current = now or datetime.now(ZoneInfo(get_settings().bot_timezone))
    weekday = "一二三四五六日"[current.weekday()]
    return (
        f"现在是 {current:%Y-%m-%d %H:%M} 周{weekday}"
        f"（{get_settings().bot_timezone}，UTC{current:%z}）"
    )


async def get_current_time() -> str:
    return format_now()


# ---------------- 天气 ----------------
def describe(code: int | None) -> str:
    if code is None:
        return "未知"
    return WMO.get(int(code), f"未知（代码 {code}）")


async def _geocode(city: str) -> dict | None:
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(
            GEO_URL, params={"name": city, "count": 1, "language": "zh", "format": "json"}
        )
        resp.raise_for_status()
        results = resp.json().get("results") or []
    return results[0] if results else None


def format_weather(place: dict, data: dict) -> str:
    cur = data.get("current") or {}
    daily = data.get("daily") or {}
    name = place.get("name", "")
    admin = place.get("admin1") or ""

    lines = [f"{name}{admin and '（' + admin + '）'}当前：{describe(cur.get('weather_code'))}"]
    if cur.get("temperature_2m") is not None:
        humidity = cur.get("relative_humidity_2m", "?")
        lines.append(f"气温 {cur['temperature_2m']}°C，湿度 {humidity}%")
    if cur.get("wind_speed_10m") is not None:
        lines.append(f"风速 {cur['wind_speed_10m']} km/h")

    highs = (daily.get("temperature_2m_max") or [None])[0]
    lows = (daily.get("temperature_2m_min") or [None])[0]
    if highs is not None and lows is not None:
        lines.append(f"今天 {lows}~{highs}°C")

    rain = (daily.get("precipitation_probability_max") or [None])[0]
    if rain is not None:
        lines.append(f"降水概率 {rain}%")

    return "；".join(lines)


async def get_weather(city: str) -> str:
    try:
        place = await _geocode(city)
        if place is None:
            return f"没找到叫「{city}」的城市，换个说法试试（比如只写城市名）。"

        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                FORECAST_URL,
                params={
                    "latitude": place["latitude"],
                    "longitude": place["longitude"],
                    "current": "temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m",
                    "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max",
                    "timezone": "auto",
                    "forecast_days": 1,
                },
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception:
        log.exception("查天气失败：%s", city)
        return "查天气的服务暂时连不上，稍后再试。"

    return format_weather(place, data)


# ---------------- 联网搜索 ----------------
def format_search(results: list[dict]) -> str:
    if not results:
        return "没有搜到相关结果。"
    lines = []
    for i, item in enumerate(results, start=1):
        snippet = (item.get("snippet") or "")[:300]
        lines.append(f"[{i}] {item.get('title', '')}\n{snippet}\n来源：{item.get('url', '')}")
    return "\n\n".join(lines)


async def web_search(query: str) -> str:
    settings = get_settings()
    if settings.web_search_provider == "off" or not settings.web_search_api_key:
        return "搜索服务没配置，只好靠我自己的知识回答，可能不准。"

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            if settings.web_search_provider == "tavily":
                resp = await client.post(
                    TAVILY_URL,
                    json={
                        "api_key": settings.web_search_api_key,
                        "query": query,
                        "max_results": 4,
                        "search_depth": "basic",
                    },
                )
            else:  # bocha
                resp = await client.post(
                    BOCHA_URL,
                    headers={"Authorization": f"Bearer {settings.web_search_api_key}"},
                    json={"query": query, "count": 4, "summary": True},
                )
            resp.raise_for_status()
            data = resp.json()
    except Exception:
        log.exception("联网搜索失败：%s", query)
        return "搜索服务暂时连不上。"

    if settings.web_search_provider == "tavily":
        results = [
            {"title": r.get("title", ""), "url": r.get("url", ""), "snippet": r.get("content", "")}
            for r in data.get("results", [])
        ]
    else:
        pages = (data.get("data") or {}).get("webPages", {}).get("value", [])
        results = [
            {
                "title": p.get("name", ""),
                "url": p.get("url", ""),
                "snippet": p.get("summary") or p.get("snippet", ""),
            }
            for p in pages
        ]
    return format_search([r for r in results if r.get("url")])


# ---------------- 生图 ----------------
# 每家接口的请求体和返回结构都不一样，抽成配置驱动：
# 换模型只改 .env，不用动代码。
PROVIDERS = {
    # 硅基流动：image_size / images[0].url
    "siliconflow": {
        "base_url": "https://api.siliconflow.cn/v1",
        "model": "Kwai-Kolors/Kolors",
        "size_field": "image_size",
    },
    # 智谱：size / data[0].url
    "zhipu": {
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "model": "cogview-4",
        "size_field": "size",
    },
    # 火山方舟（豆包 Seedream）：同步接口、OpenAI 兼容、data[0].url
    # 模型 ID 各账号/地域不同，必须用 IMAGE_MODEL 显式指定，不给默认值
    "ark": {
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "model": "",
        "size_field": "size",
    },
}


def image_conf() -> dict:
    """按 IMAGE_PROVIDER 解析出 url / model / 尺寸字段名。"""
    settings = get_settings()
    preset = PROVIDERS.get(settings.image_provider, PROVIDERS["siliconflow"])
    return {
        "url": f"{(settings.image_base_url or preset['base_url']).rstrip('/')}/images/generations",
        "model": settings.image_model or preset["model"],
        "size_field": preset["size_field"],
        "size": settings.image_size,
    }


def extract_image_url(data: dict) -> str | None:
    """两家返回结构不同：硅基是 images[0].url，智谱是 data[0].url。"""
    for key in ("images", "data"):
        items = data.get(key)
        if isinstance(items, list) and items:
            item = items[0]
            if isinstance(item, dict) and item.get("url"):
                return str(item["url"])
    return None


async def generate_image(prompt: str) -> str:
    settings = get_settings()
    if not settings.image_api_key:
        return "生图功能没配 key，管理员需要在 .env 里填 IMAGE_API_KEY。"

    conf = image_conf()
    if not conf["model"]:
        # 火山方舟这类没有通用模型名的服务，必须让用户显式指定
        return (
            f"生图功能缺 IMAGE_MODEL（provider={settings.image_provider} 没有默认模型），"
            "去 .env 里填一个。"
        )

    try:
        async with httpx.AsyncClient(timeout=90) as client:
            resp = await client.post(
                conf["url"],
                headers={"Authorization": f"Bearer {settings.image_api_key}"},
                json={
                    "model": conf["model"],
                    "prompt": prompt,
                    conf["size_field"]: conf["size"],
                },
            )
            resp.raise_for_status()
            url = extract_image_url(resp.json())
        if not url:
            log.error("生图返回里没有图片地址：%s", resp.text[:300])
            return "生图服务返回了个我看不懂的东西，稍后再试。"
    except Exception:
        log.exception("生图失败（provider=%s）：%s", settings.image_provider, prompt)
        return "生图服务出错了（可能是额度或模型名不对），稍后再试。"

    # URL 只登记到上下文，由 bot 负责发图；不要把链接写进返回值，
    # 否则模型会把它当正文贴出来（一长串 Markdown，很难看）
    context.add_image(url)
    return "图片已生成，正在发给你。"


# ---------------- 定时提醒 ----------------
def parse_due(raw: str) -> datetime:
    """解析模型给的时间。没时区的按配置时区理解——不能按 UTC 猜。"""
    settings = get_settings()
    value = datetime.fromisoformat(raw)
    if value.tzinfo is None:
        value = value.replace(tzinfo=ZoneInfo(settings.bot_timezone))
    return value


async def set_reminder(content: str, due_at: str) -> str:
    if _STORE is None:
        return "提醒功能没初始化好。"
    ctx = context.get_context()
    if ctx is None:
        return "不知道要提醒谁，稍后再试。"

    try:
        due = parse_due(due_at)
    except ValueError:
        return f"时间「{due_at}」我没解析出来，换个说法再试一次。"

    now = datetime.now(UTC)
    if due <= now:
        return "这个时间已经过去了，说个将来的时间我才好提醒你。"
    # 一年以外基本是模型算错了，别默默接受
    if due - now > timedelta(days=366):
        return "这个时间太远了（超过一年），确认一下？"

    _STORE.add(
        user_key=ctx.user_key,
        target=ctx.target,
        target_id=ctx.target_id,
        content=content,
        due_at=due,
    )
    local = due.astimezone(ZoneInfo(get_settings().bot_timezone))
    return f"记住了：{local.strftime('%m月%d日 %H:%M')} 提醒你{content}。"


HANDLERS = {
    "get_current_time": get_current_time,
    "get_weather": get_weather,
    "web_search": web_search,
    "generate_image": generate_image,
    "set_reminder": set_reminder,
}


async def run_tool(name: str, arguments: dict) -> str:
    handler = HANDLERS.get(name)
    if handler is None:
        return f"没有叫 {name} 的工具"
    try:
        return await handler(**arguments)
    except TypeError:
        # 模型偶尔会传多余的字段，别让它把整轮对话搞崩
        log.warning("工具参数不匹配：%s %s", name, arguments)
        return "参数不对，换个说法再问一次。"
