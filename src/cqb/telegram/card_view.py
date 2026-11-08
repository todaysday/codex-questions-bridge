"""Question card view (posted in the consultant topic). Secret requests render a notice only: no text, no buttons.

A card never cuts text: it has the full title and the full text of every option, labelled A., B., C. ...
The buttons carry only the short label (`A`, `B`, ...), so Telegram's button limits cut nothing. The view builds a
Doc: as a rich message each option is a paragraph with its button right after the text and the actions are button
rows; as the classic fallback the buttons go to the inline keyboard, and a text longer than one Telegram message is
split into continuation messages by the card poster (telegram/card_post.py)."""

from cqb.domain.enums import QuestionLifecycle as L
from cqb.domain.labels import question_label
from cqb.domain.models import Question, QuestionOption
from cqb.telegram.fmt import clip
from cqb.telegram.models import ActionSpec, Bold, Button, Divider, Doc, Para, Row, View
from cqb.telegram.render import to_view

ANSWER_LIMIT = 1000

SECRET_NOTICE = (
    "🔒 Запрошен секретный ввод. Ответьте в Codex: Bridge не показывает и не принимает секреты."
)
DISCUSS_HINT = "💬 Ответ на это сообщение уйдёт консультанту: так обсуждают вопрос."
ARMED_TEXT = "следующий ответ на это сообщение уйдёт в Codex"
ARMED_HINT = f"✍️ {ARMED_TEXT}"
TEXT_BUTTON = "✍️ Ответить текстом"
CONFIRM_PROMPT = "Подтвердите отправку"
CONFIRM_BUTTON = "✅ Отправить в Codex"
CANCEL_BUTTON = "↩️ Отмена"
TAG_ANSWERED = "✅ отвечено"
TAG_ELSEWHERE = "отвечено в другом месте"
TAG_STALE = "устарел"


def card_header(number: int, thread_name: str, tag: str | None = None) -> Para:
    """`❓ Q3 · <thread name>` on its own line, plus a short state tag only when the question is no longer pending.
    No clock time: Telegram shows the time of the message itself."""
    tail = f" · {clip(thread_name, 80)}" + (f" · {tag}" if tag else "")
    return Para(("❓ ", Bold(question_label(number)), tail), volatile=True)


def render_card(q: Question, thread_name: str, armed: bool = False) -> View:
    """The classic rendering of the card."""
    return to_view(card_doc(q, thread_name, armed))


def card_doc(q: Question, thread_name: str, armed: bool = False) -> Doc:
    header = card_header(q.display_no, thread_name, _tag(q))
    if q.is_secret:
        return Doc((header, Para((SECRET_NOTICE,))))
    blocks: list[Para | Row | Divider] = [header]
    choose = _can_choose(q)
    for n, item in enumerate(q.items, start=1):
        buttons = [option_button(i, "opt", q.root_thread_id, f"{q.question_id}:{i}")
                   for i in range(len(item.options))] if choose else None
        title = f"{item.header}: {item.text}" if item.header else item.text
        blocks.append(Para((f"{n}. {title}" if len(q.items) > 1 else title,)))
        blocks += option_paras([_option_text(o) for o in item.options], buttons)
    closing = q.lifecycle in (L.RESOLVED_OBSERVED, L.EXPIRED_OR_STALE) or q.delivered_to_codex
    blocks += status_paras(_status(q, armed), divided=closing)
    blocks += _answer_rows(q, armed)
    return Doc(tuple(blocks))


def _tag(q: Question) -> str | None:
    if q.lifecycle is L.RESOLVED_OBSERVED:
        return TAG_ANSWERED if q.delivered_to_codex else TAG_ELSEWHERE
    return TAG_STALE if q.lifecycle is L.EXPIRED_OR_STALE else None


def _option_text(o: QuestionOption) -> str:
    return f"{o.label} — {o.description}" if o.description else o.label


def _can_choose(q: Question) -> bool:
    return len(q.items) == 1 and q.lifecycle is L.PENDING_CONFIRMED and not q.delivered_to_codex


def option_paras(texts: list[str], buttons: list[Button] | None) -> list[Para]:
    """One paragraph per option, `A. <full text>`, and its letter button right after the text (a tight list when
    classic). `buttons` is None when nothing can be chosen (anymore)."""
    return [
        Para((f"{letter(i)}. {text}", *([buttons[i]] if buttons else [])), glue="\n")
        for i, text in enumerate(texts)
    ]


def status_paras(text: str, divided: bool = False) -> list[Para | Divider]:
    """The state line; a final one (delivered, sent, stale) is set apart from the question by a divider."""
    if not text:
        return []
    return [Divider(), Para((text,))] if divided else [Para((text,))]


def option_button(index: int, action: str, root: str, payload: str) -> Button:
    """In the text the button says what it does (`Ответить A`); the classic keyboard keeps the short letter."""
    return Button(f"Ответить {letter(index)}", ActionSpec(action, root, payload), style="primary",
                  classic_text=letter(index))


def _status(q: Question, armed: bool) -> str:
    if q.lifecycle is L.RESOLVED_OBSERVED:
        if q.delivered_to_codex:
            return join_lines("✅ Доставлено: Codex закрыл запрос (serverRequest/resolved).", _chosen(q))
        return ""
    if q.lifecycle is L.EXPIRED_OR_STALE:
        return "⌛ Вопрос устарел: Codex больше не ждёт ответа."
    if len(q.items) != 1:
        return "Несколько вопросов в одном запросе: ответьте в Codex."
    if q.delivered_to_codex:
        return join_lines("📤 Ответ отправлен в Codex, ждём подтверждения (serverRequest/resolved).", _chosen(q))
    if q.lifecycle is L.DECISION_STAGED:
        return join_lines(_chosen(q), "Отправить этот ответ в Codex?")
    if q.lifecycle is L.CAPTURED:
        return "Нет живого соединения с запросом Codex; ответ пока невозможен."
    return ARMED_HINT if armed else DISCUSS_HINT


def join_lines(*lines: str) -> str:
    return "\n".join(line for line in lines if line)


def _chosen(q: Question) -> str:
    if not q.staged:
        return ""
    return "Выбрано: " + "; ".join(", ".join(v) for v in q.staged.values())


def _answer_rows(q: Question, armed: bool) -> list[Row]:
    if q.delivered_to_codex or len(q.items) != 1:
        return []
    root = q.root_thread_id
    if q.lifecycle is L.DECISION_STAGED:
        return [confirm_row("send", "cancel", root, q.question_id)]
    if q.lifecycle is not L.PENDING_CONFIRMED or not q.items[0].is_other:
        return []
    return [text_row(armed, "other", root, q.question_id)]


def text_row(armed: bool, other: str, root: str, question_id: str) -> Row:
    """`✍️ Ответить текстом`, or its cancel while the card waits for the typed answer."""
    return Row((Button(CANCEL_BUTTON, ActionSpec("unarm", root, question_id), style="danger") if armed
                else Button(TEXT_BUTTON, ActionSpec(other, root, question_id)),))


def confirm_row(send: str, cancel: str, root: str, question_id: str) -> Row:
    """The stage -> confirm step: send and cancel, under the card and under the confirmation of a typed answer."""
    return Row((
        Button(CONFIRM_BUTTON, ActionSpec(send, root, question_id), style="success"),
        Button(CANCEL_BUTTON, ActionSpec(cancel, root, question_id), style="danger"),
    ))


def confirm_doc(answer: str, send: str, cancel: str, root: str, question_id: str) -> Doc:
    """The message that asks to confirm a typed answer: it shows what will be sent, then the same two buttons as under
    the card. The answer is at most ANSWER_LIMIT characters (both card kinds refuse longer ones before staging)."""
    return Doc((
        Para((f"{CONFIRM_PROMPT}:",)),
        Para((answer.strip(),), glue="\n"),
        confirm_row(send, cancel, root, question_id),
    ))


def letter(idx: int) -> str:
    """A..Z, then plain numbers: the label of an option in the body and on its button."""
    return chr(ord("A") + idx) if idx < 26 else str(idx + 1)
