"""Thread list view: renders registry DTOs into a Doc (rich message with in-text buttons, or classic text + keyboard).

Row markers: ❓ a question is pending, 🟢 the thread is working right now (both may apply), then ⚠️ (consultant error) or
🔒 (consultant closed). The consultant state is otherwise not printed: the row's button already says it.
"""

from cqb.domain.enums import ConsultantState
from cqb.services.registry import RegistryPage, RegistryRow
from cqb.telegram.fmt import BUTTON_TEXT_LIMIT, age, clip, topic_link
from cqb.telegram.models import ActionSpec, Blank, Bold, Button, Divider, Doc, Para, Row, View
from cqb.telegram.render import to_view
from cqb.telegram.status_view import folder_name

NAME_LIMIT = 60
FOLDER_LIMIT = 32
CONSULTANT_MARK = {
    None: "—",
    ConsultantState.OPEN: "✅",
    ConsultantState.ARCHIVED: "🔒",
    ConsultantState.ERROR: "⚠️",
    ConsultantState.CREATING: "⏳",
}
STATE_MARKS = {ConsultantState.ERROR: "⚠️", ConsultantState.ARCHIVED: "🔒"}  # the exceptional states only
QUESTION_MARK = "❓"
WORKING_MARK = "🟢"


def thread_name(row: RegistryRow) -> str:
    t = row.thread
    return clip(" ".join((t.display_name or "").split()) or f"Thread {t.thread_id[:8]}", NAME_LIMIT)


def render_registry(page: RegistryPage, chat_id: int, now: int) -> View:
    """The classic rendering of the list."""
    return to_view(registry_doc(page, chat_id, now))


def registry_doc(page: RegistryPage, chat_id: int, now: int) -> Doc:
    """Rich: a title, a counts line, then (after one divider) per thread two paragraphs, `markers <b>name</b> [button]` and
    `📁 folder · age`, then the page row (◀️ n/total ▶️) and the archive toggle. Classic: the numbered two-line entries, one
    button per keyboard row, and the page in the title. No clock time anywhere: the message carries its own."""
    paged = f" · стр. {page.page + 1}/{page.pages}" if page.pages > 1 else ""
    kind = "Архивные треды Codex" if page.archived else "Треды Codex"
    icon = "📦" if page.archived else "📋"
    counts = " · ".join(filter(None, (
        f"{QUESTION_MARK} {page.pending}" if page.pending else "",
        f"{WORKING_MARK} {page.working}" if page.working else "", f"всего {page.total}",
    )))
    blocks: list[Para | Row | Divider | Blank] = [
        Para((f"{icon} ", Bold(kind)), classic=f"{icon} {kind}{paged}"),
        Para((counts,), glue="\n"),
        Divider(),  # once, under the header block; threads are set apart by an empty line
    ]
    for n, row in enumerate(page.rows, start=1):
        marks = "".join(filter(None, (
            QUESTION_MARK if row.pending_questions else "", WORKING_MARK if row.working else "",
            STATE_MARKS.get(row.consultant_state, ""),
        )))
        name = thread_name(row)
        folder = folder_name(row.thread.cwd)
        line2 = " · ".join(filter(None, (
            f"📁 {clip(folder, FOLDER_LIMIT)}" if folder else "", age(row.thread.updated_at, now),
        )))
        button = _row_button(n, row, chat_id, page.archived)
        if n > 1:
            blocks.append(Blank())
        blocks.append(Para(
            (f"{marks} " if marks else "", Bold(name), *([button] if button else [])),
            classic=f"{n}. {marks + ' ' if marks else ''}{name}",
        ))
        blocks.append(Para((line2,), glue="\n", classic=f"   {line2}"))
    if not page.rows:
        blocks.append(Para((f"Нет тредов за последние {page.recency_days} дн.",)))
    if nav := _nav_buttons(page):
        blocks.append(Row(nav))
    blocks.append(Row((_archive_toggle(page),)))
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
    """◀️ n/total ▶️: the counter is a disabled in-text button; an arrow only where that page exists."""
    if page.pages <= 1:
        return ()
    a = int(page.archived)
    buttons: list[Button] = []
    if page.page > 0:
        buttons.append(Button("◀️", ActionSpec("page", payload=f"{page.page - 1}:{a}")))
    buttons.append(Button(f"{page.page + 1}/{page.pages}", disabled=True))
    if page.has_next:
        buttons.append(Button("▶️", ActionSpec("page", payload=f"{page.page + 1}:{a}")))
    return tuple(buttons)


def _archive_toggle(page: RegistryPage) -> Button:
    a = int(page.archived)
    return Button("📋 Активные" if page.archived else "📦 Архивные", ActionSpec("page", payload=f"0:{1 - a}"))


def _label(text: str) -> str:
    return clip(text, BUTTON_TEXT_LIMIT)
