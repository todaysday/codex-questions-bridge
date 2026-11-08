"""Plain-text renderings for consultant topics and the questions list."""

from cqb.services.registry import RegistryRow
from cqb.telegram.fmt import when
from cqb.telegram.registry_view import CONSULTANT_MARK, thread_name
from cqb.telegram.status_view import tokens
from cqb.telegram.models import Button, Keyboard

OPENING_QUESTIONS = "opening:questions"
OPENING_STATUS = "opening:status"


def opening_keyboard() -> Keyboard:
    return ((Button("❓ Вопросы", callback_data=OPENING_QUESTIONS),
             Button("📊 Статус", callback_data=OPENING_STATUS)),)


def render_opening(
    row: RegistryRow, source_id: str, usage: tuple[int, int] | None = None, compactions: int | None = None,
) -> str:
    context = f"ctx {tokens(usage[0])}/{tokens(usage[1])} ({round(usage[0] * 100 / usage[1])}%)" if usage else "ctx —"
    compact = f"🗜️{compactions}" if compactions is not None else "🗜️—"
    return (
        f"{context} · {compact}\n"
        f"🤖 {thread_name(row)}\n"
        f"Codex source: {source_id} · thread: …{row.thread.thread_id[-8:]}\n\n"
        "/status — состояние треда, контекст, субагенты, лимиты."
    )


def render_thread(row: RegistryRow, source_id: str, now: int) -> str:
    t = row.thread
    return (
        f"Тред: {thread_name(row)}\nid: {t.thread_id}\nsource: {source_id}\n"
        f"состояние: {t.archive_state.value}\n"
        f"последняя активность: {when(t.updated_at, now)}\n"
        f"консультант: {CONSULTANT_MARK[row.consultant_state]} · вопросов: {row.pending_questions}"
    )
