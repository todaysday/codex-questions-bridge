"""Posts the status card: one message per root, edited in place in the topic it was requested from."""

import logging

from cqb.dao.bot_state import BotStateDao
from cqb.services.status import StatusService, StatusUnavailable
from cqb.telegram.api import BadRequest, BotApi
from cqb.telegram.status_view import render_status

log = logging.getLogger(__name__)

UNAVAILABLE = "Нет данных от демона Codex, повторите позже"


class StatusCards:
    def __init__(self, api: BotApi, state: BotStateDao, service: StatusService, chat_id: int, source_id: str) -> None:
        self._api = api
        self._state = state
        self._service = service
        self._chat_id = chat_id
        self._source = source_id

    async def show(self, root_thread_id: str, topic_id: int) -> str | None:
        """Send or refresh the card; returns a user-facing error text, or None on success."""
        try:
            report = await self._service.collect(self._source, root_thread_id)
        except StatusUnavailable:
            return UNAVAILABLE
        text = render_status(report)
        key = f"status.msg.{root_thread_id}"
        message_id = self._known_message(key, topic_id)
        if message_id is not None and await self._edit(message_id, text):
            return None
        sent = await self._api.send_message(self._chat_id, text, thread_id=topic_id)
        self._state.set(key, f"{topic_id}:{sent.message_id}")
        return None

    def _known_message(self, key: str, topic_id: int) -> int | None:
        stored = self._state.get(key)
        topic, _, message = (stored or "").partition(":")
        return int(message) if message.isdigit() and topic == str(topic_id) else None

    async def _edit(self, message_id: int, text: str) -> bool:
        """False when the message is gone (the caller sends a new one)."""
        try:
            await self._api.edit_message_text(self._chat_id, message_id, text)
        except BadRequest as exc:
            if exc.not_modified:
                return True
            if exc.gone:
                log.info("status message is gone; sending a new one")
                return False
            raise
        return True
