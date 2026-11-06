"""按渠道分发主动消息。

调度器只知道"要提醒谁"，不该关心那个人在 QQ 还是在微信。
把分发逻辑收在这里，加新渠道时只改这一处。
"""

import logging

log = logging.getLogger(__name__)

WECHAT = "wechat"


class PushRouter:
    """调度器拿到的就是这个对象——它只需要 send_proactive。"""

    def __init__(self, bot) -> None:
        self.bot = bot

    async def send_proactive(self, text: str, *, target: str, target_id: str) -> bool:
        if target == WECHAT:
            # 延迟导入：wechat 模块依赖 fastapi，QQ-only 部署时不必加载
            from fatfish.wechat import push_text

            return await push_text(target_id, text)
        return await self.bot.send_proactive(text, target=target, target_id=target_id)
