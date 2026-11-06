"""Read model for the thread list view (design section 2.1): no side effects, no network."""

import time
from collections.abc import Callable
from dataclasses import dataclass

from cqb.dao.consultants import ConsultantDao
from cqb.dao.questions import QuestionDao
from cqb.dao.threads import ThreadDao
from cqb.domain.enums import ArchiveState, ConsultantState
from cqb.domain.models import Thread


@dataclass(frozen=True, slots=True)
class RegistryRow:
    thread: Thread
    consultant_state: ConsultantState | None
    topic_id: int | None
    pending_questions: int


@dataclass(frozen=True, slots=True)
class RegistryPage:
    rows: tuple[RegistryRow, ...]
    page: int
    has_next: bool
    archived: bool
    recency_days: int


class RegistryQuery:
    def __init__(
        self,
        threads: ThreadDao,
        consultants: ConsultantDao,
        questions: QuestionDao,
        source_id: str,
        recency_days: int,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._threads = threads
        self._consultants = consultants
        self._questions = questions
        self._source = source_id
        self._recency_days = recency_days
        self._clock = clock

    def page(self, page: int, page_size: int, archived: bool = False) -> RegistryPage:
        """Roots (never nested workers) by updated_at desc inside the recency window."""
        page = max(page, 0)
        state = ArchiveState.ARCHIVED if archived else ArchiveState.ACTIVE
        since = int(self._clock()) - self._recency_days * 86400
        roots = self._threads.list_roots(self._source, state, page_size + 1, page * page_size, since)
        has_next = len(roots) > page_size
        return RegistryPage(
            rows=self._rows(roots[:page_size]), page=page, has_next=has_next,
            archived=archived, recency_days=self._recency_days,
        )

    def describe(self, root_thread_id: str) -> RegistryRow | None:
        thread = self._threads.get(self._source, root_thread_id)
        if thread is None:
            return None
        return self._rows([thread])[0]

    def _rows(self, roots: list[Thread]) -> tuple[RegistryRow, ...]:
        bound = self._consultants.get_many(self._source, [t.thread_id for t in roots])
        pending = self._questions.open_counts_by_root(self._source)
        return tuple(
            RegistryRow(
                thread=t,
                consultant_state=bound[t.thread_id].state if t.thread_id in bound else None,
                topic_id=bound[t.thread_id].tg_topic_id if t.thread_id in bound else None,
                pending_questions=pending.get(t.thread_id, 0),
            )
            for t in roots
        )
