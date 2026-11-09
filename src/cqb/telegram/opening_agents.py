"""Refresh the pinned topic message on demand, coalescing concurrent updates."""

import asyncio
import html
import json
import logging

from cqb.domain.enums import ConsultantState
from cqb.telegram.api import BadRequest, RichRejected
from cqb.telegram.consultant_view import opening_keyboard, render_opening
from cqb.telegram.models import ActionSpec, Button
from cqb.telegram.status_view import NOW_MAX, TITLE_MAX, _agent_line, _e

log = logging.getLogger(__name__)


class OpeningAgents:
    def __init__(self, present, keys, consultants, registry, status, chat_id, source_id, state=None):
        self._present, self._keys = present, keys
        self._consultants, self._registry, self._status = consultants, registry, status
        self._chat_id, self._source = chat_id, source_id
        self._state = state
        self._usage = {}
        self._reports = {}
        self._tasks = {}
        self._locks = {}
        self._last = {}

    def request(self, root):
        if not root or root in self._tasks:
            return
        task = asyncio.create_task(self._refresh(root))
        self._tasks[root] = task
        task.add_done_callback(lambda _: self._tasks.pop(root, None))

    async def update_usage(self, consultant, usage, compactions):
        root = consultant.root_thread_id
        self._usage[root] = (usage, compactions)
        if self._state:
            self._state.set(f"opening_usage:{self._source}:{root}", json.dumps([usage, compactions]))
        if root not in self._reports:
            await self._refresh(root)
            return
        async with self._locks.setdefault(root, asyncio.Lock()):
            await self._render(root)

    async def _refresh(self, root):
        try:
            report = await self._status.collect(self._source, root)
            async with self._locks.setdefault(root, asyncio.Lock()):
                self._reports[root] = report
                await self._render(root)
        except Exception:
            log.exception("could not refresh pinned agent list")

    async def _render(self, root):
        consultant = self._consultants.get(root)
        row = self._registry.describe(root)
        if not consultant or consultant.state is not ConsultantState.OPEN or consultant.opening_message_id is None or row is None:
            return
        saved = self._state.get(f"opening_usage:{self._source}:{root}") if self._state else None
        usage, compactions = self._usage.get(root) or (json.loads(saved) if saved else (None, None))
        heading = render_opening(row, self._source, usage, compactions)
        report = self._reports.get(root)
        keyboard = opening_keyboard()
        if report is None:
            text = heading
            rich = None
        else:
            count = report.agents_total
            label = "🤖 Субагенты · " + ("—" if count is None else str(count))
            lines, buttons = [], []
            for agent in report.agents:
                line = _agent_line(agent, report.now, NOW_MAX)
                if agent.thread_id:
                    payload = json.dumps({"thread": agent.thread_id, "title": agent.label or "Агент"}, ensure_ascii=False)
                    button = self._keys.bind(((Button("👁️‍🗨️ " + (agent.label or "Агент"), action=ActionSpec("agent_thread", root, payload)),),))[0][0]
                    buttons.append((button,))
                    marker = line.find(_e(agent.label or "—", TITLE_MAX))
                    line = line[:marker] + f'<tg-button type="callback_data" data="{html.escape(button.callback_data, quote=True)}">👁️‍🗨️</tg-button>' + line[marker:]
                lines.append(line)
            if count and count > len(report.agents):
                lines.append(f" … и ещё {count - len(report.agents)}")
            body = "<br>".join(lines) or ("Нет данных" if count is None else "Нет субагентов")
            rich = f'<p>{html.escape(heading).replace(chr(10), "<br>")}</p><details><summary>{html.escape(label)}</summary><p>{body}</p></details>'
            plain_lines = "\n".join(_agent_line(a, report.now, NOW_MAX) for a in report.agents)
            if count and count > len(report.agents):
                plain_lines += f"\n … и ещё {count - len(report.agents)}"
            text = html.escape(heading) + "\n\n" + html.escape(label) + "\n<blockquote expandable>" + (plain_lines or ("Нет данных" if count is None else "Нет субагентов")) + "</blockquote>"
            keyboard = keyboard + tuple(buttons)
        signature = (consultant.opening_message_id, rich, text)
        if self._last.get(root) == signature:
            return
        try:
            if rich and self._present.rich:
                try:
                    await self._present.api.edit_rich_message(self._chat_id, consultant.opening_message_id, rich, keyboard=opening_keyboard())
                except RichRejected as exc:
                    self._present.rejected(exc)
                    await self._present.api.edit_message_text(self._chat_id, consultant.opening_message_id, text, keyboard=keyboard, parse_mode="HTML")
            else:
                await self._present.api.edit_message_text(self._chat_id, consultant.opening_message_id, text, keyboard=keyboard, parse_mode="HTML" if report is not None else None)
        except BadRequest as exc:
            if not exc.not_modified:
                raise
        self._last[root] = signature

    async def close(self):
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
