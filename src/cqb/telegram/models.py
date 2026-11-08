"""Small DTOs for the Bot API objects and keyboards this bridge uses."""

import dataclasses
import hashlib
from collections.abc import Callable
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
    style: str | None = None  # rich messages only: primary | success | danger | link
    classic_text: str | None = None  # the label on the classic inline keyboard, when it differs from `text`
    disabled: bool = False  # rich only: a label that does nothing (the page counter); left out of the classic keyboard


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
class Bold:
    text: str


Part = str | Bold | Button


@dataclass(frozen=True, slots=True)
class Para:
    """One paragraph: text, bold text and the buttons that sit in the line right after their text.

    Classic rendering joins paragraphs with `glue` (a blank line, or one newline for a tight list) and moves the
    buttons to the inline keyboard; `classic` replaces the plain text there when the classic line reads differently."""

    parts: tuple[Part, ...]
    glue: str = "\n\n"
    classic: str | None = None
    volatile: bool = False  # the header: the classic View.header, and not part of the fingerprint


@dataclass(frozen=True, slots=True)
class Row:
    """A row of buttons on their own (rich: `<tg-button-row>`, classic: one keyboard row)."""

    buttons: tuple[Button, ...]


@dataclass(frozen=True, slots=True)
class Divider:
    """A thin line between sections (rich: `<hr/>`). The classic text already has a blank line there: nothing."""


@dataclass(frozen=True, slots=True)
class Doc:
    """What a view wants to say, neutral between rich messages and the classic text + inline keyboard.

    `per_row`: how many in-line buttons share one classic keyboard row."""

    blocks: tuple[Para | Row | Divider, ...]
    per_row: int = 5

    def mapped(self, fn: Callable[[Button], Button]) -> "Doc":
        """The same document with every button replaced by `fn(button)` (binding callback ids)."""
        blocks: list[Para | Row | Divider] = []
        for b in self.blocks:
            if isinstance(b, Divider):
                blocks.append(b)
            elif isinstance(b, Row):
                blocks.append(Row(tuple(fn(x) for x in b.buttons)))
            else:
                blocks.append(dataclasses.replace(b, parts=tuple(fn(p) if isinstance(p, Button) else p for p in b.parts)))
        return dataclasses.replace(self, blocks=tuple(blocks))

    def fingerprint(self) -> str:
        """Stable over the header paragraph and over bound callback ids: changes only with what the owner would see."""
        raw = repr([_stable(b) for b in self.blocks if not (isinstance(b, Para) and b.volatile)])
        return hashlib.sha1(raw.encode()).hexdigest()


def _stable(block: "Para | Row | Divider") -> object:
    def one(b: Button) -> tuple[object, ...]:
        return (b.text, b.action, b.url, b.style, b.disabled)
    if isinstance(block, Divider):
        return "---"
    if isinstance(block, Row):
        return tuple(one(b) for b in block.buttons)
    return (block.glue, block.classic, tuple(one(p) if isinstance(p, Button) else p for p in block.parts))


@dataclass(frozen=True, slots=True)
class Reply:
    """Outcome of a button press: shown to the user as a toast (or alert) on the button. For a typed answer it is a
    message in the topic instead, and then `doc` (already bound, with its buttons) is what gets posted."""

    text: str | None = None
    alert: bool = False
    doc: Doc | None = None
