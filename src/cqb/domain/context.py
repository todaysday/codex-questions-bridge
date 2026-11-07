"""DTOs of the consultant context API (design section 4.2): everything is already bounded."""

from dataclasses import dataclass



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
class QuestionItemInfo:
    title: str
    options: tuple[str, ...]
    text_truncated: bool = False  # a bound cut the title or an option, or dropped options (ask by `number` for more)


@dataclass(frozen=True, slots=True)
class QuestionInfo:
    """A question of either kind. Secret questions carry no text at all."""

    question_id: str
    number: str  # `Q3`: what the owner calls it
    kind: str  # request_user_input | agent_message
    state: str  # pending | answered | stale
    thread_id: str
    is_secret: bool
    created_at: int
    items: tuple[QuestionItemInfo, ...]


@dataclass(frozen=True, slots=True)
class QuestionList:
    source_id: str
    root_id: str
    questions: tuple[QuestionInfo, ...]
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class FileChunk:
    """One page of a project text file: `N: text` lines, already redacted and within the byte budget."""

    root_id: str
    path: str
    text: str
    offset_lines: int
    returned_lines: int
    total_lines: int | None  # None when the scan stopped early (large file)
    truncated: bool  # more lines follow; continue at next_offset_lines
    next_offset_lines: int | None
    lines_cut: bool  # some line was longer than the per-line cap (or the budget) and is shortened
    redacted: bool
    size: int


@dataclass(frozen=True, slots=True)
class DirEntryInfo:
    name: str
    type: str  # file | dir | symlink | other
    size: int | None  # files only


@dataclass(frozen=True, slots=True)
class DirListing:
    root_id: str
    path: str
    entries: tuple[DirEntryInfo, ...]  # one level, dirs first
    hidden: int  # entries with denied names: not listed, only counted
    truncated: bool
    total: int  # visible entries seen (before the entry/byte caps)
