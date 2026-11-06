"""Adapter: the consultant service's TopicPort on top of the Bot API."""

from cqb.services.consultants import TopicGone
from cqb.telegram.api import BadRequest, BotApi


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
        """Opening message in a new consultant topic; best effort, the binding is already saved."""
        await self._api.send_message(self._chat_id, text, thread_id=topic_id)
