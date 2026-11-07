from enum import StrEnum


class ArchiveState(StrEnum):
    UNKNOWN = "unknown"
    ACTIVE = "active"
    ARCHIVED = "archived"


class QuestionLifecycle(StrEnum):
    CAPTURED = "captured"
    PENDING_CONFIRMED = "pending_confirmed"
    DECISION_STAGED = "decision_staged"
    RESOLVED_OBSERVED = "resolved_observed"
    EXPIRED_OR_STALE = "expired_or_stale"


OPEN_LIFECYCLES = (
    QuestionLifecycle.CAPTURED,
    QuestionLifecycle.PENDING_CONFIRMED,
    QuestionLifecycle.DECISION_STAGED,
)


class VerificationSource(StrEnum):
    HOOK = "hook"
    JOURNAL = "journal"
    HOST_RPC = "host_rpc"
    USER = "user"


class ConsultantState(StrEnum):
    CREATING = "creating"
    OPEN = "open"
    ARCHIVED = "archived"
    ERROR = "error"


class RunState(StrEnum):
    """Runtime state of a Codex thread as the status card shows it."""

    WORKING = "working"
    WAITING_INPUT = "waiting_input"
    WAITING_APPROVAL = "waiting_approval"
    IDLE = "idle"
    NOT_LOADED = "not_loaded"
    ERROR = "error"
    STALLED = "stalled"  # derived: working, but quiet for too long
    UNKNOWN = "unknown"

    @property
    def is_waiting(self) -> bool:
        return self in (RunState.WAITING_INPUT, RunState.WAITING_APPROVAL)


class ItemKind(StrEnum):
    """What the newest item of a turn is (never its text): drives the 'now' line of the status card."""

    COMMAND = "command"
    FILES = "files"
    TOOL = "tool"  # MCP or dynamic tool call
    MESSAGE = "message"
    REASONING = "reasoning"
    SPAWN = "spawn"  # collab: starts a subagent
    COLLAB = "collab"  # collab: any other subagent tool (wait, send input, ...)
    WEB = "web"
    OTHER = "other"
