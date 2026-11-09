"""Question lifecycle (design section 6) on top of the app-server connection.

A question is `pending_confirmed` only while its live request is held on the current
connection; reconnects demote it to `captured` until the re-resume replays it.
"""

import hashlib
import logging
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import replace

from cqb.codex.appserver import AppServerClient, NotConnected
from cqb.codex.events import (
    Connected, Disconnected, Event, QuestionRequested, RequestResolved,
    ThreadStatusChanged, TurnEnded,
)
from cqb.dao.questions import QuestionDao
from cqb.dao.threads import ThreadDao
from cqb.domain.enums import OPEN_LIFECYCLES, QuestionLifecycle, VerificationSource
from cqb.domain.models import Question

log = logging.getLogger(__name__)

Hook = Callable[[Question], Awaitable[None]]
Answers = Mapping[str, Sequence[str]]
ANSWER_SUFFIX = "и не трьінди"
WAIT_SUFFIX = "After delegation, wait for the agent’s result using notifications or a wait tool."


def with_answer_suffix(text: str) -> str:
    stripped = text.rstrip()
    if stripped.endswith(f"{ANSWER_SUFFIX} {WAIT_SUFFIX}"):
        return text
    if stripped.endswith(ANSWER_SUFFIX):
        return f"{text} {WAIT_SUFFIX}"
    return f"{text} {ANSWER_SUFFIX} {WAIT_SUFFIX}"


class QuestionError(Exception):
    pass


class QuestionService:
    def __init__(
        self,
        client: AppServerClient,
        questions: QuestionDao,
        threads: ThreadDao,
        source_id: str,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._client = client
        self._questions = questions
        self._threads = threads
        self._source = source_id
        self._clock = clock
        self._subscribed: set[str] = set()
        self._live: dict[str, int | str] = {}  # question_id -> request id on this connection
        self._question_hooks: list[Hook] = []
        self._resolved_hooks: list[Hook] = []
        self._questions.detach_requests(source_id, self._now())  # ids from a previous run are dead

    def subscribe(self, on_question: Hook | None = None, on_resolved: Hook | None = None) -> None:
        """on_question fires once per new question; on_resolved when it leaves the open states."""
        if on_question:
            self._question_hooks.append(on_question)
        if on_resolved:
            self._resolved_hooks.append(on_resolved)

    def get(self, question_id: str) -> Question | None:
        return self._questions.get(question_id)

    def open_questions(self, root_thread_id: str | None = None, limit: int = 20) -> list[Question]:
        return self._questions.list_open(self._source, root_thread_id, limit)

    def open_count(self) -> int:
        return sum(self._questions.open_counts_by_root(self._source).values())

    def postable_without_card(self, limit: int = 50) -> list[Question]:
        return self._questions.list_postable(self._source, limit)

    def bind_card_messages(self, question_id: str, ids: list[int]) -> None:
        self._questions.set_card_messages(question_id, ids)

    def by_card_message(self, message_id: int) -> Question | None:
        return self._questions.find_by_card_message(message_id)

    async def handle(self, event: Event) -> None:
        match event:
            case Connected():
                for thread_id in self._questions.open_thread_ids(self._source):
                    await self.watch_thread(thread_id)
            case Disconnected():
                self._subscribed.clear()
                self._live.clear()
                self._questions.detach_requests(self._source, self._now())
            case ThreadStatusChanged():
                await self._on_status(event)
            case QuestionRequested():
                await self._on_request(event)
            case RequestResolved():
                await self._on_resolved(event)
            case TurnEnded():
                for q in self._questions.list_open_for_thread(self._source, event.thread_id, event.turn_id):
                    await self._close(q, QuestionLifecycle.EXPIRED_OR_STALE)
                await self._release_if_idle(event.thread_id)

    async def watch_thread(self, thread_id: str) -> None:
        """Subscribe via thread/resume so the thread's live requests (and replays) reach us."""
        if thread_id in self._subscribed:
            return
        try:
            result = await self._client.call("thread/resume", {"threadId": thread_id, "excludeTurns": True})
        except NotConnected:
            return  # the Connected event re-runs this
        except Exception as exc:
            log.warning("resume %s failed: %s", thread_id, exc)
            return
        self._subscribed.add(thread_id)
        status = (result.get("thread") or {}).get("status") or {}
        flags = status.get("activeFlags") or []
        if not ({"waitingOnUserInput", "waitingOnApproval"} & set(flags)):
            await self._expire_thread(thread_id)

    def stage_decision(self, question_id: str, answers: Answers) -> Question:
        q = self._require_open(question_id)
        staged = _validate(q, answers)
        self._questions.stage(question_id, staged, self._now())
        return replace(q, lifecycle=QuestionLifecycle.DECISION_STAGED, staged=staged)

    def unstage_decision(self, question_id: str) -> Question:
        q = self._require_open(question_id)
        if q.delivered_to_codex:
            raise QuestionError("answer already sent")
        lifecycle = (
            QuestionLifecycle.PENDING_CONFIRMED if question_id in self._live else QuestionLifecycle.CAPTURED
        )
        self._questions.unstage(question_id, lifecycle, self._now())
        return replace(q, lifecycle=lifecycle, staged=None)

    async def answer(self, question_id: str, answers: Answers | None = None) -> None:
        """Answer the captured request on its live connection. Never reports it resolved."""
        q = self._require_open(question_id)
        if q.delivered_to_codex:
            raise QuestionError("answer already sent; waiting for serverRequest/resolved")
        request_id = self._live.get(question_id)
        if request_id is None:
            raise QuestionError("request is not live on the current connection")
        chosen = _validate(q, answers if answers is not None else (q.staged or {}))
        self._questions.stage(question_id, chosen, self._now())
        wire_answers = {k: list(v) for k, v in chosen.items()}
        last = next(reversed(wire_answers))
        wire_answers[last][-1] = with_answer_suffix(wire_answers[last][-1])
        await self._client.respond(
            request_id, {"answers": {k: {"answers": v} for k, v in wire_answers.items()}}
        )
        self._questions.mark_delivered(question_id, self._now())

    def _require_open(self, question_id: str) -> Question:
        q = self._questions.get(question_id)
        if q is None:
            raise QuestionError("unknown question")
        if q.is_secret:
            raise QuestionError("secret input must be given in Codex")
        if q.lifecycle not in OPEN_LIFECYCLES:
            raise QuestionError(f"question is {q.lifecycle.value}")
        return q

    async def _on_status(self, e: ThreadStatusChanged) -> None:
        if e.waiting_on_user_input:
            await self.watch_thread(e.thread_id)
        elif not e.waiting_on_approval:
            await self._expire_thread(e.thread_id)

    async def _expire_thread(self, thread_id: str) -> None:
        for q in self._questions.list_open_for_thread(self._source, thread_id):
            await self._close(q, QuestionLifecycle.EXPIRED_OR_STALE)
        await self._release_if_idle(thread_id)

    async def _on_request(self, e: QuestionRequested) -> None:
        now = self._now()
        key = "\0".join((self._source, e.thread_id, e.turn_id, e.item_id))
        qid = "q_" + hashlib.sha1(key.encode()).hexdigest()[:12]
        thread = self._threads.get(self._source, e.thread_id)
        fresh = Question(
            question_id=qid, source_id=self._source,
            root_thread_id=thread.root_thread_id if thread else e.thread_id,
            origin_thread_id=e.thread_id, turn_id=e.turn_id, tool_use_id=e.item_id,
            request_id=str(e.request_id), items=e.items, is_secret=e.is_secret,
            is_blocking=e.is_blocking, lifecycle=QuestionLifecycle.PENDING_CONFIRMED,
            verification_source=VerificationSource.HOST_RPC, staged=None,
            delivered_to_codex=False, created_at=now, updated_at=now,
        )
        if self._questions.insert_if_new(fresh):
            self._live[qid] = e.request_id
            await self._fire(self._question_hooks, self._questions.get(qid) or fresh)  # with its Q number
            return
        existing = self._questions.get(qid)
        if existing is None or existing.lifecycle not in OPEN_LIFECYCLES:
            return  # already resolved or stale: a replay must not reopen it
        lifecycle = (
            QuestionLifecycle.DECISION_STAGED
            if existing.lifecycle is QuestionLifecycle.DECISION_STAGED
            else QuestionLifecycle.PENDING_CONFIRMED
        )
        self._questions.attach_request(qid, str(e.request_id), lifecycle, now)
        self._live[qid] = e.request_id

    async def _on_resolved(self, e: RequestResolved) -> None:
        q = self._questions.find_open_by_request(self._source, e.thread_id, str(e.request_id))
        if q is not None:
            await self._close(q, QuestionLifecycle.RESOLVED_OBSERVED)
        await self._release_if_idle(e.thread_id)

    async def _close(self, q: Question, lifecycle: QuestionLifecycle) -> None:
        self._questions.set_lifecycle(q.question_id, lifecycle, self._now())
        self._live.pop(q.question_id, None)
        await self._fire(self._resolved_hooks, replace(q, lifecycle=lifecycle))

    async def _release_if_idle(self, thread_id: str) -> None:
        if thread_id not in self._subscribed:
            return
        if self._questions.list_open_for_thread(self._source, thread_id):
            return
        self._subscribed.discard(thread_id)
        try:
            await self._client.call("thread/unsubscribe", {"threadId": thread_id})
        except Exception as exc:
            log.debug("unsubscribe %s failed: %s", thread_id, exc)

    async def _fire(self, hooks: list[Hook], q: Question) -> None:
        for hook in hooks:
            try:
                await hook(q)
            except Exception:
                log.exception("question hook failed")

    def _now(self) -> int:
        return int(self._clock())


def _validate(q: Question, answers: Answers) -> dict[str, tuple[str, ...]]:
    chosen = {k: tuple(v) for k, v in answers.items()}
    if set(chosen) != {i.id for i in q.items} or not all(chosen.values()):
        raise QuestionError("answers must cover every question exactly")
    for item in q.items:
        labels = {o.label for o in item.options}
        if labels and not item.is_other and not set(chosen[item.id]) <= labels:
            raise QuestionError(f"answer for {item.id} is not one of the options")
    return chosen
