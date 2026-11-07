"""Authorizes and dispatches Telegram updates. Nothing unauthorized reaches a service."""

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from cqb.domain.enums import ConsultantState
from cqb.domain.models import Consultant, Question, TelegramAction
from cqb.services.agent_questions import AgentQuestionService
from cqb.services.consultants import ConsultantError, ConsultantService
from cqb.services.focus import QuestionFocus
from cqb.services.question_context import discussion_prefix
from cqb.services.questions import QuestionService
from cqb.services.registry import RegistryQuery
from cqb.services.relay import ConsultantRelay
from cqb.telegram.api import BotApi, TelegramError
from cqb.telegram.agent_cards import AgentQuestionCards
from cqb.telegram.consultant_view import THREAD_COMMAND_ENABLED, render_questions, render_thread
from cqb.telegram.cards import QuestionCards
from cqb.telegram.control import ControlTopics, RegistryPublisher
from cqb.telegram.keys import ActionKeys
from cqb.telegram.models import CallbackQuery, Keyboard, Message, Reply, Update
from cqb.telegram.registry_view import thread_name
from cqb.telegram.status_post import BUSY as STATUS_BUSY, StatusPoster

log = logging.getLogger(__name__)

STALE = "Кнопка устарела; обновляю сообщение"
DONE_NOTE = "✅ отправлено"
CANCELLED_NOTE = "↩️ отменено"
STALE_NOTE = "⌛ устарело"
CONFIRMATION_DONE = ("send", "asend", "cancel", "acancel")  # presses that end the confirmation of a typed answer
SECRET_REFUSAL = "Секретный ввод: Bridge не обсуждает и не принимает его, ответьте в Codex"


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
        registry: RegistryQuery,
        questions: QuestionService,
        agent_questions: AgentQuestionService,
        relay: ConsultantRelay,
        focus: QuestionFocus,
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
        self._registry = registry
        self._questions = questions
        self._agent_questions = agent_questions
        self._relay = relay
        self._focus = focus
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
        if THREAD_COMMAND_ENABLED and command == "thread":
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
        elif command is None:
            await self._text(consultant, m)
        else:
            await self._relay.relay(consultant, m.text or "")

    async def _text(self, consultant: Consultant, m: Message) -> None:
        """Plain text of the owner: the answer to an armed card, or chat. The focus question is updated first."""
        text, root = m.text or "", consultant.root_thread_id
        card, reply_to = None, m.reply_to_message_id
        if reply_to is not None:
            card = self._questions.by_card_message(reply_to) or self._agent_questions.by_card_message(reply_to)
            if card is not None and card.root_thread_id != root:
                card = None  # a card of another thread is nothing to this topic
        if card is None:
            self._focus.note_mentions(root, text)
            await self._relay.relay(consultant, text)  # not a card of this topic: ordinary chat
            return
        self._focus.point_card(card)
        assert reply_to is not None
        reply = await self._cards.stage_text(reply_to, text)
        if reply is None:
            reply = await self._agent_cards.stage_text(reply_to, text)
        if reply is not None:  # the armed card took it as its answer
            if reply.text:
                await self._say(m, reply.text, reply.keyboard)  # the confirmation carries the card's send/cancel buttons
        elif isinstance(card, Question) and card.is_secret:
            await self._say(m, SECRET_REFUSAL)  # whatever the owner typed may be the secret: nothing is forwarded
        elif text.strip():
            self._focus.note_mentions(root, text)
            await self._relay.relay(consultant, text, lead=discussion_prefix(card))

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

    async def _say(self, m: Message, text: str, keyboard: Keyboard = ()) -> None:
        await self._api.send_message(self._cfg.chat_id, text, thread_id=m.thread_id, keyboard=keyboard)

    # --- callbacks -------------------------------------------------------------------

    async def _callback(self, cb: CallbackQuery) -> None:
        if not self._authorized_callback(cb):
            self.rejected += 1
            return  # no answer: do not confirm anything to a stranger
        assert cb.data is not None and cb.message_id is not None
        action = self._keys.resolve(cb.data)
        retire: str | None = None  # what to do with a confirmation message once the press is answered
        if action is None:
            retire = None if self._heal(cb.message_id) else STALE_NOTE
            reply = Reply(STALE)
        else:
            try:
                reply = await self._dispatch(action)
                if action.action in CONFIRMATION_DONE and not reply.alert:
                    retire = DONE_NOTE if action.action in ("send", "asend") else CANCELLED_NOTE
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
        if retire is not None and not self._is_card(cb.message_id):
            await self._retire(cb.message_id, retire)

    def _is_card(self, message_id: int) -> bool:
        return (self._questions.by_card_message(message_id) is not None
                or self._agent_questions.by_card_message(message_id) is not None)

    async def _retire(self, message_id: int, note: str) -> None:
        """A confirmation message of a typed answer is no longer needed: delete it, or when the bot may not,
        leave a one-line note without buttons. Errors (the message is already gone) are ignored."""
        try:
            await self._api.delete_message(self._cfg.chat_id, message_id)
        except TelegramError:
            try:
                await self._api.edit_message_text(self._cfg.chat_id, message_id, note)
            except TelegramError as exc:
                log.debug("cannot retire the confirmation message: %s", exc)

    def _heal(self, message_id: int) -> bool:
        """An expired or used key on a visible message: re-render it with fresh keys. False when the message is
        neither the registry nor a card: a confirmation of a typed answer, which has nothing to re-render."""
        if message_id == self._publisher.message_id:
            self._publisher.request_refresh(force=True)
        elif (q := self._questions.by_card_message(message_id)) is not None:
            self._cards.mark(q.question_id)
        elif (aq := self._agent_questions.by_card_message(message_id)) is not None:
            self._agent_cards.mark(aq.agent_question_id)
        else:
            return False
        return True

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
                self._focus_on(qid)
                return await self._cards.stage(qid, int(index))
            case "send":
                return await self._cards.confirm(a.payload or "")
            case "cancel":
                return await self._cards.cancel(a.payload or "")
            case "other":
                self._focus_on(a.payload or "")
                return await self._cards.arm(a.payload or "")
            case "unarm":
                qid = a.payload or ""
                return await (self._agent_cards if qid.startswith("a_") else self._cards).unarm(qid)
            case "aopt":
                qid, _, option = (a.payload or "").rpartition(":")
                self._focus_on(qid)
                return await self._agent_cards.stage(qid, int(option))
            case "asend":
                return await self._agent_cards.confirm(a.payload or "")
            case "acancel":
                return await self._agent_cards.cancel(a.payload or "")
            case "aother":
                self._focus_on(a.payload or "")
                return await self._agent_cards.arm(a.payload or "")
        return Reply("Неизвестное действие")

    def _focus_on(self, question_id: str) -> None:
        """Pressing an option or the text button on a card points the conversation at that question."""
        card = self._questions.get(question_id) or self._agent_questions.get(question_id)
        if card is not None:
            self._focus.point_card(card)

    async def _create(self, root: str) -> Reply:
        before = self._consultants.get(root)
        await self._consultants.create(root)  # sends and pins the opening message before the cards may be posted
        known = before is not None and before.state in (ConsultantState.OPEN, ConsultantState.ARCHIVED)
        self._publisher.request_refresh()
        return Reply("Консультант уже создан" if known else "Консультант создан")
