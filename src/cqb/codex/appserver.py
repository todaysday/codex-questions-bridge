"""Client of the shared Codex app-server daemon (WebSocket over its unix control socket).

The connection has full rights on the daemon, so the method whitelist below is the
only boundary. Everything the Bridge sends goes through call(), respond() or send_user_message().
call() is read-only. send_user_message() is the single write path into a thread: one plain user
message (the answer to an agent-message question), built from threadId and text only (plus the
app's own clientUserMessageId and, for turn/start, turnTrigger).
"""

import asyncio
import contextlib
import itertools
import logging
import uuid
from dataclasses import dataclass
from typing import Any

import aiohttp

log = logging.getLogger(__name__)

ALLOWED_METHODS = frozenset(
    {
        "thread/list", "thread/read", "thread/loaded/list", "thread/resume", "thread/unsubscribe",
        "thread/turns/list", "thread/goal/get", "account/rateLimits/read",  # status card: reads only
        "thread/items/list",  # consultant context API: paged history reads
    }
)
# thread/resume may carry config overrides; the Bridge may only select the thread.
_RESUME_KEYS = frozenset({"threadId", "excludeTurns"})
# The only writes, reachable solely through send_user_message(): never through call().
STEER, START = "turn/steer", "turn/start"
START_TRIGGER = "send_user_message_async_question"  # what the Codex app sets on the turn that carries a question reply
MAX_USER_MESSAGE_CHARS = 20_000
RACE_PAUSE = 0.5  # a turn that has just begun reads as "interrupted" for ~0.3 s: look again after this

INBOX_MAX = 1024
MAX_MESSAGE_BYTES = 8 * 1024 * 1024


class MethodNotAllowed(Exception):
    pass


class NotConnected(Exception):
    pass


class RpcError(Exception):
    def __init__(self, method: str, error: dict[str, Any]) -> None:
        super().__init__(f"{method}: {error.get('message', error)}")
        self.error = error


@dataclass(frozen=True, slots=True)
class Inbound:
    """kind: notification | request | connected | disconnected."""

    kind: str
    method: str = ""
    params: dict[str, Any] | None = None
    request_id: int | str | None = None


class AppServerClient:
    def __init__(
        self,
        socket_path: str,
        *,
        client_name: str = "cqb",
        request_timeout: float = 30.0,
        backoff_min: float = 1.0,
        backoff_max: float = 30.0,
    ) -> None:
        self._socket_path = socket_path
        self._client_name = client_name
        self._timeout = request_timeout
        self._backoff_min = backoff_min
        self._backoff_max = backoff_max
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._healthy = False
        self._ready = False
        self.inbox: asyncio.Queue[Inbound] = asyncio.Queue(maxsize=INBOX_MAX)

    @property
    def connected(self) -> bool:
        return self._ready and self._ws is not None and not self._ws.closed

    async def call(self, method: str, params: dict[str, Any] | None = None, timeout: float | None = None) -> dict[str, Any]:
        if method not in ALLOWED_METHODS:
            raise MethodNotAllowed(method)
        if method == "thread/resume" and not set(params or {}) <= _RESUME_KEYS:
            raise MethodNotAllowed("thread/resume with parameters beyond threadId/excludeTurns")
        return await self._rpc(method, params, timeout)

    async def send_user_message(self, thread_id: str, text: str) -> str:
        """Send `text` into the thread as a normal user message; returns "steer" or "start".

        turn/steer when the latest turn is in progress, else thread/resume + turn/start (and an unsubscribe
        again). The params are built here: threadId, input (one text item), clientUserMessageId (a fresh uuid4,
        as the app sends), for steer expectedTurnId and for start turnTrigger. Never any approvalPolicy, sandbox, model, cwd, effort or other override. A rejected call (the turn
        ended or a new one began meanwhile) is re-planned once from a fresh read; a timeout is not retried,
        because the message may have gone through.
        """
        if not thread_id or not text.strip() or len(text) > MAX_USER_MESSAGE_CHARS:
            raise ValueError(f"a user message must have 1..{MAX_USER_MESSAGE_CHARS} characters")
        content = [{"type": "text", "text": text}]
        for attempt in (0, 1):
            try:
                turn_id = await self._running_turn(thread_id)
                if turn_id is not None:
                    await self._rpc(STEER, {"threadId": thread_id, "input": content, "expectedTurnId": turn_id,
                                            "clientUserMessageId": str(uuid.uuid4())})
                    return "steer"
                await self._start_turn(thread_id, content)
                return "start"
            except RpcError:
                if attempt:
                    raise
                await asyncio.sleep(RACE_PAUSE)
        raise AssertionError("unreachable")

    async def _running_turn(self, thread_id: str) -> str | None:
        page = await self.call("thread/turns/list", {"threadId": thread_id, "limit": 1, "itemsView": "notLoaded"})
        turns = page.get("data") or []
        turn = turns[0] if turns and isinstance(turns[0], dict) else {}
        return turn.get("id") if turn.get("status") == "inProgress" and isinstance(turn.get("id"), str) else None

    async def _start_turn(self, thread_id: str, content: list[dict[str, Any]]) -> None:
        await self._rpc("thread/resume", {"threadId": thread_id, "excludeTurns": True})
        try:
            await self._rpc(START, {"threadId": thread_id, "input": content,
                                    "clientUserMessageId": str(uuid.uuid4()), "turnTrigger": START_TRIGGER})
        finally:
            try:
                await self._rpc("thread/unsubscribe", {"threadId": thread_id})
            except Exception as exc:
                log.debug("unsubscribe %s failed: %s", thread_id, type(exc).__name__)

    async def respond(self, request_id: int | str, result: dict[str, Any]) -> None:
        """Answer a server request by its id (the only non-whitelisted write)."""
        await self._send({"id": request_id, "result": result})

    async def run(self) -> None:
        """Connect, serve and reconnect forever with bounded exponential backoff."""
        delay = self._backoff_min
        while True:
            self._healthy = False
            try:
                async with aiohttp.ClientSession(connector=aiohttp.UnixConnector(path=self._socket_path)) as session:
                    async with session.ws_connect(
                        "http://localhost/", max_msg_size=MAX_MESSAGE_BYTES, heartbeat=30
                    ) as ws:
                        await self._serve(ws)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("app-server connection failed: %s", exc)
            if self._healthy:
                self._publish(Inbound("disconnected"))
                delay = self._backoff_min
            await asyncio.sleep(delay)
            delay = min(delay * 2, self._backoff_max)

    async def _serve(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        self._ws = ws
        reader = asyncio.create_task(self._read(ws))
        try:
            await self._rpc(
                "initialize",
                {
                    "clientInfo": {"name": self._client_name, "title": self._client_name, "version": "0.1.0"},
                    "capabilities": {"experimentalApi": True},
                },
            )
            await self._send({"method": "initialized"})
            self._healthy = self._ready = True
            log.info("connected to app-server")
            self._publish(Inbound("connected"))
            await reader
        finally:
            reader.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await reader
            self._ws = None
            self._ready = False
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(NotConnected("connection lost"))
            self._pending.clear()

    async def _read(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        async for msg in ws:
            if msg.type != aiohttp.WSMsgType.TEXT:
                continue
            m = msg.json()
            if "method" in m:
                kind = "request" if "id" in m else "notification"
                self._publish(Inbound(kind, m["method"], m.get("params"), m.get("id")))
            elif "id" in m:
                fut = self._pending.pop(m["id"], None)
                if fut is not None and not fut.done():
                    fut.set_result(m)

    def _publish(self, item: Inbound) -> None:
        # Never block the reader: a stalled consumer must not stall responses.
        try:
            self.inbox.put_nowait(item)
        except asyncio.QueueFull:
            log.error("inbox full, dropping %s %s", item.kind, item.method)

    async def _send(self, payload: dict[str, Any]) -> None:
        ws = self._ws
        if ws is None or ws.closed:
            raise NotConnected("not connected")
        await ws.send_json(payload)

    async def _rpc(self, method: str, params: dict[str, Any] | None, timeout: float | None = None) -> dict[str, Any]:
        rid = next(self._ids)
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        try:
            await self._send({"id": rid, "method": method, "params": params})
            msg = await asyncio.wait_for(fut, timeout or self._timeout)
        finally:
            self._pending.pop(rid, None)
        if "error" in msg:
            raise RpcError(method, msg["error"])
        return msg.get("result") or {}
