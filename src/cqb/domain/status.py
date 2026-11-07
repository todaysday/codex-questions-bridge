"""DTOs of the status card (design section 5): deterministic stats, no text from conversations."""

from dataclasses import dataclass

from cqb.domain.enums import ItemKind, RunState

AGENT_LINES = 6  # subagent lines the card can show; the service reads 'now' data for exactly these
PROGRESS_MAX = 650  # characters of the Status Agent's `В процессе` part (the prompt asks for about 900 in all)
NEXT_MAX = 400  # and of its `Дальше` part


@dataclass(frozen=True, slots=True)
class ThreadSnapshot:
    """What thread/read or thread/list says about one thread right now."""

    thread_id: str
    parent_id: str | None
    state: RunState
    model: str | None
    effort: str | None
    nickname: str | None
    role: str | None
    name: str | None
    updated_at: int
    path: str | None
    agent_path: str | None = None  # `source.subAgent.thread_spawn.agent_path`: "/root/<task>/<subtask>"
    depth: int | None = None  # spawn depth, 1 = direct child of the root
    cwd: str | None = None  # working directory as the daemon reports it (the card shows only its last component)

    @property
    def title(self) -> str | None:
        """What the Codex app shows: the humanized last segment of the agent path, else nickname, else role."""
        return humanize_path(self.agent_path) or self.nickname or self.role


def humanize_path(agent_path: str | None) -> str | None:
    """Last path segment, underscores as spaces, first letter capitalized; None when nothing usable is left."""
    segments = [s for s in (agent_path or "").split("/") if s.strip()]
    words = " ".join(segments[-1].replace("_", " ").split()) if segments else ""
    return words[:1].upper() + words[1:] or None


@dataclass(frozen=True, slots=True)
class Descendants:
    threads: tuple[ThreadSnapshot, ...]
    more: bool  # the daemon has further pages that were not read


@dataclass(frozen=True, slots=True)
class UsageSnapshot:
    """The latest model call: context fill and prompt-cache hit rate (never the cumulative totals)."""

    context_tokens: int
    context_window: int | None
    input_tokens: int
    cached_input_tokens: int
    total_input_tokens: int | None = None  # since the session started (cached tokens are part of the input)
    total_cached_input_tokens: int | None = None

    @property
    def fill_percent(self) -> int | None:
        if not self.context_window or self.context_window <= 0:
            return None
        return round(100 * self.context_tokens / self.context_window)

    @property
    def cache_permille(self) -> int | None:
        """Prompt-cache hit rate of the whole session in tenths of a percent, rounded DOWN (1000 only when every input
        token was cached). Not the latest call: a call that re-sends a long prefix is nearly all cached, always ~100%.
        None when the session totals are unknown: a last-call figure would mislead."""
        total, cached = self.total_input_tokens, self.total_cached_input_tokens
        if not total or total <= 0 or cached is None or not 0 <= cached <= total:
            return None
        return cached * 1000 // total


@dataclass(frozen=True, slots=True)
class GoalSummary:
    objective: str
    status: str
    tokens_used: int | None
    token_budget: int | None


@dataclass(frozen=True, slots=True)
class RateLimits:
    used_percent: int | None
    resets_at: int | None
    window_minutes: int | None


@dataclass(frozen=True, slots=True)
class ItemBrief:
    """The newest item of a turn, reduced to its kind and a short detail: no message text, no output."""

    kind: ItemKind
    finished: bool
    detail: str | None = None  # command head, `server.tool`, tool name or collab tool name
    count: int | None = None  # files changed


@dataclass(frozen=True, slots=True)
class Activity:
    """What the latest turn of a thread says about it right now (the 'now' line is derived from it)."""

    turn_running: bool
    started_at: int | None
    completed_at: int | None
    item: ItemBrief | None  # only read for a running turn, and only when asked for


@dataclass(frozen=True, slots=True)
class AgentLine:
    label: str | None  # display name; None = unknown
    model: str | None
    effort: str | None
    state: RunState
    context_percent: int | None
    last_activity: int | None
    activity: Activity | None = None
    depth: int | None = None  # 2 and deeper are drawn as nested
    compactions: int | None = None  # how many times its context was compacted; None = unknown
    cache_permille: int | None = None  # session-wide prompt-cache hit rate (see UsageSnapshot); None = unknown


@dataclass(frozen=True, slots=True)
class StatusSummary:
    """The Status Agent's answer, already parsed, bounded and redacted: what is going on, and what comes next."""

    progress: str  # three to five short lines
    next_steps: str | None = None  # two to four short lines; None when the reports do not say


@dataclass(frozen=True, slots=True)
class StatusReport:
    now: int
    title: str | None
    state: RunState
    quiet_minutes: int | None  # set when state is STALLED
    model: str | None
    effort: str | None
    turn_elapsed_s: int | None
    usage: UsageSnapshot | None
    last_activity: int | None
    goal: GoalSummary | None
    agents_total: int | None  # None: the daemon could not say
    agents_more: bool
    agents_working: int
    agents_waiting: int
    agents_idle: int
    agents: tuple[AgentLine, ...]  # already capped for display
    limits: RateLimits | None
    activity: Activity | None = None  # the root's own 'now'
    summary: StatusSummary | None = None  # None = the Status Agent was unavailable
    cwd: str | None = None  # the root's working directory
    compactions: int | None = None  # how many times the root's context was compacted; None = unknown
