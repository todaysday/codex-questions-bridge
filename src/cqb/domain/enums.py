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
