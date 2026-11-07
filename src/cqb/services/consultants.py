"""Manual consultant creation and archive mirroring (design sections 2.2, 7, 8).

A consultant is a durable binding (root thread <-> Telegram topic <-> pico session key).
Only `create` makes one, and only when called for an explicit user action.
"""

import asyncio
import base64
import hashlib
import logging
import sqlite3
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Protocol

from cqb.dao.consultants import ConsultantDao
from cqb.dao.threads import ThreadDao
from cqb.domain.enums import ArchiveState, ConsultantState
from cqb.domain.models import Consultant, Thread

log = logging.getLogger(__name__)

TOPIC_NAME_MAX = 128
PERSIST_ATTEMPTS = 3


class TopicPort(Protocol):
    async def create_topic(self, name: str) -> int: ...
    async def set_topic_closed(self, topic_id: int, closed: bool) -> None: ...
    async def announce(self, topic_id: int, text: str) -> None: ...


class TopicGone(Exception):
    """The topic no longer exists on the messenger side (adapters raise it)."""


class ConsultantError(Exception):
    """User-presentable failure of a create request."""


def session_key(source_id: str, root_thread_id: str) -> str:
    """Lowercase, separator-free apart from the fixed prefix: PicoClaw lowercases session ids."""
    digest = hashlib.sha256(f"{source_id}\0{root_thread_id}".encode()).digest()
    return "cqb-" + base64.b32encode(digest).decode().rstrip("=").lower()[:26]


def topic_name(thread: Thread) -> str:
    name = " ".join((thread.display_name or "").split()) or f"Thread {thread.thread_id[:8]}"
    return name[:TOPIC_NAME_MAX]


class ConsultantService:
    def __init__(
        self,
        consultants: ConsultantDao,
        threads: ThreadDao,
        topics: TopicPort,
        source_id: str,
        chat_id: int,
        clock: Callable[[], float] = time.time,
        persist_delay: float = 0.2,
        on_open: Callable[[str], None] | None = None,
        opening: Callable[[str], str | None] | None = None,
    ) -> None:
        self._dao = consultants
        self._threads = threads
        self._topics = topics
        self._source = source_id
        self._chat_id = chat_id
        self._clock = clock
        self._persist_delay = persist_delay
        self._on_open = on_open  # root id of a consultant that just became open (created or reopened)
        self._opening = opening  # root id -> text of the opening message of its new topic (None: send nothing)
        self._locks: dict[str, list] = {}  # root -> [lock, users]; entries live only while in use
        self._created: dict[str, int] = {}  # root -> topic created but not yet persisted

    def recover(self) -> int:
        """At startup: a CREATING row has an unknown outcome; surface it instead of guessing."""
        n = self._dao.fail_stale_creating(self._source, self._now())
        if n:
            log.warning("%d consultant creation(s) were interrupted; marked error", n)
        return n

    def get(self, root_thread_id: str) -> Consultant | None:
        return self._dao.get(self._source, root_thread_id)

    def open_count(self) -> int:
        return self._dao.count_open(self._source)

    def by_topic(self, topic_id: int) -> Consultant | None:
        return self._dao.get_by_topic(self._chat_id, topic_id)

    def scope_for_session(self, session_key: str) -> tuple[str, str] | None:
        """(source_id, root_thread_id) a pico session may read, only while its consultant is open."""
        c = self._dao.get_by_session_key(session_key)
        if c is None or c.source_id != self._source or c.state is not ConsultantState.OPEN:
            return None
        return c.source_id, c.root_thread_id

    async def create(self, root_thread_id: str) -> Consultant:
        async with self._lock(root_thread_id):
            thread = self._threads.get(self._source, root_thread_id)
            if thread is None or not thread.is_root:
                raise ConsultantError("неизвестный корневой тред")
            existing = self._dao.get(self._source, root_thread_id)
            if existing and existing.state in (ConsultantState.OPEN, ConsultantState.ARCHIVED):
                return existing
            if thread.archive_state is not ArchiveState.ACTIVE:
                raise ConsultantError("тред архивирован или его состояние не подтверждено")
            row = existing or self._dao.reserve(
                self._source, root_thread_id, self._chat_id,
                session_key(self._source, root_thread_id), self._now(),
            )
            if row.state is ConsultantState.ERROR:
                self._dao.set_state(row.consultant_id, ConsultantState.CREATING, self._now())
            topic_id = self._created.get(root_thread_id)
            if topic_id is None:
                topic_id = await self._create_topic(row, thread)
                self._created[root_thread_id] = topic_id
                await self._announce(root_thread_id, topic_id)  # before the row is open: no card can precede it
            await self._persist_open(row, topic_id)
            self._created.pop(root_thread_id, None)
            opened = self._dao.get(self._source, root_thread_id)
            assert opened is not None
            self._opened(root_thread_id)
            return opened

    async def reconcile(self) -> int:
        """Mirror verified archive state onto existing consultants; returns how many changed."""
        changed = 0
        wanted = (ConsultantState.OPEN, ConsultantState.ARCHIVED)
        for c in self._dao.list_by_states(self._source, wanted):
            thread = self._threads.get(self._source, c.root_thread_id)
            if thread is None or thread.archive_state is ArchiveState.UNKNOWN or c.tg_topic_id is None:
                continue  # absence or an unverified state never closes anything
            close = thread.archive_state is ArchiveState.ARCHIVED
            if close == (c.state is ConsultantState.ARCHIVED):
                continue
            async with self._lock(c.root_thread_id):
                if await self._apply_closed(c, close):
                    changed += 1
        return changed

    async def _apply_closed(self, c: Consultant, close: bool) -> bool:
        assert c.tg_topic_id is not None
        try:
            await self._topics.set_topic_closed(c.tg_topic_id, close)
        except TopicGone:
            self._dao.set_state(c.consultant_id, ConsultantState.ERROR, self._now())
            log.warning("topic of consultant %d is gone", c.consultant_id)
            return False
        except Exception as exc:
            log.warning("topic %s for consultant %d failed: %s", "close" if close else "reopen",
                        c.consultant_id, type(exc).__name__)
            return False
        state = ConsultantState.ARCHIVED if close else ConsultantState.OPEN
        self._dao.set_state(c.consultant_id, state, self._now())
        if not close:
            self._opened(c.root_thread_id)
        return True

    def _opened(self, root_thread_id: str) -> None:
        if self._on_open:
            self._on_open(root_thread_id)

    async def _announce(self, root_thread_id: str, topic_id: int) -> None:
        """The opening message (the topic's first message, pinned) goes out once per new topic, best effort."""
        text = self._opening(root_thread_id) if self._opening else None
        if not text:
            return
        try:
            await self._topics.announce(topic_id, text)
        except Exception as exc:
            log.warning("opening message of topic %d failed: %s", topic_id, type(exc).__name__)

    async def _create_topic(self, row: Consultant, thread: Thread) -> int:
        try:
            return await self._topics.create_topic(topic_name(thread))
        except Exception as exc:
            self._dao.set_state(row.consultant_id, ConsultantState.ERROR, self._now())
            log.warning("create topic failed for consultant %d: %s", row.consultant_id, type(exc).__name__)
            raise ConsultantError("не удалось создать топик в Telegram") from exc

    async def _persist_open(self, row: Consultant, topic_id: int) -> None:
        """The topic exists; its id must reach the DB. Retried, and kept in memory if all fail."""
        for attempt in range(PERSIST_ATTEMPTS):
            try:
                self._dao.open(row.consultant_id, topic_id, self._now())
                return
            except sqlite3.Error:
                log.exception("persisting topic %d failed (attempt %d)", topic_id, attempt + 1)
                await asyncio.sleep(self._persist_delay)
        raise ConsultantError("топик создан, но состояние не сохранено; повторите нажатие")

    @asynccontextmanager
    async def _lock(self, root_thread_id: str) -> AsyncIterator[None]:
        entry = self._locks.setdefault(root_thread_id, [asyncio.Lock(), 0])
        entry[1] += 1
        try:
            async with entry[0]:
                yield
        finally:
            entry[1] -= 1
            if entry[1] == 0:
                self._locks.pop(root_thread_id, None)

    def _now(self) -> int:
        return int(self._clock())
