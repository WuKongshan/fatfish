"""DeepSeek 调用与会话记忆。

刻意不用流式：QQ 的被动回复是一次性 HTTP 回包，
流式没有落点；等 5~15 秒拿完整回答再发，体验反而更干净（不会一条消息蹦两半）。
"""

import json
import logging
from collections import OrderedDict, deque
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

from im_bridge.config import get_settings
from im_bridge.tools import TOOLS, run_tool

# 最多两轮工具调用：查一次天气就够了，
# 给太多轮会让"模型自己绕圈"变得可能，也让延迟不可控
MAX_TOOL_ROUNDS = 2

log = logging.getLogger(__name__)


class ChatMemory:
    """按 QQ openid 存最近几轮对话。

    放内存里：进程重启就丢，但这是聊天机器人不是记账系统，
    丢了最多让用户觉得"它忘性大"。上 Redis 属于过度设计。
    """

    def __init__(self, turns: int, max_users: int = 2000) -> None:
        self._turns = turns
        # OrderedDict + 上限：防止被大量陌生 openid 把内存吃穿
        self._store: OrderedDict[str, deque[dict]] = OrderedDict()
        self._max_users = max_users

    def get(self, key: str) -> list[dict]:
        return list(self._store.get(key, ()))

    def append(self, key: str, role: str, content: str) -> None:
        if key not in self._store:
            if len(self._store) >= self._max_users:
                self._store.popitem(last=False)  # 淘汰最久没说话的
            self._store[key] = deque(maxlen=self._turns)
        self._store[key].append({"role": role, "content": content})
        self._store.move_to_end(key)

    def clear(self, key: str) -> None:
        self._store.pop(key, None)


async def _post(messages: list[dict], tools: list[dict] | None) -> dict:
    settings = get_settings()
    payload: dict = {
        "model": settings.bot_model,
        "messages": messages,
        "max_tokens": settings.bot_max_tokens,
        "temperature": settings.bot_temperature,
        "stream": False,
    }
    if tools:
        payload["tools"] = tools

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            f"{settings.deepseek_base_url}/chat/completions",
            headers={"Authorization": f"Bearer {settings.deepseek_api_key}"},
            json=payload,
        )
        resp.raise_for_status()
        return resp.json()


# 模型偶尔会把"空"写成这些字面量，直接发出去用户会看到 null
_EMPTY_WORDS = {"", "null", "none", "nil", "undefined", "空"}


def final_text(content: str | None, tool_outputs: list[str]) -> str:
    """决定最终发给用户的话。

    优先级：模型的话 → 工具自己返回的那句话 → 通用兜底。
    工具返回值本来就是给人看的（"记住了：xx 提醒你喝水"），
    模型哑火时直接用它，比回一句"我没想出怎么回"有用得多。
    """
    text = (content or "").strip()
    if text.lower() in _EMPTY_WORDS:
        text = ""
    if text:
        return text
    for output in reversed(tool_outputs):
        if output.strip().lower() not in _EMPTY_WORDS:
            return output.strip()
    return "我没想出怎么回，换个说法？"


def _as_assistant(message: dict) -> dict:
    """回填给下一轮的消息要瘦身。

    原样 append 会把 content=None、reasoning_content 等字段一起带回去，
    模型看到自己的 content 是 null，就容易继续吐 null。
    """
    clean: dict = {"role": "assistant", "content": message.get("content") or ""}
    if message.get("tool_calls"):
        clean["tool_calls"] = message["tool_calls"]
    return clean


def _parse_args(raw: str | None) -> dict:
    try:
        value = json.loads(raw or "{}")
        return value if isinstance(value, dict) else {}
    except json.JSONDecodeError:
        return {}


async def ask(question: str, history: list[dict]) -> str:
    """问一次 DeepSeek，必要时先调工具再回答。失败时返回给用户看的兜底文案。"""
    settings = get_settings()
    if not settings.deepseek_api_key:
        return "我还没配置 API Key，管理员需要在 .env 里填 DEEPSEEK_API_KEY。"

    outputs: list[str] = []  # 工具说过的话，模型哑火时拿来兜底
    # 每次请求都重新取时间：长对话里"现在几点"是会漂的
    now = datetime.now(ZoneInfo(settings.bot_timezone))
    messages: list[dict] = [{"role": "system", "content": settings.prompt(now)}]
    messages.extend(history)
    messages.append({"role": "user", "content": question})

    try:
        for _ in range(MAX_TOOL_ROUNDS):
            data = await _post(messages, TOOLS)
            message = data["choices"][0]["message"]
            calls = message.get("tool_calls") or []

            if not calls:
                return final_text(message.get("content"), outputs)

            # 回填时规范化：只留 role/content/tool_calls
            messages.append(_as_assistant(message))
            for call in calls:
                name = call["function"]["name"]
                args = _parse_args(call["function"].get("arguments"))
                result = await run_tool(name, args)
                outputs.append(result)
                messages.append(
                    {"role": "tool", "tool_call_id": call.get("id", ""), "content": result}
                )

        # 用完轮数还没给答案：再问一次，这次不给工具，逼它用已有的结果作答
        data = await _post(messages, None)
        return final_text(data["choices"][0]["message"].get("content"), outputs)
    except Exception:
        log.exception("调用 DeepSeek 失败")
        return "我这边出错了，稍后再试一次？"
