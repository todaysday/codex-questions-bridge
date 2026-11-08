"""Read model for the thread list view (design section 2.1): no side effects, no network, no daemon calls."""

import time
from collections.abc import Callable
from dataclasses import dataclass

from cqb.dao.agent_questions import AgentQuestionDao
from cqb.dao.consultants import ConsultantDao
from cqb.dao.questions import QuestionDao
from cqb.dao.threads import ThreadDao
from cqb.domain.enums import ArchiveState, ConsultantState, RunState
from cqb.domain.models import Thread
from cqb.services.live_state import LiveState

MAX_ROOTS = 1000  # roots read per render (recency window); the page is cut after sorting


@dataclass(frozen=True, slots=True)
class RegistryRow:
    thread: Thread
    consultant_state: ConsultantState | None
    topic_id: int | None
    pending_questions: int
    working: bool = False  # status active without a waitingOn* flag


@dataclass(frozen=True, slots=True)
class RegistryPage:
    rows: tuple[RegistryRow, ...]
    page: int
    has_next: bool
    archived: bool
    recency_days: int
    pages: int = 1  # how many pages the current list has (at least 1)
    older: bool = False  # the list of threads that are not relevant now
    pending: int = 0  # threads with a pending question, of the whole archive state (both lists)
    working: int = 0
    total: int = 0


class RegistryQuery:
    def __init__(
        self,
        threads: ThreadDao,
        consultants: ConsultantDao,
        questions: QuestionDao,
        source_id: str,
        recency_days: int,
        clock: Callable[[], float] = time.time,
        live: LiveState | None = None,
        agent_questions: AgentQuestionDao | None = None,
        recent_hours: int = 24,
    ) -> None:
        self._threads = threads
        self._consultants = consultants
        self._questions = questions
        self._source = source_id
        self._recency_days = recency_days
        self._clock = clock
        self._live = live
        self._agent_questions = agent_questions
        self._recent = recent_hours * 3600

    def is_root(self, thread_id: str) -> bool:
        thread = self._threads.get(self._source, thread_id)
        return thread is not None and thread.is_root

    def page(self, page: int, page_size: int, archived: bool = False, older: bool | None = None) -> RegistryPage:
        """Roots (never nested workers) in the recency window: questions first, then working, then by activity.

        `older` None lists every root; False only the relevant ones (a pending question, working, a consultant, or
        updated within `recent_hours`); True the others."""
        page = max(page, 0)
        state = ArchiveState.ARCHIVED if archived else ArchiveState.ACTIVE
        now = int(self._clock())
        since = now - self._recency_days * 86400
        roots = self._threads.list_roots(self._source, state, MAX_ROOTS, 0, since)  # updated_at desc, id
        everything = sorted(self._rows(roots), key=lambda r: (not r.pending_questions, not r.working))  # stable
        rows = everything if older is None else [r for r in everything if self._relevant(r, now) is not older]
        start = page * page_size
        return RegistryPage(
            rows=tuple(rows[start:start + page_size]), page=page, has_next=len(rows) > start + page_size,
            archived=archived, recency_days=self._recency_days, pages=max(-(-len(rows) // page_size), 1),
            older=bool(older), pending=sum(1 for r in everything if r.pending_questions),
            working=sum(1 for r in everything if r.working), total=len(everything),
        )

    def _relevant(self, row: RegistryRow, now: int) -> bool:
        return bool(row.pending_questions or row.working or row.consultant_state is not None
                    or row.thread.updated_at >= now - self._recent)

    def describe(self, root_thread_id: str) -> RegistryRow | None:
        thread = self._threads.get(self._source, root_thread_id)
        if thread is None:
            return None
        return self._rows([thread])[0]

    def _rows(self, roots: list[Thread]) -> tuple[RegistryRow, ...]:
        bound = self._consultants.get_many(self._source, [t.thread_id for t in roots])
        pending = self._questions.open_counts_by_root(self._source)
        if self._agent_questions is not None:  # blocking requests and agent-message questions both count
            for root, count in self._agent_questions.open_counts_by_root(self._source).items():
                pending[root] = pending.get(root, 0) + count
        return tuple(
            RegistryRow(
                thread=t,
                consultant_state=bound[t.thread_id].state if t.thread_id in bound else None,
                topic_id=bound[t.thread_id].tg_topic_id if t.thread_id in bound else None,
                pending_questions=pending.get(t.thread_id, 0),
                working=self._run_state(t) is RunState.WORKING,
            )
            for t in roots
        )

    def _run_state(self, thread: Thread) -> RunState | None:
        """Live broadcast first, then the status stored by the last inventory poll."""
        live = self._live.get(thread.thread_id) if self._live else None
        if live is not None and live.state is not RunState.UNKNOWN:
            return live.state
        return thread.run_state
