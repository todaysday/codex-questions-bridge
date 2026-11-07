"""The control topic and its single self-updating registry message (design section 2.1).

Nothing but that one pinned message is ever posted into the control topic.
"""

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

MAX_MESSAGE_AGE = 6 * 3600  # re-render at least this often so callback ids never expire on screen
SAFETY_SECONDS = 60  # periodic re-render in case an event was missed; unchanged content is not written


class ControlTopics:
    """Creates the control topic once, remembers its id, recreates it when deleted by hand."""

    def __init__(self, api: BotApi, state: BotStateDao, chat_id: int, name: str) -> None:
        self._api = api
        self._state = state
        self._chat_id = chat_id
        self._name = name

    async def ensure(self) -> None:
        if self.topic_id() is None:
            await self._create()

    def topic_id(self) -> int | None:
        return self._state.get_int("topic.control")

    def is_control(self, topic_id: int) -> bool:
        return self.topic_id() == topic_id

    async def send_registry(self, text: str, keyboard: Keyboard) -> SentMessage:
        """The only post into the control topic; if the topic was deleted, recreate it and retry once."""
        topic = self.topic_id()
        if topic is None:
            topic = await self._create()
        try:
            return await self._api.send_message(self._chat_id, text, thread_id=topic, keyboard=keyboard)
        except BadRequest as exc:
            if not exc.gone:
                raise
        log.warning("control topic is gone; recreating")
        topic = await self._create()
        return await self._api.send_message(self._chat_id, text, thread_id=topic, keyboard=keyboard)

    async def _create(self) -> int:
        topic = await self._api.create_forum_topic(self._chat_id, self._name)
        self._state.set("topic.control", topic.thread_id)
        return topic.thread_id


class RegistryPublisher:
    """Edits one pinned message. Event-driven: callers mark it dirty, `run` coalesces the marks.

    At most one pass per `interval` seconds, and no edit when the rendered content (without the
    time header) is unchanged.
    """

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
        safety: float = SAFETY_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._api = api
        self._topics = topics
        self._state = state
        self._keys = keys
        self._query = query
        self._chat_id = chat_id
        self._interval = interval
        self._safety = safety
        self._clock = clock
        self._lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._force = False

    @property
    def message_id(self) -> int | None:
        return self._state.get_int("registry.message_id")

    def view(self) -> tuple[int, bool]:
        """(page, archived) the pinned message currently shows; remembered across restarts."""
        page, _, archived = (self._state.get("registry.view") or "0:0").partition(":")
        return (int(page) if page.isdigit() else 0), archived == "1"

    def set_view(self, page: int, archived: bool) -> None:
        self._state.set("registry.view", f"{max(page, 0)}:{int(archived)}")

    def request_refresh(self, force: bool = False) -> None:
        """Marks the message dirty. Any number of marks before the next pass cause at most one edit."""
        self._force = self._force or force
        self._wake.set()

    async def run(self, before: Callable[[], Awaitable[object]] | None = None) -> None:
        while True:
            try:
                await asyncio.wait_for(self._wake.wait(), self._safety)
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
            await asyncio.sleep(self._interval)  # marks arriving meanwhile are served by the next pass

    async def refresh(self, force: bool = False) -> bool:
        """Returns True when Telegram was written to."""
        async with self._lock:
            now = int(self._clock())
            page_no, archived = self.view()
            page = self._query.page(page_no, PAGE_SIZE, archived)
            if not page.rows and page_no > 0:
                self.set_view(0, archived)
                page = self._query.page(0, PAGE_SIZE, archived)
            view = render_registry(page, self._chat_id, now)
            digest = view.fingerprint()
            message_id = self.message_id
            fresh = now - (self._state.get_int("registry.written_at") or 0) < MAX_MESSAGE_AGE
            if not force and message_id is not None and fresh and digest == self._state.get("registry.hash"):
                return False
            keyboard = self._keys.bind(view.keyboard)
            if message_id is not None:
                message_id = await self._edit(message_id, view.text, keyboard)
            if message_id is None:
                sent = await self._topics.send_registry(view.text, keyboard)
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
