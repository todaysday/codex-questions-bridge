"""Card of ONE agent-message question (posted in the consultant topic): the full title, the full text of every
option labelled A., B., C. ..., and short letter buttons; stage, confirm, send. No text is ever cut."""

from cqb.domain.enums import AgentQuestionState as S
from cqb.domain.models import AgentQuestion
from cqb.telegram.card_view import (
    ARMED_HINT, DISCUSS_HINT, TAG_ANSWERED, TAG_ELSEWHERE, TAG_STALE, card_header, confirm_row, join_lines, option_button,
    option_paras, status_paras, text_row,
)
from cqb.telegram.models import Blank, Divider, Doc, Para, Row, View
from cqb.telegram.render import to_view


def render_agent_card(q: AgentQuestion, thread_name: str, armed: bool = False) -> View:
    """The classic rendering of the card."""
    return to_view(agent_card_doc(q, thread_name, armed))


def agent_card_doc(q: AgentQuestion, thread_name: str, armed: bool = False) -> Doc:
    header = card_header(q.display_no, thread_name, _tag(q))
    root, qid = q.root_thread_id, q.agent_question_id
    can_choose = q.is_open and q.answerable and q.sent_at is None and q.staged is None
    buttons = [option_button(i, "aopt", root, f"{qid}:{i}") for i in range(len(q.options))] if can_choose else None
    closing = q.state in (S.ANSWERED, S.STALE) or q.sent_at is not None
    blocks: list[Para | Row | Divider | Blank] = [
        header, Blank(), Para((q.title,)), *option_paras(list(q.options), buttons),
        *status_paras(_status(q, armed), divided=closing),
    ]
    if can_choose:
        blocks.append(text_row(armed, "aother", root, qid))
    elif q.is_open and q.answerable and q.sent_at is None:  # staged: send or cancel
        blocks.append(confirm_row("asend", "acancel", root, qid))
    return Doc(tuple(blocks))


def _tag(q: AgentQuestion) -> str | None:
    if q.state is S.ANSWERED:
        return TAG_ANSWERED if q.sent_at is not None else TAG_ELSEWHERE
    return TAG_STALE if q.state is S.STALE else None


def _status(q: AgentQuestion, armed: bool) -> str:
    if q.state is S.ANSWERED:
        return join_lines("✅ Доставлено: ответ появился в треде.", _chosen(q)) if q.sent_at is not None else ""
    if q.state is S.STALE:
        return "⌛ Вопрос устарел: тред архивирован или недоступен."
    if not q.answerable:
        return "Вопрос слишком длинный для Telegram: ответьте в Codex."
    if q.sent_at is not None:
        return join_lines("📤 Ответ отправлен в Codex, ждём подтверждения (сообщение пользователя в треде).", _chosen(q))
    if q.staged is not None:
        return f"{_chosen(q)}\nОтправить этот ответ в Codex?"
    return ARMED_HINT if armed else DISCUSS_HINT


def _chosen(q: AgentQuestion) -> str:
    return f"Выбрано: {q.staged}" if q.staged is not None else ""
