"""Plain-text renderings for consultant topics and the questions list."""

from cqb.domain.enums import QuestionLifecycle as L
from cqb.domain.labels import question_label
from cqb.domain.models import AgentQuestion, Question
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
        "/status — состояние треда, контекст, субагенты, лимиты.\n"
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


def render_questions(
    questions: list[Question], names: dict[str, str], agent: list[AgentQuestion] | None = None
) -> str:
    """One list for both kinds, in the order of the Q numbers."""
    agent = agent or []
    if not questions and not agent:
        return "Нет неотвеченных вопросов."
    rows: list[tuple[str, int, str]] = []
    for q in questions:
        what = "🔒 секретный ввод: ответьте в Codex" if q.is_secret else clip(
            " / ".join(i.text for i in q.items), 120
        )
        mark = "" if q.lifecycle is not L.CAPTURED else " (без живого соединения)"
        rows.append((q.root_thread_id, q.display_no, _line(q.display_no, names, q.root_thread_id, q.created_at, what + mark)))
    for a in agent:
        rows.append((a.root_thread_id, a.display_no,
                     _line(a.display_no, names, a.root_thread_id, a.created_at, clip(a.title, 120))))
    rows.sort(key=lambda r: r[:2])
    return clip("\n".join(r[2] for r in rows[:LIST_LIMIT]), TEXT_LIMIT)


def _line(number: int, names: dict[str, str], root: str, created_at: int, what: str) -> str:
    return f"❓ {question_label(number)} · {clip(names.get(root, root[:8]), 40)} · {hhmm(created_at)}: {what}"
