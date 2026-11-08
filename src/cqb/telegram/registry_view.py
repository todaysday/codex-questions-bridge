"""Thread list view: renders registry DTOs into a Doc (rich message with in-text buttons, or classic text + keyboard).

Row markers: ❓ a question is pending, ⚙️ the thread is working right now (both may apply).
"""

from cqb.domain.enums import ConsultantState
from cqb.services.registry import RegistryPage, RegistryRow
from cqb.telegram.fmt import BUTTON_TEXT_LIMIT, clip, hhmm, topic_link, when
from cqb.telegram.models import ActionSpec, Bold, Button, Doc, Para, Row, View
from cqb.telegram.render import to_view
from cqb.telegram.status_view import folder_name

PAGE_SIZE = 5  # classic rows per page: a text message holds 4096 characters
RICH_PAGE_SIZE = 25  # rich: 32768 characters and 500 blocks, one block per thread
NAME_LIMIT = 60
FOLDER_LIMIT = 32
CONSULTANT_MARK = {
    None: "—",
    ConsultantState.OPEN: "✅",
    ConsultantState.ARCHIVED: "🔒",
    ConsultantState.ERROR: "⚠️",
    ConsultantState.CREATING: "⏳",
}
QUESTION_MARK = "❓"
WORKING_MARK = "⚙️"


def thread_name(row: RegistryRow) -> str:
    t = row.thread
    return clip(" ".join((t.display_name or "").split()) or f"Thread {t.thread_id[:8]}", NAME_LIMIT)


def render_registry(page: RegistryPage, chat_id: int, now: int) -> View:
    """The classic rendering of the list."""
    return to_view(registry_doc(page, chat_id, now))


def registry_doc(page: RegistryPage, chat_id: int, now: int) -> Doc:
    """Rich: one paragraph per thread, `markers name · 📁 folder · activity · consultant`, with its button right in the
    line, then the navigation row. Classic: the numbered two-line entries and one button per keyboard row."""
    title = "📦 Архивные треды Codex" if page.archived else "📋 Активные треды Codex"
    blocks: list[Para | Row] = [Para((Bold(title), f" · обновлено {hhmm(now)}"), volatile=True)]
    for n, row in enumerate(page.rows, start=1):
        marks = (QUESTION_MARK if row.pending_questions else "") + (WORKING_MARK if row.working else "")
        name, activity = thread_name(row), when(row.thread.updated_at, now)
        consultant = f"консультант: {CONSULTANT_MARK[row.consultant_state]}"
        folder = folder_name(row.thread.cwd)
        rich = " · ".join(filter(None, (
            f"{marks} {name}" if marks else name, f"📁 {clip(folder, FOLDER_LIMIT)}" if folder else "", activity, consultant,
        )))
        classic = f"{n}. {marks + ' ' if marks else ''}{name}\n   активность {activity} · {consultant}"
        button = _row_button(n, row, chat_id, page.archived)
        blocks.append(Para((rich, *([button] if button else [])), classic=classic))
    if not page.rows:
        blocks.append(Para((f"Нет тредов за последние {page.recency_days} дн.",)))
    blocks.append(Row(_nav_buttons(page)))
    return Doc(tuple(blocks), per_row=1)


def _row_button(n: int, row: RegistryRow, chat_id: int, archived: bool) -> Button | None:
    link = topic_link(chat_id, row.topic_id) if row.topic_id else None
    if row.consultant_state in (ConsultantState.OPEN, ConsultantState.ARCHIVED) and link:
        return Button("💬 Открыть", url=link, classic_text=_label(f"{n}. 💬 Открыть"))
    if not archived and row.consultant_state not in (ConsultantState.OPEN, ConsultantState.ARCHIVED):
        return Button("➕ Создать", ActionSpec("create", row.thread.thread_id),
                      classic_text=_label(f"{n}. ➕ Создать консультанта"))
    return None


def _nav_buttons(page: RegistryPage) -> tuple[Button, ...]:
    a = int(page.archived)
    buttons: list[Button] = []
    if page.page > 0:
        buttons.append(Button("◀️", ActionSpec("page", payload=f"{page.page - 1}:{a}")))
    if page.has_next:
        buttons.append(Button("▶️", ActionSpec("page", payload=f"{page.page + 1}:{a}")))
    other = "📋 Активные" if page.archived else "📦 Архивные"
    buttons.append(Button(other, ActionSpec("page", payload=f"0:{1 - a}")))
    return tuple(buttons)


def _label(text: str) -> str:
    return clip(text, BUTTON_TEXT_LIMIT)
