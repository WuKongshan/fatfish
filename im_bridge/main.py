"""QQ 机器人入口。

    python -m src.im_bridge.main

必需配置：QQ_APP_ID / QQ_APP_SECRET / DEEPSEEK_API_KEY。
可选：IMAGE_API_KEY（生图）、WEB_SEARCH_PROVIDER + WEB_SEARCH_API_KEY（联网搜索）。
"""

import asyncio
import logging
import sys

import uvicorn
from fastapi import FastAPI

from im_bridge import scheduler, tools
from im_bridge.config import get_settings
from im_bridge.deepseek import ChatMemory
from im_bridge.push import PushRouter
from im_bridge.qq import QQBot
from im_bridge.store import ReminderStore
from im_bridge.wechat import router as wechat_router


async def serve_http() -> None:
    """微信公众号的回调服务。没配 WECHAT_TOKEN 时完全不启动，不占 80 端口。"""
    settings = get_settings()
    app = FastAPI(title="im-bridge", docs_url=None, redoc_url=None)
    app.include_router(wechat_router)
    # access_log=False：80 端口暴露在公网，每天有大量扫描器请求 / 和 /favicon.ico，
    # 访问日志会把真正的业务日志冲掉。微信的请求我们自己打了 "收到微信消息"，
    # 排查时看那行就够
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="0.0.0.0",
            port=settings.wechat_port,
            log_level="info",
            access_log=False,
        )
    )
    await server.serve()


async def serve() -> None:
    log = logging.getLogger("im_bridge")
    settings = get_settings()

    store = ReminderStore(settings.reminder_db)
    tools.init_store(store)

    bot = QQBot(ChatMemory(settings.history_turns))
    # 调度器通过 router 发消息，QQ 和微信走不同通道
    router = PushRouter(bot)
    stop = asyncio.Event()

    # 调度和长连接并行跑：长连接阻塞在 ws 上，调度必须独立成任务
    reminder_task = asyncio.create_task(scheduler.run_forever(router, store, stop))

    log.info(
        "%s 启动（生图 %s，联网搜索 %s）",
        settings.bot_name,
        "已配置" if settings.image_api_key else "未配置",
        settings.web_search_provider if settings.web_search_api_key else "未配置",
    )
    tasks: list[asyncio.Task] = []
    if settings.qq_app_id and settings.qq_app_secret:
        tasks.append(asyncio.create_task(bot.run_forever()))
    if settings.wechat_token:
        log.info("微信回调已启动，监听 :%s/wechat", settings.wechat_port)
        tasks.append(asyncio.create_task(serve_http()))

    try:
        await asyncio.gather(*tasks)
    finally:
        stop.set()
        reminder_task.cancel()
        for task in tasks:
            task.cancel()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    log = logging.getLogger("im_bridge")

    settings = get_settings()

    # DeepSeek 是必须的；渠道至少配一个（QQ 或微信），否则起来也没用
    missing: list[str] = []
    if not settings.deepseek_api_key:
        missing.append("DEEPSEEK_API_KEY")

    has_qq = bool(settings.qq_app_id and settings.qq_app_secret)
    has_wechat = bool(settings.wechat_token and settings.wechat_app_id)
    if not has_qq and not has_wechat:
        missing.append("至少一个渠道（QQ_APP_ID+QQ_APP_SECRET 或 WECHAT_TOKEN+WECHAT_APP_ID）")

    if missing:
        log.error("缺少配置：%s", "；".join(missing))
        sys.exit(1)

    try:
        asyncio.run(serve())
    except KeyboardInterrupt:
        log.info("手动停止")


if __name__ == "__main__":
    main()
