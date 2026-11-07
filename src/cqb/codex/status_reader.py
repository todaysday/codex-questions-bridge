"""Read-only daemon queries for the status card, normalized into DTOs.

Every method fails soft: a transport or protocol problem yields None, never an exception,
so one missing stat cannot hide the others. Nothing here resumes or subscribes to a thread.
"""

import logging
import re
from typing import Any, Protocol

from cqb.codex.events import run_state
from cqb.domain.enums import ItemKind
from cqb.domain.status import Activity, Descendants, GoalSummary, ItemBrief, RateLimits, ThreadSnapshot

log = logging.getLogger(__name__)

NAME_MAX = 60
OBJECTIVE_MAX = 60
WEEK_MINUTES = 10080
DETAIL_MAX = 200  # command head as read; the status service redacts and cuts it further
_SHELL_WRAPPER = re.compile(r"^(?:\S*/)?(?:bash|sh|zsh) -[a-z]*c ")


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

    async def activity(self, thread_id: str, with_item: bool) -> Activity | None:
        """The latest turn (no items: itemsView notLoaded) and, for a running one, its newest item.

        Two cheap reads. `turns/list` with itemsView full would ship the whole turn (70 KB and 160 items
        on a busy one); `items/list` with limit 1 descending ships one entry. Never resumes or subscribes.
        """
        result = await self._soft(
            "thread/turns/list", {"threadId": thread_id, "limit": 1, "itemsView": "notLoaded"}
        )
        turns = (result or {}).get("data") or []
        turn = turns[0] if turns and isinstance(turns[0], dict) else None
        if turn is None:
            return None
        running = turn.get("status") == "inProgress"
        item = None
        if running and with_item and isinstance(turn.get("id"), str):
            page = await self._soft(
                "thread/items/list",
                {"threadId": thread_id, "turnId": turn["id"], "limit": 1, "sortDirection": "desc"},
            )
            entries = (page or {}).get("data") or []
            item = brief_from_item(entries[0].get("item")) if entries and isinstance(entries[0], dict) else None
        return Activity(running, _int(turn.get("startedAt")), _int(turn.get("completedAt")), item)

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


def brief_from_item(raw: Any) -> ItemBrief | None:
    """Kind, finished flag and a short detail of a ThreadItem. Message and output text is never read."""
    if not isinstance(raw, dict):
        return None
    finished = raw.get("status") not in (None, "inProgress")
    match raw.get("type"):
        case "commandExecution":
            return ItemBrief(ItemKind.COMMAND, finished, _command_head(raw.get("command")))
        case "fileChange":
            changes = raw.get("changes")
            return ItemBrief(ItemKind.FILES, finished, None, len(changes) if isinstance(changes, list) else None)
        case "mcpToolCall":
            return ItemBrief(ItemKind.TOOL, finished, _dotted(raw.get("server"), raw.get("tool")))
        case "dynamicToolCall":
            return ItemBrief(ItemKind.TOOL, finished, _dotted(raw.get("namespace"), raw.get("tool")))
        case "agentMessage":
            return ItemBrief(ItemKind.MESSAGE, finished)
        case "reasoning":
            return ItemBrief(ItemKind.REASONING, finished)
        case "collabAgentToolCall":
            tool = raw.get("tool") if isinstance(raw.get("tool"), str) else None
            return ItemBrief(ItemKind.SPAWN if tool == "spawnAgent" else ItemKind.COLLAB, finished, tool)
        case "webSearch":
            return ItemBrief(ItemKind.WEB, finished)
    return ItemBrief(ItemKind.OTHER, finished)


def _command_head(command: Any) -> str | None:
    if not isinstance(command, str):
        return None
    head = _SHELL_WRAPPER.sub("", " ".join(command.split()), count=1).strip("\"'")
    return head[:DETAIL_MAX] or None


def _dotted(prefix: Any, name: Any) -> str | None:
    if not isinstance(name, str) or not name:
        return None
    return f"{prefix}.{name}" if isinstance(prefix, str) and prefix else name


def _int(v: Any) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) else None
