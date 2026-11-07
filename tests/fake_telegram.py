"""Fake Telegram Bot API server: aiohttp on 127.0.0.1, implementing only the methods cqb calls."""

import asyncio
import itertools
import re
from typing import Any

from aiohttp import web

TOKEN = "123456:TEST-TOKEN"
CHAT_ID = -1001234567890
BOT_ID = 777
OWNER = 42


class FakeTelegram:
    def __init__(self, chat_id: int = CHAT_ID) -> None:
        self.chat_id = chat_id
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.topics: dict[int, dict[str, Any]] = {}
        self.messages: dict[int, dict[str, Any]] = {}
        self.callback_answers: list[dict[str, Any]] = []
        self.pinned: list[int] = []
        self.deleted_user_messages: list[int] = []  # ids of pushed (user) messages the bot deleted
        self.errors: dict[str, list[tuple[int, dict[str, Any]]]] = {}  # method -> queued failures
        self.drop_next: dict[str, int] = {}  # method -> connections to drop before answering
        self._updates: list[dict[str, Any]] = []
        self._update_ids = itertools.count(1)
        self._msg_ids = itertools.count(1000)
        self._topic_ids = itertools.count(100)
        self._wake = asyncio.Event()
        self._runner: web.AppRunner | None = None
        self.port = 0

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def start(self) -> None:
        app = web.Application()
        app.router.add_post("/bot{token}/{method}", self._handle)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        self.port = self._runner.addresses[0][1]

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()

    # --- test helpers ----------------------------------------------------------------

    def methods(self, name: str | None = None) -> list[str] | list[dict[str, Any]]:
        if name is None:
            return [m for m, _ in self.calls]
        return [p for m, p in self.calls if m == name]

    def fail(self, method: str, code: int, description: str, retry_after: int | None = None, times: int = 1) -> None:
        body: dict[str, Any] = {"ok": False, "error_code": code, "description": description}
        if retry_after is not None:
            body["parameters"] = {"retry_after": retry_after}
        self.errors.setdefault(method, []).extend([(code, body)] * times)

    def delete_topic(self, topic_id: int) -> None:
        self.topics.pop(topic_id, None)
        for m in self.messages.values():
            if m.get("thread_id") == topic_id:
                m["deleted"] = True

    def delete_message(self, message_id: int) -> None:
        self.messages[message_id]["deleted"] = True

    def topic_named(self, name: str) -> list[int]:
        return [i for i, t in self.topics.items() if t["name"] == name]

    def live_messages(self, thread_id: int) -> list[dict[str, Any]]:
        return [m for m in self.messages.values() if m["thread_id"] == thread_id and not m.get("deleted")]

    def push_message(
        self, text: str, thread_id: int | None, user_id: int = OWNER, chat_id: int | None = None,
        forwarded: bool = False, sender_chat: bool = False, reply_to: int | None = None, is_bot: bool = False,
    ) -> int:
        msg: dict[str, Any] = {
            "message_id": next(self._msg_ids), "chat": {"id": chat_id if chat_id is not None else self.chat_id},
            "from": {"id": user_id, "is_bot": is_bot}, "text": text,
        }
        if thread_id is not None:
            msg["message_thread_id"] = thread_id
            msg["is_topic_message"] = True
            msg["reply_to_message"] = {"message_id": reply_to or thread_id}
        if forwarded:
            msg["forward_origin"] = {"type": "user"}
        if sender_chat:
            msg["sender_chat"] = {"id": self.chat_id}
        self._push({"message": msg})
        return msg["message_id"]

    def push_callback(self, data: str, message_id: int, user_id: int = OWNER, chat_id: int | None = None) -> str:
        qid = f"cbq{next(self._update_ids)}"
        self._push({"callback_query": {
            "id": qid, "from": {"id": user_id, "is_bot": False}, "data": data,
            "message": {"message_id": message_id, "chat": {"id": chat_id if chat_id is not None else self.chat_id}},
        }})
        return qid

    def buttons(self, message_id: int) -> list[dict[str, Any]]:
        markup = self.messages[message_id].get("markup") or {}
        return [b for row in markup.get("inline_keyboard", []) for b in row]

    def button(self, message_id: int, text_part: str) -> dict[str, Any]:
        found = [b for b in self.buttons(message_id) if text_part in b["text"]]
        assert found, f"no button containing {text_part!r} in {[b['text'] for b in self.buttons(message_id)]}"
        return found[0]

    def _push(self, body: dict[str, Any]) -> None:
        self._updates.append({"update_id": next(self._update_ids), **body})
        self._wake.set()

    # --- Bot API ---------------------------------------------------------------------

    async def _handle(self, request: web.Request) -> web.Response:
        method = request.match_info["method"]
        if request.match_info["token"] != TOKEN:
            return web.json_response({"ok": False, "error_code": 401, "description": "Unauthorized"}, status=401)
        params = await request.json()
        self.calls.append((method, params))
        if self.drop_next.get(method):
            self.drop_next[method] -= 1
            request.transport.close()
            return web.Response()
        queued = self.errors.get(method)
        if queued:
            code, body = queued.pop(0)
            return web.json_response(body, status=code)
        handler = getattr(self, f"m_{method}", None)
        if handler is None:
            return self._err(404, "Not Found")
        try:
            result = await handler(params)
        except _Fail as exc:
            return self._err(exc.code, exc.description)
        return web.json_response({"ok": True, "result": result})

    @staticmethod
    def _err(code: int, description: str) -> web.Response:
        return web.json_response({"ok": False, "error_code": code, "description": description}, status=code)

    async def m_getMe(self, p: dict[str, Any]) -> dict[str, Any]:
        return {"id": BOT_ID, "is_bot": True, "username": "cqb_test_bot", "can_read_all_group_messages": False}

    async def m_getChat(self, p: dict[str, Any]) -> dict[str, Any]:
        return {"id": p["chat_id"], "type": "supergroup", "is_forum": True}

    async def m_getChatMember(self, p: dict[str, Any]) -> dict[str, Any]:
        return {"status": "administrator", "can_manage_topics": True, "can_pin_messages": True}

    async def m_getUpdates(self, p: dict[str, Any]) -> list[dict[str, Any]]:
        offset = p.get("offset") or 0
        self._updates = [u for u in self._updates if u["update_id"] >= offset]
        if not self._updates:
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), min(p.get("timeout", 0), 0.2))
            except TimeoutError:
                pass
        return [u for u in self._updates if u["update_id"] >= offset][: p.get("limit", 100)]

    async def m_sendMessage(self, p: dict[str, Any]) -> dict[str, Any]:
        thread = p.get("message_thread_id")
        if thread is not None and thread not in self.topics:
            raise _Fail(400, "Bad Request: message thread not found")
        if p.get("parse_mode") == "HTML":
            check_html(p["text"])
        mid = next(self._msg_ids)
        self.messages[mid] = {
            "text": p["text"], "thread_id": thread, "markup": p.get("reply_markup"), "chat_id": p["chat_id"],
            "parse_mode": p.get("parse_mode"),
        }
        return {"message_id": mid}

    async def m_editMessageText(self, p: dict[str, Any]) -> bool:
        m = self.messages.get(p["message_id"])
        if m is None or m.get("deleted"):
            raise _Fail(400, "Bad Request: message to edit not found")
        if m["text"] == p["text"] and m["markup"] == p.get("reply_markup"):
            raise _Fail(400, "Bad Request: message is not modified")
        m["text"], m["markup"] = p["text"], p.get("reply_markup")
        m["edits"] = m.get("edits", 0) + 1
        return True

    async def m_sendChatAction(self, p: dict[str, Any]) -> bool:
        return True

    async def m_pinChatMessage(self, p: dict[str, Any]) -> bool:
        self.pinned.append(p["message_id"])
        return True

    async def m_deleteMessage(self, p: dict[str, Any]) -> bool:
        if p["message_id"] in self.messages:
            self.delete_message(p["message_id"])
        else:
            self.deleted_user_messages.append(p["message_id"])
        return True

    async def m_createForumTopic(self, p: dict[str, Any]) -> dict[str, Any]:
        tid = next(self._topic_ids)
        self.topics[tid] = {"name": p["name"], "closed": False}
        return {"message_thread_id": tid, "name": p["name"], "icon_color": 0}

    async def m_closeForumTopic(self, p: dict[str, Any]) -> bool:
        return self._set_closed(p["message_thread_id"], True)

    async def m_reopenForumTopic(self, p: dict[str, Any]) -> bool:
        return self._set_closed(p["message_thread_id"], False)

    def _set_closed(self, topic_id: int, closed: bool) -> bool:
        topic = self.topics.get(topic_id)
        if topic is None:
            raise _Fail(400, "Bad Request: message thread not found")
        if topic["closed"] == closed:
            raise _Fail(400, "Bad Request: TOPIC_NOT_MODIFIED")
        topic["closed"] = closed
        return True

    async def m_answerCallbackQuery(self, p: dict[str, Any]) -> bool:
        self.callback_answers.append(p)
        return True


_HTML_TAG = re.compile(r"</?([A-Za-z][A-Za-z0-9-]*)[^<>]*>")
_HTML_TAGS = {"b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "code", "pre", "a", "tg-spoiler", "blockquote"}
_HTML_ENTITY = re.compile(r"&(#\d+|#x[0-9a-fA-F]+|lt|gt|amp|quot);")


def check_html(text: str) -> None:
    """What Telegram's HTML parse mode rejects with `can't parse entities`: unknown or unbalanced tags, a bare `<` or `&`."""
    stack: list[str] = []
    for m in _HTML_TAG.finditer(text):
        tag = m.group(1).lower()
        if tag not in _HTML_TAGS:
            raise _Fail(400, f"Bad Request: can't parse entities: Unsupported start tag \"{tag}\"")
        if m.group(0).startswith("</"):
            if not stack or stack.pop() != tag:
                raise _Fail(400, f"Bad Request: can't parse entities: Unmatched end tag \"{tag}\"")
        else:
            stack.append(tag)
    if stack:
        raise _Fail(400, "Bad Request: can't parse entities: Can't find end tag")
    bare = _HTML_TAG.sub("", text)
    if "<" in bare:
        raise _Fail(400, "Bad Request: can't parse entities: Unsupported start tag")
    if _HTML_ENTITY.sub("", bare).count("&"):
        raise _Fail(400, "Bad Request: can't parse entities: Unsupported entity")


class _Fail(Exception):
    def __init__(self, code: int, description: str) -> None:
        self.code, self.description = code, description
