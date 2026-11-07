"""Question cards: one message per question in the consultant topic of its root, edited as its
lifecycle moves (design sections 2.3, 6). A root without an open consultant gets no card."""

import asyncio
import logging
from collections.abc import Callable, Mapping, Sequence
from typing import Protocol

from cqb.domain.enums import OPEN_LIFECYCLES, ConsultantState
from cqb.domain.models import Question
from cqb.services.questions import QuestionError
from cqb.services.registry import RegistryQuery
from cqb.telegram.api import BadRequest, BotApi
from cqb.telegram.card_view import ANSWER_LIMIT, render_card
from cqb.telegram.keys import ActionKeys
from cqb.telegram.models import Reply
from cqb.telegram.registry_view import thread_name

log = logging.getLogger(__name__)

MAX_DIRTY = 200
RETRY_SECONDS = 60  # cards that failed to post are retried at least this often


class QuestionPort(Protocol):
    def get(self, question_id: str) -> Question | None: ...
    def stage_decision(self, question_id: str, answers: Mapping[str, Sequence[str]]) -> Question: ...
    def unstage_decision(self, question_id: str) -> Question: ...
    async def answer(self, question_id: str, answers: Mapping[str, Sequence[str]] | None = None) -> None: ...
    def bind_card_message(self, question_id: str, message_id: int | None) -> None: ...
    def by_card_message(self, message_id: int) -> Question | None: ...
    def postable_without_card(self, limit: int = 50) -> list[Question]: ...
    def open_questions(self, root_thread_id: str | None = None, limit: int = 20) -> list[Question]: ...


class QuestionCards:
    def __init__(
        self,
        api: BotApi,
        keys: ActionKeys,
        questions: QuestionPort,
        registry: RegistryQuery,
        chat_id: int,
        on_change: Callable[[], None],
    ) -> None:
        self._api = api
        self._keys = keys
        self._questions = questions
        self._registry = registry
        self._chat_id = chat_id
        self._on_change = on_change
        self._dirty: dict[str, None] = {}
        self._wake = asyncio.Event()
        self._lock = asyncio.Lock()

    # Listeners for QuestionService.subscribe: they only mark the card dirty, so a slow or
    # rate-limited Telegram never blocks the app-server event stream.
    async def on_question(self, q: Question) -> None:
        self.mark(q.question_id)
        self._on_change()  # the registry row shows or loses its ❓

    async def on_resolved(self, q: Question) -> None:
        self.mark(q.question_id)
        self._on_change()

    def mark(self, question_id: str) -> None:
        self._dirty.pop(question_id, None)
        self._dirty[question_id] = None
        while len(self._dirty) > MAX_DIRTY:
            del self._dirty[next(iter(self._dirty))]
        self._wake.set()

    def mark_root(self, root_thread_id: str) -> None:
        """A consultant of this root opened (created or reopened): post cards of its pending questions."""
        for q in self._questions.open_questions(root_thread_id):
            if q.card_message_id is None:
                self.mark(q.question_id)

    async def run(self) -> None:
        self._wake.set()  # first pass posts cards that were captured while Telegram was down
        while True:
            try:
                await asyncio.wait_for(self._wake.wait(), RETRY_SECONDS)
            except TimeoutError:
                pass
            self._wake.clear()
            await self.process()

    async def process(self) -> None:
        for q in self._questions.postable_without_card():
            self.mark(q.question_id)
        while self._dirty:
            question_id = next(iter(self._dirty))
            del self._dirty[question_id]
            try:
                await self.refresh_card(question_id)
            except Exception as exc:
                log.warning("card %s not updated: %s", question_id, type(exc).__name__)

    async def refresh_card(self, question_id: str) -> None:
        async with self._lock:
            q = self._questions.get(question_id)
            if q is None:
                return
            row = self._registry.describe(q.root_thread_id)
            name = thread_name(row) if row else q.root_thread_id[:8]
            view = render_card(q, name)
            keyboard = self._keys.bind(view.keyboard)
            if q.card_message_id is not None:
                try:
                    await self._api.edit_message_text(self._chat_id, q.card_message_id, view.text, keyboard)
                    return
                except BadRequest as exc:
                    if exc.not_modified:
                        return
                    if not exc.gone:
                        raise
            if q.lifecycle not in OPEN_LIFECYCLES:
                return  # nothing actionable to post for a question that already ended
            if row is None or row.consultant_state is not ConsultantState.OPEN or not row.topic_id:
                return  # no open consultant: the registry row shows ❓, nothing is posted
            sent = await self._api.send_message(self._chat_id, view.text, thread_id=row.topic_id, keyboard=keyboard)
            self._questions.bind_card_message(question_id, sent.message_id)

    # Button handlers

    async def stage(self, question_id: str, option_index: int) -> Reply:
        q = self._questions.get(question_id)
        if q is None or q.is_secret or len(q.items) != 1:
            return Reply("Ответ из Telegram недоступен", alert=True)
        options = q.items[0].options
        if not 0 <= option_index < len(options):
            return Reply("Нет такого варианта", alert=True)
        return await self._stage_answer(q, options[option_index].label)

    async def stage_text(self, message_reply_to: int, text: str) -> Reply | None:
        """Free text replied to a card stages the `Другое` answer (only for isOther questions)."""
        q = self._questions.by_card_message(message_reply_to)
        if q is None:
            return None  # not a card: ordinary consultant chat
        if q.is_secret:
            return Reply("Секретный ввод: ответьте в Codex, Bridge его не принимает")
        if len(q.items) != 1 or not q.items[0].is_other:
            return Reply("Для этого вопроса используйте кнопки карточки")
        if len(text) > ANSWER_LIMIT:
            return Reply(f"Слишком длинный ответ (максимум {ANSWER_LIMIT} символов)")
        return await self._stage_answer(q, text)

    async def confirm(self, question_id: str) -> Reply:
        try:
            await self._questions.answer(question_id)
        except QuestionError as exc:
            await self.refresh_card(question_id)
            return Reply(str(exc), alert=True)
        await self.refresh_card(question_id)
        return Reply("Отправлено в Codex; ждём подтверждения")

    async def cancel(self, question_id: str) -> Reply:
        try:
            self._questions.unstage_decision(question_id)
        except QuestionError as exc:
            return Reply(str(exc), alert=True)
        await self.refresh_card(question_id)
        return Reply("Отменено")

    def other_hint(self) -> Reply:
        return Reply("Ответьте на сообщение карточки своим текстом", alert=True)

    async def _stage_answer(self, q: Question, answer: str) -> Reply:
        try:
            self._questions.stage_decision(q.question_id, {q.items[0].id: (answer,)})
        except QuestionError as exc:
            return Reply(str(exc), alert=True)
        await self.refresh_card(q.question_id)
        return Reply("Подтвердите отправку")
