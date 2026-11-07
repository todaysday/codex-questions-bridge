from dataclasses import dataclass

from cqb.domain.enums import ArchiveState, ConsultantState, QuestionLifecycle, RunState, VerificationSource


@dataclass(frozen=True, slots=True)
class Source:
    source_id: str
    label: str
    backend: str = "app_server"
    enabled: bool = True


@dataclass(frozen=True, slots=True)
class Thread:
    source_id: str
    thread_id: str
    root_thread_id: str
    parent_thread_id: str | None
    source_kind: str | None
    display_name: str | None
    cwd: str | None
    updated_at: int
    archive_state: ArchiveState
    archive_verified_at: int | None
    last_seen_at: int
    run_state: RunState | None = None  # status from the last inventory poll; None: never reported

    @property
    def is_root(self) -> bool:
        return self.parent_thread_id is None


@dataclass(frozen=True, slots=True)
class QuestionOption:
    label: str
    description: str = ""


@dataclass(frozen=True, slots=True)
class QuestionItem:
    id: str
    header: str
    text: str
    is_other: bool
    options: tuple[QuestionOption, ...]


@dataclass(frozen=True, slots=True)
class Question:
    """One request_user_input request. Secret requests carry no items."""

    question_id: str
    source_id: str
    root_thread_id: str
    origin_thread_id: str
    turn_id: str
    tool_use_id: str
    request_id: str | None
    items: tuple[QuestionItem, ...]
    is_secret: bool
    is_blocking: bool
    lifecycle: QuestionLifecycle
    verification_source: VerificationSource
    staged: dict[str, tuple[str, ...]] | None
    delivered_to_codex: bool
    created_at: int
    updated_at: int
    card_message_id: int | None = None  # the question card in the consultant topic of the root


@dataclass(frozen=True, slots=True)
class Consultant:
    consultant_id: int
    source_id: str
    root_thread_id: str
    tg_chat_id: int
    tg_topic_id: int | None
    pico_session_key: str
    state: ConsultantState
    created_at: int
    updated_at: int


@dataclass(frozen=True, slots=True)
class TelegramAction:
    action_key: str
    action: str
    source_id: str | None
    root_thread_id: str | None
    payload: str | None
    expires_at: int
