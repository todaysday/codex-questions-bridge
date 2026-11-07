"""Question card view (posted in the consultant topic). Secret requests render a notice only: no text, no buttons.

A card never cuts text: the body has the full title and the full text of every option, labelled A., B., C. ...
The buttons carry only the short label (`A`, `B`, ...), so Telegram's button limits cut nothing. A body longer
than one Telegram message is split into continuation messages by the card poster (telegram/card_post.py)."""

from cqb.domain.enums import QuestionLifecycle as L
from cqb.domain.labels import question_label
from cqb.domain.models import Question, QuestionItem
from cqb.telegram.fmt import clip, hhmm
from cqb.telegram.models import ActionSpec, Button, View

ANSWER_LIMIT = 1000
OPTIONS_PER_ROW = 5

SECRET_NOTICE = (
    "🔒 Запрошен секретный ввод. Ответьте в Codex: Bridge не показывает и не принимает секреты."
)
DISCUSS_HINT = "💬 Ответ на это сообщение уйдёт консультанту: так обсуждают вопрос."
ARMED_TEXT = "следующий ответ на это сообщение уйдёт в Codex"
ARMED_HINT = f"✍️ {ARMED_TEXT}"
TEXT_BUTTON = "✍️ Ответить текстом"
TAG_ANSWERED = "✅ отвечено"
TAG_ELSEWHERE = "отвечено в другом месте"
TAG_STALE = "устарел"


def card_header(number: int, thread_name: str, created_at: int, tag: str | None = None) -> str:
    """`❓ Q3 · <thread name> · HH:MM`, plus a short state tag only when the question is no longer pending."""
    head = f"❓ {question_label(number)} · {clip(thread_name, 80)} · {hhmm(created_at)}"
    return f"{head} · {tag}" if tag else head


def render_card(q: Question, thread_name: str, armed: bool = False) -> View:
    header = card_header(q.display_no, thread_name, q.created_at, _tag(q))
    if q.is_secret:
        return View(header, SECRET_NOTICE)
    parts = [_item_text(i, n if len(q.items) > 1 else None) for n, i in enumerate(q.items, start=1)]
    parts.append(_status(q, armed))
    return View(header, "\n\n".join(p for p in parts if p), tuple(_answer_rows(q, armed)))


def _tag(q: Question) -> str | None:
    if q.lifecycle is L.RESOLVED_OBSERVED:
        return TAG_ANSWERED if q.delivered_to_codex else TAG_ELSEWHERE
    return TAG_STALE if q.lifecycle is L.EXPIRED_OR_STALE else None


def _item_text(item: QuestionItem, n: int | None) -> str:
    title = f"{item.header}: {item.text}" if item.header else item.text
    lines = [f"{n}. {title}" if n else title]
    for idx, o in enumerate(item.options):
        desc = f" — {o.description}" if o.description else ""
        lines.append(f"{letter(idx)}. {o.label}{desc}")
    return "\n".join(lines)


def _status(q: Question, armed: bool) -> str:
    if q.lifecycle is L.RESOLVED_OBSERVED:
        if q.delivered_to_codex:
            return "✅ Доставлено: Codex закрыл запрос (serverRequest/resolved).\n" + _chosen(q)
        return ""
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
    return ARMED_HINT if armed else DISCUSS_HINT


def _chosen(q: Question) -> str:
    if not q.staged:
        return ""
    return "Выбрано: " + "; ".join(", ".join(v) for v in q.staged.values())


def _answer_rows(q: Question, armed: bool) -> list[tuple[Button, ...]]:
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
    return option_rows(
        [Button(letter(i), ActionSpec("opt", root, f"{q.question_id}:{i}")) for i in range(len(item.options))],
        text_button=(Button("↩️ Отмена", ActionSpec("unarm", root, q.question_id)) if armed
                     else Button(TEXT_BUTTON, ActionSpec("other", root, q.question_id))) if item.is_other else None,
    )


def option_rows(letters: list[Button], text_button: Button | None) -> list[tuple[Button, ...]]:
    """Short letter buttons, a few per row, then the text-answer (or its cancel) button on its own row."""
    rows = [tuple(letters[i:i + OPTIONS_PER_ROW]) for i in range(0, len(letters), OPTIONS_PER_ROW)]
    if text_button is not None:
        rows.append((text_button,))
    return rows


def letter(idx: int) -> str:
    """A..Z, then plain numbers: the label of an option in the body and on its button."""
    return chr(ord("A") + idx) if idx < 26 else str(idx + 1)
