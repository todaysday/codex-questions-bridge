"""DTOs of the consultant context API (design section 4.2): everything is already bounded."""

from dataclasses import dataclass

from cqb.domain.models import QuestionItem


@dataclass(frozen=True, slots=True)
class HistoryEvent:
    """One normalized Codex item. `ref` is the evidence reference accepted by read_event."""

    ref: str
    thread_id: str
    turn_id: str
    kind: str
    at_ms: int | None
    status: str | None
    text: str
    text_truncated: bool = False
    redacted: bool = False


@dataclass(frozen=True, slots=True)
class EventPage:
    source_id: str
    root_id: str
    thread_id: str
    events: tuple[HistoryEvent, ...]  # chronological
    truncated: bool  # older events exist that the caps cut off


@dataclass(frozen=True, slots=True)
class ThreadOverview:
    source_id: str
    root_id: str
    thread_id: str
    name: str | None
    cwd: str | None
    state: str
    model: str | None
    effort: str | None
    updated_at: int | None
    archive_state: str | None
    agents_total: int | None
    pending_questions: int
    partial: bool  # the daemon could not be read; fields come from the local registry only


@dataclass(frozen=True, slots=True)
class AgentInfo:
    thread_id: str
    parent_id: str | None
    nickname: str | None
    role: str | None
    state: str
    model: str | None
    effort: str | None
    updated_at: int


@dataclass(frozen=True, slots=True)
class AgentList:
    source_id: str
    root_id: str
    agents: tuple[AgentInfo, ...]
    truncated: bool


@dataclass(frozen=True, slots=True)
class QuestionInfo:
    """A pending question. Secret questions carry no text at all."""

    question_id: str
    thread_id: str
    is_secret: bool
    lifecycle: str
    created_at: int
    items: tuple[QuestionItem, ...]


@dataclass(frozen=True, slots=True)
class QuestionList:
    source_id: str
    root_id: str
    questions: tuple[QuestionInfo, ...]
