"""Normalization of the few app-server messages the Bridge needs into DTOs.

Everything stored is truncated here, so no later layer can keep large payloads.
"""

from dataclasses import dataclass
from typing import Any

from cqb.codex.appserver import Inbound
from cqb.domain.enums import ArchiveState
from cqb.domain.models import QuestionItem, QuestionOption, Thread

MAX_ITEMS = 8
MAX_OPTIONS = 12
MAX_HEADER = 100
MAX_QUESTION = 500
MAX_LABEL = 200
MAX_DESCRIPTION = 300
MAX_NAME = 80


@dataclass(frozen=True, slots=True)
class Connected:
    pass


@dataclass(frozen=True, slots=True)
class Disconnected:
    pass


@dataclass(frozen=True, slots=True)
class ThreadStatusChanged:
    thread_id: str
    waiting_on_user_input: bool
    waiting_on_approval: bool


@dataclass(frozen=True, slots=True)
class QuestionRequested:
    request_id: int | str
    thread_id: str
    turn_id: str
    item_id: str
    items: tuple[QuestionItem, ...]
    is_secret: bool
    is_blocking: bool


@dataclass(frozen=True, slots=True)
class RequestResolved:
    request_id: int | str
    thread_id: str


@dataclass(frozen=True, slots=True)
class ThreadArchiveChanged:
    thread_id: str
    archived: bool


@dataclass(frozen=True, slots=True)
class TurnEnded:
    thread_id: str
    turn_id: str


Event = (
    Connected | Disconnected | ThreadStatusChanged | QuestionRequested
    | RequestResolved | ThreadArchiveChanged | TurnEnded
)


def normalize(msg: Inbound) -> Event | None:
    p = msg.params or {}
    try:
        match msg.kind, msg.method:
            case "connected", _:
                return Connected()
            case "disconnected", _:
                return Disconnected()
            case "notification", "thread/status/changed":
                flags = _flags(p["status"])
                return ThreadStatusChanged(
                    p["threadId"], "waitingOnUserInput" in flags, "waitingOnApproval" in flags
                )
            case "request", "item/tool/requestUserInput":
                raw = p["questions"]
                secret = any(q.get("isSecret") for q in raw)
                return QuestionRequested(
                    msg.request_id,  # type: ignore[arg-type]
                    p["threadId"], p["turnId"], p["itemId"],
                    () if secret else tuple(_item(q) for q in raw[:MAX_ITEMS]),  # secrets never leave here
                    secret,
                    bool(p.get("isBlocking", True)),
                )
            case "notification", "serverRequest/resolved":
                return RequestResolved(p["requestId"], p["threadId"])
            case "notification", "thread/archived":
                return ThreadArchiveChanged(p["threadId"], True)
            case "notification", "thread/unarchived":
                return ThreadArchiveChanged(p["threadId"], False)
            case "notification", "turn/completed":
                return TurnEnded(p["threadId"], p["turn"]["id"])
    except (KeyError, TypeError, AttributeError):
        return None  # malformed payload: ignore rather than crash the pump
    return None


def thread_waiting_on_user(raw: dict[str, Any]) -> bool:
    return "waitingOnUserInput" in _flags(raw.get("status") or {})


def thread_from_wire(raw: dict[str, Any], source_id: str, now: int) -> Thread:
    """Metadata only. Root is provisional and archive state unknown until the caller verifies."""
    parent = raw.get("parentThreadId") or _spawn_parent(raw.get("source"))
    src = raw.get("source")
    kind = src if isinstance(src, str) else next(iter(src), None) if isinstance(src, dict) else None
    name = raw.get("name") or " ".join((raw.get("preview") or "").split())
    return Thread(
        source_id=source_id,
        thread_id=raw["id"],
        root_thread_id=parent or raw["id"],
        parent_thread_id=parent,
        source_kind=kind,
        display_name=name[:MAX_NAME] or None,
        cwd=raw.get("cwd"),
        updated_at=int(raw.get("updatedAt") or 0),
        archive_state=ArchiveState.UNKNOWN,
        archive_verified_at=None,
        last_seen_at=now,
    )


def _spawn_parent(src: Any) -> str | None:
    if isinstance(src, dict):
        spawn = (src.get("subAgent") or {})
        if isinstance(spawn, dict):
            return (spawn.get("thread_spawn") or {}).get("parent_thread_id")
    return None


def _flags(status: dict[str, Any]) -> list[str]:
    return list(status.get("activeFlags") or []) if status.get("type") == "active" else []


def _item(q: dict[str, Any]) -> QuestionItem:
    return QuestionItem(
        id=q["id"],
        header=str(q.get("header", ""))[:MAX_HEADER],
        text=str(q["question"])[:MAX_QUESTION],
        is_other=bool(q.get("isOther", False)),
        options=tuple(
            QuestionOption(str(o["label"])[:MAX_LABEL], str(o.get("description", ""))[:MAX_DESCRIPTION])
            for o in (q.get("options") or [])[:MAX_OPTIONS]
        ),
    )
