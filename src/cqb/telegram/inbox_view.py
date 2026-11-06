"""Inbox card view. Secret requests render a notice only: no text, no buttons."""

from cqb.domain.enums import ConsultantState, QuestionLifecycle as L
from cqb.domain.models import Question, QuestionItem
from cqb.services.registry import RegistryRow
from cqb.telegram.fmt import BUTTON_TEXT_LIMIT, TEXT_LIMIT, clip, hhmm, topic_link
from cqb.telegram.models import ActionSpec, Button, Keyboard, View

QUESTION_LIMIT = 1500
OPTION_LABEL_LIMIT = 100
OPTION_DESC_LIMIT = 200
ANSWER_LIMIT = 1000
MAX_OPTIONS = 10

SECRET_NOTICE = (
    "🔒 Запрошен секретный ввод. Ответьте в Codex: Bridge не показывает и не принимает секреты."
)
OTHER_HINT = "Или ответьте на это сообщение своим текстом."


def render_card(q: Question, thread_name: str, consultant: RegistryRow | None, chat_id: int) -> View:
    header = f"❓ {clip(thread_name, 80)} · {hhmm(q.created_at)}"
    if q.is_secret:
        return View(header, SECRET_NOTICE)
    parts = [f"request_user_input · {q.question_id} · {q.lifecycle.value}"]
    parts += [_item_text(i, n if len(q.items) > 1 else None) for n, i in enumerate(q.items, start=1)]
    parts.append(_status(q))
    keyboard = [*_answer_rows(q), *_consultant_row(q, consultant, chat_id)]
    return View(header, clip("\n\n".join(p for p in parts if p), TEXT_LIMIT - 200), tuple(keyboard))


def _item_text(item: QuestionItem, n: int | None) -> str:
    lines = []
    title = clip(f"{item.header}: {item.text}" if item.header else item.text, QUESTION_LIMIT)
    lines.append(f"{n}. {title}" if n else title)
    for idx, o in enumerate(item.options[:MAX_OPTIONS]):
        desc = f" — {clip(o.description, OPTION_DESC_LIMIT)}" if o.description else ""
        lines.append(f"{_letter(idx)}. {clip(o.label, OPTION_LABEL_LIMIT)}{desc}")
    return "\n".join(lines)


def _status(q: Question) -> str:
    if q.lifecycle is L.RESOLVED_OBSERVED:
        if q.delivered_to_codex:
            return "✅ Доставлено: Codex закрыл запрос (serverRequest/resolved).\n" + _chosen(q)
        return "🔕 отвечено в другом месте"
    if q.lifecycle is L.EXPIRED_OR_STALE:
        return "⌛ Вопрос устарел: Codex больше не ждёт ответа."
    if len(q.items) != 1:
        return "Несколько вопросов в одном запросе: ответьте в Codex."
    if q.delivered_to_codex:
        return "📤 Ответ отправлен в Codex, ждём подтверждения (serverRequest/resolved).\n" + _chosen(q)
    if q.lifecycle is L.DECISION_STAGED:
        return f"{_chosen(q)}\nОтправить этот ответ в Codex?"
    if q.lifecycle is L.CAPTURED:
        return "Нет живого соединения с запросом Codex; ответ пока невозможен."
    return OTHER_HINT if q.items[0].is_other else ""


def _chosen(q: Question) -> str:
    if not q.staged:
        return ""
    return "Выбрано: " + "; ".join(clip(", ".join(v), ANSWER_LIMIT) for v in q.staged.values())


def _answer_rows(q: Question) -> list[tuple[Button, ...]]:
    if q.delivered_to_codex or len(q.items) != 1:
        return []
    root = q.root_thread_id
    if q.lifecycle is L.DECISION_STAGED:
        return [(
            Button("✅ Отправить в Codex", ActionSpec("send", root, q.question_id)),
            Button("↩️ Отмена", ActionSpec("cancel", root, q.question_id)),
        )]
    if q.lifecycle is not L.PENDING_CONFIRMED:
        return []
    item = q.items[0]
    rows = [
        (Button(clip(f"{_letter(i)}. {o.label}", BUTTON_TEXT_LIMIT),
                ActionSpec("opt", root, f"{q.question_id}:{i}")),)
        for i, o in enumerate(item.options[:MAX_OPTIONS])
    ]
    if item.is_other:
        rows.append((Button("✍️ Другое", ActionSpec("other", root, q.question_id)),))
    return rows


def _consultant_row(q: Question, row: RegistryRow | None, chat_id: int) -> Keyboard:
    if q.lifecycle in (L.RESOLVED_OBSERVED, L.EXPIRED_OR_STALE):
        return ()
    if row is not None and row.consultant_state in (ConsultantState.OPEN, ConsultantState.ARCHIVED):
        link = topic_link(chat_id, row.topic_id) if row.topic_id else None
        return ((Button("💬 Открыть консультанта", url=link),),) if link else ()
    return ((Button("➕ Создать консультанта", ActionSpec("create", q.root_thread_id)),),)


def _letter(idx: int) -> str:
    return chr(ord("A") + idx)
