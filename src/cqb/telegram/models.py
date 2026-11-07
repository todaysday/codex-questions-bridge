"""Small DTOs for the Bot API objects and keyboards this bridge uses."""

import hashlib
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ActionSpec:
    """What a button does; bound to a short opaque callback id only when it is sent."""

    action: str
    root: str | None = None
    payload: str | None = None


@dataclass(frozen=True, slots=True)
class Button:
    text: str
    action: ActionSpec | None = None
    url: str | None = None
    callback_data: str | None = None


Keyboard = tuple[tuple[Button, ...], ...]


@dataclass(frozen=True, slots=True)
class Message:
    message_id: int
    chat_id: int
    thread_id: int | None
    from_id: int | None
    from_is_bot: bool
    text: str | None
    is_forwarded: bool
    has_sender_chat: bool  # anonymous admin / channel identity
    reply_to_message_id: int | None


@dataclass(frozen=True, slots=True)
class CallbackQuery:
    query_id: str
    from_id: int
    chat_id: int | None  # None when the originating message is inaccessible
    message_id: int | None
    data: str | None


@dataclass(frozen=True, slots=True)
class Update:
    update_id: int
    message: Message | None
    callback: CallbackQuery | None


@dataclass(frozen=True, slots=True)
class SentMessage:
    message_id: int


@dataclass(frozen=True, slots=True)
class ForumTopic:
    thread_id: int
    name: str


@dataclass(frozen=True, slots=True)
class BotInfo:
    user_id: int
    username: str
    can_read_all_group_messages: bool


@dataclass(frozen=True, slots=True)
class ChatInfo:
    chat_id: int
    type: str
    is_forum: bool


@dataclass(frozen=True, slots=True)
class MemberInfo:
    status: str
    can_manage_topics: bool
    can_pin_messages: bool


@dataclass(frozen=True, slots=True)
class View:
    """Rendered message. The header carries volatile data (time) and is left out of the hash."""

    header: str
    body: str
    keyboard: Keyboard = ()

    @property
    def text(self) -> str:
        return f"{self.header}\n\n{self.body}" if self.body else self.header

    def fingerprint(self) -> str:
        raw = repr((self.body, [[(b.text, b.action, b.url) for b in row] for row in self.keyboard]))
        return hashlib.sha1(raw.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class Reply:
    """Outcome of a button press: shown to the user as a toast (or alert) on the button. For a typed answer it is a
    message in the topic instead, and then `keyboard` (already bound) goes under it."""

    text: str | None = None
    alert: bool = False
    keyboard: Keyboard = ()
