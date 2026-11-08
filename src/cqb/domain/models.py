from dataclasses import dataclass

from cqb.domain.enums import AgentQuestionState, ArchiveState, ConsultantState, QuestionLifecycle, RunState, VerificationSource


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
    card_message_id: int | None = None  # the first message of the question card in the consultant topic
    display_no: int = 0  # `Q<n>`: unique and stable within the root thread; 0 until stored
    card_extra_ids: tuple[int, ...] = ()  # continuation messages of a long card


@dataclass(frozen=True, slots=True)
class AgentQuestionItem:
    """One question of an agentMessage as parsed: a title and plain-string options (no ids on the wire).
    `complete` is False when a bound cut the text, so the question cannot be answered from Telegram."""

    title: str
    options: tuple[str, ...]
    complete: bool = True


@dataclass(frozen=True, slots=True)
class AgentQuestion:
    """ONE question an agent put into a final agentMessage (delivery async); a message with several questions
    gives several of these, each with its own number, card and answer. They never block the turn.

    Identity is the message item id plus `index` (the question's position in the message). Pending until a
    userMessage answers it: one that names it (questionItemId), or a later one that names no question at all.
    `staged` is the owner's choice; `sent_at` is set once the answer message was sent (not yet seen).
    """

    agent_question_id: str
    source_id: str
    thread_id: str
    root_thread_id: str
    turn_id: str
    item_id: str
    index: int
    title: str
    options: tuple[str, ...]
    answerable: bool  # False when a bound cut the text: the answer would not quote it exactly
    state: AgentQuestionState
    staged: str | None
    sent_at: int | None
    created_at: int
    updated_at: int
    card_message_id: int | None = None  # the first message of the card
    display_no: int = 0  # `Q<n>`, shared with request_user_input questions of the same root
    card_extra_ids: tuple[int, ...] = ()  # continuation messages of a long card

    @property
    def is_open(self) -> bool:
        return self.state is AgentQuestionState.PENDING


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
    focus_no: int | None = None  # the Q number the owner pointed at last (the conversation's topic)
    opening_message_id: int | None = None


@dataclass(frozen=True, slots=True)
class TelegramAction:
    action_key: str
    action: str
    source_id: str | None
    root_thread_id: str | None
    payload: str | None
    expires_at: int
