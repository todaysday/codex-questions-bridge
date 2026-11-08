"""Plain-text renderings for consultant topics and the questions list."""

from cqb.services.registry import RegistryRow
from cqb.telegram.fmt import when
from cqb.telegram.registry_view import CONSULTANT_MARK, thread_name

THREAD_COMMAND_ENABLED = False  # /thread: disabled by owner request, may return later


def _commands() -> str:
    lines = ["/questions — показать неотвеченные вопросы этого треда карточками заново."]
    if THREAD_COMMAND_ENABLED:
        lines.append("/thread — сведения о привязке.")
    lines.append("/status — состояние треда, контекст, субагенты, лимиты.")
    return "\n".join(lines)


def render_opening(row: RegistryRow, source_id: str) -> str:
    return (
        f"Консультант: {thread_name(row)}\n"
        f"Codex source: {source_id} · thread: …{row.thread.thread_id[-8:]}\n\n"
        f"{_commands()}\n"
        "Диалог с моделью пока не подключён."
    )


def render_thread(row: RegistryRow, source_id: str, now: int) -> str:
    t = row.thread
    return (
        f"Тред: {thread_name(row)}\nid: {t.thread_id}\nsource: {source_id}\n"
        f"состояние: {t.archive_state.value}\n"
        f"последняя активность: {when(t.updated_at, now)}\n"
        f"консультант: {CONSULTANT_MARK[row.consultant_state]} · вопросов: {row.pending_questions}"
    )
