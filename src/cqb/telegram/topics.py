"""Adapter: the consultant service's TopicPort on top of the Bot API."""

import logging

from cqb.services.consultants import TopicGone
from cqb.telegram.api import BadRequest, BotApi, TelegramError
from cqb.telegram.fmt import chunks

log = logging.getLogger(__name__)


class TelegramTopics:
    def __init__(self, api: BotApi, chat_id: int) -> None:
        self._api = api
        self._chat_id = chat_id

    async def create_topic(self, name: str) -> int:
        return (await self._api.create_forum_topic(self._chat_id, name)).thread_id

    async def set_topic_closed(self, topic_id: int, closed: bool) -> None:
        try:
            if closed:
                await self._api.close_forum_topic(self._chat_id, topic_id)
            else:
                await self._api.reopen_forum_topic(self._chat_id, topic_id)
        except BadRequest as exc:
            if exc.gone:
                raise TopicGone(str(exc)) from None
            if "TOPIC_NOT_MODIFIED" in exc.description or exc.not_modified:
                return  # already in the wanted state
            raise

    async def announce(self, topic_id: int, text: str) -> None:
        """Opening message in a new consultant topic, pinned (by its own message id). A failed pin (no rights) is only
        logged: the message stays. A failed send raises; the caller decides."""
        sent = await self._api.send_message(self._chat_id, text, thread_id=topic_id)
        try:
            await self._api.pin_chat_message(self._chat_id, sent.message_id)
        except TelegramError as exc:
            log.warning("cannot pin the opening message in topic %d: %s", topic_id, exc)

    async def say(self, topic_id: int, text: str) -> None:
        """Plain text (no parse mode, so a reply can never fail on entities), split below 4096."""
        for piece in chunks(text):
            await self._api.send_message(self._chat_id, piece, thread_id=topic_id)

    async def typing(self, topic_id: int) -> None:
        await self._api.send_chat_action(self._chat_id, "typing", thread_id=topic_id)
