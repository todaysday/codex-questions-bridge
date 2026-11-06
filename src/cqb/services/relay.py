"""Consultant relay (design sections 4.1, 10, 11): topic message -> consultant dialog -> same topic.

The router has already authorized the sender and resolved the topic to a consultant, so only
registered topics reach `relay`; this class still refuses anything that is not an open binding.
The dialog (PicoClaw gateway) and the messenger are ports: no network code lives here.
Replies are plain text, bounded, and failures become one readable line, never a stack trace.
"""

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from cqb.domain.enums import ConsultantState
from cqb.domain.models import Consultant

log = logging.getLogger(__name__)

REPLY_MAX_CHARS = 12000  # three Telegram messages at most
TYPING_SECONDS = 4.0  # Telegram shows "typing" for about 5 s per call

NOT_CONFIGURED = "Чат консультанта не настроен (нет секции [pico] или токенов)."
UNAVAILABLE = "Консультант временно недоступен"
BUSY = "Консультант занят, повторите чуть позже"
QUEUED = "В очереди: консультант ещё отвечает на предыдущее сообщение"
QUEUE_FULL = "В очереди уже слишком много сообщений; дождитесь ответа консультанта"
TIMED_OUT = "Консультант не ответил вовремя"
FAILED = "Консультант вернул ошибку; подробности в логе"
TRUNCATED = "\n…[ответ обрезан]"


class Outcome(StrEnum):
    REPLY = "reply"
    UNAVAILABLE = "unavailable"
    BUSY = "busy"
    TIMEOUT = "timeout"
    ERROR = "error"


class Submit(StrEnum):
    ACCEPTED = "accepted"  # sent now
    QUEUED = "queued"  # behind a turn that is still running
    STOPPED = "stopped"  # `/stop` was passed to the running turn; no turn of its own
    QUEUE_FULL = "queue_full"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class DialogResult:
    outcome: Outcome
    text: str = ""


Deliver = Callable[[DialogResult], Awaitable[None]]


class ConsultantDialog(Protocol):
    def submit(self, session_id: str, text: str, deliver: Deliver) -> Submit:
        """Queue one user message; `deliver` is awaited once with the turn's result."""


class TopicSink(Protocol):
    async def say(self, topic_id: int, text: str) -> None: ...
    async def typing(self, topic_id: int) -> None: ...


class ConsultantRelay:
    def __init__(self, dialog: ConsultantDialog | None, sink: TopicSink, *, typing_seconds: float = TYPING_SECONDS) -> None:
        self._dialog = dialog  # None: consultants are not configured
        self._sink = sink
        self._typing_seconds = typing_seconds
        self._pending: dict[int, list] = {}  # topic -> [turns not yet delivered, typing task]

    async def relay(self, consultant: Consultant, text: str) -> None:
        topic = consultant.tg_topic_id
        if consultant.state is not ConsultantState.OPEN or topic is None or not text.strip():
            return
        if self._dialog is None:
            await self._say(topic, NOT_CONFIGURED)
            return

        async def deliver(result: DialogResult) -> None:
            self._leave(topic)
            await self._say(topic, present(result))

        self._enter(topic)  # typing starts before submit so a fast reply cannot race the task creation
        outcome = self._dialog.submit(consultant.pico_session_key, text, deliver)
        if outcome in (Submit.QUEUE_FULL, Submit.UNAVAILABLE, Submit.STOPPED):
            self._leave(topic)  # deliver will never run for these
        match outcome:
            case Submit.QUEUED:
                await self._say(topic, QUEUED)
            case Submit.QUEUE_FULL:
                await self._say(topic, QUEUE_FULL)
            case Submit.UNAVAILABLE:
                await self._say(topic, UNAVAILABLE)

    def _enter(self, topic: int) -> None:
        entry = self._pending.setdefault(topic, [0, None])
        entry[0] += 1
        if entry[1] is None:
            entry[1] = asyncio.create_task(self._typing(topic), name="consultant-typing")

    def _leave(self, topic: int) -> None:
        entry = self._pending.get(topic)
        if entry is None:
            return
        entry[0] -= 1
        if entry[0] <= 0:
            del self._pending[topic]
            if entry[1] is not None:
                entry[1].cancel()

    async def _typing(self, topic: int) -> None:
        """Keeps the typing indicator alive for as long as a turn is pending: no extra messages."""
        while True:
            with contextlib.suppress(Exception):  # cosmetic; a failed action must never stop the relay
                await self._sink.typing(topic)
            await asyncio.sleep(self._typing_seconds)

    async def _say(self, topic: int, text: str) -> None:
        try:
            await self._sink.say(topic, text)
        except Exception as exc:  # the Bot API client already retried; a lost reply must not kill a worker
            log.warning("could not post consultant reply to topic %d: %s", topic, type(exc).__name__)


def present(result: DialogResult) -> str:
    """The user-visible line for a dialog result."""
    match result.outcome:
        case Outcome.REPLY:
            text = result.text.strip() or FAILED
            return text if len(text) <= REPLY_MAX_CHARS else text[: REPLY_MAX_CHARS - len(TRUNCATED)] + TRUNCATED
        case Outcome.UNAVAILABLE:
            return UNAVAILABLE
        case Outcome.BUSY:
            return BUSY
        case Outcome.TIMEOUT:
            return TIMED_OUT
    return FAILED
