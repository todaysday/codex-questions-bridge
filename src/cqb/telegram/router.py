"""Authorizes and dispatches Telegram updates. Nothing unauthorized reaches a service."""

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from cqb.domain.enums import ConsultantState
from cqb.domain.models import TelegramAction
from cqb.services.consultants import ConsultantError, ConsultantService
from cqb.services.questions import QuestionService
from cqb.services.registry import RegistryQuery
from cqb.services.relay import ConsultantRelay
from cqb.telegram.api import BotApi, TelegramError
from cqb.telegram.consultant_view import render_opening, render_questions, render_thread
from cqb.telegram.control import CONTROL, INBOX, ControlTopics, RegistryPublisher
from cqb.telegram.inbox import InboxCards
from cqb.telegram.keys import ActionKeys
from cqb.telegram.models import CallbackQuery, Message, Reply, Update
from cqb.telegram.registry_view import render_info, thread_name
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
        inbox: InboxCards,
        consultants: ConsultantService,
        consultant_topics: TelegramTopics,
        registry: RegistryQuery,
        questions: QuestionService,
        relay: ConsultantRelay,
        source_id: str,
        request_poll: Callable[[], None],
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._cfg = config
        self._api = api
        self._keys = keys
        self._topics = topics
        self._publisher = publisher
        self._inbox = inbox
        self._consultants = consultants
        self._consultant_topics = consultant_topics
        self._registry = registry
        self._questions = questions
        self._relay = relay
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
        kind = self._topics.kind_of(m.thread_id)
        if kind == CONTROL:
            if command == "threads":
                self._request_poll()
                self._publisher.request_refresh(force=True)
        elif kind == INBOX:
            await self._inbox_message(m, command)
        else:
            await self._consultant_message(m, command)

    async def _inbox_message(self, m: Message, command: str | None) -> None:
        if command == "questions":
            await self._say(m, render_questions(self._questions.open_questions(), self._names()))
        elif m.reply_to_message_id is not None and command is None:
            reply = await self._inbox.stage_text(m.reply_to_message_id, m.text or "")
            if reply and reply.text:
                await self._say(m, reply.text)

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
            await self._say(m, render_questions(qs, self._names()))
        else:
            await self._relay.relay(consultant, m.text or "")

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
        for q in self._questions.open_questions():
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
        elif (q := self._questions.by_inbox_message(message_id)) is not None:
            self._inbox.mark(q.question_id)

    async def _dispatch(self, a: TelegramAction) -> Reply:
        match a.action:
            case "create":
                return await self._create(a.root_thread_id or "")
            case "info":
                row = self._registry.describe(a.root_thread_id or "")
                return Reply(render_info(row, int(self._clock())) if row else "Тред не найден", alert=True)
            case "page":
                page, _, archived = (a.payload or "0:0").partition(":")
                self._publisher.set_view(int(page), archived == "1")
                self._publisher.request_refresh(force=True)
                return Reply()
            case "refresh":
                self._request_poll()
                self._publisher.request_refresh(force=True)
                return Reply("Обновляю")
            case "opt":
                qid, _, index = (a.payload or "").rpartition(":")
                return await self._inbox.stage(qid, int(index))
            case "send":
                return await self._inbox.confirm(a.payload or "")
            case "cancel":
                return await self._inbox.cancel(a.payload or "")
            case "other":
                return self._inbox.other_hint()
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
        self._inbox.mark_root(root)
        return Reply("Консультант уже создан" if known else "Консультант создан")
