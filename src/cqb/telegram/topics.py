"""Adapter: the consultant service's TopicPort on top of the Bot API."""

import logging

from cqb.services.consultants import TopicGone
from cqb.telegram.api import BadRequest, RichRejected, TelegramError
from cqb.telegram.fmt import chunks
from cqb.telegram.markdown import markdown_parts
from cqb.telegram.present import Presenter

log = logging.getLogger(__name__)


class TelegramTopics:
    def __init__(self, present: Presenter, chat_id: int) -> None:
        self._present = present
        self._api = present.api
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

    async def announce(self, topic_id: int, text: str) -> int:
        """Opening message in a new consultant topic, pinned (by its own message id). A failed pin (no rights) is only
        logged: the message stays. A failed send raises; the caller decides."""
        sent = await self._api.send_message(self._chat_id, text, thread_id=topic_id)
        try:
            await self._api.pin_chat_message(self._chat_id, sent.message_id)
        except TelegramError as exc:
            log.warning("cannot pin the opening message in topic %d: %s", topic_id, exc)
        return sent.message_id

    async def say(self, topic_id: int, text: str, *, markdown: bool = False) -> None:
        """A message in the topic. Plain text by default (no parse mode, so it can never fail on entities), split
        below 4096. `markdown`: a consultant reply, sanitized and sent as rich Markdown (one to a few messages);
        with rich mode off, or when Telegram refuses the Markdown, it goes as plain text with the markers as they are."""
        if markdown and self._present.rich:
            parts = markdown_parts(text)
            for n, part in enumerate(parts):
                try:
                    await self._api.send_rich_markdown(self._chat_id, part, thread_id=topic_id)
                except RichRejected as exc:
                    self._present.rejected(exc)
                    await self._plain(topic_id, "\n\n".join(parts[n:]))  # this part and the rest, markers left as they are
                    return
            return
        await self._plain(topic_id, text)

    async def _plain(self, topic_id: int, text: str) -> None:
        for piece in chunks(text):
            await self._api.send_message(self._chat_id, piece, thread_id=topic_id)

    async def typing(self, topic_id: int) -> None:
        await self._api.send_chat_action(self._chat_id, "typing", thread_id=topic_id)
