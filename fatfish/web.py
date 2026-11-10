"""Single-owner web pet interface."""

import hashlib
import hmac
import logging
import time
from collections import defaultdict, deque
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from fatfish import context
from fatfish.config import get_settings
from fatfish.deepseek import ChatMemory, ask
from fatfish.store import ReminderStore

log = logging.getLogger(__name__)
router = APIRouter()
memory = ChatMemory(get_settings().history_turns)
_store: ReminderStore | None = None
_requests: dict[str, deque[float]] = defaultdict(deque)
_PAGE = Path(__file__).with_name("static") / "pet.html"


def init_web(store: ReminderStore) -> None:
    global _store
    _store = store


def _signature(token: str) -> str:
    secret = get_settings().web_session_secret or get_settings().web_password
    return hmac.new(secret.encode(), token.encode(), hashlib.sha256).hexdigest()


def _valid_session(request: Request) -> bool:
    token = request.cookies.get("pet_session", "")
    supplied = request.cookies.get("pet_signature", "")
    return bool(token and supplied and hmac.compare_digest(supplied, _signature(token)))


def _require_session(request: Request) -> None:
    if not get_settings().web_password or not _valid_session(request):
        raise HTTPException(status_code=401, detail="请先登录")


def _rate_limit(request: Request, limit: int = 10, window: int = 60) -> None:
    now = time.monotonic()
    key = request.client.host if request.client else "unknown"
    bucket = _requests[key]
    while bucket and bucket[0] < now - window:
        bucket.popleft()
    if len(bucket) >= limit:
        raise HTTPException(status_code=429, detail="请求太频繁，请稍后再试")
    bucket.append(now)


class LoginBody(BaseModel):
    password: str = Field(min_length=1, max_length=256)


class ChatBody(BaseModel):
    message: str = Field(min_length=1, max_length=2000)


@router.get("/pet")
async def pet_page():
    if not get_settings().web_password:
        raise HTTPException(status_code=404, detail="网页桌宠未启用")
    return FileResponse(_PAGE)


@router.post("/api/pet/login")
async def login(body: LoginBody, request: Request, response: Response):
    settings = get_settings()
    if not settings.web_password:
        raise HTTPException(status_code=404, detail="网页桌宠未启用")
    _rate_limit(request, limit=5, window=300)
    if not hmac.compare_digest(body.password, settings.web_password):
        raise HTTPException(status_code=401, detail="口令不正确")
    token = hmac.new(
        settings.web_password.encode(), b"fatfish-web-owner", hashlib.sha256
    ).hexdigest()
    cookie_args = {"httponly": True, "samesite": "strict", "max_age": 86400 * 14}
    response.set_cookie("pet_session", token, **cookie_args)
    response.set_cookie("pet_signature", _signature(token), **cookie_args)
    return {"ok": True}


@router.post("/api/pet/logout")
async def logout(response: Response):
    response.delete_cookie("pet_session")
    response.delete_cookie("pet_signature")
    return {"ok": True}


@router.get("/api/pet/session")
async def session(request: Request):
    _require_session(request)
    return {"ok": True}


@router.post("/api/pet/chat")
async def chat(body: ChatBody, request: Request):
    _require_session(request)
    _rate_limit(request)
    question = body.message.strip()
    if not question:
        raise HTTPException(status_code=422, detail="消息不能为空")
    key = "web:owner"
    context.set_context(context.IMContext(user_key=key, target="web", target_id="owner"))
    context.reset_images()
    answer = await ask(question, memory.get(key))
    memory.append(key, "user", question)
    memory.append(key, "assistant", answer)
    return {"answer": answer, "images": context.take_images()}


@router.get("/api/pet/reminders")
async def reminders(request: Request):
    _require_session(request)
    if _store is None:
        return {"reminders": []}
    rows = _store.pending_of("web:owner")
    return {"reminders": [{"content": row["content"], "due_at": row["due_at"]} for row in rows]}
