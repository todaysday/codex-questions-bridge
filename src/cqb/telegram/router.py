"""Authorizes and dispatches Telegram updates. Nothing unauthorized reaches a service."""

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from cqb.domain.enums import ConsultantState
from cqb.domain.models import TelegramAction
from cqb.services.agent_questions import AgentQuestionService
from cqb.services.consultants import ConsultantError, ConsultantService
from cqb.services.questions import QuestionService
from cqb.services.registry import RegistryQuery
from cqb.services.relay import ConsultantRelay
from cqb.telegram.api import BotApi, TelegramError
from cqb.telegram.agent_cards import AgentQuestionCards
from cqb.telegram.consultant_view import render_opening, render_questions, render_thread
from cqb.telegram.cards import QuestionCards
from cqb.telegram.control import ControlTopics, RegistryPublisher
from cqb.telegram.keys import ActionKeys
from cqb.telegram.models import CallbackQuery, Message, Reply, Update
from cqb.telegram.registry_view import thread_name
from cqb.telegram.status_post import BUSY as STATUS_BUSY, StatusPoster
from cqb.telegram.topics import TelegramTopics

log = logging.getLogger(__name__)

STALE = "Кнопка устарела; обновляю сообщение"


@dataclass(frozen=True, slots=True)
class RouterConfig:
    chat_id: int
    allowed_user_ids: frozenset[int]
    bot_username: str


class Router:
    def __init__(
        self,
        config: RouterConfig,
        api: BotApi,
        keys: ActionKeys,
        topics: ControlTopics,
        publisher: RegistryPublisher,
        cards: QuestionCards,
        agent_cards: AgentQuestionCards,
        consultants: ConsultantService,
        consultant_topics: TelegramTopics,
        registry: RegistryQuery,
        questions: QuestionService,
        agent_questions: AgentQuestionService,
        relay: ConsultantRelay,
        status: StatusPoster,
        source_id: str,
        request_poll: Callable[[], None],
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._cfg = config
        self._api = api
        self._keys = keys
        self._topics = topics
        self._publisher = publisher
        self._cards = cards
        self._agent_cards = agent_cards
        self._consultants = consultants
        self._consultant_topics = consultant_topics
        self._registry = registry
        self._questions = questions
        self._agent_questions = agent_questions
        self._relay = relay
        self._status = status
        self._source = source_id
        self._request_poll = request_poll
        self._clock = clock
        self.rejected = 0

    async def handle(self, update: Update) -> None:
        if update.callback is not None:
            await self._callback(update.callback)
        elif update.message is not None:
            await self._message(update.message)

    # --- authorization ---------------------------------------------------------------

    def _authorized_message(self, m: Message) -> bool:
        return (
            m.chat_id == self._cfg.chat_id
            and m.from_id in self._cfg.allowed_user_ids
            and not m.from_is_bot
            and not m.is_forwarded
            and not m.has_sender_chat
        )

    def _authorized_callback(self, cb: CallbackQuery) -> bool:
        return (
            cb.chat_id == self._cfg.chat_id
            and cb.from_id in self._cfg.allowed_user_ids
            and cb.message_id is not None
            and cb.data is not None
        )

    # --- messages --------------------------------------------------------------------

    async def _message(self, m: Message) -> None:
        if not self._authorized_message(m):
            self.rejected += 1
            return
        if not m.text or m.thread_id is None:
            return
        command = self._command(m.text)
        if command == "":
            return  # addressed to another bot
        if self._topics.is_control(m.thread_id):
            if command == "threads":
                await self._threads_command(m)
        else:
            await self._consultant_message(m, command)

    async def _threads_command(self, m: Message) -> None:
        """Re-render the one registry message now. The command message is removed if the bot may."""
        self._request_poll()
        await self._publisher.refresh(force=True)
        try:
            await self._api.delete_message(self._cfg.chat_id, m.message_id)
        except TelegramError as exc:
            log.debug("cannot delete the command message: %s", exc)

    async def _consultant_message(self, m: Message, command: str | None) -> None:
        assert m.thread_id is not None
        consultant = self._consultants.by_topic(m.thread_id)
        if consultant is None or consultant.state is not ConsultantState.OPEN:
            return  # unknown, unbound or closed topic
        if command == "thread":
            row = self._registry.describe(consultant.root_thread_id)
            if row:
                await self._say(m, render_thread(row, self._source, int(self._clock())))
        elif command == "questions":
            qs = self._questions.open_questions(consultant.root_thread_id)
            agent = self._agent_questions.open_questions(consultant.root_thread_id)
            await self._say(m, render_questions(qs, self._names(), agent))
        elif command == "status":
            if not self._status.request(consultant.root_thread_id, m.thread_id):
                await self._say(m, STATUS_BUSY)
        elif command is None and m.reply_to_message_id is not None and (
            reply := await self._card_reply(m.reply_to_message_id, m.text or "")
        ) is not None:
            if reply.text:  # a reply to a question card is never chat: it must not reach the model
                await self._say(m, reply.text)
        else:
            await self._relay.relay(consultant, m.text or "")

    async def _card_reply(self, reply_to: int, text: str) -> Reply | None:
        """Free text replied to a question card of either kind; None when the message is not a card."""
        reply = await self._cards.stage_text(reply_to, text)
        return reply if reply is not None else await self._agent_cards.stage_text(reply_to, text)

    def _command(self, text: str) -> str | None:
        """Command name; None for plain text; "" for a command aimed at another bot."""
        token = text.split(None, 1)[0] if text.strip() else ""
        if not token.startswith("/"):
            return None
        name, _, mention = token[1:].partition("@")
        if mention and mention.lower() != self._cfg.bot_username.lower():
            return ""
        return name.lower()

    def _names(self) -> dict[str, str]:
        rows = {}
        for q in (*self._questions.open_questions(), *self._agent_questions.open_questions()):
            row = self._registry.describe(q.root_thread_id)
            if row:
                rows[q.root_thread_id] = thread_name(row)
        return rows

    async def _say(self, m: Message, text: str) -> None:
        await self._api.send_message(self._cfg.chat_id, text, thread_id=m.thread_id)

    # --- callbacks -------------------------------------------------------------------

    async def _callback(self, cb: CallbackQuery) -> None:
        if not self._authorized_callback(cb):
            self.rejected += 1
            return  # no answer: do not confirm anything to a stranger
        assert cb.data is not None and cb.message_id is not None
        action = self._keys.resolve(cb.data)
        if action is None:
            self._heal(cb.message_id)
            reply = Reply(STALE)
        else:
            try:
                reply = await self._dispatch(action)
            except ConsultantError as exc:
                reply = Reply(str(exc), alert=True)
            except (KeyError, ValueError):
                reply = Reply("Некорректное действие", alert=True)
            except TelegramError as exc:
                log.warning("callback %s failed: %s", action.action, exc)
                reply = Reply("Ошибка Telegram, повторите позже", alert=True)
            except Exception:
                log.exception("callback %s failed", action.action)
                reply = Reply("Внутренняя ошибка; подробности в логе", alert=True)
        try:
            await self._api.answer_callback_query(cb.query_id, reply.text, reply.alert)
        except TelegramError as exc:
            log.debug("answerCallbackQuery failed: %s", exc)

    def _heal(self, message_id: int) -> None:
        """An expired or used key on a visible message: re-render it with fresh keys."""
        if message_id == self._publisher.message_id:
            self._publisher.request_refresh(force=True)
        elif (q := self._questions.by_card_message(message_id)) is not None:
            self._cards.mark(q.question_id)
        elif (aq := self._agent_questions.by_card_message(message_id)) is not None:
            self._agent_cards.mark(aq.agent_question_id)

    async def _dispatch(self, a: TelegramAction) -> Reply:
        match a.action:
            case "create":
                return await self._create(a.root_thread_id or "")
            case "page":
                page, _, archived = (a.payload or "0:0").partition(":")
                self._publisher.set_view(int(page), archived == "1")
                await self._publisher.refresh(force=True)  # edits the same message
                return Reply()
            case "opt":
                qid, _, index = (a.payload or "").rpartition(":")
                return await self._cards.stage(qid, int(index))
            case "send":
                return await self._cards.confirm(a.payload or "")
            case "cancel":
                return await self._cards.cancel(a.payload or "")
            case "other":
                return self._cards.other_hint()
            case "aopt":
                qid, question, option = (a.payload or "").split(":")
                return await self._agent_cards.stage(qid, int(question), int(option))
            case "asend":
                return await self._agent_cards.confirm(a.payload or "")
            case "acancel":
                return await self._agent_cards.cancel(a.payload or "")
            case "aother":
                return self._agent_cards.other_hint((a.payload or "").partition(":")[0])
        return Reply("Неизвестное действие")

    async def _create(self, root: str) -> Reply:
        before = self._consultants.get(root)
        consultant = await self._consultants.create(root)
        known = before is not None and before.state in (ConsultantState.OPEN, ConsultantState.ARCHIVED)
        if not known and consultant.tg_topic_id is not None:
            row = self._registry.describe(root)
            if row:
                try:
                    await self._consultant_topics.announce(consultant.tg_topic_id, render_opening(row, self._source))
                except TelegramError as exc:
                    log.warning("opening message failed: %s", exc)
        self._publisher.request_refresh()
        return Reply("Консультант уже создан" if known else "Консультант создан")
