"""Posts a Doc as a rich message (Bot API 10.3 in-text buttons) or, when rich mode is off or refused, as the classic
text + inline keyboard.

Rich mode starts as the `[telegram] rich_messages` flag says. If Telegram answers that the method is not available for
this bot or chat, one warning is logged and rich mode stays off until the process restarts. If it only refuses one
message's markup, that message goes out classic and is remembered (bounded, until restart) so it is not retried as rich
on every refresh; the next new message tries rich again. No message text is ever logged."""

import logging

from cqb.telegram.api import BotApi, RichRejected
from cqb.telegram.models import Doc, SentMessage
from cqb.telegram.render import fits_rich, to_html, to_view

log = logging.getLogger(__name__)


CLASSIC_MEMORY = 512  # message ids kept; the oldest are forgotten first


class Presenter:
    def __init__(self, api: BotApi, rich: bool = True) -> None:
        self.api = api
        self._rich = rich
        self._classic: dict[int, None] = {}

    def keep_classic(self, *message_ids: int) -> None:
        """Remember messages that stay classic (Telegram refused to make them rich)."""
        for i in message_ids:
            self._classic.pop(i, None)
            self._classic[i] = None
        while len(self._classic) > CLASSIC_MEMORY:
            del self._classic[next(iter(self._classic))]

    def is_classic(self, message_id: int | None) -> bool:
        return message_id in self._classic

    @property
    def rich(self) -> bool:
        return self._rich

    def html(self, doc: Doc) -> str:
        """The rich HTML of a document, refused here (as Telegram would) when it exceeds the rich limits."""
        if not fits_rich(doc):
            raise RichRejected("rich", 400, "rich message is too long")
        return to_html(doc)

    def rejected(self, exc: RichRejected) -> None:
        """Account for a refused rich message; the caller then renders the same document classic."""
        if exc.unsupported and self._rich:
            self._rich = False
            log.warning("%s: rich messages are not available for this bot or chat; classic messages from now on",
                        exc.method)
        else:
            log.warning("%s: Telegram refused the rich message (%d); sending it as classic text", exc.method, exc.code)

    async def send(
        self, chat_id: int, doc: Doc, *, thread_id: int | None = None, rich: bool | None = None,
    ) -> SentMessage:
        """One message, never split (the classic fallback of a long card is the card poster's job).

        `rich` None: rich when enabled, classic when refused. True: rich only (a refusal is raised after being noted).
        False: classic."""
        if rich is not False and (rich or self._rich):
            try:
                return await self.api.send_rich_message(chat_id, self.html(doc), thread_id=thread_id)
            except RichRejected as exc:
                self.rejected(exc)
                if rich:
                    raise
        view = to_view(doc)
        return await self.api.send_message(chat_id, view.text, thread_id=thread_id, keyboard=view.keyboard)

    async def edit(self, chat_id: int, message_id: int, doc: Doc) -> None:
        if self._rich:
            try:
                return await self.api.edit_rich_message(chat_id, message_id, self.html(doc))
            except RichRejected as exc:
                self.rejected(exc)
        view = to_view(doc)
        await self.api.edit_message_text(chat_id, message_id, view.text, view.keyboard)
