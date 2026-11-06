"""QQ 机器人：长连接收消息，REST 回消息。

为什么用 WebSocket 而不是 Webhook：
- 长连接是**出站**的，不需要公网回调地址、不需要域名、不用配签名校验
- 掉线自动重连，比维护一个公网入口省心
代价是进程必须常驻（webhook 可以无状态），这对一台 2G 的机器完全不是问题。

QQ 的被动回复窗口是**收到消息后 5 分钟内、最多 5 条**，
所以「生成慢」不是问题，不会像公众号那样 5 秒超时。
"""

import asyncio
import json
import logging

import httpx
import websockets

from im_bridge import context
from im_bridge.config import get_settings
from im_bridge.deepseek import ChatMemory, ask

log = logging.getLogger(__name__)

TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
API_BASE = "https://api.sgroup.qq.com"

OP_DISPATCH = 0
OP_HEARTBEAT = 1
OP_IDENTIFY = 2
OP_RECONNECT = 7
OP_INVALID_SESSION = 9
OP_HELLO = 10

# 单条消息上限保守取 800 字：官方限制约 1000，
# 留余量是因为链接和 emoji 可能被算成多个字符
MAX_CHARS = 800
# 官方限制：同一条消息最多被动回复 5 次
MAX_REPLIES = 5


def clean_text(raw: str) -> str:
    """去掉群聊里 @ 机器人留下的 <@!12345> 前缀。"""
    text = (raw or "").strip()
    while text.startswith("<@") and ">" in text:
        text = text.split(">", 1)[1].strip()
    return text


def split_chunks(content: str, size: int = MAX_CHARS, limit: int = MAX_REPLIES) -> list[str]:
    """按长度切段，最多 limit 段——超出的部分宁可不发，也不触发接口报错。"""
    return [content[i : i + size] for i in range(0, len(content), size)][:limit]


class QQBot:
    def __init__(self, memory: ChatMemory) -> None:
        self.settings = get_settings()
        self.memory = memory
        self.token = ""
        # msg_id -> 已回复次数。QQ 要求同一条消息的多次回复递增 msg_seq
        self._seq: dict[str, int] = {}

    # ---------------- 认证 ----------------
    async def refresh_token(self) -> None:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                TOKEN_URL,
                json={
                    "appId": self.settings.qq_app_id,
                    "clientSecret": self.settings.qq_app_secret,
                },
            )
            resp.raise_for_status()
            data = resp.json()
        self.token = data["access_token"]
        log.info("已获取 access_token（%s 秒有效）", data.get("expires_in"))

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"QQBot {self.token}", "X-Union-Appid": self.settings.qq_app_id}

    async def _gateway(self) -> str:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(f"{API_BASE}/gateway", headers=self._headers)
            resp.raise_for_status()
            return resp.json()["url"]

    # ---------------- 发消息 ----------------
    async def _send(
        self, *, target: str, target_id: str, content: str, msg_id: str | None = None
    ) -> bool:
        """target 是 users 或 groups；带 msg_id 表示被动回复。"""
        url = f"{API_BASE}/v2/{target}/{target_id}/messages"
        payload: dict = {"content": content, "msg_type": 0}
        if msg_id:
            self._seq[msg_id] = self._seq.get(msg_id, 0) + 1
            payload["msg_id"] = msg_id
            payload["msg_seq"] = self._seq[msg_id]

        for attempt in range(2):
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.post(url, headers=self._headers, json=payload)
            if resp.status_code in (200, 201):
                return True
            if resp.status_code == 401 and attempt == 0:
                await self.refresh_token()  # token 过期，刷新后重试一次
                continue
            log.error("发送失败 %s：%s", resp.status_code, resp.text[:300])
            return False
        return False

    async def send_image(self, url: str, *, target: str, target_id: str, msg_id: str) -> bool:
        """发图片：先上传拿到 file_info，再用 msg_type=7 发出去。

        两步是官方的富媒体流程，少一步都会 400。
        失败就让调用方退回"发链接"，不影响主流程。
        """
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                up = await client.post(
                    f"{API_BASE}/v2/{target}/{target_id}/files",
                    headers=self._headers,
                    json={"file_type": 1, "url": url, "srv_send_msg": False},
                )
                up.raise_for_status()
                file_info = up.json().get("file_info", "")
        except Exception:
            log.exception("图片上传失败：%s", url)
            return False

        if not file_info:
            return False

        self._seq[msg_id] = self._seq.get(msg_id, 0) + 1
        payload = {
            "msg_type": 7,
            "media": {"file_info": file_info},
            "msg_id": msg_id,
            "msg_seq": self._seq[msg_id],
        }
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                f"{API_BASE}/v2/{target}/{target_id}/messages",
                headers=self._headers,
                json=payload,
            )
        if resp.status_code in (200, 201):
            return True
        log.error("发图片失败 %s：%s", resp.status_code, resp.text[:200])
        return False

    async def send_proactive(self, text: str, *, target: str, target_id: str) -> bool:
        """主动消息（不带 msg_id）。需要平台开通权限，否则会被拒。"""
        return await self._send(target=target, target_id=target_id, content=text)

    async def reply(self, content: str, *, target: str, target_id: str, msg_id: str) -> None:
        """长回答切段发送。超过 5 段就截断——官方限制同一条消息最多回 5 次。"""
        # 空内容宁可不发，也不要让用户看到空白气泡或 "null"
        if not content or not content.strip():
            log.warning("回答为空，跳过发送")
            return

        chunks = split_chunks(content)
        for chunk in chunks:
            ok = await self._send(target=target, target_id=target_id, content=chunk, msg_id=msg_id)
            if not ok:
                return
            if len(chunks) > 1:
                await asyncio.sleep(0.3)  # 间隔一下，避免触发频率限制

    # ---------------- 事件 ----------------
    async def handle(self, payload: dict) -> None:
        event = payload.get("t")
        data = payload.get("d") or {}

        if event == "C2C_MESSAGE_CREATE":
            await self._on_message(
                data, target="users", target_id=(data.get("author") or {}).get("user_openid", "")
            )
        elif event == "GROUP_AT_MESSAGE_CREATE":
            await self._on_message(data, target="groups", target_id=data.get("group_openid", ""))

    async def _on_message(self, data: dict, *, target: str, target_id: str) -> None:
        text = clean_text(data.get("content") or "")
        msg_id = data.get("id", "")
        if not text or not msg_id:
            return

        # 私聊用 openid，群里用 member_openid：同一个人在群和私聊里有各自的记忆
        author = (data.get("author") or {}).get("member_openid") or target_id
        key = f"{target}:{author}"

        log.info("收到 [%s …%s] %s", target, author[-6:], text[:60])

        # 工具需要知道"替谁做事"（设提醒），但不能让模型传这些参数
        context.set_context(context.IMContext(user_key=key, target=target, target_id=target_id))
        context.reset_images()

        answer = await ask(text, self.memory.get(key))
        self.memory.append(key, "user", text)
        self.memory.append(key, "assistant", answer)

        await self.reply(answer, target=target, target_id=target_id, msg_id=msg_id)

        # 工具生成的图片在正文之后补发，失败了就退回链接（正文里已经有 URL）
        for url in context.take_images():
            if await self.send_image(url, target=target, target_id=target_id, msg_id=msg_id):
                continue
            # 发图失败：退回文字链接。至少用户还能点开看
            log.warning("图片没能发出，改发链接：%s", url)
            await self._send(
                target=target,
                target_id=target_id,
                content=f"图片发不出来，点这里看：{url}",
                msg_id=msg_id,
            )

    # ---------------- 主循环 ----------------
    async def run_forever(self) -> None:
        backoff = 3
        while True:
            try:
                await self.refresh_token()
                await self._session(await self._gateway())
                backoff = 3  # 服务端主动要求重连，不算失败
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("连接异常，%s 秒后重连", backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def _session(self, url: str) -> None:
        async with websockets.connect(url, max_size=2**22) as ws:
            hello = json.loads(await ws.recv())
            interval = (hello.get("d") or {}).get("heartbeat_interval", 41250) / 1000

            await ws.send(
                json.dumps(
                    {
                        "op": OP_IDENTIFY,
                        "d": {
                            "token": f"QQBot {self.token}",
                            "intents": self.settings.qq_intents,
                            "shard": [0, 1],
                            "properties": {},
                        },
                    }
                )
            )
            heartbeat = asyncio.create_task(self._heartbeat(ws, interval))
            try:
                async for raw in ws:
                    payload = json.loads(raw)
                    if payload.get("op") == OP_DISPATCH:
                        await self.handle(payload)
                    elif payload.get("op") == OP_RECONNECT:
                        log.warning("服务端要求重连")
                        return
                    elif payload.get("op") == OP_INVALID_SESSION:
                        log.error("会话失效，重新 identify")
                        self.token = ""
                        return
            finally:
                heartbeat.cancel()

    async def _heartbeat(self, ws, interval: float) -> None:
        while True:
            await asyncio.sleep(interval)
            try:
                await ws.send(json.dumps({"op": OP_HEARTBEAT, "d": None}))
            except Exception:
                return
