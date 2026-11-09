"""Authorizes and dispatches Telegram updates. Nothing unauthorized reaches a service."""

import asyncio
import html
import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from cqb.codex.agent_reply import reply_answers
from cqb.codex.history import HistoryReader, event_from_entry
from cqb.codex.interactions import read_interactions
from cqb.domain.enums import ConsultantState
from cqb.domain.models import Consultant, Question, TelegramAction
from cqb.services.agent_questions import AgentQuestionService
from cqb.services.consultants import ConsultantError, ConsultantService
from cqb.services.focus import QuestionFocus
from cqb.services.question_context import discussion_prefix
from cqb.services.questions import QuestionService
from cqb.services.registry import RegistryQuery
from cqb.services.relay import ConsultantRelay
from cqb.telegram.api import TelegramError
from cqb.telegram.agent_cards import AgentQuestionCards
from cqb.telegram.consultant_view import OPENING_QUESTIONS, OPENING_STATUS, OPENING_THREAD
from cqb.telegram.cards import QuestionCards
from cqb.telegram.control import ControlTopics, RegistryPublisher
from cqb.telegram.keys import ActionKeys
from cqb.telegram.models import CallbackQuery, Doc, Message, Para, Reply, Update
from cqb.telegram.present import Presenter
from cqb.telegram.registry_view import thread_name
from cqb.telegram.status_post import BUSY as STATUS_BUSY, StatusPoster

log = logging.getLogger(__name__)

STALE = "Кнопка устарела; обновляю сообщение"
DONE_NOTE = "✅ отправлено"
CANCELLED_NOTE = "↩️ отменено"
STALE_NOTE = "⌛ устарело"
CONFIRMATION_DONE = ("send", "asend", "cancel", "acancel")  # presses that end the confirmation of a typed answer
NO_QUESTIONS = "Нет неотвеченных вопросов"
NOTICE_TASKS = 20  # pending "delete this notice" timers
EXPANDABLE_LINES = 10
SECRET_REFUSAL = "Секретный ввод: Bridge не обсуждает и не принимает его, ответьте в Codex"


class ThreadClient(Protocol):
    async def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]: ...

    async def send_user_message(self, thread_id: str, text: str) -> str: ...


@dataclass(frozen=True, slots=True)
class RouterConfig:
    chat_id: int
    allowed_user_ids: frozenset[int]
    bot_username: str


class Router:
    def __init__(
        self,
        config: RouterConfig,
        present: Presenter,
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
        thread_client: ThreadClient,
        codex_home: str,
        clock: Callable[[], float] = time.time,
        notice_seconds: float = 10.0,
    ) -> None:
        self._cfg = config
        self._present = present
        self._api = present.api
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
        self._notice_seconds = notice_seconds
        self._thread_client = thread_client
        self._codex_home = codex_home
        self._notices: set[asyncio.Task[None]] = set()
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

    async def _questions_command(self, root: str, topic_id: int, message_id: int | None = None) -> None:
        """Post the pending questions of this thread anew: the old card messages go, fresh full cards follow in the
        order of their numbers. The command message is removed if the bot may; with nothing pending a short notice
        answers and is removed after a few seconds."""
        pending = sorted(
            [(q.display_no, 0, q.question_id) for q in self._questions.open_questions(root)]
            + [(a.display_no, 1, a.agent_question_id) for a in self._agent_questions.open_questions(root)]
        )
        if message_id is not None:
            await self._delete_quietly(message_id)
        if not pending:
            sent = await self._api.send_message(self._cfg.chat_id, NO_QUESTIONS, thread_id=topic_id)
            self._forget_later(sent.message_id)
            return
        for _, kind, question_id in pending:
            try:
                await (self._agent_cards if kind else self._cards).refresh_card(question_id, fresh=True)
            except TelegramError as exc:
                log.warning("card %s not reposted: %s", question_id, type(exc).__name__)

    async def _delete_quietly(self, message_id: int) -> None:
        try:
            await self._api.delete_message(self._cfg.chat_id, message_id)
        except TelegramError as exc:
            log.debug("cannot delete message: %s", exc)

    def _forget_later(self, message_id: int) -> None:
        """Delete a notice after `notice_seconds`. At most NOTICE_TASKS timers at once (more notices simply stay);
        a timer that never fires, e.g. across a restart, leaves one harmless message."""
        if len(self._notices) >= NOTICE_TASKS:
            return

        async def later() -> None:
            await asyncio.sleep(self._notice_seconds)
            await self._delete_quietly(message_id)

        task = asyncio.create_task(later(), name="notice-delete")
        self._notices.add(task)
        task.add_done_callback(self._notices.discard)

    async def close(self) -> None:
        tasks = list(self._notices)
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _consultant_message(self, m: Message, command: str | None) -> None:
        assert m.thread_id is not None
        consultant = self._consultants.by_topic(m.thread_id)
        if consultant is None or consultant.state is not ConsultantState.OPEN:
            return  # unknown, unbound or closed topic
        if m.reply_to_message_id == consultant.opening_message_id and m.reply_to_message_id is not None:
            await self._send_to_thread(consultant, m)
        elif command == "questions":
            await self._questions_command(consultant.root_thread_id, m.thread_id, m.message_id)
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
            if reply.doc is not None:  # the confirmation carries the card's send/cancel buttons
                await self._present.send(self._cfg.chat_id, reply.doc, thread_id=m.thread_id)
            elif reply.text:
                await self._say(m, reply.text)
        elif isinstance(card, Question) and card.is_secret:
            await self._say(m, SECRET_REFUSAL)  # whatever the owner typed may be the secret: nothing is forwarded
        elif text.strip():
            self._focus.note_mentions(root, text)
            await self._relay.relay(consultant, text, lead=discussion_prefix(card))

    async def _send_to_thread(self, consultant: Consultant, m: Message) -> None:
        try:
            await self._thread_client.send_user_message(consultant.root_thread_id, m.text or "")
        except Exception:
            log.exception("sending message to Codex thread failed")
            await self._say(m, "Не удалось подтвердить отправку в тред Codex")
            return
        await self._say(m, "✅ Отправлено в тред Codex")

    def _command(self, text: str) -> str | None:
        """Command name; None for plain text; "" for a command aimed at another bot."""
        token = text.split(None, 1)[0] if text.strip() else ""
        if not token.startswith("/"):
            return None
        name, _, mention = token[1:].partition("@")
        if mention and mention.lower() != self._cfg.bot_username.lower():
            return ""
        return name.lower()

    async def _say(self, m: Message, text: str) -> None:
        await self._api.send_message(self._cfg.chat_id, text, thread_id=m.thread_id)

    # --- callbacks -------------------------------------------------------------------

    async def _callback(self, cb: CallbackQuery) -> None:
        if not self._authorized_callback(cb):
            self.rejected += 1
            return  # no answer: do not confirm anything to a stranger
        assert cb.data is not None and cb.message_id is not None
        if cb.data in (OPENING_QUESTIONS, OPENING_STATUS, OPENING_THREAD):
            await self._opening_callback(cb)
            return
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

    async def _opening_callback(self, cb: CallbackQuery) -> None:
        assert cb.message_id is not None
        try:
            consultant = self._consultants.by_opening_message(cb.message_id)
            if consultant is None or consultant.state is not ConsultantState.OPEN or consultant.tg_topic_id is None:
                reply = Reply(STALE, alert=True)
            elif cb.data == OPENING_QUESTIONS:
                await self._questions_command(consultant.root_thread_id, consultant.tg_topic_id)
                reply = Reply()
            elif cb.data == OPENING_THREAD:
                await self._show_thread(consultant.root_thread_id, consultant.tg_topic_id)
                reply = Reply()
            elif not self._status.request(consultant.root_thread_id, consultant.tg_topic_id):
                reply = Reply(STATUS_BUSY)
            else:
                reply = Reply()
        except TelegramError as exc:
            log.warning("opening callback failed: %s", type(exc).__name__)
            reply = Reply("Ошибка Telegram, повторите позже", alert=True)
        except Exception:
            log.exception("opening callback failed")
            reply = Reply("Внутренняя ошибка; подробности в логе", alert=True)
        try:
            await self._api.answer_callback_query(cb.query_id, reply.text, reply.alert)
        except TelegramError as exc:
            log.debug("answerCallbackQuery failed: %s", exc)

    async def _show_thread(self, root: str, topic_id: int, title: str = "Главный тред") -> None:
        entries, older = await HistoryReader(self._thread_client).recent(root, 100)
        interaction_ids = {entry["item"]["id"] for entry in entries
                           if isinstance(entry.get("item"), dict) and entry["item"].get("type") == "subAgentActivity"
                           and isinstance(entry["item"].get("id"), str)}
        interactions = {}
        names = {}
        sender = "Codex"
        try:
            thread = (await self._thread_client.call("thread/read", {"threadId": root, "includeTurns": False})).get("thread", {})
            source = thread.get("source")
            subagent = source.get("subAgent") if isinstance(source, dict) else None
            spawn = subagent.get("thread_spawn") if isinstance(subagent, dict) else None
            spawn = spawn if isinstance(spawn, dict) else {}
            sender_path = spawn.get("agent_path")
            if isinstance(sender_path, str) and sender_path:
                sender = sender_path.rstrip("/").split("/")[-1]
            elif isinstance(thread.get("agentNickname"), str) and thread["agentNickname"]:
                sender = thread["agentNickname"]
            path = thread.get("path")
            if isinstance(path, str):
                interactions, incoming = await asyncio.to_thread(read_interactions, path, self._codex_home, interaction_ids)
                first_at = min((e.get("startedAtMs") for e in entries if isinstance(e.get("startedAtMs"), int)), default=0)
                for at, author, recipient in incoming:
                    if older and at < first_at:
                        continue
                    entries.append({"turnId": "incoming", "startedAtMs": at, "item": {
                        "id": f"incoming-{at}-{len(entries)}", "type": "incomingAgentMessage",
                        "author": author, "recipient": recipient,
                    }})
                entries.sort(key=lambda e: e.get("startedAtMs") or e.get("completedAtMs") or 0)
        except Exception:
            log.debug("cannot read collaboration inputs", exc_info=True)
        if interaction_ids:
            ids = {entry["item"].get("agentThreadId") for entry in entries
                   if isinstance(entry.get("item"), dict) and entry["item"].get("type") == "subAgentActivity"}
            async def resolve_name(agent_id: str) -> tuple[str, str | None]:
                try:
                    result = await asyncio.wait_for(
                        self._thread_client.call("thread/read", {"threadId": agent_id, "includeTurns": False}), 3
                    )
                    thread = result.get("thread", {})
                    spawn = ((thread.get("source") or {}).get("subAgent") or {}).get("thread_spawn") or {}
                    name = spawn.get("agent_path") or thread.get("agentNickname") or thread.get("name")
                    return agent_id, name.rstrip("/").split("/")[-1] if isinstance(name, str) and name else None
                except Exception:
                    return agent_id, None

            resolved = await asyncio.gather(*(resolve_name(i) for i in ids if isinstance(i, str)))
            names = {agent_id: name for agent_id, name in resolved if name}
        groups = []
        for entry in entries:
            item = entry.get("item")
            if not isinstance(item, dict):
                continue
            kind = item.get("type")
            if kind in ("functionCallOutput", "hookPrompt"):
                continue
            event = event_from_entry(root, entry, max_chars=1_000_000)
            if event is None or not event.text:
                continue
            text = event.text
            if kind == "userMessage":
                answers = reply_answers(text)
                if answers is not None:
                    kind = "questionReply"
                    text = "\n".join(answers)
            if kind == "reasoning":
                text = text.replace("**", "")
            if kind == "incomingAgentMessage":
                author = item["author"].rstrip("/").split("/")[-1]
                recipient = item["recipient"].rstrip("/").split("/")[-1]
                author = "Codex" if author == "root" else author
                recipient = "Codex" if recipient == "root" else recipient
                text = f"message · {author} → {recipient}"
            elif kind == "subAgentActivity":
                agent_id = item.get("agentThreadId")
                path = item.get("agentPath")
                activity = item.get("kind")
                action, target = interactions.get(item.get("id"), (str(activity or "interacted"), None))
                if activity in ("started", "completed"):
                    target = None
                if target:
                    target_name = names.get(target) or target.rstrip("/").split("/")[-1]
                    if re.fullmatch(r"[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}", target_name):
                        target_name = "unknown agent"
                else:
                    target_name = names.get(agent_id) or (path.rstrip("/").split("/")[-1] if isinstance(path, str) and path else None) or "unknown agent"
                if activity in ("started", "completed"):
                    text = f"{activity} · {target_name}"
                else:
                    text = f"{action} · {sender} → {target_name}"
            elif kind in ("mcpToolCall", "dynamicToolCall"):
                args = item.get("arguments")
                if args is None:
                    args = ""
                elif not isinstance(args, str):
                    args = json.dumps(args, ensure_ascii=False)
                name = f"{item.get('server')}/" if kind == "mcpToolCall" else ""
                text = f"{name}{item.get('tool')} {args}"
            elif kind == "commandExecution":
                text = "$ " + str(item.get("command") or "")
            elif kind == "collabAgentToolCall" and item.get("tool") == "wait" and not item.get("receiverThreadIds"):
                kind = "agentWait"
                text = "wait ->"
                if groups and groups[-1][0] == kind:
                    continue
            expandable = kind == "commandExecution" or (
                kind not in ("userMessage", "questionReply", "agentMessage", "subAgentActivity", "incomingAgentMessage") and len(text) > 180
            )
            if expandable:
                lines = text.splitlines()
                if len(lines) > EXPANDABLE_LINES:
                    text = "\n".join(lines[:EXPANDABLE_LINES]) + f"\n... {len(lines) - EXPANDABLE_LINES} lines more"
            can_group = kind == "commandExecution" or (
                not expandable and kind not in ("userMessage", "questionReply", "agentMessage", "subAgentActivity", "incomingAgentMessage")
            )
            if can_group and groups and groups[-1][0] == kind and (
                kind == "commandExecution" or not groups[-1][2]
            ):
                groups[-1][1].append(text)
            else:
                groups.append((kind, [text], expandable))
        blocks = []
        size = len(f"👁️‍🗨️ {title}\n──────────\n\n".encode("utf-16-le")) // 2
        for kind, parts, expandable in reversed(groups):
            text = ("\0" if kind == "commandExecution" else "\n\n").join(parts)
            agent_interaction = kind in ("subAgentActivity", "incomingAgentMessage")
            label = {"userMessage": "👤 Вы", "questionReply": "👤 Вы answer", "agentMessage": "🤖 Codex"}.get(kind, "🔧 " + str(kind))
            if agent_interaction:
                label = "🤖"
            elif kind == "agentWait":
                label = "💤"
            elif kind == "reasoning":
                label = "🧠 reasoning"
            remaining = 3600 - size - len(label) - 4
            if len(text.encode("utf-16-le")) // 2 > remaining:
                if remaining <= 1 or kind == "agentWait":
                    break
                encoded = text.encode("utf-16-le")
                more_lines = re.search(r"\n\.\.\. (\d+) lines more$", text) if expandable and len(parts) == 1 else None
                if more_lines:
                    if remaining <= 50:
                        break
                    visible = text[:more_lines.start()]
                    visible_lines = len(visible.splitlines())
                    available = max(0, remaining - 50)
                    prefix = visible.encode("utf-16-le")[:2 * available].decode("utf-16-le", errors="ignore").rstrip("\n")
                    omitted = int(more_lines.group(1)) + visible_lines - len(prefix.splitlines())
                    text = prefix + "…" + f"\n... {omitted} lines more"
                elif kind in ("userMessage", "questionReply", "agentMessage") or len(parts) > 1:
                    text = "…" + encoded[-2 * (remaining - 1):].decode("utf-16-le", errors="ignore")
                else:
                    text = encoded[:2 * (remaining - 1)].decode("utf-16-le", errors="ignore") + "…"
            size += len(label) + len(text.encode("utf-16-le")) // 2 + 4
            body = html.escape(text)
            if kind == "commandExecution":
                quotes = "\n".join(f"<blockquote expandable>{html.escape(part)}</blockquote>" for part in text.split("\0"))
                block = f"<b>{html.escape(label)}</b>\n{quotes}"
            elif expandable:
                block = f"<b>{html.escape(label)}</b>\n<blockquote expandable>{body}</blockquote>"
            elif kind == "agentWait":
                block = "💤 <b>wait</b> -&gt;"
            elif agent_interaction:
                action, separator, rest = text.partition(" · ")
                block = f"🤖 <b>{html.escape(action)}</b>{separator}{html.escape(rest)}"
            elif len(parts) > 1 and "\n\n" in text:
                heading = "🧠 <b>reasoning</b>" if kind == "reasoning" else f"<b>{html.escape(label)}</b>"
                bullets = "\n".join(f"▪️ {html.escape(part)}" for part in text.split("\n\n"))
                block = f"{heading}\n{bullets}"
            elif kind == "reasoning":
                block = f"🧠 <b>reasoning</b> {body}"
            elif kind == "questionReply":
                block = f"<b>👤 Вы</b> answer\n{body}"
            elif kind not in ("userMessage", "agentMessage"):
                block = f"<b>{html.escape(label)}</b> {body}"
            else:
                block = f"<b>{html.escape(label)}</b>\n{body}"
            blocks.append(block)
            if size >= 3598:
                break
        text = f"👁️‍🗨️ <b>{html.escape(title)}</b>\n──────────\n\n" + ("\n\n".join(reversed(blocks)) or "История пуста")
        await self._api.send_message(self._cfg.chat_id, text, thread_id=topic_id, parse_mode="HTML")

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
                await self._present.edit(self._cfg.chat_id, message_id, Doc((Para((note,)),)))
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
            case "agent_thread":
                consultant = self._consultants.get(a.root_thread_id or "")
                if not consultant or consultant.state is not ConsultantState.OPEN or consultant.tg_topic_id is None:
                    return Reply(STALE, alert=True)
                data = json.loads(a.payload or "{}")
                await self._show_thread(data["thread"], consultant.tg_topic_id, data["title"])
                return Reply()
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
