"""QQ 机器人的配置。

独立于主应用：这个服务只做「收消息 → 问 DeepSeek → 回消息」，
不碰数据库、不碰 RAG，所以单独一套 settings。
"""

from datetime import datetime
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class BotSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    # ---- QQ 开放平台（q.qq.com → 你的机器人 → 开发设置）----
    qq_app_id: str = ""
    qq_app_secret: str = ""
    # 1 << 25 覆盖「群 @」和「单聊」两类事件。
    # 要收频道消息再按需加位，具体值见开放平台文档的 intents 表
    qq_intents: int = 1 << 25

    # ---- DeepSeek ----
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    bot_model: str = "deepseek-chat"
    bot_max_tokens: int = 800
    bot_temperature: float = 1.3

    # ---- 微信公众号 ----
    # 后台「接口配置信息」里的 Token，自己定，和微信页面填一致
    wechat_token: str = ""
    wechat_app_id: str = ""
    # 有 AppSecret 才能用客服消息（异步推答案），没有就只能同步回
    wechat_app_secret: str = ""
    # 5 秒窗口里先回这句占位
    wechat_ack_text: str = "收到，正在想…"
    wechat_port: int = 80

    # ---- 生图 ----
    # siliconflow（Kwai-Kolors）/ zhipu（CogView）。换家只改这里和 key
    image_provider: str = "zhipu"
    image_api_key: str = ""
    # 留空则用 provider 预设的地址和模型
    image_base_url: str = ""
    image_model: str = ""
    image_size: str = "1024x1024"

    # ---- 联网搜索：off / tavily / bocha ----
    web_search_provider: str = "off"
    web_search_api_key: str = ""

    # ---- 提醒 ----
    bot_timezone: str = "Asia/Shanghai"
    reminder_db: str = "data/bot.db"
    # 调度扫描间隔：30 秒对提醒足够，再密就是浪费
    reminder_scan_seconds: int = 30

    # ---- 行为 ----
    bot_name: str = "大肥鱼"
    # 单条消息里保留几条历史（一问一答算两条）。QQ 官方限制被动回复最多 5 条，
    # 这里取 10 是给"记忆"用的，不影响回复次数
    history_turns: int = 10
    system_prompt: str = ""

    def prompt(self, now: datetime | None = None) -> str:
        """系统提示。每次都带上当前时间——模型自己不知道今天是几号。

        不注入的话，用户说「一分钟后提醒我」，模型会答「我拿不到当前时间」，
        这是真实踩过的坑（见 docs/23 第 7 节）。
        """
        base = self.system_prompt or (
            f"你叫{self.bot_name}，是用户在 QQ 上的私人助手。"
            "回答简洁、口语化，能一句话说清就别写三段。\n"
            "遇到天气、气温、要不要带伞这类问题，必须用 get_weather 工具查，不要凭印象编。"
            "不知道对方在哪个城市就先问一句，不要猜。\n"
            "涉及时间（提醒、倒计时、「多久后」）时，直接看下面的【当前时间】自己算，"
            "不要向用户索要当前时间。\n"
            "不确定的事情直说不知道，不要编。"
        )
        if now is None:
            return base
        weekday = "一二三四五六日"[now.weekday()]
        return f"{base}\n\n【当前时间】{now:%Y-%m-%d %H:%M} 周{weekday}（{self.bot_timezone}）"


@lru_cache
def get_settings() -> BotSettings:
    return BotSettings()
