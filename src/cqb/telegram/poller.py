"""getUpdates long polling; the offset is persisted so a restart neither replays nor skips."""

import asyncio
import logging
from collections.abc import Awaitable, Callable

from cqb.dao.bot_state import BotStateDao
from cqb.telegram.api import BotApi, TelegramError, Unauthorized
from cqb.telegram.models import Update

log = logging.getLogger(__name__)

OFFSET_KEY = "poll_offset"
BACKOFF_MAX = 30.0


class Poller:
    def __init__(
        self,
        api: BotApi,
        state: BotStateDao,
        handle: Callable[[Update], Awaitable[None]],
        *,
        poll_timeout: int = 30,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._api = api
        self._state = state
        self._handle = handle
        self._timeout = poll_timeout
        self._sleep = sleep

    async def run(self) -> None:
        backoff = 1.0
        while True:
            try:
                updates = await self._api.get_updates(self._state.get_int(OFFSET_KEY) or 0, self._timeout)
            except Unauthorized:
                raise
            except TelegramError as exc:
                log.warning("getUpdates failed: %s; retry in %.0fs", exc, backoff)
                await self._sleep(backoff)
                backoff = min(backoff * 2, BACKOFF_MAX)
                continue
            backoff = 1.0
            await self.process(updates)

    async def process(self, updates: list[Update]) -> None:
        for update in updates:
            try:
                await self._handle(update)
            except Exception:
                log.exception("update %d failed", update.update_id)
        if updates:
            self._state.set(OFFSET_KEY, max(u.update_id for u in updates) + 1)
