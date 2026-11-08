"""Posts and edits the Telegram messages of one question card. As a rich message the card is ONE message (32768
characters, buttons in the text). As the classic fallback a card never cuts text: a body longer than one Telegram
message continues in further messages, and the keyboard (one-time action keys) sits on the LAST message only, so the
owner reads the whole question and then finds the buttons under it."""

import contextlib
import logging
from collections.abc import Callable

from cqb.telegram.api import BadRequest, RichRejected
from cqb.telegram.fmt import chunks
from cqb.telegram.models import Doc, Keyboard
from cqb.telegram.present import Presenter
from cqb.telegram.render import to_view

log = logging.getLogger(__name__)

PART_LIMIT = 3900  # below Telegram's 4096, leaving room for the continuation label


def card_parts(text: str, label: str) -> list[str]:
    """The text split on line or word boundaries into messages; the continuations start with `↪ Q3 · 2/3`."""
    pieces = chunks(text, PART_LIMIT) or [text]
    total = len(pieces)
    return [pieces[0], *(f"↪ {label} · {n}/{total}\n\n{p}" for n, p in enumerate(pieces[1:], start=2))]


class CardPoster:
    def __init__(self, present: Presenter, chat_id: int) -> None:
        self._present = present
        self._api = present.api
        self._chat_id = chat_id

    async def sync(
        self, ids: list[int], doc: Doc, label: str, topic_id: int | None, bind: Callable[[list[int]], None],
    ) -> list[int]:
        """The card (a bound Doc) as one rich message; when Telegram refuses it, as classic message(s) from the same
        Doc, split where needed. A refusal of the method itself switches every later card to classic as well."""
        if self._present.rich and not self._present.is_classic(ids[0] if ids else None):
            try:
                return await self._sync_rich(ids, doc, topic_id, bind)
            except RichRejected as exc:
                self._present.rejected(exc)
        view = to_view(doc)
        ids = await self.sync_text(ids, card_parts(view.text, label), view.keyboard, topic_id, bind)
        if self._present.rich and ids:  # this card stays classic: do not ask for rich again on every refresh
            self._present.keep_classic(ids[0])
        return ids

    async def _sync_rich(
        self, ids: list[int], doc: Doc, topic_id: int | None, bind: Callable[[list[int]], None],
    ) -> list[int]:
        html = self._present.html(doc)
        ids = list(ids)
        if ids:
            try:
                await self._api.edit_rich_message(self._chat_id, ids[0], html)
            except BadRequest as exc:
                if not exc.not_modified:
                    if not exc.gone:
                        raise
                    await self._forget(ids)
                    bind([])
                    return await self._sync_rich([], doc, topic_id, bind) if topic_id is not None else []
        elif topic_id is not None:
            sent = await self._api.send_rich_message(self._chat_id, html, thread_id=topic_id)
            ids.append(sent.message_id)
            bind(ids)
        if len(ids) > 1:  # it was a classic card of several messages
            await self._forget(ids[1:])
            ids = ids[:1]
            bind(ids)
        return ids

    async def sync_text(
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
                    return await self.sync_text([], parts, keyboard, topic_id, bind) if topic_id is not None else []
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
