"""Bridge side of PicoClaw's `pico` WebSocket channel (design section 4.1, spike S0-B facts).

Wire facts this module is built on (PicoClaw v0.3.1, verified on a mock model and real binary):
  * ws://127.0.0.1:18790/pico/ws?session_id=<id>, `Authorization: Bearer <token>`.
  * A reply is delivered only to a WebSocket attached with the same session_id; otherwise it is
    dropped (the history keeps it). So: one WebSocket per ACTIVE session, held for the whole turn.
  * A message sent while a turn runs is merged into that turn (steering), not queued. So: strict
    per-session serialization here, with a bounded queue.
  * Frames carry no request id. A reply is `message.create`, or `message.create` plus cumulative
    `message.update` with the same message_id when streaming. Reasoning frames have
    payload.kind == "thought"; a tool-call notice is a `message.create` with empty content (or
    kind == "tool_calls") sent before the tool runs. `typing.stop` arrives BEFORE the final
    `message.create` in the non-streaming case, so it is not an end marker. `error` frames
    (empty_content, unknown_type) end the turn.

END-OF-TURN RULE (the spike offers no explicit final flag): the reply is the latest non-empty
message of the turn. A tool-call notice discards what came before it (that text was intermediate).
The turn is complete when a reply exists and no further frame arrives for `quiet` seconds, or
or when an `error` frame arrives. There is NO fixed reply time limit by owner decision (nobody
can know how long an answer takes): `turn_timeout_seconds` is 0 = disabled by default, and only
when set above 0 does it abandon the turn (`/stop` sent best effort, socket closed so a late reply
cannot be mistaken for the next one). A closed WebSocket mid-turn means the reply is lost: the
user is told and the message is not resent (PicoClaw may still be running that turn, and would
merge the next message into it or deliver its late reply to the new socket, so the next turn
first sends `/stop` on the fresh socket and waits for its confirmation); messages still queued behind it are kept and tried
on a fresh connection (a few attempts with bounded backoff, then each is reported unavailable).

`ask(session_id, text, timeout)` is the one-shot variant for the Status Agent: a single request on a named
session with its own deadline (instead of `turn_timeout_seconds`), stopped like any timed-out turn.

The owner can cancel a long turn: a message that is exactly `/stop` is the ONE deliberate
exception to slash escaping. It is passed to PicoClaw unescaped (a built-in that stops the
running turn without a model call). While a turn is in flight it is sent at once on that turn's
socket instead of waiting in the queue; with nothing running it is an ordinary message. Every
other slash/bang text, `/stop now` and `/STOP` included, is escaped.

Memory: sessions exist only while they have a socket, a queue or a worker. Sockets close after
`idle_seconds` and, at the connection cap, the least recently used idle one is closed first.
"""

import asyncio
import contextlib
import json
import logging
import re
import time
import uuid
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import aiohttp

from cqb.config import PicoConfig
from cqb.pico.health import HEALTH_FAILING, PicoHealth
from cqb.security import redact
from cqb.services.relay import REPLY_MAX_CHARS, STOP, Deliver, DialogResult, Outcome, Submit

log = logging.getLogger(__name__)

QUEUE_MAX = 5
STOP_SETTLE_SECONDS = 10.0
CONNECT_ATTEMPTS = 3  # per message, before it is reported unavailable
QUIET_SECONDS = 1.0
CONNECT_TIMEOUT = 5.0
MAX_FRAME_BYTES = 1 << 20
BACKOFF_MAX = 30.0
LIVENESS_SECONDS = 15.0
RECOVERY_PROBE_SECONDS = 2.0  # while unhealthy, notice the recovery quickly
_SESSION_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
# invisible characters PicoClaw does not treat as whitespace but a reader cannot see
_INVISIBLE = "​‌‍⁠﻿"
ERROR_FRAME_CHARS = 200

CONN_LOST = "соединение с PicoClaw оборвано"
CONN_FAILED = "не удаётся подключиться к PicoClaw"
CONN_BACKOFF = "PicoClaw недавно не отвечал, повторная попытка позже"
BAD_SESSION = "недопустимая сессия консультанта"
TOO_MANY_SOCKETS = "слишком много открытых соединений с PicoClaw"
EXCHANGE_FAILED = "сбой обмена с PicoClaw"


def escape_command(text: str) -> str:
    """PicoClaw runs `/cmd` and `!cmd` itself, before any hook and without the model (/clear wipes
    the history, /switch changes the live model, /reload reloads config). Verified rule: a message
    is a command when its first token after trimming Unicode whitespace starts with `/` or `!`;
    leading whitespace therefore does not escape, but invisible characters do. A visible prefix
    does, and keeps the user's literal text readable in the history. Exception: exactly `/stop`."""
    if text.strip() == STOP:
        return STOP
    i = 0
    while i < len(text) and (text[i].isspace() or text[i] in _INVISIBLE):  # str.isspace is a superset of Go's
        i += 1
    return f"User says: {text}" if text[i:i + 1] in ("/", "!") else text


class ReplyAssembler:
    """Frames of one turn -> the reply text (see the module docstring for the rule)."""

    def __init__(self, max_chars: int = REPLY_MAX_CHARS) -> None:
        self._max = max_chars
        self.text = ""
        self.error: str | None = None
        self.error_text = ""

    @property
    def has_reply(self) -> bool:
        return bool(self.text)

    def feed(self, frame: dict[str, Any]) -> None:
        kind = frame.get("type")
        payload = frame.get("payload")
        payload = payload if isinstance(payload, dict) else {}
        if kind == "error":
            self.error = str(payload.get("code") or "error")
            detail = payload.get("message")
            self.error_text = detail if isinstance(detail, str) and detail.strip() else self.error
            return
        if kind not in ("message.create", "message.update"):
            return  # typing.*, pong
        if payload.get("kind") == "thought" or payload.get("thought") is True or payload.get("placeholder"):
            return
        content = payload.get("content")
        text = content if isinstance(content, str) else ""
        if payload.get("kind") == "tool_calls" or (kind == "message.create" and not text.strip()):
            self.text = ""  # a tool call follows: anything before it was intermediate narration
            return
        if text.strip():
            self.text = text[: self._max]  # updates are cumulative; the newest content replaces


@dataclass(slots=True)
class _Session:
    sid: str
    ws: aiohttp.ClientWebSocketResponse | None = None
    queue: deque[tuple[str, Deliver, float | None]] = field(default_factory=deque)
    worker: asyncio.Task[None] | None = None
    touched: float = 0.0
    inflight: bool = False  # a turn is waiting for its reply on `ws`
    orphaned: bool = False  # the last socket died mid-turn: PicoClaw may still be running that turn

    @property
    def busy(self) -> bool:
        return self.worker is not None or bool(self.queue)


class _Unavailable(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _Busy(Exception):
    pass


class PicoGateway:
    def __init__(
        self, cfg: PicoConfig, token: str, http: aiohttp.ClientSession, health: PicoHealth, *,
        quiet: float = QUIET_SECONDS, liveness: float = LIVENESS_SECONDS, backoff: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._cfg = cfg
        self._token = token
        self._http = http
        self._health = health
        self._quiet = quiet
        self._liveness = liveness
        self._clock = clock
        self._sessions: OrderedDict[str, _Session] = OrderedDict()  # least recently used first
        self._down_until = 0.0
        self._backoff = 0.0
        self._backoff_base = backoff
        self._refusal = ""

    # --- public ----------------------------------------------------------------------

    @property
    def open_sockets(self) -> int:
        return sum(1 for s in self._sessions.values() if s.ws is not None)

    def submit(self, session_id: str, text: str, deliver: Deliver, timeout: float | None = None) -> Submit:
        """Queue one user message for the session; never blocks, never sends concurrently.

        `timeout` (seconds) bounds this turn's reply instead of `turn_timeout_seconds`; None = the config value."""
        if not _SESSION_ID.match(session_id) or not session_id.startswith(self._cfg.session_prefix):
            log.error("refusing a session id outside the %r namespace", self._cfg.session_prefix)
            self._refusal = BAD_SESSION
            return Submit.UNAVAILABLE
        if not self._health.ok:
            self._refusal = self._health.user_reason or HEALTH_FAILING
            return Submit.UNAVAILABLE
        s = self._sessions.get(session_id)
        if text.strip() == STOP and s is not None and s.inflight and s.ws is not None and not s.ws.closed:
            asyncio.get_running_loop().create_task(self._send_stop(s, s.ws), name="pico-stop")
            return Submit.STOPPED
        if s is None:
            s = self._sessions[session_id] = _Session(session_id)
        if len(s.queue) >= QUEUE_MAX:
            return Submit.QUEUE_FULL
        queued = s.busy
        s.queue.append((escape_command(text), deliver, timeout))
        s.touched = self._clock()
        self._sessions.move_to_end(session_id)
        if s.worker is None:
            s.worker = asyncio.create_task(self._work(s), name="pico-session")
        return Submit.QUEUED if queued else Submit.ACCEPTED

    def unavailable_reason(self) -> str:
        return self._refusal

    async def ask(self, session_id: str, text: str, timeout: float) -> DialogResult:
        """One request on a named session, answered within `timeout` seconds: the Status Agent's way in.

        At the deadline the turn is stopped like any timed-out turn (`/stop`, socket closed) and the result is
        TIMEOUT. If the caller is cancelled the turn is stopped too, so nothing keeps running unobserved."""
        done: asyncio.Future[DialogResult] = asyncio.get_running_loop().create_future()

        async def deliver(result: DialogResult) -> None:
            if not done.done():
                done.set_result(result)

        async def ignore(result: DialogResult) -> None:
            return None

        match self.submit(session_id, text, deliver, timeout=timeout):
            case Submit.ACCEPTED | Submit.QUEUED:
                try:
                    return await done
                except asyncio.CancelledError:
                    self.submit(session_id, STOP, ignore)
                    raise
            case Submit.QUEUE_FULL:
                return DialogResult(Outcome.BUSY)
        return DialogResult(Outcome.UNAVAILABLE)

    async def run(self) -> None:
        """Liveness probe and idle sweeper; one loop for both."""
        next_probe = 0.0
        tick = min(self._liveness, RECOVERY_PROBE_SECONDS, max(self._cfg.idle_seconds / 4, 0.05))
        while True:
            now = self._clock()
            if now >= next_probe:
                healthy = await self._health.check()
                next_probe = now + (self._liveness if healthy else min(self._liveness, RECOVERY_PROBE_SECONDS))
            await self._sweep()
            await asyncio.sleep(tick)

    async def close(self) -> None:
        workers = [s.worker for s in self._sessions.values() if s.worker]
        for w in workers:
            w.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        for s in list(self._sessions.values()):
            await self._drop(s)

    # --- per-session worker ----------------------------------------------------------

    async def _work(self, s: _Session) -> None:
        try:
            while s.queue:
                text, deliver, timeout = s.queue.popleft()
                result = await self._turn(s, text, timeout)
                s.touched = self._clock()
                try:
                    await deliver(result)
                except Exception as exc:
                    log.warning("delivering a consultant result failed: %s", type(exc).__name__)
        finally:
            s.worker = None  # no await between the loop test and here: submit() sees a consistent state
            if s.ws is None and not s.queue:
                self._sessions.pop(s.sid, None)

    async def _turn(self, s: _Session, text: str, timeout: float | None) -> DialogResult:
        ws = None
        for attempt in range(CONNECT_ATTEMPTS):
            try:
                ws = await self._attach(s)
                if s.orphaned:
                    await self._settle(s, ws)
                await self._send(s, ws, text)
                break
            except _Busy:
                return DialogResult(Outcome.BUSY, TOO_MANY_SOCKETS)
            except _Unavailable as exc:
                if attempt == CONNECT_ATTEMPTS - 1:
                    return DialogResult(Outcome.UNAVAILABLE, exc.reason)
                # nothing was sent, so waiting out the backoff keeps the message instead of losing it
                await asyncio.sleep(max(self._down_until - self._clock(), 0.05))
        assert ws is not None
        s.inflight = True
        try:
            return await self._await_reply(s, ws, timeout)
        except asyncio.CancelledError:
            await self._drop(s)
            raise
        except Exception as exc:
            log.warning("pico turn failed: %s", type(exc).__name__)
            s.orphaned = True
            await self._drop(s)
            return DialogResult(Outcome.ERROR, EXCHANGE_FAILED)
        finally:
            s.inflight = False

    async def _settle(self, s: _Session, ws: aiohttp.ClientWebSocketResponse) -> None:
        """Cancel an orphaned turn on the fresh socket and swallow its confirmation."""
        await self._send(s, ws, STOP)
        asm = ReplyAssembler()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + STOP_SETTLE_SECONDS
        while loop.time() < deadline:
            try:
                msg = await ws.receive(timeout=min(self._quiet, deadline - loop.time()) if asm.has_reply
                                       else deadline - loop.time())
            except TimeoutError:
                break
            if msg.type is aiohttp.WSMsgType.TEXT and (frame := _parse(msg.data)) is not None:
                asm.feed(frame)
                if asm.error is not None:
                    break
            elif msg.type is not aiohttp.WSMsgType.TEXT:
                await self._drop(s)
                self._note_failure()
                raise _Unavailable(CONN_LOST)
        s.orphaned = False

    async def _send_stop(self, s: _Session, ws: aiohttp.ClientWebSocketResponse) -> None:
        """Owner cancel: the running turn's own loop consumes PicoClaw's confirmation as its reply."""
        with contextlib.suppress(Exception):
            await ws.send_json({"type": "message.send", "id": str(uuid.uuid4()), "session_id": s.sid,
                                "payload": {"content": STOP}})

    async def _await_reply(
        self, s: _Session, ws: aiohttp.ClientWebSocketResponse, timeout: float | None = None
    ) -> DialogResult:
        asm = ReplyAssembler()
        loop = asyncio.get_running_loop()
        limit = self._cfg.turn_timeout_seconds if timeout is None else timeout  # 0 = no limit
        deadline = loop.time() + limit if limit > 0 else None
        while True:
            remaining = deadline - loop.time() if deadline is not None else None
            if remaining is not None and remaining <= 0:
                log.warning("consultant turn timed out (session %s)", s.sid[:8])
                with contextlib.suppress(Exception):
                    await ws.send_json({"type": "message.send", "id": str(uuid.uuid4()), "session_id": s.sid,
                                        "payload": {"content": "/stop"}})
                await self._drop(s)  # a late reply must not be taken for the next turn's
                return DialogResult(Outcome.TIMEOUT)
            try:
                wait = self._quiet if asm.has_reply else None
                if remaining is not None:
                    wait = remaining if wait is None else min(wait, remaining)
                msg = await ws.receive(timeout=wait)
            except TimeoutError:
                if asm.has_reply:
                    return DialogResult(Outcome.REPLY, asm.text)
                continue
            if msg.type is aiohttp.WSMsgType.TEXT:
                frame = _parse(msg.data)
                if frame is None:
                    continue
                asm.feed(frame)
                if asm.error is not None:
                    log.warning("pico error frame %r (session %s)", asm.error, s.sid[:8])
                    return DialogResult(Outcome.ERROR, _error_reason(asm.error_text))
            elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.CLOSED,
                              aiohttp.WSMsgType.ERROR):
                log.warning("pico socket closed mid-turn (session %s): the reply is lost", s.sid[:8])
                s.orphaned = True
                await self._drop(s)
                self._note_failure()
                return DialogResult(Outcome.UNAVAILABLE, CONN_LOST)

    # --- sockets ---------------------------------------------------------------------

    async def _attach(self, s: _Session) -> aiohttp.ClientWebSocketResponse:
        if s.ws is not None and not s.ws.closed:
            await self._drain(s.ws)
            if s.ws is not None and not s.ws.closed:
                return s.ws
        s.ws = None
        if self._clock() < self._down_until:
            raise _Unavailable(CONN_BACKOFF)  # inside the backoff window: fail fast instead of piling up connects
        await self._make_room(s)
        try:
            async with asyncio.timeout(CONNECT_TIMEOUT):
                s.ws = await self._http.ws_connect(
                    self._cfg.url, params={"session_id": s.sid},
                    headers={"Authorization": f"Bearer {self._token}"},
                    heartbeat=30, max_msg_size=MAX_FRAME_BYTES,
                )
        except (aiohttp.ClientError, TimeoutError, OSError) as exc:
            log.warning("cannot connect to PicoClaw: %s", type(exc).__name__)
            self._note_failure()
            raise _Unavailable(CONN_FAILED) from None
        self._backoff = 0.0
        return s.ws

    async def _send(self, s: _Session, ws: aiohttp.ClientWebSocketResponse, text: str) -> None:
        try:
            await ws.send_json({"type": "message.send", "id": str(uuid.uuid4()), "session_id": s.sid,
                                "payload": {"content": text}})
        except (aiohttp.ClientError, ConnectionError, OSError):
            # Not delivered (the socket was already dead), so nothing ran: report it, do not guess.
            self._note_failure()
            await self._drop(s)
            raise _Unavailable(CONN_LOST) from None

    async def _drain(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        """Between turns every frame is stale (late typing.stop, a reply to an abandoned turn)."""
        for _ in range(64):
            try:
                msg = await ws.receive(timeout=0.005)
            except TimeoutError:
                return
            if msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.CLOSED,
                            aiohttp.WSMsgType.ERROR):
                return  # ws.closed is now true; the caller reconnects

    async def _make_room(self, s: _Session) -> None:
        """Hard cap on sockets: close least recently used idle ones; refuse when all are busy."""
        while self.open_sockets >= self._cfg.max_connections:
            victim = next((v for v in self._sessions.values() if v is not s and v.ws is not None and not v.busy), None)
            if victim is None:
                raise _Busy
            await self._drop(victim)

    async def _drop(self, s: _Session) -> None:
        ws, s.ws = s.ws, None
        if ws is not None and not ws.closed:
            with contextlib.suppress(Exception):
                await ws.close()
        if not s.busy:
            self._sessions.pop(s.sid, None)

    async def _sweep(self) -> None:
        cutoff = self._clock() - self._cfg.idle_seconds
        for s in [s for s in self._sessions.values() if not s.busy and s.touched < cutoff]:
            await self._drop(s)

    def _note_failure(self) -> None:
        self._backoff = min(max(self._backoff * 2, self._backoff_base), BACKOFF_MAX)
        self._down_until = self._clock() + self._backoff


def _error_reason(text: str) -> str:
    """The model/gateway error frame, redacted and cut: it is shown to the owner, so it stays bounded."""
    clean = " ".join(redact(text)[0].split())
    if len(clean) > ERROR_FRAME_CHARS:
        clean = clean[: ERROR_FRAME_CHARS - 1] + "…"
    return f"модель вернула ошибку: {clean}"


def _parse(data: str) -> dict[str, Any] | None:
    try:
        frame = json.loads(data)
    except ValueError:
        return None
    return frame if isinstance(frame, dict) else None
