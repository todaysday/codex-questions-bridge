"""Plain-text renderings for consultant topics and the questions list."""

from cqb.domain.enums import QuestionLifecycle as L
from cqb.domain.models import Question
from cqb.services.registry import RegistryRow
from cqb.telegram.fmt import TEXT_LIMIT, clip, hhmm, when
from cqb.telegram.registry_view import CONSULTANT_MARK, thread_name

LIST_LIMIT = 20


def render_opening(row: RegistryRow, source_id: str) -> str:
    return (
        f"Консультант: {thread_name(row)}\n"
        f"Codex source: {source_id} · thread: …{row.thread.thread_id[-8:]}\n\n"
        "/questions — неотвеченные вопросы этого треда.\n"
        "/thread — сведения о привязке.\n"
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


def render_questions(questions: list[Question], names: dict[str, str]) -> str:
    if not questions:
        return "Нет неотвеченных вопросов."
    lines = []
    for q in questions[:LIST_LIMIT]:
        what = "🔒 секретный ввод: ответьте в Codex" if q.is_secret else clip(
            " / ".join(i.text for i in q.items), 120
        )
        mark = "" if q.lifecycle is not L.CAPTURED else " (без живого соединения)"
        lines.append(f"❓ {clip(names.get(q.root_thread_id, q.root_thread_id[:8]), 40)} · "
                     f"{hhmm(q.created_at)}: {what}{mark}")
    return clip("\n".join(lines), TEXT_LIMIT)
