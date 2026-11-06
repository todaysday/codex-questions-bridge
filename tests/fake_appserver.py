"""Fake Codex app-server daemon: aiohttp WebSocket server on a unix socket."""

import asyncio
import itertools
import json
import os
import tempfile
from typing import Any

from aiohttp import WSMsgType, web


class FakeConn:
    def __init__(self, ws: web.WebSocketResponse) -> None:
        self.ws = ws
        self.subscribed: set[str] = set()
        self.ids = itertools.count(100)
        self.requests: dict[str, int] = {}  # thread id -> request id sent on this connection
        self.initialized = False
        self.capabilities: dict[str, Any] = {}


class FakeAppServer:
    def __init__(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="cqb")
        self.path = os.path.join(self.dir, "s.sock")
        self.threads: dict[str, dict[str, Any]] = {}
        self.archived: set[str] = set()
        self.conns: list[FakeConn] = []
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.pending: dict[str, dict[str, Any]] = {}  # thread id -> request params
        self.answers: list[dict[str, Any]] = []
        self.turns: dict[str, list[dict[str, Any]]] = {}  # thread id -> turns, newest first
        self.goals: dict[str, dict[str, Any]] = {}
        self.items: dict[str, list[dict[str, Any]]] = {}  # thread id -> thread/items/list entries, oldest first
        self.rate_limits: dict[str, Any] | None = {
            "ordinaryUsageAllowed": True,
            "rateLimits": {"primary": {"usedPercent": 30, "windowDurationMins": 10080, "resetsAt": 1_800_100_000},
                           "secondary": None},
        }
        self._runner: web.AppRunner | None = None

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/", self._handle)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        await web.UnixSite(self._runner, self.path).start()

    async def stop(self) -> None:
        for c in list(self.conns):
            await c.ws.close()
        if self._runner:
            await self._runner.cleanup()

    def add_thread(self, thread_id: str, updated_at: int, parent: str | None = None, archived: bool = False,
                   name: str | None = None, path: str | None = None, model: str = "gpt-6", effort: str = "low",
                   nickname: str | None = None, role: str | None = None) -> None:
        self.threads[thread_id] = {
            "id": thread_id, "name": name or f"thread {thread_id}", "preview": "p", "cwd": "/w",
            "updatedAt": updated_at, "status": {"type": "idle"},
            "source": {"subAgent": {"thread_spawn": {"parent_thread_id": parent, "depth": 1}}} if parent else "cli",
            "parentThreadId": parent, "model": model, "reasoningEffort": effort, "path": path,
            "agentNickname": nickname, "agentRole": role,
        }
        if archived:
            self.archived.add(thread_id)

    def methods(self) -> list[str]:
        return [m for m, _ in self.calls]

    async def drop_connections(self) -> None:
        for c in list(self.conns):
            await c.ws.close()

    async def broadcast(self, method: str, params: dict[str, Any], only_subscribed: str | None = None) -> None:
        for c in list(self.conns):
            if only_subscribed is None or only_subscribed in c.subscribed:
                await c.ws.send_json({"method": method, "params": params})

    async def set_status(self, thread_id: str, flags: list[str] | None) -> None:
        status = {"type": "active", "activeFlags": flags} if flags is not None else {"type": "idle"}
        self.threads[thread_id]["status"] = status
        await self.broadcast("thread/status/changed", {"threadId": thread_id, "status": status})

    async def token_usage(self, thread_id: str, last: dict[str, int], window: int | None = 100_000) -> None:
        await self.broadcast("thread/tokenUsage/updated", {
            "threadId": thread_id, "turnId": "u1",
            "tokenUsage": {"last": last, "total": {k: v * 10 for k, v in last.items()}, "modelContextWindow": window},
        })

    async def ask(self, thread_id: str, turn_id: str, item_id: str, questions: list[dict[str, Any]]) -> None:
        params = {
            "threadId": thread_id, "turnId": turn_id, "itemId": item_id, "questions": questions,
            "isBlocking": True, "autoResolutionMs": None,
        }
        self.pending[thread_id] = params
        await self.set_status(thread_id, ["waitingOnUserInput"])
        for c in list(self.conns):
            if thread_id in c.subscribed:
                await self._send_request(c, params)

    async def resolve(self, thread_id: str) -> None:
        """Another client answered: tell every subscriber and clear the waiting flag."""
        self.pending.pop(thread_id, None)
        for c in list(self.conns):
            rid = c.requests.pop(thread_id, None)
            if rid is not None:
                await c.ws.send_json({"method": "serverRequest/resolved", "params": {"threadId": thread_id, "requestId": rid}})
        await self.set_status(thread_id, [])

    async def _send_request(self, c: FakeConn, params: dict[str, Any]) -> None:
        rid = next(c.ids)
        c.requests[params["threadId"]] = rid
        await c.ws.send_json({"id": rid, "method": "item/tool/requestUserInput", "params": params})

    async def _handle(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        conn = FakeConn(ws)
        self.conns.append(conn)
        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    await self._on_message(conn, json.loads(msg.data))
        finally:
            if conn in self.conns:
                self.conns.remove(conn)
        return ws

    async def _on_message(self, conn: FakeConn, m: dict[str, Any]) -> None:
        if "method" not in m:  # response to one of our requests
            thread_id = next((t for t, r in conn.requests.items() if r == m.get("id")), None)
            if thread_id is not None:
                self.answers.append(m["result"])
                await self.resolve(thread_id)
            return
        method, params = m["method"], m.get("params") or {}
        if method == "initialized":
            conn.initialized = True
            return
        if method == "initialize":
            conn.capabilities = params.get("capabilities", {})
            await self._reply(conn, m, {"userAgent": "fake/0"})
            return
        self.calls.append((method, params))
        if method == "thread/list":
            await self._reply(conn, m, self._list(params))
        elif method == "thread/read":
            tid = params["threadId"]
            if tid not in self.threads:
                await conn.ws.send_json({"id": m["id"], "error": {"code": -32600, "message": "thread not found"}})
                return
            await self._reply(conn, m, {"thread": {**self.threads[tid], "turns": []}})
        elif method == "thread/turns/list":
            await self._reply(conn, m, {"data": self.turns.get(params["threadId"], [])[: params.get("limit") or 25]})
        elif method == "thread/items/list":
            await self._reply(conn, m, self._items(params))
        elif method == "thread/goal/get":
            await self._reply(conn, m, {"goal": self.goals.get(params["threadId"])})
        elif method == "account/rateLimits/read":
            await self._reply(conn, m, self.rate_limits or {})
        elif method == "thread/loaded/list":
            await self._reply(conn, m, {"data": sorted(self.threads)})
        elif method == "thread/resume":
            tid = params["threadId"]
            if tid not in self.threads:
                await conn.ws.send_json({"id": m["id"], "error": {"code": -32600, "message": "no rollout found"}})
                return
            conn.subscribed.add(tid)
            await self._reply(conn, m, {"thread": {**self.threads[tid], "turns": []}})
            if tid in self.pending:
                await self._send_request(conn, self.pending[tid])
        elif method == "thread/unsubscribe":
            conn.subscribed.discard(params["threadId"])
            await self._reply(conn, m, {"status": "unsubscribed"})
        else:
            await conn.ws.send_json({"id": m["id"], "error": {"code": -32601, "message": "method not found"}})

    async def _reply(self, conn: FakeConn, m: dict[str, Any], result: dict[str, Any]) -> None:
        await conn.ws.send_json({"id": m["id"], "result": result})

    def _items(self, p: dict[str, Any]) -> dict[str, Any]:
        rows = [e for e in self.items.get(p["threadId"], []) if not p.get("turnId") or e["turnId"] == p["turnId"]]
        if p.get("sortDirection") == "desc":
            rows = rows[::-1]
        start = int(p.get("cursor") or 0)
        limit = p.get("limit") or 25
        nxt = str(start + limit) if start + limit < len(rows) else None
        return {"data": rows[start:start + limit], "nextCursor": nxt}

    def _descends(self, t: dict[str, Any], ancestor: str) -> bool:
        parent = t.get("parentThreadId")
        for _ in range(16):
            if parent is None:
                return False
            if parent == ancestor:
                return True
            parent = self.threads.get(parent, {}).get("parentThreadId")
        return False

    def _list(self, p: dict[str, Any]) -> dict[str, Any]:
        want_archived = bool(p.get("archived"))
        ancestor = p.get("ancestorThreadId")
        rows = sorted(
            (t for t in self.threads.values()
             if (t["id"] in self.archived) == want_archived and (not ancestor or self._descends(t, ancestor))),
            key=lambda t: -t["updatedAt"],
        )
        start = int(p.get("cursor") or 0)
        limit = p.get("limit") or 25
        page = rows[start:start + limit]
        nxt = str(start + limit) if start + limit < len(rows) else None
        return {"data": page, "nextCursor": nxt}


async def wait_until(pred, timeout: float = 3.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not pred():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.01)
