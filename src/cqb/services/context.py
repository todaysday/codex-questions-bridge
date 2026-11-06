"""Scoped, bounded, read-only Codex context for the consultant (design section 4.2).

MCP calls carry no caller identity, so every method takes the `root_id` the scope hook has
already overwritten with the trusted value. This service still verifies it (defence in depth):
the root must be an open consultant of the configured source and every thread asked for must be
the root or one of its descendants. Redaction and byte caps are applied here, not by the caller.
"""

import dataclasses
import logging
import re
from typing import Any, Protocol

from cqb.codex.history import HistoryReader, HistoryUnavailable, event_from_entry, split_ref
from cqb.dao.consultants import ConsultantDao
from cqb.dao.questions import QuestionDao
from cqb.dao.threads import ThreadDao
from cqb.domain.context import (
    AgentInfo, AgentList, EventPage, HistoryEvent, QuestionInfo, QuestionList, ThreadOverview,
)
from cqb.domain.enums import ConsultantState
from cqb.domain.status import Descendants, ThreadSnapshot
from cqb.security import redact

log = logging.getLogger(__name__)

MAX_EVENTS = 100
DEFAULT_EVENTS = 30
MAX_BYTES = 64 * 1024
DEFAULT_BYTES = 16 * 1024
MIN_BYTES = 1024
READ_MAX_BYTES = 16 * 1024
DEFAULT_READ_BYTES = 8 * 1024
MAX_AGENTS = 50
MAX_QUESTIONS = 20
SCOPE_FETCH = 100
_ID = re.compile(r"^[A-Za-z0-9._-]{1,100}$")


class ToolError(Exception):
    """A structured, model-visible failure: code is stable, message short, never a stack trace."""

    def __init__(self, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra


class ThreadReader(Protocol):
    async def thread(self, thread_id: str) -> ThreadSnapshot | None: ...
    async def descendants(self, root_id: str, limit: int) -> Descendants | None: ...


class ContextService:
    def __init__(
        self,
        consultants: ConsultantDao,
        threads: ThreadDao,
        questions: QuestionDao,
        reader: ThreadReader,
        history: HistoryReader,
        source_id: str,
    ) -> None:
        self._consultants = consultants
        self._threads = threads
        self._questions = questions
        self._reader = reader
        self._history = history
        self._source = source_id

    # --- tools -----------------------------------------------------------------------

    async def thread_overview(self, root_id: str, source_id: str | None = None) -> ThreadOverview:
        self._root(root_id, source_id)
        local = self._threads.get(self._source, root_id)
        snap = await self._reader.thread(root_id)
        agents = await self._reader.descendants(root_id, MAX_AGENTS)
        pending = len(self._questions.list_open(self._source, root_id, MAX_QUESTIONS))
        name = (snap.name if snap else None) or (local.display_name if local else None)
        return ThreadOverview(
            source_id=self._source, root_id=root_id, thread_id=root_id,
            name=_clean(name), cwd=_clean(local.cwd if local else None),
            state=snap.state.value if snap else "unknown",
            model=snap.model if snap else None, effort=snap.effort if snap else None,
            updated_at=snap.updated_at if snap else (local.updated_at if local else None),
            archive_state=local.archive_state.value if local else None,
            agents_total=len(agents.threads) if agents else None,
            pending_questions=pending, partial=snap is None,
        )

    async def list_agents(self, root_id: str, source_id: str | None = None) -> AgentList:
        self._root(root_id, source_id)
        found = await self._reader.descendants(root_id, MAX_AGENTS)
        if found is None:
            raise ToolError("unavailable", "Codex daemon is not readable right now")
        ranked = sorted(found.threads, key=lambda t: -t.updated_at)[:MAX_AGENTS]
        agents = tuple(
            AgentInfo(t.thread_id, t.parent_id, _clean(t.nickname), _clean(t.role), t.state.value,
                      t.model, t.effort, t.updated_at)
            for t in ranked
        )
        return AgentList(self._source, root_id, agents, truncated=found.more or len(found.threads) > MAX_AGENTS)

    async def recent_events(
        self, root_id: str, source_id: str | None = None, thread_id: str | None = None,
        since: int | None = None, max_events: int | None = None, max_bytes: int | None = None,
    ) -> EventPage:
        self._root(root_id, source_id)
        events = _cap("max_events", max_events, DEFAULT_EVENTS, 1, MAX_EVENTS)
        budget = _cap("max_bytes", max_bytes, DEFAULT_BYTES, MIN_BYTES, MAX_BYTES)
        if since is not None and (isinstance(since, bool) or not isinstance(since, int) or since < 0):
            raise ToolError("invalid_arguments", "since must be a unix time in seconds")
        thread = await self._thread_in_scope(root_id, thread_id or root_id)
        try:
            entries, more = await self._history.recent(thread, events)
        except HistoryUnavailable:
            raise ToolError("unavailable", "Codex history is not readable right now") from None
        normalized = (event_from_entry(thread, e) for e in entries)
        wanted = [
            self._present(ev) for ev in normalized
            if ev is not None and (since is None or ev.at_ms is None or ev.at_ms >= since * 1000)
        ]
        kept: list[HistoryEvent] = []
        used = 0
        for ev in reversed(wanted):  # newest first: the budget keeps the most recent evidence
            size = _size(ev)
            if used + size > budget:
                more = True
                break
            kept.append(ev)
            used += size
        kept.reverse()
        return EventPage(self._source, root_id, thread, tuple(kept), truncated=more)

    async def read_event(
        self, root_id: str, event_ref: str, source_id: str | None = None, max_bytes: int | None = None
    ) -> HistoryEvent:
        self._root(root_id, source_id)
        budget = _cap("max_bytes", max_bytes, DEFAULT_READ_BYTES, MIN_BYTES, READ_MAX_BYTES)
        parts = split_ref(event_ref) if isinstance(event_ref, str) else None
        if parts is None:
            raise ToolError("invalid_arguments", "event_ref must come from get_recent_events")
        thread_id, turn_id, item_id = parts
        thread = await self._thread_in_scope(root_id, thread_id)
        try:
            entry = await self._history.find(thread, turn_id, item_id)
        except HistoryUnavailable:
            raise ToolError("unavailable", "Codex history is not readable right now") from None
        event = event_from_entry(thread, entry, max_chars=budget, detail=True) if entry else None
        if event is None:
            raise ToolError("not_found", "no such event in this thread")
        return _fit(self._present(event), budget)

    async def questions(self, root_id: str, source_id: str | None = None) -> QuestionList:
        self._root(root_id, source_id)
        rows = self._questions.list_open(self._source, root_id, MAX_QUESTIONS)
        return QuestionList(self._source, root_id, tuple(
            QuestionInfo(
                q.question_id, q.origin_thread_id, q.is_secret, q.lifecycle.value, q.created_at,
                () if q.is_secret else q.items,  # a secret question never leaves, whatever was stored
            )
            for q in rows
        ))

    # --- scope -----------------------------------------------------------------------

    def _root(self, root_id: Any, source_id: Any) -> None:
        if not isinstance(root_id, str) or not _ID.match(root_id):
            raise ToolError("invalid_arguments", "root_id is required")
        if source_id is not None and source_id != self._source:
            raise ToolError("scope_denied", "unknown source")
        c = self._consultants.get(self._source, root_id)
        if c is None or c.state is not ConsultantState.OPEN:
            raise ToolError("scope_denied", "root is not an open consultant of this source")

    async def _thread_in_scope(self, root_id: str, thread_id: Any) -> str:
        if not isinstance(thread_id, str) or not _ID.match(thread_id):
            raise ToolError("invalid_arguments", "thread_id is malformed")
        if thread_id == root_id:
            return thread_id
        known = self._threads.get(self._source, thread_id)
        if known is not None and known.root_thread_id == root_id:
            return thread_id
        found = await self._reader.descendants(root_id, SCOPE_FETCH)  # authoritative, any depth
        if found is not None and any(t.thread_id == thread_id for t in found.threads):
            return thread_id
        raise ToolError("scope_denied", "thread is not the root or one of its descendants")

    def _present(self, ev: HistoryEvent) -> HistoryEvent:
        text, changed = redact(ev.text)
        return dataclasses.replace(ev, text=text, redacted=ev.redacted or changed)


def _cap(name: str, value: Any, default: int, lo: int, hi: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value < lo:
        raise ToolError("invalid_arguments", f"{name} must be an integer >= {lo}")
    if value > hi:
        raise ToolError("limit_exceeded", f"{name} is capped", limit=name, max=hi, requested=value)
    return value


def _size(ev: HistoryEvent) -> int:
    return len(ev.ref) + len(ev.thread_id) + len(ev.turn_id) + len(ev.kind) + len((ev.status or "")) + len(
        ev.text.encode()) + 96  # JSON punctuation and field names


def _fit(ev: HistoryEvent, budget: int) -> HistoryEvent:
    raw = ev.text.encode()
    if len(raw) <= budget:
        return ev
    return dataclasses.replace(ev, text=raw[:budget].decode(errors="ignore") + "…", text_truncated=True)


def _clean(value: str | None) -> str | None:
    return " ".join(value.split())[:200] if value else None
