"""Normalization of the few app-server messages the Bridge needs into DTOs.

Everything stored is truncated here, so no later layer can keep large payloads.
"""

from dataclasses import dataclass
from typing import Any

from cqb.codex.appserver import Inbound
from cqb.domain.enums import ArchiveState, RunState
from cqb.domain.models import AgentQuestionItem, QuestionItem, QuestionOption, Thread
from cqb.domain.status import UsageSnapshot

# Bounds on what is stored. They are far above anything a person writes, because a card never cuts text:
# its body carries the full title and every option (long cards are split into several messages).
MAX_ITEMS = 8
MAX_OPTIONS = 26
MAX_HEADER = 100
MAX_QUESTION = 20000
MAX_LABEL = 2000
MAX_DESCRIPTION = 8000
MAX_NAME = 80
MAX_AGENT_QUESTIONS = 20
MAX_AGENT_OPTIONS = 26
MAX_AGENT_TITLE = 20000
MAX_AGENT_OPTION = 8000


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
    state: RunState = RunState.UNKNOWN


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


@dataclass(frozen=True, slots=True)
class TokenUsageUpdated:
    thread_id: str
    usage: UsageSnapshot


@dataclass(frozen=True, slots=True)
class GoalChanged:
    thread_id: str
    present: bool  # goal/updated -> True, goal/cleared -> False


Event = (
    Connected | Disconnected | ThreadStatusChanged | QuestionRequested
    | RequestResolved | ThreadArchiveChanged | TurnEnded | TokenUsageUpdated | GoalChanged
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
                    p["threadId"], "waitingOnUserInput" in flags, "waitingOnApproval" in flags,
                    run_state(p["status"]),
                )
            case "notification", "thread/tokenUsage/updated":
                return TokenUsageUpdated(p["threadId"], usage_from_wire(p["tokenUsage"]))
            case "notification", "thread/goal/updated":
                return GoalChanged(p["threadId"], True)
            case "notification", "thread/goal/cleared":
                return GoalChanged(p["threadId"], False)
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
    except (KeyError, TypeError, AttributeError, ValueError):
        return None  # malformed payload: ignore rather than crash the pump
    return None


def agent_questions_from_item(raw: Any) -> tuple[AgentQuestionItem, ...] | None:
    """A ThreadItem agentMessage with a non-empty `questions` list -> its questions in order (the position is the
    question's index), else None.

    Only titles and options are read; the message text is never touched. A question past the bounds is kept cut
    with `complete=False`: it is shown, but not answerable from Telegram (the answer must quote the title exactly).
    """
    if not isinstance(raw, dict) or raw.get("type") != "agentMessage":
        return None
    questions = raw.get("questions")
    if not isinstance(questions, list) or not questions:
        return None
    items: list[AgentQuestionItem] = []
    for q in questions[:MAX_AGENT_QUESTIONS]:
        options = q.get("options") if isinstance(q, dict) else None
        options = [] if options is None else options
        if not isinstance(q, dict) or not isinstance(q.get("title"), str) or not isinstance(options, list) \
                or not all(isinstance(o, str) for o in options):
            return None  # an unknown shape: better unseen than answered wrongly
        complete = len(q["title"]) <= MAX_AGENT_TITLE and len(options) <= MAX_AGENT_OPTIONS \
            and all(len(o) <= MAX_AGENT_OPTION for o in options)
        items.append(AgentQuestionItem(
            q["title"][:MAX_AGENT_TITLE], tuple(o[:MAX_AGENT_OPTION] for o in options[:MAX_AGENT_OPTIONS]), complete
        ))
    return tuple(items)


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
        run_state=run_state(raw["status"]) if isinstance(raw.get("status"), dict) else None,
    )


def run_state(status: dict[str, Any]) -> RunState:
    flags = _flags(status)
    match status.get("type"):
        case "active":
            if "waitingOnUserInput" in flags:
                return RunState.WAITING_INPUT
            return RunState.WAITING_APPROVAL if "waitingOnApproval" in flags else RunState.WORKING
        case "idle":
            return RunState.IDLE
        case "notLoaded":
            return RunState.NOT_LOADED
        case "systemError":
            return RunState.ERROR
    return RunState.UNKNOWN


def usage_from_wire(raw: dict[str, Any]) -> UsageSnapshot:
    """thread/tokenUsage/updated payload: `last` (the latest call) gives the context fill; `total` (the whole
    session) provides cumulative tokens and its input and cached input for the cache hit rate."""
    last = raw["last"]
    window = raw.get("modelContextWindow")
    total = raw.get("total") if isinstance(raw.get("total"), dict) else {}
    total_input, total_cached = total.get("inputTokens"), total.get("cachedInputTokens")
    total_tokens = total.get("totalTokens")
    known = isinstance(total_input, int) and isinstance(total_cached, int)
    return UsageSnapshot(
        int(last["totalTokens"]), int(window) if window else None,
        int(last["inputTokens"]), int(last.get("cachedInputTokens") or 0),
        total_input if known else None, total_cached if known else None,
        total_tokens if type(total_tokens) is int and total_tokens >= 0 else None,
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
