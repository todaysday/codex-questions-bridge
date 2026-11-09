"""Scoped, bounded, read-only Codex context for the consultant (design section 4.2).

MCP calls carry no caller identity, so every method takes the `root_id` the scope hook has
already overwritten with the trusted value. This service still verifies it (defence in depth):
the root must be an open consultant of the configured source and every thread asked for must be
the root or one of its descendants. Redaction and byte caps are applied here, not by the caller.
"""

import asyncio
import dataclasses
import logging
import re
import time
from typing import Any, Protocol

from cqb.codex.history import SUMMARY_CHARS, HistoryReader, HistoryUnavailable, event_from_entry, split_ref
from cqb.dao.agent_questions import AgentQuestionDao
from cqb.dao.consultants import ConsultantDao
from cqb.dao.questions import QuestionDao
from cqb.dao.threads import ThreadDao
from cqb.domain.context import (
    AgentInfo, AgentList, EventPage, HistoryEvent, QuestionInfo, QuestionItemInfo, QuestionList, ThreadOverview,
)
from cqb.domain.enums import OPEN_LIFECYCLES, ConsultantState, QuestionLifecycle, RunState
from cqb.domain.labels import parse_number, question_label
from cqb.domain.models import AgentQuestion, Question
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
MAX_QUESTION_ITEMS = 10
# A listing is an overview; asking for one `number` returns that question in detail.
OVERVIEW = (500, 200, 10)  # title chars, option chars, options
DETAIL = (4000, 2000, 26)
SCOPE_FETCH = 100
STATUS_EVIDENCE_TIMEOUT = 8
_ID = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
_OPAQUE = re.compile(r"(?<![A-Za-z0-9_-])gAAAAA[A-Za-z0-9_-]{40,}={0,2}")


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
    async def archived_descendants(self, root_id: str, limit: int) -> Descendants | None: ...


class ContextService:
    def __init__(
        self,
        consultants: ConsultantDao,
        threads: ThreadDao,
        questions: QuestionDao,
        reader: ThreadReader,
        history: HistoryReader,
        source_id: str,
        agent_questions: AgentQuestionDao | None = None,
    ) -> None:
        self._agent_questions = agent_questions
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
        if self._agent_questions is not None:
            pending += len(self._agent_questions.list_open(self._source, root_id, MAX_QUESTIONS))
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
        events = cap_int("max_events", max_events, DEFAULT_EVENTS, 1, MAX_EVENTS)
        budget = cap_int("max_bytes", max_bytes, DEFAULT_BYTES, MIN_BYTES, MAX_BYTES)
        if since is not None and (isinstance(since, bool) or not isinstance(since, int) or since < 0):
            raise ToolError("invalid_arguments", "since must be a unix time in seconds")
        thread = await self._thread_in_scope(root_id, thread_id or root_id)
        return await self._recent_events(root_id, thread, events, budget, since)

    async def _recent_events(
        self, root_id: str, thread: str, events: int, budget: int, since: int | None = None, *, status: bool = False,
    ) -> EventPage:
        try:
            entries, more = await self._history.recent(thread, events)
        except HistoryUnavailable:
            raise ToolError("unavailable", "Codex history is not readable right now") from None
        normalized = (_status_event(thread, e) if status else event_from_entry(thread, e) for e in entries)
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

    async def status_evidence(self, root_id: str, source_id: str) -> dict[str, Any]:
        """Bounded initial evidence, independently of the model's tool calls."""
        self._root(root_id, source_id)
        result: dict[str, Any] = {"root": None, "agents": [], "incomplete": False}
        async def root() -> None:
            result["root"] = dataclasses.asdict(await self._recent_events(root_id, root_id, 30, DEFAULT_BYTES, status=True))

        async def descendants() -> None:
            active, archived = await asyncio.gather(
                self._reader.descendants(root_id, 10000), self._reader.archived_descendants(root_id, 10000)
            )
            if active is None or archived is None:
                result["incomplete"] = True
            found = [x for x in (active, archived) if x is not None]
            result["incomplete"] |= any(x.more for x in found)
            unique = {t.thread_id: t for group in found for t in group.threads}
            running = {RunState.WORKING, RunState.WAITING_INPUT, RunState.WAITING_APPROVAL}
            recent = int(time.time()) - 3600
            selected = sorted((t for t in unique.values() if t.state in running or t.updated_at >= recent),
                              key=lambda t: (t.state not in running, -t.updated_at))
            result["incomplete"] |= len(selected) > 80
            sem = asyncio.Semaphore(12)

            async def one(t: ThreadSnapshot) -> None:
                async with sem:
                    try:
                        is_active = t.state in running
                        data = {"thread_id": t.thread_id, "name": redact(_clean(t.nickname) or "")[0],
                                "state": t.state.value, "updated_at": t.updated_at}
                        if is_active:
                            page = await self._recent_events(root_id, t.thread_id, 10, 4096, status=True)
                            data["history"] = dataclasses.asdict(page)
                        else:
                            entries, _ = await self._history.recent(t.thread_id, 30)
                            final = next((e for row in reversed(entries)
                                          if (e := _status_event(t.thread_id, row, max_chars=4000))
                                          and e.kind == "agentMessage" and e.status == "final_answer"), None)
                            final = self._present(final) if final else None
                            data["summary"] = dataclasses.asdict(final) if final else None
                            if final is None:
                                result["incomplete"] = True
                        result["agents"].append(data)
                    except (HistoryUnavailable, ToolError):
                        result["incomplete"] = True

            await asyncio.gather(*(one(t) for t in selected[:80]))

        tasks = [asyncio.create_task(root()), asyncio.create_task(descendants())]
        try:
            await asyncio.wait(tasks, timeout=STATUS_EVIDENCE_TIMEOUT)
        finally:
            pending = [task for task in tasks if not task.done()]
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        for task in tasks:
            if not task.cancelled() and task.exception() is not None:
                result["incomplete"] = True
        result["incomplete"] |= bool(pending)
        return result

    async def read_event(
        self, root_id: str, event_ref: str, source_id: str | None = None, max_bytes: int | None = None
    ) -> HistoryEvent:
        self._root(root_id, source_id)
        budget = cap_int("max_bytes", max_bytes, DEFAULT_READ_BYTES, MIN_BYTES, READ_MAX_BYTES)
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

    async def questions(self, root_id: str, source_id: str | None = None, number: Any = None) -> QuestionList:
        """Questions of both kinds (pending first, then the most recent closed ones), optionally one Q number."""
        self._root(root_id, source_id)
        wanted = None
        if number is not None:
            wanted = parse_number(number)
            if wanted is None:
                raise ToolError("invalid_arguments", "number must look like Q3 or 3")
        caps = OVERVIEW if wanted is None else DETAIL
        found = [_request_info(q, caps) for q in
                 self._questions.list_for_root(self._source, root_id, MAX_QUESTIONS, wanted)]
        if self._agent_questions is not None:
            found += [_agent_info(q, caps) for q in
                      self._agent_questions.list_for_root(self._source, root_id, MAX_QUESTIONS, wanted)]
        found.sort(key=lambda i: (i.state != "pending", _number(i) if i.state == "pending" else -_number(i)))
        return QuestionList(self._source, root_id, tuple(found[:MAX_QUESTIONS]), truncated=len(found) > MAX_QUESTIONS)

    # --- scope -----------------------------------------------------------------------

    def check_scope(self, root_id: Any, source_id: Any = None) -> None:
        """Raise ToolError unless root_id is an open consultant of this source (used by other tool services)."""
        self._root(root_id, source_id)

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


def _status_event(thread: str, entry: dict[str, Any], *, max_chars: int = SUMMARY_CHARS) -> HistoryEvent | None:
    """Remove unreadable encoded data before clipping, so useful surrounding text survives."""
    def clean(value: Any) -> Any:
        if isinstance(value, str):
            return redact(_OPAQUE.sub("[encoded data omitted]", value))[0]
        if isinstance(value, list):
            return [clean(v) for v in value]
        if isinstance(value, dict):
            return {k: clean(v) for k, v in value.items()}
        return value

    item = entry.get("item")
    if not isinstance(item, dict):
        return event_from_entry(thread, entry, max_chars=max_chars)
    fields = ("text", "content", "summary", "command", "arguments", "query")
    sanitized = {**item, **{key: clean(item[key]) for key in fields if key in item}}
    event = event_from_entry(thread, {**entry, "item": sanitized}, max_chars=max_chars)
    return dataclasses.replace(event, redacted=True) if event is not None and sanitized != item else event


def cap_int(name: str, value: Any, default: int, lo: int, hi: int) -> int:
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


def _number(info: QuestionInfo) -> int:
    return int(info.number[1:])


def _cut(text: str, limit: int) -> tuple[str, bool]:
    clean = " ".join(text.split())
    return (clean, False) if len(clean) <= limit else (clean[: limit - 1] + "…", True)


def _item_info(title: str, options: list[str], caps: tuple[int, int, int]) -> QuestionItemInfo:
    title_chars, option_chars, option_max = caps
    shown_title, cut = _cut(title, title_chars)
    shown = [_cut(o, option_chars) for o in options[:option_max]]
    return QuestionItemInfo(
        shown_title, tuple(o for o, _ in shown), text_truncated=cut or len(options) > option_max or any(c for _, c in shown)
    )


def _request_info(q: Question, caps: tuple[int, int, int]) -> QuestionInfo:
    state = ("pending" if q.lifecycle in OPEN_LIFECYCLES else
             "answered" if q.lifecycle is QuestionLifecycle.RESOLVED_OBSERVED else "stale")
    items: tuple[QuestionItemInfo, ...] = ()
    if not q.is_secret:  # a secret question never leaves, whatever was stored
        items = tuple(
            _item_info(f"{i.header}: {i.text}" if i.header else i.text,
                       [f"{o.label} — {o.description}" if o.description else o.label for o in i.options], caps)
            for i in q.items[:MAX_QUESTION_ITEMS]
        )
    return QuestionInfo(q.question_id, question_label(q.display_no), "request_user_input", state,
                        q.origin_thread_id, q.is_secret, q.created_at, items)


def _agent_info(q: AgentQuestion, caps: tuple[int, int, int]) -> QuestionInfo:
    return QuestionInfo(q.agent_question_id, question_label(q.display_no), "agent_message", q.state.value,
                        q.thread_id, False, q.created_at, (_item_info(q.title, list(q.options), caps),))
