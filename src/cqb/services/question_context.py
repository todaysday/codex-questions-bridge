"""Context lines Bridge puts in front of the owner's text for the consultant (design sections 2.3 and 4.1).

Pure and deterministic: the same questions always give the same line, bounded in size, with the question text
redacted like everything else that goes to a model. A secret question contributes no text at all.

* `discussion_prefix`: the owner replied to the card of one question.
* `context_line`: every other message, when the thread has pending questions (the focus first)."""

from dataclasses import dataclass

from cqb.domain.enums import OPEN_LIFECYCLES
from cqb.domain.labels import question_label
from cqb.domain.models import AgentQuestion, Question
from cqb.security import redact

TITLE_MAX = 200
OPTIONS_MAX = 6
OPTION_MAX = 60
ANSWERED = "(already answered)"
SECRET = "(secret input, details are not shown)"

PENDING_OTHERS = 3
PENDING_OTHER_TITLE_MAX = 60
PENDING_TITLE_MAX = 120
PENDING_OPTIONS_MAX = 4
PENDING_LINE_MAX = 600
PENDING_SECRET = "(secret input, not shown)"


@dataclass(frozen=True, slots=True)
class _Shown:
    label: str
    secret: bool
    open: bool
    title: str  # several items (request_user_input only): `1. first / 2. second`, otherwise the one title
    options: tuple[str, ...]  # only for a question with a single item


def _shown(q: Question | AgentQuestion) -> _Shown:
    if isinstance(q, Question):
        titles = [f"{i.header}: {i.text}" if i.header else i.text for i in q.items]
        options = tuple(o.label for o in q.items[0].options) if len(q.items) == 1 else ()
        secret, open_ = q.is_secret, q.lifecycle in OPEN_LIFECYCLES
    else:
        titles = [q.title]
        options = q.options
        secret, open_ = False, q.is_open
    if secret:
        titles, options = [], ()  # whatever was stored, a secret question never leaves
    if len(titles) > 1:  # several items: titles only, the consultant reads the options with get_questions
        title = " / ".join(f"{n}. {t}" for n, t in enumerate(titles, start=1))
    else:
        title = titles[0] if titles else ""
    return _Shown(question_label(q.display_no), secret, open_, title, options)


def discussion_prefix(q: Question | AgentQuestion) -> str:
    """`[Context: the user replies about question Q3 (<title>; options: ...)] ` (with a trailing space)."""
    s = _shown(q)
    head = f"[Context: the user replies about question {s.label}"
    if s.secret:
        return f"{head} {SECRET}] "
    body = _cut(s.title, TITLE_MAX) or "no text"
    if s.options:
        body += "; options: " + ", ".join(_cut(o, OPTION_MAX) for o in s.options[:OPTIONS_MAX])
    return f"{head} {ANSWERED + ' ' if not s.open else ''}({body})] "


def pick_focus(
    questions: list[Question | AgentQuestion], focus_no: int | None
) -> tuple[Question | AgentQuestion, bool, list[Question | AgentQuestion]] | None:
    """(focus, explicit, other pending newest first) among the PENDING questions of one thread; None when nothing
    is pending. The stored focus counts while its question is still pending (it moves on when it resolves). Else the
    only pending question is the focus, and with several it is the newest one, which is `assumed` (not explicit)."""
    pending = sorted((q for q in questions if _shown(q).open), key=lambda q: -q.display_no)
    if not pending:
        return None
    chosen = next((q for q in pending if q.display_no == focus_no), None)
    explicit = chosen is not None or len(pending) == 1  # a single question cannot be mistaken for another
    chosen = chosen or pending[0]
    return chosen, explicit, [q for q in pending if q is not chosen]


def context_line(questions: list[Question | AgentQuestion], focus_no: int | None = None) -> str:
    """`[Context: focus Q3 "<title>" (options: ...; explicit); other pending: Q4 "<title>", Q5 "..."]`.

    At most 600 characters: the focus is shortened to its title if needed, others are added while they fit
    (at most 3). A secret question appears only as `Q5 (secret input, not shown)`. Empty when nothing is pending."""
    picked = pick_focus(questions, focus_no)
    if picked is None:
        return ""
    chosen, explicit, others = picked
    mode = "explicit" if explicit else "assumed"
    head, tail = "[Context: ", "]"
    focus = _focus_entry(_shown(chosen), mode, with_options=True)
    if len(head) + len(focus) + len(tail) > PENDING_LINE_MAX:
        focus = _focus_entry(_shown(chosen), mode, with_options=False)
    line = head + focus
    entries: list[str] = []
    label = "; other pending: "
    for q in others[:PENDING_OTHERS]:
        entry = _entry(_shown(q), PENDING_OTHER_TITLE_MAX)
        trial = line + label + ", ".join([*entries, entry])
        if len(trial) + len(tail) > PENDING_LINE_MAX:
            break
        entries.append(entry)
    return (line + label + ", ".join(entries) if entries else line) + tail


def _entry(s: _Shown, title_max: int) -> str:
    if s.secret:
        return f"{s.label} {PENDING_SECRET}"
    return f'{s.label} "{_cut(s.title, title_max).replace(chr(34), chr(39))}"'


def _focus_entry(s: _Shown, mode: str, *, with_options: bool) -> str:
    if s.secret:
        return f"focus {s.label} (secret input, not shown; {mode})"
    notes = []
    if with_options and s.options:
        notes.append("options: " + ", ".join(_cut(o, OPTION_MAX).replace('"', "'") for o in s.options[:PENDING_OPTIONS_MAX]))
    notes.append(mode)
    title = _cut(s.title, PENDING_TITLE_MAX if with_options else PENDING_TITLE_MAX // 2).replace('"', "'")
    return f'focus {s.label} "{title}" ({"; ".join(notes)})'


def _cut(text: str, limit: int) -> str:
    clean = " ".join(redact(text)[0].split()).replace("[", "(").replace("]", ")")
    return clean if len(clean) <= limit else clean[: limit - 1] + "…"
