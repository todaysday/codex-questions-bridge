"""Fake PicoClaw `pico` channel: aiohttp WebSocket server with the wire behaviour the spike verified.

* Bearer token auth (401 otherwise), `?session_id=` attaches the socket to a session.
* `message.send` is processed in the session named by its own `session_id`; the reply goes only to
  sockets attached with that session id and is DROPPED when there is none.
* A message sent while a turn runs is MERGED into it (no new turn, no reply of its own): the test
  detects a Bridge that sends concurrently through `merged`.
* Reply frames: typing.start, typing.stop, message.create; streaming and tool-call variants.
* `/stop` cancels the running turn ("Task stopped"), empty content gives an error frame.
"""

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

from aiohttp import WSMsgType, web

TOKEN = "pico-test-token"
Script = Callable[[str, str], Awaitable[list[dict[str, Any]]]]


def create(content: str, message_id: str = "m1", **extra: Any) -> dict[str, Any]:
    return {"type": "message.create", "payload": {"content": content, "message_id": message_id, **extra}}


def update(content: str, message_id: str = "m1") -> dict[str, Any]:
    return {"type": "message.update", "payload": {"content": content, "message_id": message_id}}


async def echo(sid: str, text: str) -> list[dict[str, Any]]:
    return [{"type": "typing.start"}, {"type": "typing.stop"}, create(f"echo:{text}", context_usage={"used": 1})]


class FakePico:
    def __init__(self, script: Script = echo) -> None:
        self.script = script
        self.sockets: dict[str, list[web.WebSocketResponse]] = {}
        self.sent: list[tuple[str, str]] = []  # (session id, content) of every message.send
        self.merged = 0  # messages that arrived while a turn was running
        self.dropped = 0  # replies produced with no socket attached
        self.opened = 0
        self.max_open = 0
        self.auth_failures = 0
        self.turn_delay = 0.0
        self._turns: dict[str, asyncio.Task[None]] = {}
        self._runner: web.AppRunner | None = None
        self.port = 0

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/pico/ws"

    @property
    def open_count(self) -> int:
        return sum(len(v) for v in self.sockets.values())

    def texts(self, sid: str) -> list[str]:
        return [t for s, t in self.sent if s == sid]

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/pico/ws", self._ws)
        app.router.add_get("/health", self._health)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", self.port)
        await site.start()
        self.port = self._runner.addresses[0][1]

    async def stop(self) -> None:
        for t in self._turns.values():
            t.cancel()
        if self._runner:
            await self._runner.cleanup()
            self._runner = None

    async def drop_connections(self) -> None:
        for socks in list(self.sockets.values()):
            for ws in list(socks):
                await ws.close()

    async def _health(self, request: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    async def _ws(self, request: web.Request) -> web.StreamResponse:
        if request.headers.get("Authorization") != f"Bearer {TOKEN}":
            self.auth_failures += 1
            raise web.HTTPUnauthorized()
        sid = request.query.get("session_id", "")
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.sockets.setdefault(sid, []).append(ws)
        self.opened += 1
        self.max_open = max(self.max_open, self.open_count)
        try:
            async for msg in ws:
                if msg.type is WSMsgType.TEXT:
                    await self._on(ws, json.loads(msg.data))
        finally:
            if ws in self.sockets.get(sid, []):
                self.sockets[sid].remove(ws)
        return ws

    async def _on(self, ws: web.WebSocketResponse, m: dict[str, Any]) -> None:
        if m.get("type") != "message.send":
            return
        sid, text = m["session_id"], (m.get("payload") or {}).get("content", "")
        if not text.strip():
            await ws.send_json({"type": "error", "payload": {"code": "empty_content", "message": "empty"}})
            return
        self.sent.append((sid, text))
        running = self._turns.get(sid)
        if text.strip() == "/stop":
            if running and not running.done():
                running.cancel()
                await self._reply(sid, [create('Task stopped. "work" was canceled.', "stop")])
            else:
                await self._reply(sid, [create("No active task to stop.", "stop")])
            return
        if running and not running.done():
            self.merged += 1
            return
        self._turns[sid] = asyncio.create_task(self._turn(sid, text))

    async def _turn(self, sid: str, text: str) -> None:
        if self.turn_delay:
            await asyncio.sleep(self.turn_delay)
        await self._reply(sid, await self.script(sid, text))

    async def _reply(self, sid: str, frames: list[dict[str, Any]]) -> None:
        for frame in frames:
            socks = self.sockets.get(sid, [])
            if not socks:
                self.dropped += 1
                continue
            for ws in list(socks):
                await ws.send_json(frame)
            await asyncio.sleep(0)
