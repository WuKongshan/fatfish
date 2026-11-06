"""QQ 桥接的纯逻辑：文本清洗、分段、会话记忆。

协议部分（WebSocket/HTTP）不在这里测——那要靠真实 AppID，
单测里 mock 掉只会测到 mock 本身。
"""

from datetime import UTC, datetime, timedelta

import pytest

from fatfish.deepseek import ChatMemory
from fatfish.qq import MAX_REPLIES, clean_text, split_chunks


class TestCleanText:
    def test_strips_at_prefix(self):
        assert clean_text("<@!123456> 你好") == "你好"

    def test_multiple_prefixes(self):
        assert clean_text("<@!1> <@!2>  问个事") == "问个事"

    def test_keeps_plain_text(self):
        assert clean_text("  纯文本  ") == "纯文本"

    def test_empty(self):
        assert clean_text("") == ""
        assert clean_text(None) == ""


class TestSplitChunks:
    def test_short_text_single_chunk(self):
        assert split_chunks("短回答") == ["短回答"]

    def test_long_text_is_split(self):
        chunks = split_chunks("a" * 2000, size=800)
        assert len(chunks) == 3
        assert all(len(c) <= 800 for c in chunks)

    def test_capped_at_five(self):
        # 官方限制同一条消息最多被动回复 5 次，超出的部分不发
        chunks = split_chunks("a" * 9000, size=800)
        assert len(chunks) == MAX_REPLIES


class TestChatMemory:
    def test_keeps_order(self):
        m = ChatMemory(turns=4)
        m.append("u1", "user", "第一句")
        m.append("u1", "assistant", "第一答")
        assert [x["content"] for x in m.get("u1")] == ["第一句", "第一答"]

    def test_trims_to_turns(self):
        m = ChatMemory(turns=2)
        for i in range(5):
            m.append("u1", "user", f"第{i}句")
        assert len(m.get("u1")) == 2
        assert m.get("u1")[-1]["content"] == "第4句"

    def test_users_are_isolated(self):
        m = ChatMemory(turns=4)
        m.append("a", "user", "A 的话")
        m.append("b", "user", "B 的话")
        assert [x["content"] for x in m.get("a")] == ["A 的话"]

    def test_evicts_oldest_user_when_full(self):
        m = ChatMemory(turns=2, max_users=2)
        m.append("a", "user", "1")
        m.append("b", "user", "2")
        m.append("c", "user", "3")  # a 被淘汰
        assert m.get("a") == []
        assert [x["content"] for x in m.get("c")] == ["3"]

    def test_clear(self):
        m = ChatMemory(turns=2)
        m.append("a", "user", "1")
        m.clear("a")
        assert m.get("a") == []


class TestWeather:
    def test_describe_known_code(self):
        from fatfish.tools import describe

        assert describe(0) == "晴"
        assert describe(61) == "小雨"

    def test_describe_unknown(self):
        from fatfish.tools import describe

        assert describe(None) == "未知"
        assert "999" in describe(999)

    def test_format_includes_numbers(self):
        from fatfish.tools import format_weather

        text = format_weather(
            {"name": "杭州", "admin1": "浙江"},
            {
                "current": {
                    "temperature_2m": 15.1,
                    "relative_humidity_2m": 81,
                    "weather_code": 2,
                    "wind_speed_10m": 4.8,
                },
                "daily": {
                    "temperature_2m_max": [20.9],
                    "temperature_2m_min": [14.3],
                    "precipitation_probability_max": [11],
                },
            },
        )
        assert "杭州（浙江）" in text
        assert "多云" in text
        assert "15.1°C" in text
        assert "14.3~20.9°C" in text
        assert "11%" in text

    def test_format_tolerates_missing_fields(self):
        from fatfish.tools import format_weather

        # 接口偶尔少字段，不能因此抛异常让整轮对话挂掉
        assert format_weather({"name": "某地"}, {}) == "某地当前：未知"


async def test_run_tool_unknown():
    from fatfish.tools import run_tool

    assert "没有叫" in await run_tool("delete_everything", {})


async def test_run_tool_bad_args():
    from fatfish.tools import run_tool

    # 模型传了多余字段时不能崩，给一句可继续对话的话
    assert "参数不对" in await run_tool("get_weather", {"city": "杭州", "when": "明天"})


class TestReminders:
    """提醒的校验与调度：不联网，全是本地状态。"""

    @pytest.fixture
    def store(self, tmp_path):
        from fatfish import context, tools
        from fatfish.store import ReminderStore

        s = ReminderStore(str(tmp_path / "bot.db"))
        tools.init_store(s)
        context.set_context(context.IMContext("users:abc", "users", "abc"))
        return s

    async def test_past_time_rejected(self, store):
        from fatfish.tools import set_reminder

        msg = await set_reminder("喝水", "2020-01-01T10:00:00+08:00")
        assert "已经过去" in msg
        assert store.pending_of("users:abc") == []

    async def test_too_far_rejected(self, store):
        from fatfish.tools import set_reminder

        msg = await set_reminder("喝水", "2030-01-01T10:00:00+08:00")
        assert "太远" in msg

    async def test_bad_format_rejected(self, store):
        from fatfish.tools import set_reminder

        assert "没解析出来" in await set_reminder("喝水", "明天下午")

    async def test_naive_time_uses_local_tz(self):
        from fatfish.tools import parse_due

        # 不带时区不能按 UTC 猜，否则提醒会差 8 小时
        assert parse_due("2026-11-05T10:00:00").utcoffset().total_seconds() == 8 * 3600

    async def test_valid_reminder_saved(self, store):
        from fatfish.tools import set_reminder

        due = (datetime.now(UTC) + timedelta(minutes=30)).isoformat()
        assert "记住了" in await set_reminder("喝水", due)
        assert len(store.pending_of("users:abc")) == 1

    async def test_scheduler_sends_and_marks(self, store):
        from fatfish import scheduler

        due = (datetime.now(UTC) - timedelta(seconds=5)).isoformat()
        store.add(
            user_key="users:abc", target="users", target_id="abc", content="喝水", due_at=parse(due)
        )
        bot = FakeBot(ok=True)

        assert await scheduler.fire_due(bot, store) == 1
        assert bot.sent and "喝水" in bot.sent[0]
        assert store.due() == []  # 已标记

    async def test_scheduler_retries_on_failure(self, store):
        from fatfish import scheduler

        due = (datetime.now(UTC) - timedelta(seconds=5)).isoformat()
        store.add(
            user_key="users:abc", target="users", target_id="abc", content="喝水", due_at=parse(due)
        )
        bot = FakeBot(ok=False)

        # 发失败不能标记已发，否则一次抖动就永久丢提醒
        assert await scheduler.fire_due(bot, store) == 0
        assert len(store.due()) == 1

    async def test_stale_reminder_skipped_without_sending(self, store):
        from fatfish import scheduler

        due = (datetime.now(UTC) - timedelta(hours=48)).isoformat()
        store.add(
            user_key="users:abc", target="users", target_id="abc", content="喝水", due_at=parse(due)
        )
        bot = FakeBot(ok=True)

        assert await scheduler.fire_due(bot, store) == 0
        assert bot.sent == []  # 过期太久不补发
        assert store.due() == []  # 但标记掉，别每次都扫


class FakeBot:
    def __init__(self, ok: bool) -> None:
        self.ok = ok
        self.sent: list[str] = []

    async def send_proactive(self, text: str, *, target: str, target_id: str) -> bool:
        self.sent.append(text)
        return self.ok


def parse(raw: str):
    from datetime import datetime as dt

    return dt.fromisoformat(raw)


class TestImageProvider:
    """生图适配层：换家只改配置，不动代码。"""

    def test_zhipu_defaults(self, monkeypatch):
        from fatfish.config import get_settings
        from fatfish.tools import image_conf

        s = get_settings()
        monkeypatch.setattr(s, "image_provider", "zhipu")
        monkeypatch.setattr(s, "image_base_url", "")
        monkeypatch.setattr(s, "image_model", "")

        conf = image_conf()
        assert conf["url"] == "https://open.bigmodel.cn/api/paas/v4/images/generations"
        assert conf["model"] == "cogview-4"
        # 智谱用的字段名是 size，硅基是 image_size，搞错直接 400
        assert conf["size_field"] == "size"

    def test_siliconflow_defaults(self, monkeypatch):
        from fatfish.config import get_settings
        from fatfish.tools import image_conf

        s = get_settings()
        monkeypatch.setattr(s, "image_provider", "siliconflow")
        monkeypatch.setattr(s, "image_base_url", "")
        monkeypatch.setattr(s, "image_model", "")

        conf = image_conf()
        assert "siliconflow.cn" in conf["url"]
        assert conf["size_field"] == "image_size"

    def test_env_overrides_preset(self, monkeypatch):
        from fatfish.config import get_settings
        from fatfish.tools import image_conf

        s = get_settings()
        monkeypatch.setattr(s, "image_provider", "zhipu")
        monkeypatch.setattr(s, "image_model", "glm-image")
        monkeypatch.setattr(s, "image_base_url", "https://example.com/v4/")

        conf = image_conf()
        assert conf["model"] == "glm-image"
        # 末尾斜杠要归一，否则拼出双斜杠
        assert conf["url"] == "https://example.com/v4/images/generations"

    def test_extract_zhipu_shape(self):
        from fatfish.tools import extract_image_url

        assert extract_image_url({"data": [{"url": "https://a/1.png"}]}) == "https://a/1.png"

    def test_extract_siliconflow_shape(self):
        from fatfish.tools import extract_image_url

        assert extract_image_url({"images": [{"url": "https://b/2.png"}]}) == "https://b/2.png"

    def test_extract_garbage(self):
        from fatfish.tools import extract_image_url

        assert extract_image_url({"error": "quota"}) is None
        assert extract_image_url({"data": []}) is None


def test_ark_requires_explicit_model(monkeypatch):
    """火山方舟的模型 ID 每个账号不同，不能瞎给默认值。"""
    from fatfish.config import get_settings
    from fatfish.tools import image_conf

    s = get_settings()
    monkeypatch.setattr(s, "image_provider", "ark")
    monkeypatch.setattr(s, "image_model", "")

    conf = image_conf()
    assert conf["model"] == ""
    assert "ark.cn-beijing.volces.com" in conf["url"]


class TestFinalText:
    """模型哑火时不能把 null 发给用户——这是线上真实出现过的。"""

    def test_normal_answer_passthrough(self):
        from fatfish.deepseek import final_text

        assert final_text("好的", []) == "好的"

    def test_null_string_falls_back_to_tool_output(self):
        from fatfish.deepseek import final_text

        # 用户实际看到的 bug：模型返回字面量 "null"
        assert final_text("null", ["记住了：11月05日 21:26 提醒你喝水"]) == (
            "记住了：11月05日 21:26 提醒你喝水"
        )

    def test_none_falls_back_to_tool_output(self):
        from fatfish.deepseek import final_text

        assert final_text(None, ["图片已生成"]) == "图片已生成"

    def test_empty_without_tools_uses_generic(self):
        from fatfish.deepseek import final_text

        assert final_text("", []) == "我没想出怎么回，换个说法？"

    def test_last_tool_output_wins(self):
        from fatfish.deepseek import final_text

        assert final_text("null", ["第一条", "第二条"]) == "第二条"

    def test_assistant_message_is_trimmed(self):
        from fatfish.deepseek import _as_assistant

        cleaned = _as_assistant(
            {
                "role": "assistant",
                "content": None,
                "reasoning_content": "思考过程不该回填",
                "tool_calls": [{"id": "1", "function": {"name": "x", "arguments": "{}"}}],
            }
        )
        # content 必须是字符串，多余字段要丢掉
        assert cleaned["content"] == ""
        assert "reasoning_content" not in cleaned
        assert cleaned["tool_calls"][0]["id"] == "1"

    def test_assistant_without_tool_calls(self):
        from fatfish.deepseek import _as_assistant

        cleaned = _as_assistant({"role": "assistant", "content": "hi"})
        assert cleaned == {"role": "assistant", "content": "hi"}


class TestTimeAwareness:
    """模型不知道今天是几号，必须由我们告诉它——否则算不出「一分钟后」。"""

    def test_prompt_carries_current_time(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo

        from fatfish.config import get_settings

        now = datetime(2026, 11, 5, 20, 29, tzinfo=ZoneInfo("Asia/Shanghai"))
        prompt = get_settings().prompt(now)

        assert "2026-11-05 20:29" in prompt
        assert "周四" in prompt
        # 明确禁止向用户索要当前时间
        assert "不要向用户索要当前时间" in prompt

    def test_prompt_without_time_still_works(self):
        from fatfish.config import get_settings

        # 不传时间时不该崩，也不该在末尾拼出半截时间戳
        # （基础提示里本来就有"看下面的【当前时间】"这句指令，所以只查最后一行）
        assert not get_settings().prompt().splitlines()[-1].startswith("【当前时间】")

    async def test_time_tool(self):
        from fatfish.tools import get_current_time

        text = await get_current_time()
        assert text.startswith("现在是")
        assert "UTC+0800" in text

    def test_format_now_fixed_moment(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo

        from fatfish.tools import format_now

        moment = datetime(2026, 1, 1, 9, 5, tzinfo=ZoneInfo("Asia/Shanghai"))
        assert "2026-01-01 09:05" in format_now(moment)
        assert "周四" in format_now(moment)  # 2026-01-01 是周四


class TestWechat:
    """微信公众号回调：验签、解析、去重、回复格式。都是纯函数，不联网。"""

    def test_signature_ok(self):
        import hashlib

        from fatfish.wechat import check_signature

        token, ts, nonce = "dafeiyu", "1730000000", "abc123"
        sig = hashlib.sha1("".join(sorted([token, ts, nonce])).encode()).hexdigest()

        assert check_signature(token, ts, nonce, sig) is True

    def test_signature_rejects_wrong_token(self):
        import hashlib

        from fatfish.wechat import check_signature

        sig = hashlib.sha1("".join(sorted(["t1", "1", "n"])).encode()).hexdigest()
        # token 不同就必须判失败，否则谁都能伪造消息
        assert check_signature("t2", "1", "n", sig) is False

    def test_parse_text_message(self):
        from fatfish.wechat import parse_message

        xml = """<xml>
            <ToUserName><![CDATA[gh_abc]]></ToUserName>
            <FromUserName><![CDATA[oUser123]]></FromUserName>
            <CreateTime>1730000000</CreateTime>
            <MsgType><![CDATA[text]]></MsgType>
            <Content><![CDATA[你好]]></Content>
            <MsgId>1234567890</MsgId>
        </xml>""".encode()

        msg = parse_message(xml)
        assert msg["FromUserName"] == "oUser123"
        assert msg["Content"] == "你好"
        assert msg["MsgType"] == "text"

    def test_parse_broken_xml_returns_empty(self):
        from fatfish.wechat import parse_message

        assert parse_message(b"not xml at all") == {}

    def test_reply_is_valid_xml(self):
        import xml.etree.ElementTree as ET

        from fatfish.wechat import build_text_reply

        text = build_text_reply("oUser", "gh_abc", "收到")
        root = ET.fromstring(text)
        assert root.findtext("ToUserName") == "oUser"
        assert root.findtext("Content") == "收到"

    def test_duplicate_msgid_detected(self):
        from fatfish.wechat import is_duplicate

        assert is_duplicate("msg-1") is False
        # 微信 5 秒没响应会重发同一条，必须认出来
        assert is_duplicate("msg-1") is True
        assert is_duplicate("msg-2") is False


async def test_reminder_text_uses_configured_timezone(tmp_path):
    """通知里的时间必须是用户看到的本地时间，不能是容器 UTC。

    线上真实 bug：用户设定 11:44，通知里写"你在 03:44 设的"。
    """
    from zoneinfo import ZoneInfo

    from fatfish import context, scheduler, tools
    from fatfish.store import ReminderStore

    store = ReminderStore(str(tmp_path / "bot.db"))
    tools.init_store(store)
    context.set_context(context.IMContext("users:abc", "users", "abc"))

    due = datetime.now(UTC) - timedelta(seconds=5)
    store.add(
        user_key="users:abc",
        target="users",
        target_id="abc",
        content="喝水",
        due_at=due,
    )
    bot = FakeBot(ok=True)
    await scheduler.fire_due(bot, store)

    expected = due.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%H:%M")
    assert expected in bot.sent[0]
    # 反向断言：UTC 时间不该出现在通知里
    assert due.strftime("%H:%M") not in bot.sent[0].replace(expected, "")


class TestPushRouter:
    """调度器只认 send_proactive，具体走哪个渠道由 router 决定。"""

    async def test_routes_wechat(self, monkeypatch):
        from fatfish.push import PushRouter

        calls: list[tuple] = []

        async def fake_push_text(openid: str, content: str) -> bool:
            calls.append(("wechat", openid, content))
            return True

        monkeypatch.setattr("fatfish.wechat.push_text", fake_push_text)
        router = PushRouter(_FakeQQBot())
        assert await router.send_proactive("喝水", target="wechat", target_id="oX") is True
        assert calls == [("wechat", "oX", "喝水")]

    async def test_routes_qq(self):
        from fatfish.push import PushRouter

        bot = _FakeQQBot()
        router = PushRouter(bot)
        assert await router.send_proactive("喝水", target="users", target_id="u1") is True
        assert bot.sent == [("users", "u1", "喝水")]


class _FakeQQBot:
    def __init__(self) -> None:
        self.sent: list[tuple] = []

    async def send_proactive(self, text: str, *, target: str, target_id: str) -> bool:
        self.sent.append((target, target_id, text))
        return True


class TestSearchQuality:
    """搜索质量：口语问句直接丢给搜索引擎等于白搜。"""

    def test_tool_schema_demands_keywords(self):
        from fatfish.tools import TOOLS

        search = next(t for t in TOOLS if t["function"]["name"] == "web_search")
        desc = search["function"]["description"]
        # 描述里必须明确要求关键词组合，否则模型会原样转发用户的话
        assert "关键词" in desc
        assert "别用问句" in search["function"]["parameters"]["properties"]["query"]["description"]

    def test_freshness_enum_limited(self):
        from fatfish.tools import TOOLS

        search = next(t for t in TOOLS if t["function"]["name"] == "web_search")
        enum = search["function"]["parameters"]["properties"]["freshness"]["enum"]
        assert enum == ["noLimit", "oneDay", "oneWeek", "oneMonth"]

    def test_snippet_not_truncated_too_short(self):
        from fatfish.tools import SNIPPET_CHARS, format_search

        text = format_search([{"title": "t", "url": "u", "snippet": "字" * 800}])
        # 摘要太短会丢掉新闻里的关键信息，至少留 500 字
        assert SNIPPET_CHARS >= 500
        assert len(text) > 600
