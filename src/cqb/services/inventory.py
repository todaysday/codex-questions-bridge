import asyncio
import dataclasses
import logging
import time
from collections.abc import Awaitable, Callable

from cqb.codex.appserver import AppServerClient, NotConnected
from cqb.codex.events import (
    Connected, Event, ThreadArchiveChanged, thread_from_wire, thread_waiting_on_user,
)
from cqb.dao.threads import ThreadDao
from cqb.domain.enums import ArchiveState
from cqb.domain.models import Thread

log = logging.getLogger(__name__)

PAGE_SIZE = 50
MAX_PAGES = 20
MAX_DEPTH = 16
SOURCE_KINDS = [
    "cli", "vscode", "exec", "appServer", "subAgent", "subAgentReview",
    "subAgentCompact", "subAgentThreadSpawn", "subAgentOther", "unknown",
]


class InventoryService:
    """Polls thread/list. Only an explicit listing or archive event changes archive state."""

    def __init__(
        self,
        client: AppServerClient,
        threads: ThreadDao,
        source_id: str,
        *,
        poll_interval: float,
        recency_days: int,
        on_waiting: Callable[[str], Awaitable[None]] | None = None,
        clock: Callable[[], float] = time.time,
        page_size: int = PAGE_SIZE,
        max_pages: int = MAX_PAGES,
    ) -> None:
        self._client = client
        self._threads = threads
        self._source = source_id
        self._interval = poll_interval
        self._recency = recency_days * 86400
        self._on_waiting = on_waiting
        self._clock = clock
        self._page_size = page_size
        self._max_pages = max_pages
        self._poke = asyncio.Event()

    async def run(self) -> None:
        while True:
            try:
                await self.poll_once()
            except NotConnected:
                log.debug("inventory poll skipped: not connected")
            except Exception:
                log.exception("inventory poll failed")
            try:
                await asyncio.wait_for(self._poke.wait(), self._interval)
            except TimeoutError:
                pass
            self._poke.clear()

    def poke(self) -> None:
        """Ask for an immediate poll (manual /threads)."""
        self._poke.set()

    async def handle(self, event: Event) -> None:
        if isinstance(event, Connected):
            self._poke.set()
        elif isinstance(event, ThreadArchiveChanged):
            state = ArchiveState.ARCHIVED if event.archived else ArchiveState.ACTIVE
            self._threads.set_archive_state(self._source, event.thread_id, state, int(self._clock()))

    async def poll_once(self) -> int:
        seen = 0
        for archived in (False, True):
            seen += await self._poll_list(archived)
        return seen

    async def _poll_list(self, archived: bool) -> int:
        state = ArchiveState.ARCHIVED if archived else ArchiveState.ACTIVE
        cutoff = int(self._clock()) - self._recency
        cursor: str | None = None
        seen = 0
        for _ in range(self._max_pages):
            result = await self._client.call(
                "thread/list",
                {
                    "archived": archived, "limit": self._page_size, "cursor": cursor,
                    "sortKey": "updated_at", "sourceKinds": SOURCE_KINDS,
                },
            )
            raw_page = result.get("data") or []
            now = int(self._clock())
            fresh = [r for r in raw_page if int(r.get("updatedAt") or 0) >= cutoff]
            page = {t.thread_id: t for t in (thread_from_wire(r, self._source, now) for r in fresh)}
            self._threads.upsert_many(
                dataclasses.replace(
                    t, root_thread_id=self._resolve_root(t, page),
                    archive_state=state, archive_verified_at=now,
                )
                for t in page.values()
            )
            seen += len(page)
            if self._on_waiting and not archived:
                for r in fresh:
                    if thread_waiting_on_user(r):
                        await self._on_waiting(r["id"])
            cursor = result.get("nextCursor")
            if not cursor or len(fresh) < len(raw_page):
                break
        else:
            log.warning("thread/list (archived=%s) stopped at the %d page limit", archived, self._max_pages)
        return seen

    def _resolve_root(self, thread: Thread, page: dict[str, Thread]) -> str:
        cur = thread
        for _ in range(MAX_DEPTH):
            if cur.parent_thread_id is None:
                return cur.thread_id
            parent = page.get(cur.parent_thread_id)
            if parent is None:
                known = self._threads.get(self._source, cur.parent_thread_id)
                return known.root_thread_id if known else cur.parent_thread_id
            cur = parent
        return cur.thread_id
