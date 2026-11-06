"""微信公众号：HTTP 回调。

和 QQ 的架构完全不同——微信**没有长连接**，只能由微信主动 POST 到你的公网地址。

两个硬约束：
1. 5 秒内必须响应一次，否则微信显示"暂时无法提供服务"
2. 被动回复只能回一次，之后本轮结束

所以流程是：先秒回一句"收到，正在想…"占住窗口，
拿到答案再用**客服消息**主动推一条（用户发消息后 48 小时内有效，可多次）。
"""

import asyncio
import hashlib
import logging
import time
import xml.etree.ElementTree as ET
from collections import OrderedDict

import httpx
from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import PlainTextResponse

from im_bridge import context
from im_bridge.config import get_settings
from im_bridge.deepseek import ChatMemory, ask

log = logging.getLogger(__name__)

# 微信侧的记忆按 openid 存；和 QQ 分开命名，避免同一个人的两个渠道串上下文
_memory = ChatMemory(get_settings().history_turns)

router = APIRouter()

WX_API = "https://api.weixin.qq.com"

# 微信会在 5 秒无响应时重发同一条消息（最多 3 次），
# 不去重用户会收到重复回答。只留最近的，别无限涨内存
_seen: OrderedDict[str, float] = OrderedDict()


def check_signature(token: str, timestamp: str, nonce: str, signature: str) -> bool:
    """微信的验签：三个参数按字典序拼接后 sha1。"""
    raw = "".join(sorted([token, timestamp, nonce]))
    return hashlib.sha1(raw.encode()).hexdigest() == signature


def parse_message(body: bytes) -> dict[str, str]:
    """解析微信推来的 XML。格式很老，字段名大小写敏感。"""
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        log.warning("微信消息解析失败：%s", body[:200])
        return {}
    return {child.tag: (child.text or "") for child in root}


def build_text_reply(to_user: str, from_user: str, content: str) -> str:
    return (
        "<xml>"
        f"<ToUserName><![CDATA[{to_user}]]></ToUserName>"
        f"<FromUserName><![CDATA[{from_user}]]></FromUserName>"
        f"<CreateTime>{int(time.time())}</CreateTime>"
        "<MsgType><![CDATA[text]]></MsgType>"
        f"<Content><![CDATA[{content}]]></Content>"
        "</xml>"
    )


def is_duplicate(msg_id: str, ttl: float = 300) -> bool:
    """同一个 MsgId 只处理一次。返回 True 表示是重发的。"""
    now = time.time()
    while _seen and now - next(iter(_seen.values())) > ttl:
        _seen.popitem(last=False)

    if msg_id in _seen:
        return True
    _seen[msg_id] = now
    return False


# ---------------- 客服消息 ----------------
_token: dict[str, float | str] = {"value": "", "expires": 0.0}


async def access_token() -> str:
    """带缓存：有效期 7200 秒，别每次发消息都换一次（有频率限制）。"""
    settings = get_settings()
    if _token["value"] and float(_token["expires"]) > time.time() + 60:
        return str(_token["value"])

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(
            f"{WX_API}/cgi-bin/token",
            params={
                "grant_type": "client_credential",
                "appid": settings.wechat_app_id,
                "secret": settings.wechat_app_secret,
            },
        )
        data = resp.json()

    if "access_token" not in data:
        log.error("获取 access_token 失败：%s", data)
        return ""

    _token["value"] = data["access_token"]
    _token["expires"] = time.time() + int(data.get("expires_in", 7200))
    return str(_token["value"])


async def push_text(openid: str, content: str) -> bool:
    """客服消息：主动推送。需要认证服务号或测试号。"""
    token = await access_token()
    if not token:
        return False

    # 微信文本上限约 2048 字节，超了直接截断（这里按字符保守处理）
    body = content[:1800]
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{WX_API}/cgi-bin/message/custom/send",
            params={"access_token": token},
            json={"touser": openid, "msgtype": "text", "text": {"content": body}},
        )
    data = resp.json()
    if data.get("errcode") not in (0, None):
        log.error("客服消息发送失败：%s", data)
        return False
    return True


async def push_image(openid: str, url: str) -> bool:
    """客服消息发图片，要先上传临时素材拿 media_id。

    微信的图片消息不接受外链，只认自己素材库里的 media_id——
    这和 QQ 拿到 URL 就能发的逻辑完全不同，容易踩。
    """
    token = await access_token()
    if not token:
        return False

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            src = await client.get(url)
            src.raise_for_status()
            data = src.content
    except Exception:
        log.exception("下载待发图片失败：%s", url[:80])
        return False

    try:
        async with httpx.AsyncClient(timeout=40) as client:
            up = await client.post(
                f"{WX_API}/cgi-bin/media/upload",
                params={"access_token": token, "type": "image"},
                files={"media": ("image.jpg", data, "image/jpeg")},
            )
            media_id = up.json().get("media_id", "")
    except Exception:
        log.exception("上传临时素材失败")
        return False

    if not media_id:
        return False

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{WX_API}/cgi-bin/message/custom/send",
            params={"access_token": token},
            json={"touser": openid, "msgtype": "image", "image": {"media_id": media_id}},
        )
    ok = resp.json().get("errcode", 0) == 0
    if not ok:
        log.error("微信发图失败：%s", resp.text[:200])
    return ok


# ---------------- 路由 ----------------
@router.get("/wechat", response_class=PlainTextResponse)
async def verify(
    signature: str = Query(""),
    timestamp: str = Query(""),
    nonce: str = Query(""),
    echostr: str = Query(""),
) -> str:
    """微信配置服务器地址时来验证一次：验签通过就原样返回 echostr。"""
    settings = get_settings()
    if not check_signature(settings.wechat_token, timestamp, nonce, signature):
        log.warning("微信验签失败，检查 Token 配置")
        return "invalid signature"
    log.info("微信服务器验证通过")
    return echostr


@router.post("/wechat")
async def receive(request: Request) -> Response:
    settings = get_settings()
    params = request.query_params
    body = await request.body()

    # POST 也要验签：不然任何人都能伪造消息让你的 bot 干活
    if not check_signature(
        settings.wechat_token,
        params.get("timestamp", ""),
        params.get("nonce", ""),
        params.get("signature", ""),
    ):
        log.warning("收到未通过验签的推送，已忽略")
        return PlainTextResponse("invalid signature", status_code=403)

    msg = parse_message(body)
    if not msg or msg.get("MsgType") != "text":
        # 图片、语音、事件等一律回 success，避免微信反复重试
        return PlainTextResponse("success")

    msg_id = msg.get("MsgId", "")
    if msg_id and is_duplicate(msg_id):
        log.info("重复推送，跳过：%s", msg_id)
        return PlainTextResponse("success")

    openid = msg.get("FromUserName", "")
    question = msg.get("Content", "").strip()
    if not openid or not question:
        return PlainTextResponse("success")

    log.info("收到微信消息 [%s] %s", openid[-6:], question[:60])

    # 工具要知道"替谁做事"（设提醒），调度器也要靠 target 找到推送通道
    context.set_context(
        context.IMContext(user_key=f"wechat:{openid}", target="wechat", target_id=openid)
    )
    context.reset_images()

    if settings.wechat_app_secret:
        # 有客服消息权限：先占住 5 秒窗口，答案异步推
        asyncio.create_task(_answer_and_push(openid, question))
        ack = settings.wechat_ack_text
        return Response(
            content=build_text_reply(openid, msg.get("ToUserName", ""), ack),
            media_type="application/xml",
        )

    # 没有客服权限：只能同步答，尽量压短
    answer = await ask(question, [])
    return Response(
        content=build_text_reply(openid, msg.get("ToUserName", ""), answer),
        media_type="application/xml",
    )


async def _answer_and_push(openid: str, question: str) -> None:
    key = f"wechat:{openid}"
    try:
        answer = await ask(question, _memory.get(key))
        _memory.append(key, "user", question)
        _memory.append(key, "assistant", answer)

        if not await push_text(openid, answer):
            log.error("答案没推出去（可能没开通客服消息权限）：%s", openid[-6:])

        # 工具生成的图片单独发；发不出来就退回文字链接
        for url in context.take_images():
            if await push_image(openid, url):
                continue
            log.warning("微信发图失败，改发链接：%s", url[:80])
            await push_text(openid, f"图片发不出来，点这里看：{url}")
    except Exception:
        log.exception("微信异步回答失败")
