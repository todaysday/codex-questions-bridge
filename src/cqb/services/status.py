"""Status snapshot (design section 5): deterministic stats and 'now' lines, plus an optional Status Agent summary.

A snapshot is collected once per request and shown as one static message. Stats and 'now' lines never
call an LLM; the summary comes from the StatusNarrator port and may be missing (the card then says so).
Status is separate from the consultant: nothing produced here goes into the consultant's context.
The daemon is reached only through the StatusSource port (read methods, no resume/subscribe).
"""

import asyncio
import contextlib
import dataclasses
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Protocol

from cqb.domain.enums import RunState
from cqb.domain.status import (
    AGENT_LINES, Activity, AgentLine, Descendants, GoalSummary, RateLimits, StatusReport, StatusSummary, ThreadSnapshot,
    UsageSnapshot, StatusSummaryError,
)
from cqb.security import redact
from cqb.services.live_state import LiveState

log = logging.getLogger(__name__)

AGENT_FETCH_LIMIT = 300  # three daemon pages; more shows as `300+`
AGENT_TOTAL_FETCH_LIMIT = 10000
AGENT_TOTAL_BATCH = 32
AGENT_TOTAL_ENUM_TIMEOUT = 3
AGENT_TOTAL_TIMEOUT = 8
STALL_SECONDS = 600
LIMITS_TTL = 30
COLLECT_TIMEOUT = 15  # stats and 'now' lines; the summary has its own, much longer deadline
NOW_FETCHES = 4  # concurrent per-thread 'now' reads
DETAIL_MAX = 80  # a command head in a 'now' line
SUMMARY_TIMEOUT = 300
SUMMARY_GRACE = 5  # the narrator stops its own turn at the deadline; this only bounds a narrator that does not


class StatusUnavailable(Exception):
    """The thread or the daemon cannot be read right now."""


class StatusSource(Protocol):
    async def thread(self, thread_id: str) -> ThreadSnapshot | None: ...
    async def descendants(self, root_id: str, limit: int) -> Descendants | None: ...
    async def archived_descendants(self, root_id: str, limit: int) -> Descendants | None: ...
    async def activity(self, thread_id: str, with_item: bool) -> Activity | None: ...
    async def goal(self, thread_id: str) -> GoalSummary | None: ...
    async def rate_limits(self) -> RateLimits | None: ...


class StatusNarrator(Protocol):
    """A short summary from a separate Status Agent; None when it is unavailable or ran out of time."""

    async def summarize(self, source_id: str, root_id: str, timeout: float) -> StatusSummary | StatusSummaryError | None: ...


class NoNarrator:
    async def summarize(self, source_id: str, root_id: str, timeout: float) -> StatusSummary | None:
        return None


class StatusService:
    def __init__(
        self,
        source: StatusSource,
        live: LiveState,
        tail_usage: Callable[[str], UsageSnapshot | None],
        source_id: str,
        narrator: StatusNarrator | None = None,
        clock: Callable[[], float] = time.time,
        summary_timeout: float = SUMMARY_TIMEOUT,
        compactions: Callable[[str], Awaitable[int | None]] | None = None,
    ) -> None:
        self._source = source
        self._live = live
        self._tail_usage = tail_usage  # rollout path -> latest usage
        self._compactions = compactions  # rollout path -> how many times the context was compacted (a grep child, may take a moment)
        self._source_id = source_id
        self._narrator = narrator or NoNarrator()
        self._clock = clock
        self._summary_timeout = summary_timeout
        self._limits: tuple[float, RateLimits | None] | None = None

    async def snapshot(self, source_id: str, root_thread_id: str) -> StatusReport:
        """Everything the card shows, ready together: numbers, 'now' lines and the summary (or None).

        The summary is gathered concurrently and never delays or fails the rest: the whole call is
        bounded by the summary timeout, and a narrator that fails or runs out of time yields no summary.
        """
        summary = asyncio.create_task(self._summarize(root_thread_id), name="status-summary")
        try:
            report = await self.collect(source_id, root_thread_id)
            return dataclasses.replace(report, summary=await summary)
        finally:
            if not summary.done():
                summary.cancel()
                await asyncio.gather(summary, return_exceptions=True)

    async def collect(self, source_id: str, root_thread_id: str) -> StatusReport:
        """Deterministic part only: stats and 'now' lines."""
        if source_id != self._source_id:
            raise StatusUnavailable("unknown source")
        try:
            async with asyncio.timeout(COLLECT_TIMEOUT):
                return await self._collect(root_thread_id)
        except TimeoutError:
            raise StatusUnavailable("timeout") from None

    async def _summarize(self, root_id: str) -> StatusSummary | StatusSummaryError:
        try:
            async with asyncio.timeout(self._summary_timeout + SUMMARY_GRACE):
                text = await self._narrator.summarize(self._source_id, root_id, self._summary_timeout)
        except TimeoutError:
            log.warning("status summary timed out")
            return StatusSummaryError("агент статуса не ответил вовремя")
        except Exception as exc:
            log.warning("status narrator failed: %s", type(exc).__name__)  # never the message: it may quote text
            return StatusSummaryError(f"ошибка агента статуса: {type(exc).__name__}")
        return text or StatusSummaryError("агент статуса не настроен или не вернул ответ")

    async def _collect(self, root_id: str) -> StatusReport:
        root = await self._source.thread(root_id)
        if root is None:
            raise StatusUnavailable("thread unreadable")
        now = int(self._clock())
        live = self._live.get(root_id)
        skip_goal = live is not None and live.has_goal is False
        activity, goal, agents, archived, limits, compacted = await asyncio.gather(
            self._activity(root_id, root.state, turn_needed=True),
            _none() if skip_goal else self._source.goal(root_id),
            self._source.descendants(root_id, AGENT_FETCH_LIMIT),
            self._archived_agents(root_id),
            self._rate_limits(now),
            self._compacted(root.path),
        )
        last = max(root.updated_at, live.seen_at if live else 0) or None
        state, quiet = root.state, None
        if state is RunState.WORKING and last is not None and now - last > STALL_SECONDS:
            state, quiet = RunState.STALLED, (now - last) // 60
        usage = self._tail_usage(root.path) if root.path else None
        started = activity.started_at if activity and activity.turn_running else None
        active = root.state in (RunState.WORKING, RunState.WAITING_INPUT, RunState.WAITING_APPROVAL)
        all_agents = agents
        if agents and agents.more:
            try:
                async with asyncio.timeout(AGENT_TOTAL_ENUM_TIMEOUT):
                    all_agents = await self._source.descendants(root_id, AGENT_TOTAL_FETCH_LIMIT) or agents
            except TimeoutError:
                all_agents = agents
        if all_agents:
            all_agents = Descendants(
                all_agents.threads + (archived.threads if archived else ()),
                all_agents.more or archived is None or archived.more,
            )
        lines, total, working, waiting, idle, sub_tokens, sub_incomplete = await self._agents(agents, all_agents)
        return StatusReport(
            now=now, title=root.name, state=state, quiet_minutes=quiet, model=root.model, effort=root.effort,
            turn_elapsed_s=max(now - started, 0) if active and started is not None else None,
            usage=usage, last_activity=last, goal=goal,
            agents_total=total, agents_more=bool(agents and agents.more),
            agents_working=working, agents_waiting=waiting, agents_idle=idle, agents=lines, limits=limits,
            activity=activity, cwd=root.cwd, compactions=compacted, thread_id=root.thread_id,
            subagent_tokens=sub_tokens, subagent_tokens_incomplete=sub_incomplete,
        )

    async def _agents(
        self, found: Descendants | None, all_found: Descendants | None
    ) -> tuple[tuple[AgentLine, ...], int | None, int, int, int, int | None, bool]:
        if found is None:
            return (), None, 0, 0, 0, None, True
        threads = found.threads
        working = sum(t.state is RunState.WORKING for t in threads)
        waiting = sum(t.state.is_waiting for t in threads)
        ranked = sorted(threads, key=lambda t: (_rank(t.state), -self._last(t)))[:AGENT_LINES]
        sem = asyncio.Semaphore(NOW_FETCHES)
        activities, extras = await asyncio.gather(
            asyncio.gather(*(self._activity(t.thread_id, t.state, sem=sem) for t in ranked)),
            asyncio.gather(*(self._rollout_facts(t, sem) for t in ranked)),
        )
        lines = tuple(self._line(t, a, *e) for t, a, e in zip(ranked, activities, extras))
        selected = {t.thread_id: facts[0] for t, facts in zip(ranked, extras)}
        sub_tokens, incomplete = await self._subagent_tokens(all_found, selected)
        return lines, len(threads), working, waiting, len(threads) - working - waiting, sub_tokens, incomplete

    async def _subagent_tokens(
        self, found: Descendants | None, selected: dict[str, UsageSnapshot | None],
    ) -> tuple[int | None, bool]:
        if found is None:
            return None, True
        total, known, incomplete = 0, 0, found.more
        unique = {t.thread_id: t for t in found.threads}
        incomplete |= not selected.keys() <= unique.keys()
        threads = list(unique.values())
        try:
            async with asyncio.timeout(AGENT_TOTAL_TIMEOUT):
                for offset in range(0, len(threads), AGENT_TOTAL_BATCH):
                    batch = threads[offset:offset + AGENT_TOTAL_BATCH]
                    remaining = [t for t in batch if t.thread_id not in selected]
                    usages = await asyncio.to_thread(
                        lambda: [self._tail_usage(t.path) if t.path else None for t in remaining]
                    )
                    current = dict(zip((t.thread_id for t in remaining), usages))
                    for thread in batch:
                        usage = selected.get(thread.thread_id) if thread.thread_id in selected else current[thread.thread_id]
                        if usage is None or usage.total_tokens is None:
                            live = self._live.get(thread.thread_id)
                            if live and live.usage and live.usage.total_tokens is not None:
                                usage = live.usage
                        if usage is None or usage.total_tokens is None:
                            incomplete = True
                        else:
                            total += usage.total_tokens
                            known += 1
        except TimeoutError:
            incomplete = True
        return (total if known or not threads else None), incomplete

    async def _archived_agents(self, root_id: str) -> Descendants | None:
        try:
            async with asyncio.timeout(AGENT_TOTAL_ENUM_TIMEOUT):
                return await self._source.archived_descendants(root_id, AGENT_TOTAL_FETCH_LIMIT)
        except TimeoutError:
            return None

    async def _rollout_facts(self, t: ThreadSnapshot, sem: asyncio.Semaphore) -> tuple[UsageSnapshot | None, int | None]:
        """Context usage and compaction count of one subagent from its rollout, which survives a Bridge restart.

        The daemon writes the rollout when it broadcasts, so the tail is never older than the live value: it wins,
        and the live value only fills in when the tail has none (no path, unreadable, no token_count yet)."""
        async with sem:
            usage = self._tail_usage(t.path) if t.path else None
            live = self._live.get(t.thread_id)
            return usage or (live.usage if live else None), await self._compacted(t.path)

    async def _compacted(self, path: str | None) -> int | None:
        if not path or self._compactions is None:
            return None
        try:
            return await self._compactions(path)
        except Exception as exc:  # fail soft: the counter is a nicety
            log.warning("compaction count failed: %s", type(exc).__name__)
            return None

    def _line(
        self, t: ThreadSnapshot, activity: Activity | None, usage: UsageSnapshot | None, compactions: int | None
    ) -> AgentLine:
        return AgentLine(
            label=t.title, depth=t.depth, model=t.model, effort=t.effort, state=t.state,
            context_percent=usage.fill_percent if usage else None,
            last_activity=self._last(t) or None, activity=activity, compactions=compactions,
            cache_permille=usage.cache_permille if usage else None,
            thread_id=t.thread_id,
        )

    def _last(self, t: ThreadSnapshot) -> int:
        live = self._live.get(t.thread_id)
        return max(t.updated_at, live.seen_at if live else 0)

    async def _activity(
        self, thread_id: str, state: RunState, *, turn_needed: bool = False, sem: asyncio.Semaphore | None = None
    ) -> Activity | None:
        """Read-only 'now' data. A waiting thread needs no item (its state says what it waits for); a
        subagent that waits needs no read at all. The root always reads its turn for the elapsed time."""
        if state.is_waiting and not turn_needed:
            return None
        async with sem or contextlib.nullcontext():
            activity = await self._source.activity(thread_id, state is RunState.WORKING)
        return _safe(activity)

    async def _rate_limits(self, now: int) -> RateLimits | None:
        if self._limits is not None and now - self._limits[0] < LIMITS_TTL:
            return self._limits[1]
        limits = await self._source.rate_limits()
        self._limits = (now, limits)
        return limits


def _safe(activity: Activity | None) -> Activity | None:
    """A command head can carry a secret: redact first, cut after (a token must not be cut in half)."""
    item = activity.item if activity else None
    if activity is None or item is None or item.detail is None:
        return activity
    detail = redact(item.detail)[0].strip()
    detail = detail if len(detail) <= DETAIL_MAX else detail[: DETAIL_MAX - 1] + "…"
    return dataclasses.replace(activity, item=dataclasses.replace(item, detail=detail or None))


def _rank(state: RunState) -> int:
    return 0 if state is RunState.WORKING else 1 if state.is_waiting else 2


async def _none() -> None:
    return None
