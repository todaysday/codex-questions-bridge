"""Cards of agent-message questions: ONE card per question (a message with several questions gives several cards),
posted in the consultant topic of its root and edited as the watcher sees it answered or stale. A root without an
open consultant gets no card."""

import asyncio
import logging
import time
from collections.abc import Callable

from cqb.domain.labels import question_label
from cqb.domain.models import AgentQuestion
from cqb.services.agent_questions import ANSWER_MAX_CHARS, AgentQuestionService
from cqb.services.questions import QuestionError
from cqb.services.registry import RegistryQuery
from cqb.domain.enums import ConsultantState
from cqb.telegram.agent_card_view import render_agent_card
from cqb.telegram.api import BotApi
from cqb.telegram.armed import ArmedAnswers
from cqb.telegram.card_post import CardPoster, card_parts
from cqb.telegram.card_view import CONFIRM_PROMPT, confirm_keyboard, confirm_message
from cqb.telegram.cards import MAX_DIRTY, RETRY_SECONDS
from cqb.telegram.keys import ActionKeys
from cqb.telegram.models import Reply
from cqb.telegram.registry_view import thread_name

log = logging.getLogger(__name__)


class AgentQuestionCards:
    def __init__(
        self,
        api: BotApi,
        keys: ActionKeys,
        questions: AgentQuestionService,
        registry: RegistryQuery,
        chat_id: int,
        on_change: Callable[[], None],
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._poster = CardPoster(api, chat_id)
        self._keys = keys
        self._questions = questions
        self._registry = registry
        self._on_change = on_change
        self._dirty: dict[str, None] = {}
        self._wake = asyncio.Event()
        self._lock = asyncio.Lock()
        self._armed = ArmedAnswers(clock)

    # Listeners for AgentQuestionService.subscribe: they only mark the card dirty.
    async def on_question(self, q: AgentQuestion) -> None:
        self.mark(q.agent_question_id)
        self._on_change()  # the registry row shows or loses its ❓

    async def on_closed(self, q: AgentQuestion) -> None:
        self._armed.disarm(q.agent_question_id)
        self.mark(q.agent_question_id)
        self._on_change()

    def mark(self, agent_question_id: str) -> None:
        self._dirty.pop(agent_question_id, None)
        self._dirty[agent_question_id] = None
        while len(self._dirty) > MAX_DIRTY:
            del self._dirty[next(iter(self._dirty))]
        self._wake.set()

    def mark_root(self, root_thread_id: str) -> None:
        """A consultant of this root opened (created or reopened): post cards of its pending questions."""
        for q in self._questions.open_questions(root_thread_id):
            if q.card_message_id is None:
                self.mark(q.agent_question_id)

    async def run(self) -> None:
        self._wake.set()
        while True:
            try:
                await asyncio.wait_for(self._wake.wait(), RETRY_SECONDS)
            except TimeoutError:
                pass
            self._wake.clear()
            await self.process()

    async def process(self) -> None:
        for agent_question_id in self._armed.drop_expired():  # the hint on the card must not outlive the state
            self.mark(agent_question_id)
        for q in self._questions.postable_without_card():
            self.mark(q.agent_question_id)
        while self._dirty:
            agent_question_id = next(iter(self._dirty))
            del self._dirty[agent_question_id]
            try:
                await self.refresh_card(agent_question_id)
            except Exception as exc:
                log.warning("agent card %s not updated: %s", agent_question_id, type(exc).__name__)

    async def refresh_card(self, agent_question_id: str) -> None:
        async with self._lock:
            q = self._questions.get(agent_question_id)
            if q is None:
                return
            row = self._registry.describe(q.root_thread_id)
            view = render_agent_card(
                q, thread_name(row) if row else q.root_thread_id[:8], self._armed.is_armed(agent_question_id))
            ids = [q.card_message_id, *q.card_extra_ids] if q.card_message_id is not None else []
            topic = None  # where a new card may be posted: only for an open question of an open consultant
            if q.is_open and row is not None and row.consultant_state is ConsultantState.OPEN:
                topic = row.topic_id or None
            if not ids and topic is None:
                return  # no open consultant (the registry row shows ❓) or nothing actionable left to post
            await self._poster.sync(
                ids, card_parts(view.text, question_label(q.display_no)), self._keys.bind(view.keyboard), topic,
                lambda new_ids: self._questions.bind_card_messages(agent_question_id, new_ids),
            )

    # Button handlers

    async def stage(self, agent_question_id: str, option_index: int) -> Reply:
        return await self._staged(agent_question_id, lambda: self._questions.choose(agent_question_id, option_index))

    async def stage_text(self, message_reply_to: int, text: str) -> Reply | None:
        """A reply to a card is an answer only while the card is armed by `✍️ Ответить текстом`.

        None means "not an answer": the router then treats the reply as a discussion with the consultant."""
        q = self._questions.by_card_message(message_reply_to)
        if q is None or not self._armed.is_armed(q.agent_question_id):
            return None
        if not q.is_open or q.sent_at is not None:
            self._armed.disarm(q.agent_question_id)
            self.mark(q.agent_question_id)
            return None
        if len(text.strip()) > ANSWER_MAX_CHARS:
            return Reply(f"Слишком длинный ответ (максимум {ANSWER_MAX_CHARS} символов); карточка ждёт ответ")
        staged = await self._staged(q.agent_question_id, lambda: self._questions.answer_text(q.agent_question_id, text))
        if staged.alert:
            return staged
        keyboard = confirm_keyboard("asend", "acancel", q.root_thread_id, q.agent_question_id)
        return Reply(confirm_message(text), keyboard=self._keys.bind(keyboard))  # the same buttons as under the card

    async def confirm(self, agent_question_id: str) -> Reply:
        try:
            await self._questions.send(agent_question_id)
        except QuestionError as exc:
            await self.refresh_card(agent_question_id)
            return Reply(str(exc), alert=True)
        await self.refresh_card(agent_question_id)
        return Reply("Отправлено в Codex; ждём подтверждения")

    async def cancel(self, agent_question_id: str) -> Reply:
        try:
            self._questions.unstage(agent_question_id)
        except QuestionError as exc:
            return Reply(str(exc), alert=True)
        await self.refresh_card(agent_question_id)
        return Reply("Отменено")

    async def arm(self, agent_question_id: str) -> Reply:
        """`✍️ Ответить текстом`: the next reply to this card is the answer (for ten minutes)."""
        q = self._questions.get(agent_question_id)
        if q is None or not q.is_open or not q.answerable or q.sent_at is not None or q.staged is not None:
            return Reply("Ответ текстом недоступен", alert=True)
        for key in self._armed.arm(agent_question_id):
            self.mark(key)
        await self.refresh_card(agent_question_id)
        return Reply("Ответьте на это сообщение: текст уйдёт в Codex")

    async def unarm(self, agent_question_id: str) -> Reply:
        self._armed.disarm(agent_question_id)
        await self.refresh_card(agent_question_id)
        return Reply("Отменено")

    async def _staged(self, agent_question_id: str, action: Callable[[], AgentQuestion]) -> Reply:
        self._armed.disarm(agent_question_id)
        try:
            action()
        except QuestionError as exc:
            await self.refresh_card(agent_question_id)
            return Reply(str(exc), alert=True)
        await self.refresh_card(agent_question_id)
        return Reply(CONFIRM_PROMPT)
