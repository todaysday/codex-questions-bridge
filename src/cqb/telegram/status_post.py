"""/status in a consultant topic: one static snapshot message per request, posted when everything is ready.

No placeholder, no edits, no auto-update: the Telegram typing indicator (refreshed every few seconds) is the
only sign of life while the snapshot is collected, and previous snapshots stay in the topic as history.
A collect can take minutes (the Status Agent summary), so it runs as its own task and never blocks polling;
one collect per root at a time.
"""

import asyncio
import contextlib
import logging

from cqb.services.status import StatusService, StatusUnavailable
from cqb.telegram.api import BotApi
from cqb.telegram.status_view import render_status

log = logging.getLogger(__name__)

TYPING_SECONDS = 4.0  # Telegram shows "typing" for about 5 s per call
UNAVAILABLE = "Нет данных от демона Codex, повторите позже"
FAILED = "Не удалось собрать статус; подробности в логе"
BUSY = "статус уже собирается"


class StatusPoster:
    def __init__(
        self, api: BotApi, service: StatusService, chat_id: int, source_id: str, *, typing_seconds: float = TYPING_SECONDS
    ) -> None:
        self._api = api
        self._service = service
        self._chat_id = chat_id
        self._source = source_id
        self._typing_seconds = typing_seconds
        self._running: dict[str, asyncio.Task[None]] = {}  # root -> its collect

    def request(self, root_thread_id: str, topic_id: int) -> bool:
        """Start a collect; False when one is already running for this root (nothing is started)."""
        if root_thread_id in self._running:
            return False
        task = asyncio.create_task(self._post(root_thread_id, topic_id), name="status-snapshot")
        self._running[root_thread_id] = task
        task.add_done_callback(lambda _: self._running.pop(root_thread_id, None))
        return True

    async def close(self) -> None:
        tasks = list(self._running.values())
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _post(self, root_thread_id: str, topic_id: int) -> None:
        typing = asyncio.create_task(self._typing(topic_id), name="status-typing")
        try:
            try:
                text = render_status(await self._service.snapshot(self._source, root_thread_id))
            except StatusUnavailable:
                text = UNAVAILABLE
            except Exception:
                log.exception("status snapshot failed")
                text = FAILED
        finally:
            typing.cancel()
        try:
            await self._api.send_message(self._chat_id, text, thread_id=topic_id)
        except Exception as exc:  # the Bot API client already retried; the snapshot is lost, the next /status works
            log.warning("could not post the status snapshot to topic %d: %s", topic_id, type(exc).__name__)

    async def _typing(self, topic_id: int) -> None:
        while True:
            with contextlib.suppress(Exception):  # cosmetic
                await self._api.send_chat_action(self._chat_id, "typing", thread_id=topic_id)
            await asyncio.sleep(self._typing_seconds)
