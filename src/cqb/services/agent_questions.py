"""Agent-message questions: the "Question / Answer question" cards of the Codex apps.

Findings (schema v2 ThreadItem -> agentMessage, checked on the real daemon, read-only):
* The card is NOT an item/tool/requestUserInput server request. It is a thread item
  {"type": "agentMessage", "phase": "final_answer", "delivery": "async", "text": ..., "questions": [...]}.
  Each question is {"title": str, "options": [str, ...] | null}: no id, no flags, options are plain strings,
  and no multi-select exists. The question's identity is its zero-based index inside the message.
* It does not block the turn: the thread stays `active` with no waitingOnUserInput flag, then goes idle.
  The owner answers with a normal user message, stored as a `userMessage` item after the agentMessage.
  So a question is pending while no userMessage follows its agentMessage in the thread.
* The app's answer text (what the Bridge must reproduce, see codex/agent_reply.py) is
  `<send_user_message_question_reply>\\n[{"question": <title>, "answer": <option or free text>,
  "questionItemId": "[\\"request_user_input_async\\",\\"<agentMessage item id>\\",<index>]"}]\\n</...>`.
  The app sends one message per answered question. The Bridge does the same: every question of a message is its
  own row (item id + index), has its own card and Q number, and is staged, confirmed and sent on its own.
  A reply message names its question in questionItemId, which marks exactly that question answered; a user
  message that names none answers every question before it.
* Only roots were seen with such items: 0 of 1581 items in 40 subagent threads, so only roots are scanned.
* thread/items/list (descending, no turnId) walks the whole thread newest-first and works on notLoaded and
  archived threads without resume or subscribe; a limit-1 call costs about 5 ms.

Detection reads items only: a limit-1 probe tells whether the newest item changed since the last look, and
only then pages of 40 are walked newest-first, stopping at the first userMessage that names no question
(everything older is answered), at the item seen last time, or after 5 pages. A reply message that names
questions answers those and the walk goes on. Triggers: a status broadcast of a root (debounced)
and a periodic sweep over roots that are working, recently updated or hold a pending question (at most
25 per sweep, the least recently read first). Kept in memory per thread: one item id and one time.
Message text is never stored or logged: only titles and options (cut) of pending questions.
"""

import asyncio
import hashlib
import itertools
import logging
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Any

from cqb.codex.agent_reply import answered_questions, build_reply
from cqb.codex.appserver import AppServerClient, NotConnected, RpcError
from cqb.codex.events import Event, ThreadArchiveChanged, ThreadStatusChanged, agent_questions_from_item
from cqb.dao.agent_questions import AgentQuestionDao
from cqb.dao.threads import ThreadDao
from cqb.domain.enums import AgentQuestionState
from cqb.domain.models import AgentQuestion, AgentQuestionItem
from cqb.services.questions import QuestionError

log = logging.getLogger(__name__)

PAGE_SIZE = 40
MAX_PAGES = 5
MAX_PER_THREAD = 3  # agent messages with pending questions kept per scan of one thread
SWEEP_MAX_THREADS = 25
RECENT_SECONDS = 600
DEBOUNCE_SECONDS = 1.0
MAX_TRACKED = 1024
MAX_DIRTY = 256
ANSWER_MAX_CHARS = 1000
PRUNE_AFTER_SECONDS = 30 * 86400

Hook = Callable[[AgentQuestion], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class _Found:
    """One agentMessage with questions seen in the walk (the questions that are not known to be answered)."""

    item_id: str
    turn_id: str
    questions: tuple[tuple[int, AgentQuestionItem], ...]  # (index in the message, question)


@dataclass(frozen=True, slots=True)
class _Walk:
    found: list[_Found]  # newest first
    answered: set[tuple[str, int]]  # (item id, index) named by reply messages
    sweep: bool  # a user message naming no question was met: every older question is answered
    newest: str | None


class AgentQuestionService:
    def __init__(
        self,
        client: AppServerClient,
        questions: AgentQuestionDao,
        threads: ThreadDao,
        source_id: str,
        *,
        scan_seconds: float = 20,
        clock: Callable[[], float] = time.time,
        debounce: float = DEBOUNCE_SECONDS,
    ) -> None:
        self._client = client
        self._questions = questions
        self._threads = threads
        self._source = source_id
        self._interval = scan_seconds
        self._clock = clock
        self._debounce = debounce
        self._seen: OrderedDict[str, tuple[str, float]] = OrderedDict()  # thread -> (newest item id, read at)
        self._dirty: dict[str, None] = {}
        self._wake = asyncio.Event()
        self._question_hooks: list[Hook] = []
        self._closed_hooks: list[Hook] = []

    def subscribe(self, on_question: Hook | None = None, on_closed: Hook | None = None) -> None:
        """on_question fires once per new pending question; on_closed when it is answered or goes stale."""
        if on_question:
            self._question_hooks.append(on_question)
        if on_closed:
            self._closed_hooks.append(on_closed)

    # --- reads ---------------------------------------------------------------------------

    def get(self, agent_question_id: str) -> AgentQuestion | None:
        return self._questions.get(agent_question_id)

    def open_questions(self, root_thread_id: str | None = None, limit: int = 20) -> list[AgentQuestion]:
        return self._questions.list_open(self._source, root_thread_id, limit)

    def open_count(self) -> int:
        return sum(self._questions.open_counts_by_root(self._source).values())

    def postable_without_card(self, limit: int = 50) -> list[AgentQuestion]:
        return self._questions.list_postable(self._source, limit)

    def bind_card_messages(self, agent_question_id: str, ids: list[int]) -> None:
        self._questions.set_card_messages(agent_question_id, ids)

    def by_card_message(self, message_id: int) -> AgentQuestion | None:
        return self._questions.find_by_card_message(message_id)

    # --- events and loop -----------------------------------------------------------------

    async def handle(self, event: Event) -> None:
        match event:
            case ThreadStatusChanged():
                thread = self._threads.get(self._source, event.thread_id)
                if thread is not None and thread.is_root:
                    self._mark(event.thread_id)
            case ThreadArchiveChanged(archived=True):
                for q in self._questions.list_open_for_thread(self._source, event.thread_id):
                    await self._close(q, AgentQuestionState.STALE)

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        next_sweep = 0.0
        while True:
            try:
                await asyncio.wait_for(self._wake.wait(), max(0.0, next_sweep - loop.time()))
                self._wake.clear()
                await asyncio.sleep(self._debounce)  # a burst of broadcasts becomes one read
                await self._guarded(self.process_dirty)
            except TimeoutError:
                pass
            if loop.time() >= next_sweep:
                await self._guarded(self.sweep)
                next_sweep = loop.time() + self._interval

    async def process_dirty(self) -> None:
        batch = list(itertools.islice(self._dirty, SWEEP_MAX_THREADS))
        for thread_id in batch:
            del self._dirty[thread_id]
        for thread_id in batch:
            await self.scan_thread(thread_id)
        if self._dirty:
            self._wake.set()

    async def sweep(self) -> None:
        now = self._now()
        for q in self._questions.list_open_in_archived(self._source):
            await self._close(q, AgentQuestionState.STALE)
        roots = self._questions.scan_roots(self._source, now - RECENT_SECONDS)
        roots.sort(key=lambda t: self._seen[t][1] if t in self._seen else 0.0)  # stable: never read first
        for thread_id in roots[:SWEEP_MAX_THREADS]:
            await self.scan_thread(thread_id)
        self._questions.prune_closed(self._source, now - PRUNE_AFTER_SECONDS)

    async def _guarded(self, step: Callable[[], Awaitable[None]]) -> None:
        try:
            await step()
        except Exception:
            log.exception("agent question scan failed")

    def _mark(self, thread_id: str) -> None:
        self._dirty.pop(thread_id, None)
        self._dirty[thread_id] = None
        while len(self._dirty) > MAX_DIRTY:
            del self._dirty[next(iter(self._dirty))]
        self._wake.set()

    # --- scan ----------------------------------------------------------------------------

    async def scan_thread(self, thread_id: str) -> None:
        """Newest items of one root -> pending/answered questions. Never resumes or subscribes."""
        known = self._seen[thread_id][0] if thread_id in self._seen else None
        try:
            if known is not None:
                probe = await self._items(thread_id, 1, None)
                first = _item_id((probe.get("data") or [None])[0])
                if first == known:
                    self._remember(thread_id, known)
                    return
            walk = await self._walk(thread_id, known)
        except NotConnected:
            return
        except RpcError:
            await self._check_gone(thread_id)
            return
        except Exception as exc:  # timeout and the like: the next trigger reads again
            log.debug("agent question read failed: %s", type(exc).__name__)
            return
        if walk.newest is not None:
            self._remember(thread_id, walk.newest)
        thread = self._threads.get(self._source, thread_id)
        root = thread.root_thread_id if thread else thread_id
        for f in reversed(walk.found):  # oldest message first, so cards come out in the thread's order
            for index, item in f.questions:
                await self._add(thread_id, root, f, index, item)
        if walk.answered or walk.sweep:
            still = {(f.item_id, i) for f in walk.found for i, _ in f.questions}
            for q in self._questions.list_open_for_thread(self._source, thread_id):
                key = (q.item_id, q.index)
                if key in walk.answered or (walk.sweep and key not in still):
                    await self._close(q, AgentQuestionState.ANSWERED)

    async def _walk(self, thread_id: str, known: str | None) -> _Walk:
        """Newest-first until a user message that names no question, the item seen last time, or the page cap."""
        found: list[_Found] = []
        answered: set[tuple[str, int]] = set()
        newest: str | None = None
        cursor: str | None = None
        for _ in range(MAX_PAGES):
            page = await self._items(thread_id, PAGE_SIZE, cursor)
            entries = page.get("data") or []
            for entry in entries:
                item = entry.get("item") if isinstance(entry, dict) else None
                item_id = _item_id(entry)
                if item_id is None:
                    continue
                newest = newest or item_id
                if item_id == known:
                    return _Walk(found, answered, False, newest)
                if item.get("type") == "userMessage":
                    named = answered_questions(_user_text(item))
                    if named is None:
                        return _Walk(found, answered, True, newest)
                    answered |= named
                    continue
                parsed = agent_questions_from_item(item)
                if parsed is not None and len(found) < MAX_PER_THREAD:
                    open_ones = tuple((i, q) for i, q in enumerate(parsed) if (item_id, i) not in answered)
                    if open_ones:
                        found.append(_Found(item_id, str(entry.get("turnId") or ""), open_ones))
            cursor = page.get("nextCursor")
            if not cursor or not entries:
                break
        return _Walk(found, answered, False, newest)

    async def _items(self, thread_id: str, limit: int, cursor: str | None) -> dict[str, Any]:
        params: dict[str, Any] = {"threadId": thread_id, "limit": limit, "sortDirection": "desc"}
        if cursor:
            params["cursor"] = cursor
        return await self._client.call("thread/items/list", params)

    async def _check_gone(self, thread_id: str) -> None:
        """A failed read matters only for a thread with open questions: if it cannot be read at all, it is gone."""
        open_now = self._questions.list_open_for_thread(self._source, thread_id)
        if not open_now:
            return
        try:
            await self._client.call("thread/read", {"threadId": thread_id, "includeTurns": False})
        except RpcError:
            for q in open_now:
                await self._close(q, AgentQuestionState.STALE)
        except Exception as exc:
            log.debug("agent question thread check failed: %s", type(exc).__name__)

    def _remember(self, thread_id: str, newest: str) -> None:
        self._seen[thread_id] = (newest, time.monotonic())
        self._seen.move_to_end(thread_id)
        while len(self._seen) > MAX_TRACKED:
            self._seen.popitem(last=False)

    async def _add(self, thread_id: str, root: str, f: _Found, index: int, item: AgentQuestionItem) -> None:
        now = self._now()
        key = "\0".join((self._source, thread_id, f.item_id, str(index)))
        fresh = AgentQuestion(
            agent_question_id="a_" + hashlib.sha1(key.encode()).hexdigest()[:12],
            source_id=self._source, thread_id=thread_id, root_thread_id=root, turn_id=f.turn_id,
            item_id=f.item_id, index=index, title=item.title, options=item.options, answerable=item.complete,
            state=AgentQuestionState.PENDING, staged=None, sent_at=None, created_at=now, updated_at=now,
        )
        if self._questions.insert_if_new(fresh):
            log.info("agent question %s pending thread=%s root=%s index=%d",
                     fresh.agent_question_id, thread_id, root, index)
            await self._fire(self._question_hooks, self._questions.get(fresh.agent_question_id) or fresh)

    async def _close(self, q: AgentQuestion, state: AgentQuestionState) -> None:
        self._questions.set_state(q.agent_question_id, state, self._now())
        log.info("agent question %s thread=%s -> %s", q.agent_question_id, q.thread_id, state.value)
        await self._fire(self._closed_hooks, replace(q, state=state))

    async def _fire(self, hooks: list[Hook], q: AgentQuestion) -> None:
        for hook in hooks:
            try:
                await hook(q)
            except Exception:
                log.exception("agent question hook failed")

    # --- answering -----------------------------------------------------------------------

    def choose(self, agent_question_id: str, option_index: int) -> AgentQuestion:
        q = self._editable(agent_question_id)
        if not 0 <= option_index < len(q.options):
            raise QuestionError("Нет такого варианта")
        return self._stage(q, q.options[option_index])

    def answer_text(self, agent_question_id: str, text: str) -> AgentQuestion:
        q = self._editable(agent_question_id)
        text = text.strip()
        if not text or len(text) > ANSWER_MAX_CHARS:
            raise QuestionError(f"Ответ: от 1 до {ANSWER_MAX_CHARS} символов")
        return self._stage(q, text)

    def unstage(self, agent_question_id: str) -> AgentQuestion:
        q = self._editable(agent_question_id)
        self._questions.set_staged(agent_question_id, None, self._now())
        return replace(q, staged=None)

    async def send(self, agent_question_id: str) -> None:
        """One message with this question's answer. A fresh read first: if the thread already moved on, nothing is sent."""
        q = self._editable(agent_question_id)
        if q.staged is None:
            raise QuestionError("Сначала выберите ответ")
        await self.scan_thread(q.thread_id)
        q = self._editable(agent_question_id)  # raises when the scan found it answered elsewhere
        assert q.staged is not None
        try:
            await self._client.send_user_message(q.thread_id, build_reply(q.item_id, q.index, q.title, q.staged))
        except TimeoutError:
            raise QuestionError("Codex не ответил: проверьте тред, прежде чем повторять") from None
        except (NotConnected, RpcError, ValueError):
            raise QuestionError("Не удалось отправить в Codex") from None
        self._questions.mark_sent(agent_question_id, self._now())
        self._mark(q.thread_id)  # the userMessage should be visible within a second or two

    def _editable(self, agent_question_id: str) -> AgentQuestion:
        q = self._questions.get(agent_question_id)
        if q is None:
            raise QuestionError("Неизвестный вопрос")
        if q.state is AgentQuestionState.ANSWERED:
            raise QuestionError("Вопрос уже отвечен")
        if q.state is AgentQuestionState.STALE:
            raise QuestionError("Вопрос устарел")
        if not q.answerable:
            raise QuestionError("Вопрос слишком длинный для Telegram: ответьте в Codex")
        if q.sent_at is not None:
            raise QuestionError("Ответ уже отправлен; ждём подтверждения")
        return q

    def _stage(self, q: AgentQuestion, answer: str) -> AgentQuestion:
        self._questions.set_staged(q.agent_question_id, answer, self._now())
        return replace(q, staged=answer)

    def _now(self) -> int:
        return int(self._clock())


def _item_id(entry: Any) -> str | None:
    item = entry.get("item") if isinstance(entry, dict) else None
    item_id = item.get("id") if isinstance(item, dict) else None
    return item_id if isinstance(item_id, str) and item_id else None


def _user_text(item: dict[str, Any]) -> str:
    """The text parts of a userMessage item (nothing else is read)."""
    parts = item.get("content")
    if not isinstance(parts, list):
        return ""
    return "\n".join(p["text"] for p in parts if isinstance(p, dict) and isinstance(p.get("text"), str))
