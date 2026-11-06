"""Deterministic status card (design section 5): collects stats, never calls an LLM.

Status is separate from the consultant: nothing produced here goes into the consultant's context.
The daemon is reached only through the StatusSource port (read methods, no resume/subscribe).
"""

import asyncio
import dataclasses
import logging
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Protocol

from cqb.domain.enums import RunState
from cqb.domain.status import (
    AgentLine, Descendants, GoalSummary, RateLimits, StatusReport, ThreadSnapshot, UsageSnapshot,
)
from cqb.services.live_state import LiveState

log = logging.getLogger(__name__)

AGENT_FETCH_LIMIT = 50
AGENT_LINES = 8
STALL_SECONDS = 600
LIMITS_TTL = 30
MIN_COLLECT_INTERVAL = 3
COLLECT_TIMEOUT = 15
RECENT_MAX = 16
NARRATIVE_MAX = 300


class StatusUnavailable(Exception):
    """The thread or the daemon cannot be read right now."""


class StatusSource(Protocol):
    async def thread(self, thread_id: str) -> ThreadSnapshot | None: ...
    async def descendants(self, root_id: str, limit: int) -> Descendants | None: ...
    async def running_turn_start(self, thread_id: str) -> int | None: ...
    async def goal(self, thread_id: str) -> GoalSummary | None: ...
    async def rate_limits(self) -> RateLimits | None: ...


class StatusNarrator(Protocol):
    """Optional short narrative from a separate Status Agent (a later increment)."""

    async def narrate(self, report: StatusReport) -> str | None: ...


class NoNarrator:
    async def narrate(self, report: StatusReport) -> str | None:
        return None


class StatusService:
    def __init__(
        self,
        source: StatusSource,
        live: LiveState,
        root_usage: Callable[[str], UsageSnapshot | None],
        source_id: str,
        narrator: StatusNarrator | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._source = source
        self._live = live
        self._root_usage = root_usage
        self._source_id = source_id
        self._narrator = narrator or NoNarrator()
        self._clock = clock
        self._limits: tuple[float, RateLimits | None] | None = None
        self._recent: OrderedDict[str, tuple[float, StatusReport]] = OrderedDict()

    async def collect(self, source_id: str, root_thread_id: str) -> StatusReport:
        if source_id != self._source_id:
            raise StatusUnavailable("unknown source")
        now = self._clock()
        cached = self._recent.get(root_thread_id)
        if cached and now - cached[0] < MIN_COLLECT_INTERVAL:
            return cached[1]
        try:
            async with asyncio.timeout(COLLECT_TIMEOUT):
                report = await self._collect(root_thread_id)
        except TimeoutError:
            raise StatusUnavailable("timeout") from None
        report = await self._narrate(report)
        self._recent[root_thread_id] = (now, report)
        self._recent.move_to_end(root_thread_id)
        while len(self._recent) > RECENT_MAX:
            self._recent.popitem(last=False)
        return report

    async def _collect(self, root_id: str) -> StatusReport:
        root = await self._source.thread(root_id)
        if root is None:
            raise StatusUnavailable("thread unreadable")
        now = int(self._clock())
        live = self._live.get(root_id)
        active = root.state in (RunState.WORKING, RunState.WAITING_INPUT, RunState.WAITING_APPROVAL)
        skip_goal = live is not None and live.has_goal is False
        started, goal, agents, limits = await asyncio.gather(
            self._source.running_turn_start(root_id) if active else _none(),
            _none() if skip_goal else self._source.goal(root_id),
            self._source.descendants(root_id, AGENT_FETCH_LIMIT),
            self._rate_limits(now),
        )
        last = max(root.updated_at, live.seen_at if live else 0) or None
        state, quiet = root.state, None
        if state is RunState.WORKING and last is not None and now - last > STALL_SECONDS:
            state, quiet = RunState.STALLED, (now - last) // 60
        usage = self._root_usage(root.path) if root.path else None
        lines, total, working, waiting, idle = self._agents(agents, now)
        return StatusReport(
            now=now, title=root.name, state=state, quiet_minutes=quiet, model=root.model, effort=root.effort,
            turn_elapsed_s=max(now - started, 0) if started is not None else None,
            usage=usage, last_activity=last, goal=goal,
            agents_total=total, agents_more=bool(agents and agents.more),
            agents_working=working, agents_waiting=waiting, agents_idle=idle, agents=lines, limits=limits,
        )

    def _agents(
        self, found: Descendants | None, now: int
    ) -> tuple[tuple[AgentLine, ...], int | None, int, int, int]:
        if found is None:
            return (), None, 0, 0, 0
        threads = found.threads
        working = sum(t.state is RunState.WORKING for t in threads)
        waiting = sum(t.state.is_waiting for t in threads)
        ranked = sorted(threads, key=lambda t: (_rank(t.state), -self._last(t)))
        lines = tuple(self._line(t) for t in ranked[:AGENT_LINES])
        return lines, len(threads), working, waiting, len(threads) - working - waiting

    def _line(self, t: ThreadSnapshot) -> AgentLine:
        live = self._live.get(t.thread_id)
        return AgentLine(
            label=t.nickname or t.role or t.thread_id[:8], model=t.model, effort=t.effort, state=t.state,
            context_percent=live.usage.fill_percent if live and live.usage else None,
            last_activity=self._last(t) or None,
        )

    def _last(self, t: ThreadSnapshot) -> int:
        live = self._live.get(t.thread_id)
        return max(t.updated_at, live.seen_at if live else 0)

    async def _rate_limits(self, now: int) -> RateLimits | None:
        if self._limits is not None and now - self._limits[0] < LIMITS_TTL:
            return self._limits[1]
        limits = await self._source.rate_limits()
        self._limits = (now, limits)
        return limits

    async def _narrate(self, report: StatusReport) -> StatusReport:
        try:
            text = await self._narrator.narrate(report)
        except Exception:
            log.exception("status narrator failed")
            return report
        return dataclasses.replace(report, narrative=text[:NARRATIVE_MAX]) if text else report


def _rank(state: RunState) -> int:
    return 0 if state.is_waiting else 1 if state is RunState.WORKING else 2


async def _none() -> None:
    return None
