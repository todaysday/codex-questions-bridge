"""Read-only daemon queries for the status card, normalized into DTOs.

Every method fails soft: a transport or protocol problem yields None, never an exception,
so one missing stat cannot hide the others. Nothing here resumes or subscribes to a thread.
"""

import logging
from typing import Any, Protocol

from cqb.codex.events import run_state
from cqb.domain.status import Descendants, GoalSummary, RateLimits, ThreadSnapshot

log = logging.getLogger(__name__)

NAME_MAX = 60
OBJECTIVE_MAX = 60
WEEK_MINUTES = 10080


class Caller(Protocol):
    async def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]: ...


class DaemonStatusReader:
    def __init__(self, client: Caller) -> None:
        self._client = client

    async def thread(self, thread_id: str) -> ThreadSnapshot | None:
        result = await self._soft("thread/read", {"threadId": thread_id, "includeTurns": False})
        return snapshot_from_wire(result["thread"]) if result and result.get("thread") else None

    async def descendants(self, root_id: str, limit: int) -> Descendants | None:
        """All depths, one page; the ancestor filter is cheap, an unfiltered list takes seconds."""
        result = await self._soft("thread/list", {"ancestorThreadId": root_id, "limit": limit})
        if result is None:
            return None
        threads = tuple(s for s in map(snapshot_from_wire, result.get("data") or []) if s is not None)
        return Descendants(threads, bool(result.get("nextCursor")))

    async def running_turn_start(self, thread_id: str) -> int | None:
        """Start time of the newest turn if it is still in progress (durationMs is null then)."""
        result = await self._soft("thread/turns/list", {"threadId": thread_id, "limit": 1})
        turns = (result or {}).get("data") or []
        if turns and turns[0].get("status") == "inProgress" and isinstance(turns[0].get("startedAt"), int):
            return turns[0]["startedAt"]
        return None

    async def goal(self, thread_id: str) -> GoalSummary | None:
        result = await self._soft("thread/goal/get", {"threadId": thread_id})
        raw = (result or {}).get("goal")
        if not isinstance(raw, dict):
            return None
        return GoalSummary(
            " ".join(str(raw.get("objective") or "").split())[:OBJECTIVE_MAX],
            str(raw.get("status") or ""),
            _int(raw.get("tokensUsed")), _int(raw.get("tokenBudget")),
        )

    async def rate_limits(self) -> RateLimits | None:
        result = await self._soft("account/rateLimits/read", None)
        raw = (result or {}).get("rateLimits")
        if not isinstance(raw, dict):
            return None
        windows = [w for w in (raw.get("primary"), raw.get("secondary")) if isinstance(w, dict)]
        if not windows:
            return None
        window = next((w for w in windows if w.get("windowDurationMins") == WEEK_MINUTES), windows[0])
        return RateLimits(
            _int(window.get("usedPercent")), _int(window.get("resetsAt")), _int(window.get("windowDurationMins"))
        )

    async def _soft(self, method: str, params: dict[str, Any] | None) -> dict[str, Any] | None:
        try:
            return await self._client.call(method, params)
        except Exception as exc:  # NotConnected, RpcError, timeout: all mean "no data"
            log.debug("status read %s failed: %s", method, type(exc).__name__)
            return None


def snapshot_from_wire(raw: dict[str, Any]) -> ThreadSnapshot | None:
    try:
        name = raw.get("name") or " ".join((raw.get("preview") or "").split())
        return ThreadSnapshot(
            thread_id=raw["id"],
            parent_id=raw.get("parentThreadId"),
            state=run_state(raw.get("status") or {}),
            model=raw.get("model") or None,
            effort=raw.get("reasoningEffort") or None,
            nickname=raw.get("agentNickname") or None,
            role=raw.get("agentRole") or None,
            name=name[:NAME_MAX] or None,
            updated_at=int(raw.get("updatedAt") or 0),
            path=raw.get("path") if isinstance(raw.get("path"), str) else None,
        )
    except (KeyError, TypeError, ValueError):
        return None


def _int(v: Any) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) else None
