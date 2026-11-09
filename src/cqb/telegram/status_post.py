"""/status in a consultant topic: one static snapshot message (Telegram HTML) per request, posted when everything is ready.

No placeholder, no edits, no auto-update: the Telegram typing indicator (refreshed every few seconds) is the
only sign of life while the snapshot is collected, and previous snapshots stay in the topic as history.
A collect can take minutes (the Status Agent summary), so it runs as its own task and never blocks polling;
one collect per root at a time.
"""

import asyncio
import contextlib
import logging
import html
import json
import re

from cqb.services.status import StatusService, StatusUnavailable
from cqb.telegram.api import BotApi, RichRejected
from cqb.telegram.keys import ActionKeys
from cqb.telegram.models import ActionSpec, Button
from cqb.telegram.present import Presenter
from cqb.telegram.status_view import TITLE_MAX, _e
from cqb.telegram.status_view import render_status

log = logging.getLogger(__name__)

TYPING_SECONDS = 4.0  # Telegram shows "typing" for about 5 s per call
UNAVAILABLE = "Нет данных от демона Codex, повторите позже"
FAILED = "Не удалось собрать статус; подробности в логе"
BUSY = "статус уже собирается"


class StatusPoster:
    def __init__(
        self, api: BotApi, service: StatusService, chat_id: int, source_id: str, *, typing_seconds: float = TYPING_SECONDS,
        open_link: str = "", keys: ActionKeys | None = None, presenter: Presenter | None = None,
    ) -> None:
        self._api = api
        self._service = service
        self._chat_id = chat_id
        self._source = source_id
        self._typing_seconds = typing_seconds
        self._open_link = open_link  # [status] open_link: the card's last line; "" = none
        self._keys = keys
        self._presenter = presenter
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
        parse_mode = None  # only the card is HTML; the fixed notices are plain text
        keyboard = ()
        rich_text = None
        try:
            try:
                report = await self._service.snapshot(self._source, root_thread_id)
                text = render_status(report, self._open_link)
                if self._keys:
                    lines = text.splitlines()
                    buttons = []
                    used_lines = set()
                    for agent in report.agents:
                        if not agent.thread_id:
                            continue
                        name = _e(agent.label or "—", TITLE_MAX)
                        index = next((i for i, line in enumerate(lines) if i not in used_lines and re.match(r"^ (?:↳ )?[🟢🟡🔴⚪🟠💤❔]" + re.escape(name) + r"(?:<b> · </b>|$)", line)), None)
                        if index is None:
                            continue
                        used_lines.add(index)
                        payload = json.dumps({"thread": agent.thread_id, "title": agent.label or "Агент"}, ensure_ascii=False)
                        button = self._keys.bind(((Button("👁️‍🗨️ " + (agent.label or "Агент"), action=ActionSpec("agent_thread", root_thread_id, payload)),),))[0][0]
                        buttons.append((button,))
                        marker = lines[index].find(name)
                        lines[index] = lines[index][:marker] + f'<tg-button type="callback_data" data="{html.escape(button.callback_data or "", quote=True)}">👁️‍🗨️</tg-button>' + lines[index][marker:]
                    keyboard = tuple(buttons)
                    if buttons:
                        parts = re.split(r"(<blockquote expandable>.*?</blockquote>)", "\n".join(lines), flags=re.DOTALL)
                        for i, part in enumerate(parts):
                            if '<tg-button ' in part and i:
                                before, _, heading = parts[i - 1].rstrip("\n").rpartition("\n")
                                parts[i - 1] = before
                                content = part[len("<blockquote expandable>"):-len("</blockquote>")].strip("\n")
                                parts[i] = f"<details><summary>{heading}</summary><p>{content}</p></details>"
                        rich_text = "".join(
                            part.replace("\n", "<br>") if part.startswith("<details>")
                            else part.replace("\n", "<br>") if part.startswith("<blockquote expandable>")
                            else "<p>" + part.strip("\n").replace("\n", "<br>") + "</p>"
                            for part in parts if part.strip("\n")
                        )
                parse_mode = "HTML"
            except StatusUnavailable:
                text = UNAVAILABLE
            except Exception:
                log.exception("status snapshot failed")
                text = FAILED
        finally:
            typing.cancel()
        try:
            if rich_text and self._presenter and self._presenter.rich:
                try:
                    await self._api.send_rich_message(self._chat_id, rich_text, thread_id=topic_id)
                    return
                except RichRejected as exc:
                    self._presenter.rejected(exc)
            await self._api.send_message(self._chat_id, text, thread_id=topic_id, parse_mode=parse_mode, keyboard=keyboard)
        except Exception as exc:  # the Bot API client already retried; the snapshot is lost, the next /status works
            log.warning("could not post the status snapshot to topic %d: %s", topic_id, type(exc).__name__)

    async def _typing(self, topic_id: int) -> None:
        while True:
            with contextlib.suppress(Exception):  # cosmetic
                await self._api.send_chat_action(self._chat_id, "typing", thread_id=topic_id)
            await asyncio.sleep(self._typing_seconds)
