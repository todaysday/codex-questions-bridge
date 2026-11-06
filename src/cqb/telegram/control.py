"""Service topics and the single editable registry message (design section 2.1)."""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

from cqb.dao.bot_state import BotStateDao
from cqb.services.registry import RegistryQuery
from cqb.telegram.api import BadRequest, BotApi, TelegramError
from cqb.telegram.keys import ActionKeys
from cqb.telegram.models import Keyboard, SentMessage
from cqb.telegram.registry_view import PAGE_SIZE, render_registry

log = logging.getLogger(__name__)

CONTROL = "control"
INBOX = "inbox"
MAX_MESSAGE_AGE = 6 * 3600  # re-render at least this often so callback ids never expire on screen


class ControlTopics:
    """Creates the two service topics once, remembers their ids, recreates a deleted one."""

    def __init__(self, api: BotApi, state: BotStateDao, chat_id: int, control_name: str, inbox_name: str) -> None:
        self._api = api
        self._state = state
        self._chat_id = chat_id
        self._names = {CONTROL: control_name, INBOX: inbox_name}

    async def ensure(self) -> None:
        for kind in (CONTROL, INBOX):
            if self.topic_id(kind) is None:
                await self._create(kind)

    def topic_id(self, kind: str) -> int | None:
        return self._state.get_int(f"topic.{kind}")

    def kind_of(self, topic_id: int) -> str | None:
        return next((k for k in (CONTROL, INBOX) if self.topic_id(k) == topic_id), None)

    async def send(self, kind: str, text: str, keyboard: Keyboard | None = None) -> SentMessage:
        """Post into a service topic; if the topic was deleted by hand, recreate it and retry once."""
        topic = self.topic_id(kind)
        if topic is None:
            topic = await self._create(kind)
        try:
            return await self._api.send_message(self._chat_id, text, thread_id=topic, keyboard=keyboard)
        except BadRequest as exc:
            if not exc.gone:
                raise
        log.warning("service topic %s is gone; recreating", kind)
        topic = await self._create(kind)
        return await self._api.send_message(self._chat_id, text, thread_id=topic, keyboard=keyboard)

    async def _create(self, kind: str) -> int:
        topic = await self._api.create_forum_topic(self._chat_id, self._names[kind])
        self._state.set(f"topic.{kind}", topic.thread_id)
        return topic.thread_id


class RegistryPublisher:
    """Edits one pinned message; skips the edit when the rendered content did not change."""

    def __init__(
        self,
        api: BotApi,
        topics: ControlTopics,
        state: BotStateDao,
        keys: ActionKeys,
        query: RegistryQuery,
        chat_id: int,
        *,
        interval: float,
        min_edit_interval: float = 3.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._api = api
        self._topics = topics
        self._state = state
        self._keys = keys
        self._query = query
        self._chat_id = chat_id
        self._interval = interval
        self._min_edit = min_edit_interval
        self._clock = clock
        self._lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._force = False
        self._last_write = float("-inf")
        self._page = 0
        self._archived = False

    @property
    def message_id(self) -> int | None:
        return self._state.get_int("registry.message_id")

    def set_view(self, page: int, archived: bool) -> None:
        self._page, self._archived = max(page, 0), archived

    def request_refresh(self, force: bool = False) -> None:
        """Coalesces: any number of requests before the next pass cause one edit."""
        self._force = self._force or force
        self._wake.set()

    async def run(self, before: Callable[[], Awaitable[object]] | None = None) -> None:
        while True:
            try:
                await asyncio.wait_for(self._wake.wait(), self._interval)
            except TimeoutError:
                pass
            force, self._force = self._force, False
            self._wake.clear()
            try:
                if before is not None:
                    await before()
                await self.refresh(force)
            except TelegramError as exc:
                log.warning("registry refresh failed: %s", exc)
            except Exception:
                log.exception("registry refresh failed")

    async def refresh(self, force: bool = False) -> bool:
        """Returns True when Telegram was written to."""
        async with self._lock:
            now = int(self._clock())
            page = self._query.page(self._page, PAGE_SIZE, self._archived)
            if not page.rows and self._page > 0:
                self._page = 0
                page = self._query.page(0, PAGE_SIZE, self._archived)
            view = render_registry(page, self._chat_id, now)
            digest = view.fingerprint()
            message_id = self.message_id
            fresh = now - (self._state.get_int("registry.written_at") or 0) < MAX_MESSAGE_AGE
            if not force and message_id is not None and fresh and digest == self._state.get("registry.hash"):
                return False
            keyboard = self._keys.bind(view.keyboard)
            await self._pace()
            if message_id is not None:
                message_id = await self._edit(message_id, view.text, keyboard)
            if message_id is None:
                sent = await self._topics.send(CONTROL, view.text, keyboard)
                self._state.set("registry.message_id", sent.message_id)
                await self._pin(sent.message_id)
            self._state.set("registry.hash", digest)
            self._state.set("registry.written_at", now)
            self._keys.purge_expired()
            return True

    async def _edit(self, message_id: int, text: str, keyboard: Keyboard) -> int | None:
        try:
            await self._api.edit_message_text(self._chat_id, message_id, text, keyboard)
        except BadRequest as exc:
            if exc.not_modified:
                return message_id
            if exc.gone:
                log.warning("registry message is gone; recreating")
                return None
            raise
        return message_id

    async def _pin(self, message_id: int) -> None:
        try:
            await self._api.pin_chat_message(self._chat_id, message_id)
        except TelegramError as exc:
            log.warning("cannot pin the registry message: %s", exc)

    async def _pace(self) -> None:
        wait = self._min_edit - (time.monotonic() - self._last_write)
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_write = time.monotonic()
