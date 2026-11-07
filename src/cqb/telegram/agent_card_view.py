"""Card of an agent-message question (posted in the consultant topic). One card per agentMessage with
questions: every title with lettered options as buttons, a `Другое` path per question, one confirmation."""

from cqb.domain.enums import AgentQuestionState as S
from cqb.domain.models import AgentQuestion, AgentQuestionItem
from cqb.telegram.card_view import ANSWER_LIMIT, OPTION_LABEL_LIMIT, QUESTION_LIMIT
from cqb.telegram.fmt import BUTTON_TEXT_LIMIT, TEXT_LIMIT, clip, hhmm
from cqb.telegram.models import ActionSpec, Button, View

OTHER_SINGLE = "Или ответьте на это сообщение своим текстом."
OTHER_MULTI = "Свой вариант: ответьте на это сообщение так: «2: ваш текст»."


def render_agent_card(q: AgentQuestion, thread_name: str) -> View:
    header = f"❓ {clip(thread_name, 80)} · {hhmm(q.created_at)}"
    multi = len(q.items) > 1
    parts = [f"agent question · {q.agent_question_id} · {q.state.value}"]
    parts += [_item_text(i, n if multi else None) for n, i in enumerate(q.items, start=1)]
    parts.append(_status(q, multi))
    return View(header, clip("\n\n".join(p for p in parts if p), TEXT_LIMIT - 200), tuple(_rows(q, multi)))


def _item_text(item: AgentQuestionItem, n: int | None) -> str:
    title = clip(item.title, QUESTION_LIMIT)
    lines = [f"{n}. {title}" if n else title]
    lines += [f"{_letter(i)}. {clip(o, OPTION_LABEL_LIMIT)}" for i, o in enumerate(item.options)]
    return "\n".join(lines)


def _status(q: AgentQuestion, multi: bool) -> str:
    if q.state is S.ANSWERED:
        if q.sent_at is not None:
            return "✅ Доставлено: ответ появился в треде.\n" + _chosen(q)
        return "🔕 отвечено в другом месте"
    if q.state is S.STALE:
        return "⌛ Вопрос устарел: тред архивирован или недоступен."
    if not q.answerable:
        return "Вопрос слишком длинный для Telegram: ответьте в Codex."
    if q.sent_at is not None:
        return "📤 Ответ отправлен в Codex, ждём подтверждения (сообщение пользователя в треде).\n" + _chosen(q)
    if len(q.staged) == len(q.items):
        return f"{_chosen(q)}\nОтправить этот ответ в Codex?"
    left = ", ".join(str(n) for n in range(1, len(q.items) + 1) if n - 1 not in q.staged)
    prefix = f"{_chosen(q)}\nОсталось ответить: {left}\n" if q.staged else ""
    return prefix + (OTHER_MULTI if multi else OTHER_SINGLE)


def _chosen(q: AgentQuestion) -> str:
    if not q.staged:
        return ""
    multi = len(q.items) > 1
    return "Выбрано: " + "; ".join(
        clip(f"{i + 1}) {a}" if multi else a, ANSWER_LIMIT) for i, a in sorted(q.staged.items())
    )


def _rows(q: AgentQuestion, multi: bool) -> list[tuple[Button, ...]]:
    if not q.is_open or not q.answerable or q.sent_at is not None:
        return []
    root, qid = q.root_thread_id, q.agent_question_id
    if len(q.staged) == len(q.items):
        return [(
            Button("✅ Отправить в Codex", ActionSpec("asend", root, qid)),
            Button("↩️ Отмена", ActionSpec("acancel", root, qid)),
        )]
    rows: list[tuple[Button, ...]] = []
    for n, item in enumerate(q.items):
        if n in q.staged:
            continue
        tag = f"{n + 1}" if multi else ""
        rows += [
            (Button(clip(f"{tag}{_letter(i)}. {o}", BUTTON_TEXT_LIMIT), ActionSpec("aopt", root, f"{qid}:{n}:{i}")),)
            for i, o in enumerate(item.options)
        ]
        rows.append((Button(f"✍️ Другое{f' {n + 1}' if multi else ''}", ActionSpec("aother", root, f"{qid}:{n}")),))
    if q.staged:
        rows.append((Button("↩️ Отмена", ActionSpec("acancel", root, qid)),))
    return rows


def _letter(idx: int) -> str:
    return chr(ord("A") + idx)
