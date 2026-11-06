"""提醒调度：到点主动发消息。

⚠️ 前提：QQ 的「主动消息」需要平台开通权限，且官方有配额限制。
没开通时这里每次发送都会 403，日志里能看到，用户收不到——
这不是代码能绕过的，只能去开放平台申请。

失败处理：**标记 sent=1 之前先确认发送成功**。
发不出去就留着重试，否则一次网络抖动就永久丢一条提醒。
"""

import asyncio
import logging
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from im_bridge.config import get_settings
from im_bridge.store import ReminderStore
from im_bridge.tools import parse_due

log = logging.getLogger(__name__)

# 超过这个时长还没发出去的（比如进程挂了三天），只标记不补发，
# 否则一开机炸出一堆过期提醒
STALE_HOURS = 12


async def fire_due(bot, store: ReminderStore) -> int:
    """扫一次到期提醒，返回成功发出的条数。"""
    rows = store.due()
    if not rows:
        return 0

    sent = 0
    now = datetime.now(UTC)
    for row in rows:
        due = parse_due(row["due_at"])
        if (now - due).total_seconds() > STALE_HOURS * 3600:
            store.mark_sent(row["id"])
            log.warning("提醒 %s 已过期超过 %s 小时，跳过", row["id"], STALE_HOURS)
            continue

        # 不能写 due.astimezone()：不带参数会转成系统时区，
        # 而容器默认是 UTC，通知里就会比用户设定的时间早 8 小时
        tz = ZoneInfo(get_settings().bot_timezone)
        local = due.astimezone(tz).strftime("%m月%d日 %H:%M")
        text = f"⏰ 提醒你：{row['content']}\n（你在 {local} 设的）"
        ok = await bot.send_proactive(text, target=row["target"], target_id=row["target_id"])
        if ok:
            store.mark_sent(row["id"])
            sent += 1
            log.info("提醒已发出：%s", row["content"])
        else:
            # 不发成功就不标记，下一轮重试。连续失败会在日志里堆出来，方便发现权限问题
            log.error("提醒发送失败（可能是没开通主动消息权限）：%s", row["content"])

    return sent


async def run_forever(bot, store: ReminderStore, stop: asyncio.Event) -> None:
    interval = max(10, get_settings().reminder_scan_seconds)
    log.info("提醒调度已启动，每 %s 秒扫描一次", interval)
    while not stop.is_set():
        try:
            await fire_due(bot, store)
        except Exception:
            log.exception("提醒调度本轮失败")
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except TimeoutError:
            continue
