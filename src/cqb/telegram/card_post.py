"""Posts and edits the Telegram messages of one question card. A card never cuts text: a body longer than one
Telegram message continues in further messages. The keyboard (one-time action keys) sits on the LAST message only,
so the owner reads the whole question and then finds the buttons under it."""

import contextlib
import logging
from collections.abc import Callable

from cqb.telegram.api import BadRequest, BotApi
from cqb.telegram.fmt import chunks
from cqb.telegram.models import Keyboard

log = logging.getLogger(__name__)

PART_LIMIT = 3900  # below Telegram's 4096, leaving room for the continuation label


def card_parts(text: str, label: str) -> list[str]:
    """The text split on line or word boundaries into messages; the continuations start with `↪ Q3 · 2/3`."""
    pieces = chunks(text, PART_LIMIT) or [text]
    total = len(pieces)
    return [pieces[0], *(f"↪ {label} · {n}/{total}\n\n{p}" for n, p in enumerate(pieces[1:], start=2))]


class CardPoster:
    def __init__(self, api: BotApi, chat_id: int) -> None:
        self._api = api
        self._chat_id = chat_id

    async def sync(
        self, ids: list[int], parts: list[str], keyboard: Keyboard, topic_id: int | None,
        bind: Callable[[list[int]], None],
    ) -> list[int]:
        """Edit the existing messages, send the missing ones (only with a `topic_id`), delete surplus ones.

        `bind` stores the ids after every change, so a failure half way continues the same card next time.
        A message the owner deleted makes the whole card be posted anew."""
        ids = list(ids)
        last = len(parts) - 1
        for n, text in enumerate(parts):
            shown = keyboard if n == last else ()
            if n < len(ids):
                try:
                    await self._api.edit_message_text(self._chat_id, ids[n], text, shown)
                except BadRequest as exc:
                    if exc.not_modified:
                        continue
                    if not exc.gone:
                        raise
                    await self._forget(ids)
                    bind([])
                    return await self.sync([], parts, keyboard, topic_id, bind) if topic_id is not None else []
            elif topic_id is not None:
                sent = await self._api.send_message(self._chat_id, text, thread_id=topic_id, keyboard=shown)
                ids.append(sent.message_id)
                bind(ids)
        if len(ids) > len(parts):  # the card got shorter
            await self._forget(ids[len(parts):])
            ids = ids[: len(parts)]
            bind(ids)
        return ids

    async def _forget(self, ids: list[int]) -> None:
        for message_id in ids:
            with contextlib.suppress(Exception):  # best effort: a message that is gone is what we want anyway
                await self._api.delete_message(self._chat_id, message_id)
