"""Thin Bot API client on one shared aiohttp session.

The token only lives in the request URL, which is never logged: errors carry the method name
and Telegram's description, and exception text from aiohttp is dropped.
"""

import asyncio
import html
import json
import logging
import random
import re
from collections.abc import Awaitable, Callable
from typing import Any

import aiohttp

from cqb.telegram.models import (
    BotInfo, Button, CallbackQuery, ChatInfo, ForumTopic, MemberInfo, Message, SentMessage,
    Update,
)

log = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org"
ALLOWED_UPDATES = ["message", "callback_query"]
MAX_ATTEMPTS = 5
MAX_RETRY_AFTER = 60.0
BACKOFF_BASE = 0.5
BACKOFF_CAP = 10.0
_TAG = re.compile(r"</?[A-Za-z][^<>]*>")


class TelegramError(Exception):
    def __init__(self, method: str, code: int, description: str) -> None:
        super().__init__(f"{method}: {code} {description}")
        self.method = method
        self.code = code
        self.description = description


class RateLimited(TelegramError):
    def __init__(self, method: str, retry_after: float) -> None:
        super().__init__(method, 429, f"retry after {retry_after:g}s")
        self.retry_after = retry_after


class NetworkError(TelegramError):
    def __init__(self, method: str, kind: str) -> None:
        super().__init__(method, 0, f"network error ({kind})")


class Unauthorized(TelegramError):
    """Bad token (401) or the bot was removed (403)."""


class BadRequest(TelegramError):
    @property
    def not_modified(self) -> bool:
        d = self.description.lower()
        return "not modified" in d

    @property
    def gone(self) -> bool:
        """The message or topic does not exist (anymore)."""
        d = self.description.lower()
        return any(s in d for s in (
            "message to edit not found", "message to delete not found", "message thread not found",
            "topic_id_invalid", "topic_deleted", "message to pin not found",
        ))


class RichRejected(BadRequest):
    """Telegram refused a rich message: `unsupported` = the method does not exist (or is not allowed) for this bot or
    chat, so rich mode is off for good; otherwise only this message's markup was refused."""

    def __init__(self, method: str, code: int, description: str) -> None:
        super().__init__(method, code, description)
        d = description.lower()
        self.unsupported = code == 404 or (
            "tag" not in d and any(s in d for s in ("method", "not available", "not supported", "unsupported"))
        )


class BotApi:
    def __init__(
        self,
        token: str,
        *,
        base_url: str = API_BASE,
        session: aiohttp.ClientSession | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rng: Callable[[], float] = random.random,
    ) -> None:
        self._token = token
        self._base = base_url.rstrip("/")
        self._session = session
        self._own_session = session is None
        self._sleep = sleep
        self._rng = rng

    async def start(self) -> None:
        if self._session is None:
            self._session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=4))

    async def close(self) -> None:
        if self._session is not None and self._own_session:
            await self._session.close()
        self._session = None

    # --- Bot API methods -------------------------------------------------------------

    async def get_me(self) -> BotInfo:
        r = await self._call("getMe", {})
        return BotInfo(r["id"], r.get("username") or "", bool(r.get("can_read_all_group_messages")))

    async def get_chat(self, chat_id: int) -> ChatInfo:
        r = await self._call("getChat", {"chat_id": chat_id})
        return ChatInfo(r["id"], r.get("type") or "", bool(r.get("is_forum")))

    async def get_chat_member(self, chat_id: int, user_id: int) -> MemberInfo:
        r = await self._call("getChatMember", {"chat_id": chat_id, "user_id": user_id})
        return MemberInfo(
            r.get("status") or "", bool(r.get("can_manage_topics")), bool(r.get("can_pin_messages"))
        )

    async def get_updates(self, offset: int, timeout: int = 30, limit: int = 20) -> list[Update]:
        raw = await self._call(
            "getUpdates",
            {"offset": offset, "timeout": timeout, "limit": limit, "allowed_updates": ALLOWED_UPDATES},
            read_timeout=timeout + 15,
        )
        return [u for u in (_to_update(x) for x in raw) if u is not None]

    async def send_message(
        self, chat_id: int, text: str, *, thread_id: int | None = None,
        keyboard: tuple[tuple[Button, ...], ...] | None = None, parse_mode: str | None = None,
    ) -> SentMessage:
        """With parse_mode `HTML`, a text Telegram cannot parse is sent again as plain text (tags stripped),
        so a message is never lost to its own markup."""
        params: dict[str, Any] = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
        if thread_id is not None:
            params["message_thread_id"] = thread_id
        if keyboard:
            params["reply_markup"] = _markup(keyboard)
        if parse_mode:
            params["parse_mode"] = parse_mode
        try:
            r = await self._call("sendMessage", params, idempotent=False)
        except BadRequest as exc:
            if parse_mode != "HTML" or "can't parse entities" not in exc.description.lower():
                raise
            log.warning("sendMessage: HTML was rejected; sending the same text as plain text")
            params.pop("parse_mode")
            params["text"] = strip_html(text)
            r = await self._call("sendMessage", params, idempotent=False)
        return SentMessage(r["message_id"])

    async def edit_message_text(
        self, chat_id: int, message_id: int, text: str,
        keyboard: tuple[tuple[Button, ...], ...] | None = None,
    ) -> None:
        params: dict[str, Any] = {
            "chat_id": chat_id, "message_id": message_id, "text": text,
            "disable_web_page_preview": True,
            "reply_markup": _markup(keyboard or ()),
        }
        await self._call("editMessageText", params)

    async def send_rich_message(
        self, chat_id: int, html: str, *, thread_id: int | None = None,
        keyboard: tuple[tuple[Button, ...], ...] | None = None,
    ) -> SentMessage:
        """Bot API 10.1+: `html` is the rich HTML (in-text `<tg-button>`s since 10.3). A message Telegram refuses raises
        RichRejected; never retried after a lost response, like every send."""
        return await self._send_rich(chat_id, {"html": html}, thread_id, keyboard)

    async def send_rich_markdown(self, chat_id: int, markdown: str, *, thread_id: int | None = None) -> SentMessage:
        """The same with `rich_message.markdown`: the caller has sanitized the text (telegram/markdown.py)."""
        return await self._send_rich(chat_id, {"markdown": markdown}, thread_id, None)

    async def _send_rich(
        self, chat_id: int, rich: dict[str, str], thread_id: int | None,
        keyboard: tuple[tuple[Button, ...], ...] | None,
    ) -> SentMessage:
        params: dict[str, Any] = {"chat_id": chat_id, "rich_message": rich}
        if thread_id is not None:
            params["message_thread_id"] = thread_id
        if keyboard:
            params["reply_markup"] = _markup(keyboard)
        try:
            r = await self._call("sendRichMessage", params, idempotent=False)
        except BadRequest as exc:
            raise _rich_error(exc) from None
        return SentMessage(r["message_id"])

    async def edit_rich_message(
        self, chat_id: int, message_id: int, html: str, keyboard: tuple[tuple[Button, ...], ...] | None = None,
    ) -> None:
        params: dict[str, Any] = {"chat_id": chat_id, "message_id": message_id, "rich_message": {"html": html}}
        if keyboard:
            params["reply_markup"] = _markup(keyboard)
        try:
            await self._call("editMessageText", params)
        except BadRequest as exc:
            raise _rich_error(exc) from None

    async def pin_chat_message(self, chat_id: int, message_id: int) -> None:
        await self._call(
            "pinChatMessage",
            {"chat_id": chat_id, "message_id": message_id, "disable_notification": True},
        )

    async def delete_message(self, chat_id: int, message_id: int) -> None:
        await self._call("deleteMessage", {"chat_id": chat_id, "message_id": message_id})

    async def create_forum_topic(self, chat_id: int, name: str) -> ForumTopic:
        r = await self._call("createForumTopic", {"chat_id": chat_id, "name": name}, idempotent=False)
        return ForumTopic(r["message_thread_id"], r.get("name") or name)

    async def close_forum_topic(self, chat_id: int, thread_id: int) -> None:
        await self._call("closeForumTopic", {"chat_id": chat_id, "message_thread_id": thread_id})

    async def reopen_forum_topic(self, chat_id: int, thread_id: int) -> None:
        await self._call("reopenForumTopic", {"chat_id": chat_id, "message_thread_id": thread_id})

    async def answer_callback_query(self, query_id: str, text: str | None = None, alert: bool = False) -> None:
        params: dict[str, Any] = {"callback_query_id": query_id, "show_alert": alert}
        if text:
            params["text"] = text[:200]
        await self._call("answerCallbackQuery", params)

    async def send_chat_action(self, chat_id: int, action: str, *, thread_id: int | None = None) -> None:
        params: dict[str, Any] = {"chat_id": chat_id, "action": action}
        if thread_id is not None:
            params["message_thread_id"] = thread_id
        await self._call("sendChatAction", params)

    # --- transport -------------------------------------------------------------------

    async def _call(
        self, method: str, params: dict[str, Any], *, idempotent: bool = True,
        read_timeout: float = 30,
    ) -> Any:
        """429 is always safe to retry (nothing ran). Network failures after a request may have
        reached Telegram are retried only for idempotent methods, so no duplicate topics/messages."""
        for attempt in range(MAX_ATTEMPTS):
            last = attempt == MAX_ATTEMPTS - 1
            try:
                return await self._once(method, params, read_timeout)
            except RateLimited as exc:
                if last:
                    raise
                delay = min(exc.retry_after, MAX_RETRY_AFTER) + self._rng()
                log.warning("%s rate limited; waiting %.1fs", method, delay)
                await self._sleep(delay)
            except NetworkError as exc:
                if last or not (idempotent or exc.description.endswith("(connect)")):
                    raise
                await self._sleep(min(BACKOFF_CAP, BACKOFF_BASE * 2**attempt) * (0.5 + self._rng()))
        raise AssertionError("unreachable")

    async def _once(self, method: str, params: dict[str, Any], read_timeout: float) -> Any:
        assert self._session is not None, "BotApi.start() was not called"
        url = f"{self._base}/bot{self._token}/{method}"
        try:
            async with self._session.post(
                url, json=params, timeout=aiohttp.ClientTimeout(total=read_timeout)
            ) as resp:
                body = await resp.read()
                status = resp.status
        except aiohttp.ClientConnectorError:
            raise NetworkError(method, "connect") from None
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise NetworkError(method, type(exc).__name__) from None
        try:
            data = json.loads(body)
        except ValueError:
            data = {}
        if status == 200 and data.get("ok"):
            return data.get("result")
        description = str(data.get("description") or f"HTTP {status}")
        if status == 429:
            retry = (data.get("parameters") or {}).get("retry_after", 1)
            raise RateLimited(method, float(retry))
        if status >= 500:
            raise NetworkError(method, f"http {status}")
        if status in (401, 403):
            raise Unauthorized(method, status, description)
        raise BadRequest(method, status, description)


def _rich_error(exc: BadRequest) -> BadRequest:
    """"Not modified" and a gone message mean the same as on the classic path; any other refusal is RichRejected."""
    if exc.not_modified or exc.gone:
        return exc
    return RichRejected(exc.method, exc.code, exc.description)


def strip_html(text: str) -> str:
    """The plain text of a Telegram HTML message: tags removed, entities decoded."""
    return html.unescape(_TAG.sub("", text))


def _markup(keyboard: tuple[tuple[Button, ...], ...]) -> dict[str, Any]:
    rows = []
    for row in keyboard:
        rows.append([
            {"text": b.text, "url": b.url} if b.url else {"text": b.text, "callback_data": b.callback_data}
            for b in row
        ])
    return {"inline_keyboard": rows}


def _to_update(u: dict[str, Any]) -> Update | None:
    msg, cb = u.get("message"), u.get("callback_query")
    if msg is not None:
        return Update(u["update_id"], _to_message(msg), None)
    if cb is not None:
        origin = cb.get("message") or {}
        chat = origin.get("chat") or {}
        return Update(
            u["update_id"], None,
            CallbackQuery(
                query_id=str(cb["id"]), from_id=(cb.get("from") or {}).get("id", 0),
                chat_id=chat.get("id"), message_id=origin.get("message_id"), data=cb.get("data"),
            ),
        )
    return None


def _to_message(m: dict[str, Any]) -> Message:
    sender = m.get("from") or {}
    reply = m.get("reply_to_message") or {}
    thread = m.get("message_thread_id") if m.get("is_topic_message") else None
    forwarded = any(k in m for k in ("forward_origin", "forward_date", "forward_from", "forward_from_chat"))
    reply_id = reply.get("message_id")
    return Message(
        message_id=m["message_id"],
        chat_id=m["chat"]["id"],
        thread_id=thread,
        from_id=sender.get("id"),
        from_is_bot=bool(sender.get("is_bot")),
        text=m.get("text"),
        is_forwarded=forwarded,
        has_sender_chat="sender_chat" in m,
        reply_to_message_id=reply_id if reply_id != thread else None,
    )
