"""Thread list view: renders registry DTOs into (text, inline keyboard)."""

from cqb.domain.enums import ConsultantState
from cqb.services.registry import RegistryPage, RegistryRow
from cqb.telegram.fmt import BUTTON_TEXT_LIMIT, TEXT_LIMIT, clip, hhmm, topic_link, when
from cqb.telegram.models import ActionSpec, Button, View

PAGE_SIZE = 5
NAME_LIMIT = 60
CONSULTANT_MARK = {
    None: "—",
    ConsultantState.OPEN: "✅",
    ConsultantState.ARCHIVED: "🔒",
    ConsultantState.ERROR: "⚠️",
    ConsultantState.CREATING: "⏳",
}
FRESH_SECONDS = 86400


def thread_name(row: RegistryRow) -> str:
    t = row.thread
    return clip(" ".join((t.display_name or "").split()) or f"Thread {t.thread_id[:8]}", NAME_LIMIT)


def render_registry(page: RegistryPage, chat_id: int, now: int) -> View:
    title = "📦 Архивные треды Codex" if page.archived else "📋 Активные треды Codex"
    header = f"{title} · обновлено {hhmm(now)}"
    lines: list[str] = []
    keyboard: list[tuple[Button, ...]] = []
    for n, row in enumerate(page.rows, start=1):
        fresh = "🟢" if now - row.thread.updated_at < FRESH_SECONDS else "🟡"
        extra = f" · ❓ {row.pending_questions}" if row.pending_questions else ""
        lines.append(
            f"{n}. {fresh} {thread_name(row)}\n"
            f"   активность {when(row.thread.updated_at, now)} · "
            f"консультант: {CONSULTANT_MARK[row.consultant_state]}{extra}"
        )
        keyboard.append(_row_buttons(n, row, chat_id, page.archived))
    if not page.rows:
        lines.append(f"Нет тредов за последние {page.recency_days} дн.")
    keyboard.append(_nav_buttons(page))
    return View(header, clip("\n\n".join(lines), TEXT_LIMIT - 200), tuple(keyboard))


def render_info(row: RegistryRow, now: int) -> str:
    """Short text for an alert (Telegram limits it to 200 characters)."""
    t = row.thread
    state = CONSULTANT_MARK[row.consultant_state]
    text = (
        f"{thread_name(row)}\nid …{t.thread_id[-8:]} · {t.archive_state.value}\n"
        f"активность {when(t.updated_at, now)} · консультант {state} · вопросов {row.pending_questions}"
    )
    return clip(text, 200)


def _row_buttons(n: int, row: RegistryRow, chat_id: int, archived: bool) -> tuple[Button, ...]:
    buttons: list[Button] = []
    link = topic_link(chat_id, row.topic_id) if row.topic_id else None
    if row.consultant_state in (ConsultantState.OPEN, ConsultantState.ARCHIVED) and link:
        buttons.append(Button(_label(f"{n}. 💬 Открыть"), url=link))
    elif not archived and row.consultant_state not in (ConsultantState.OPEN, ConsultantState.ARCHIVED):
        buttons.append(Button(_label(f"{n}. ➕ Создать консультанта"), ActionSpec("create", row.thread.thread_id)))
    buttons.append(Button(_label(f"{n}. ℹ️ Подробнее"), ActionSpec("info", row.thread.thread_id)))
    return tuple(buttons)


def _nav_buttons(page: RegistryPage) -> tuple[Button, ...]:
    a = int(page.archived)
    buttons: list[Button] = []
    if page.page > 0:
        buttons.append(Button("◀️", ActionSpec("page", payload=f"{page.page - 1}:{a}")))
    buttons.append(Button("🔄 Обновить", ActionSpec("refresh")))
    if page.has_next:
        buttons.append(Button("▶️", ActionSpec("page", payload=f"{page.page + 1}:{a}")))
    other = "📋 Активные" if page.archived else "📦 Архивные"
    buttons.append(Button(other, ActionSpec("page", payload=f"0:{1 - a}")))
    return tuple(buttons)


def _label(text: str) -> str:
    return clip(text, BUTTON_TEXT_LIMIT)
