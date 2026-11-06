"""当前会话的上下文。工具函数需要知道「谁在问」，但不能让模型传这些参数。

用 contextvars 而不是全局变量：将来一个进程同时服务多个事件循环/任务时，
全局变量会串号。
"""

import contextvars
from dataclasses import dataclass


@dataclass(slots=True)
class IMContext:
    user_key: str  # 记忆/提醒的归属，形如 users:openid
    target: str  # users / groups
    target_id: str


_current: contextvars.ContextVar[IMContext | None] = contextvars.ContextVar(
    "im_context", default=None
)

# 本轮要额外发送的图片 URL（工具产生，回复完由 bot 补发）。
# default 必须是 None：可变对象做默认值会在多个上下文之间共享同一个 list
_images: contextvars.ContextVar[list[str] | None] = contextvars.ContextVar(
    "im_images", default=None
)


def set_context(ctx: IMContext) -> None:
    _current.set(ctx)


def get_context() -> IMContext | None:
    return _current.get()


def reset_images() -> None:
    _images.set([])


def add_image(url: str) -> None:
    _images.set([*(_images.get() or []), url])


def take_images() -> list[str]:
    urls = list(_images.get() or [])
    _images.set([])
    return urls
