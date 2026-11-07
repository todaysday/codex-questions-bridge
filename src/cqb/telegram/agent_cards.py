"""Cards of agent-message questions: one message per agentMessage in the consultant topic of its root,
edited as the watcher sees it answered or stale. A root without an open consultant gets no card."""

import asyncio
import logging
import re
from collections.abc import Callable

from cqb.domain.enums import ConsultantState
from cqb.domain.models import AgentQuestion
from cqb.services.agent_questions import ANSWER_MAX_CHARS, AgentQuestionService
from cqb.services.questions import QuestionError
from cqb.services.registry import RegistryQuery
from cqb.telegram.agent_card_view import render_agent_card
from cqb.telegram.api import BadRequest, BotApi
from cqb.telegram.cards import MAX_DIRTY, RETRY_SECONDS
from cqb.telegram.keys import ActionKeys
from cqb.telegram.models import Reply
from cqb.telegram.registry_view import thread_name

log = logging.getLogger(__name__)

_NUMBERED = re.compile(r"\s*(\d{1,2})\s*[:.)]\s*(.+)", re.DOTALL)


class AgentQuestionCards:
    def __init__(
        self,
        api: BotApi,
        keys: ActionKeys,
        questions: AgentQuestionService,
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

    # Listeners for AgentQuestionService.subscribe: they only mark the card dirty.
    async def on_question(self, q: AgentQuestion) -> None:
        self.mark(q.agent_question_id)
        self._on_change()  # the registry row shows or loses its ❓

    async def on_closed(self, q: AgentQuestion) -> None:
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
            view = render_agent_card(q, thread_name(row) if row else q.root_thread_id[:8])
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
            if not q.is_open:
                return  # nothing actionable to post for a question that already ended
            if row is None or row.consultant_state is not ConsultantState.OPEN or not row.topic_id:
                return  # no open consultant: the registry row shows ❓, nothing is posted
            sent = await self._api.send_message(self._chat_id, view.text, thread_id=row.topic_id, keyboard=keyboard)
            self._questions.bind_card_message(agent_question_id, sent.message_id)

    # Button handlers

    async def stage(self, agent_question_id: str, question_index: int, option_index: int) -> Reply:
        return await self._staged(
            agent_question_id, lambda: self._questions.choose(agent_question_id, question_index, option_index)
        )

    async def stage_text(self, message_reply_to: int, text: str) -> Reply | None:
        """Free text replied to a card is the `Другое` answer. With several questions it starts with `N:`."""
        q = self._questions.by_card_message(message_reply_to)
        if q is None:
            return None  # not an agent card: ordinary consultant chat
        if not q.is_open or q.sent_at is not None:
            return Reply("Этот вопрос уже закрыт или ответ отправлен")
        index, body = 0, text
        if len(q.items) > 1:
            m = _NUMBERED.fullmatch(text)
            if m is None or not 1 <= int(m.group(1)) <= len(q.items):
                return Reply("Укажите номер вопроса: «2: ваш текст»")
            index, body = int(m.group(1)) - 1, m.group(2)
        if len(body.strip()) > ANSWER_MAX_CHARS:
            return Reply(f"Слишком длинный ответ (максимум {ANSWER_MAX_CHARS} символов)")
        return await self._staged(q.agent_question_id, lambda: self._questions.answer_text(q.agent_question_id, index, body))

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

    def other_hint(self, agent_question_id: str) -> Reply:
        q = self._questions.get(agent_question_id)
        multi = q is not None and len(q.items) > 1
        return Reply("Ответьте на сообщение карточки: «N: ваш текст»" if multi
                     else "Ответьте на сообщение карточки своим текстом", alert=True)

    async def _staged(self, agent_question_id: str, action: Callable[[], AgentQuestion]) -> Reply:
        try:
            q = action()
        except QuestionError as exc:
            return Reply(str(exc), alert=True)
        await self.refresh_card(agent_question_id)
        return Reply("Подтвердите отправку" if len(q.staged) == len(q.items) else "Принято")
