"""DTOs of the status card (design section 5): deterministic stats, no text from conversations."""

from dataclasses import dataclass

from cqb.domain.enums import RunState


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

    @property
    def fill_percent(self) -> int | None:
        if not self.context_window or self.context_window <= 0:
            return None
        return round(100 * self.context_tokens / self.context_window)

    @property
    def cache_percent(self) -> int | None:
        if self.input_tokens <= 0:
            return None
        return round(100 * self.cached_input_tokens / self.input_tokens)


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
class AgentLine:
    label: str
    model: str | None
    effort: str | None
    state: RunState
    context_percent: int | None
    last_activity: int | None


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
    narrative: str | None = None
