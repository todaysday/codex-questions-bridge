"""Card of ONE agent-message question (posted in the consultant topic): the full title, the full text of every
option labelled A., B., C. ..., and short letter buttons; stage, confirm, send. No text is ever cut."""

from cqb.domain.enums import AgentQuestionState as S
from cqb.domain.models import AgentQuestion
from cqb.telegram.card_view import (
    ARMED_HINT, CANCEL_BUTTON, DISCUSS_HINT, TAG_ANSWERED, TAG_ELSEWHERE, TAG_STALE, TEXT_BUTTON, card_header,
    confirm_keyboard, letter, option_rows,
)
from cqb.telegram.models import ActionSpec, Button, View


def render_agent_card(q: AgentQuestion, thread_name: str, armed: bool = False) -> View:
    header = card_header(q.display_no, thread_name, q.created_at, _tag(q))
    parts = [_text(q), _status(q, armed)]
    return View(header, "\n\n".join(p for p in parts if p), tuple(_rows(q, armed)))


def _tag(q: AgentQuestion) -> str | None:
    if q.state is S.ANSWERED:
        return TAG_ANSWERED if q.sent_at is not None else TAG_ELSEWHERE
    return TAG_STALE if q.state is S.STALE else None


def _text(q: AgentQuestion) -> str:
    return "\n".join([q.title, *(f"{letter(i)}. {o}" for i, o in enumerate(q.options))])


def _status(q: AgentQuestion, armed: bool) -> str:
    if q.state is S.ANSWERED:
        return ("✅ Доставлено: ответ появился в треде.\n" + _chosen(q)) if q.sent_at is not None else ""
    if q.state is S.STALE:
        return "⌛ Вопрос устарел: тред архивирован или недоступен."
    if not q.answerable:
        return "Вопрос слишком длинный для Telegram: ответьте в Codex."
    if q.sent_at is not None:
        return "📤 Ответ отправлен в Codex, ждём подтверждения (сообщение пользователя в треде).\n" + _chosen(q)
    if q.staged is not None:
        return f"{_chosen(q)}\nОтправить этот ответ в Codex?"
    return ARMED_HINT if armed else DISCUSS_HINT


def _chosen(q: AgentQuestion) -> str:
    return f"Выбрано: {q.staged}" if q.staged is not None else ""


def _rows(q: AgentQuestion, armed: bool) -> list[tuple[Button, ...]]:
    if not q.is_open or not q.answerable or q.sent_at is not None:
        return []
    root, qid = q.root_thread_id, q.agent_question_id
    if q.staged is not None:
        return list(confirm_keyboard("asend", "acancel", root, qid))
    return option_rows(
        [Button(letter(i), ActionSpec("aopt", root, f"{qid}:{i}")) for i in range(len(q.options))],
        text_button=Button(CANCEL_BUTTON, ActionSpec("unarm", root, qid)) if armed
        else Button(TEXT_BUTTON, ActionSpec("aother", root, qid)),
    )
