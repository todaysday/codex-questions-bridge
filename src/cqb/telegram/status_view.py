"""Status card view: renders a StatusReport into Telegram HTML (at most one message of TEXT_LIMIT characters).

Every dynamic string is clipped first and escaped after (an entity is never cut), and only the fixed tags below
are ever written, so the text always parses. When the full card is too long it shrinks in a fixed order: fewer
subagent lines (down to MIN_AGENT_LINES), shorter 'сейчас' fragments, no goal line, and only then a shorter summary.
The only absolute time is the limit reset, labelled UTC.

Header (three lines, then the goal line):
    {state icon} {thread name} · 📁 {project folder, the last path component of the cwd; left out when unknown}
    🧠 {model short}·{effort} · 🕒 {turn duration} · 🔄 {last activity} 💭 {сейчас}
     <b>ctx</b> {used}/{window} ({pct}%) · 💾 {cache pct}%
"""

import html
import re
import time
from dataclasses import dataclass

from cqb.domain.enums import ItemKind, RunState
from cqb.domain.status import (
    AGENT_LINES, NEXT_MAX, PROGRESS_MAX, Activity, AgentLine, GoalSummary, ItemBrief, RateLimits, StatusReport,
    StatusSummary, UsageSnapshot,
)
from cqb.telegram.fmt import TEXT_LIMIT, clip

DASH = "—"
ICON = {
    RunState.WORKING: "🟢", RunState.WAITING_INPUT: "🟡", RunState.WAITING_APPROVAL: "🟡",
    RunState.IDLE: "⚪", RunState.NOT_LOADED: "💤", RunState.ERROR: "🔴",
    RunState.STALLED: "🟠", RunState.UNKNOWN: "❔",
}
WORD = {  # what a waiting subagent or the root's 'сейчас' says; the root's own state is only an icon
    RunState.WORKING: "работает", RunState.WAITING_INPUT: "ждёт ответа", RunState.WAITING_APPROVAL: "ждёт одобрения",
    RunState.IDLE: "простаивает", RunState.NOT_LOADED: "не загружен", RunState.ERROR: "ошибка",
    RunState.UNKNOWN: "неизвестно",
}
NOW_WORD = "now"  # last activity under a minute ago
GOAL_ACTIVE = "active"
GOAL_WORD = {
    "active": "активна", "paused": "пауза", "blocked": "заблокирована", "usageLimited": "лимит использования",
    "budgetLimited": "лимит бюджета", "complete": "выполнена",
}
SUMMARY_UNAVAILABLE = "📝 резюме недоступно"
MIN_AGENT_LINES = 3
TITLE_MAX = 32
FOLDER_MAX = 32  # the project folder on the first line
NOW_MAX = 40  # a subagent's 'сейчас' fragment
ROOT_NOW_MAX = 120  # the service already cut a command head at 80
GOAL_MAX = 60
MODEL_MAX = 24
MODEL_NAME = re.compile(r"^gpt-(\d+(?:\.\d+)?)-(.+)$")  # `gpt-6.1-sol` -> version 6.1, name sol
# (while the item runs, once it has finished): between two steps the newest item of a running turn is a finished one
NOW_COMMAND = ("выполняет", "выполнил")
NOW_FILES = ("правит файлы", "правил файлы")
NOW_TOOL = ("вызывает", "вызывал")
NOW_SPAWN = ("запускает субагента", "запускал субагента")
NOW_COLLAB = ("общается с субагентами", "общался с субагентами")
NOW_WAIT = ("ждёт субагентов", "ждал субагентов")
ACTIVE = (RunState.WORKING, RunState.STALLED, RunState.WAITING_INPUT, RunState.WAITING_APPROVAL)


@dataclass(frozen=True, slots=True)
class Plan:
    """How much of the card is drawn; the shrink steps are successive plans."""

    agent_lines: int = AGENT_LINES
    now_max: int = NOW_MAX
    goal: bool = True
    progress_max: int = PROGRESS_MAX  # characters of the summary parts, before escaping
    next_max: int = NEXT_MAX


PLANS = (
    *(Plan(agent_lines=n) for n in range(AGENT_LINES, MIN_AGENT_LINES - 1, -1)),
    Plan(MIN_AGENT_LINES, 24), Plan(MIN_AGENT_LINES, 12), Plan(MIN_AGENT_LINES, 12, goal=False),
    Plan(MIN_AGENT_LINES, 12, False, 450, 250), Plan(MIN_AGENT_LINES, 12, False, 300, 150),  # the summary goes last
    Plan(0, 0, False, 150, 100),  # unreachable in practice, but sized for the worst escaped card: the guarantee is unconditional
)


def render_status(r: StatusReport) -> str:
    """The first plan whose card fits one message; the last one is returned even if it did not (it always does)."""
    for plan in PLANS:
        text = _card(r, plan)
        if len(text) <= TEXT_LIMIT:
            break
    return text


def _card(r: StatusReport, plan: Plan) -> str:
    top = [_title_line(r), _status_line(r), _context(r.usage)]
    if r.goal and plan.goal:
        top.append(_goal(r.goal))
    blocks = ["\n".join(top), "\n".join(_agents(r, plan)), "\n".join(_summary(r.summary, plan)), _limits(r.limits)]
    return "\n\n".join(blocks)


def _e(text: str, limit: int) -> str:
    return html.escape(clip(text, limit))


def _title_line(r: StatusReport) -> str:
    """`🟢 Fox Profile · 📁 gh-runner-dispatcher`: the folder part only when the working directory is known."""
    folder = folder_name(r.cwd)
    tail = f" · 📁 {_e(folder, FOLDER_MAX)}" if folder else ""
    return f"{ICON[r.state]} {_e(r.title or DASH, 60)}{tail}"


def folder_name(cwd: str | None) -> str | None:
    """The last component of a working directory, never a path: None for nothing usable (unknown, `/`, `.`, `..`)."""
    parts = [p.strip() for p in re.split(r"[/\\]+", cwd or "")]
    name = next((p for p in reversed(parts) if p), None)
    return name if name and name not in (".", "..") else None


def _status_line(r: StatusReport) -> str:
    """`🧠 sol 6.1·low · 🕒 25м · 🔄 2м 💭 выполняет: pytest`; the turn and 'сейчас' parts only when there are any."""
    parts = [f"🧠 {_e(_model(r.model, r.effort) or DASH, MODEL_MAX)}"]
    if r.state in ACTIVE and r.turn_elapsed_s is not None:
        parts.append(f"🕒 {_minutes(r.turn_elapsed_s)}")
    line = " · ".join([*parts, f"🔄 {_ago(r.last_activity, r.now, fresh=NOW_WORD, suffix='')}"])
    now = _now(r.state, r.activity, r.now)
    return line if now == DASH else f"{line} 💭 {_e(now, ROOT_NOW_MAX)}"


def _model(model: str | None, effort: str | None) -> str | None:
    """`gpt-6.1-sol` + `high` -> `sol 6.1·high`, `gpt-6-luna` -> `luna 6`; any other name is kept whole."""
    m = MODEL_NAME.match(model) if model else None
    name = f"{m.group(2)} {m.group(1)}" if m else model
    return "·".join(x for x in (name, effort) if x) or None


def _context(u: UsageSnapshot | None) -> str:
    """` <b>ctx</b> 160k/475k (34%) · 💾 80%`: starts with one space; the cache part is left out when unknown."""
    if u is None:
        return f" <b>ctx</b> {DASH}"
    window = tokens(u.context_window) if u.context_window else DASH
    fill = f" ({u.fill_percent}%)" if u.fill_percent is not None else ""
    cache = f" · 💾 {u.cache_percent}%" if u.cache_percent is not None else ""
    return f" <b>ctx</b> {tokens(u.context_tokens)}/{window}{fill}{cache}"


def _goal(g: GoalSummary) -> str:
    spent = tokens(g.tokens_used) if g.tokens_used is not None else DASH
    budget = f"/{tokens(g.token_budget)}" if g.token_budget is not None else ""
    state = f" · {_e(GOAL_WORD.get(g.status, g.status), 20)}" if g.status and g.status != GOAL_ACTIVE else ""
    return f"🎯 {_e(g.objective or DASH, GOAL_MAX)} · {spent}{budget}{state}"


def _agents(r: StatusReport, plan: Plan) -> list[str]:
    if r.agents_total is None:
        return [f"🤖 <b>Субагенты</b> · {DASH}"]
    if r.agents_total == 0:
        return ["🤖 <b>Субагенты</b> · нет"]
    counts = ((ICON[RunState.WORKING], r.agents_working), ("🟡", r.agents_waiting), (ICON[RunState.IDLE], r.agents_idle))
    total = f"{r.agents_total}{'+' if r.agents_more else ''}"
    lines = [" · ".join([f"🤖 <b>Субагенты {total}</b>", *(f"{icon} {n}" for icon, n in counts if n)])]
    shown = r.agents[: plan.agent_lines]
    lines += [_agent_line(a, r.now, plan.now_max) for a in shown]
    if r.agents_total > len(shown):
        lines.append(f" … и ещё {r.agents_total - len(shown)}")
    return lines


def _agent_line(a: AgentLine, now: int, now_max: int) -> str:
    """One line: icon, title, model, context, and what it does now (or what it waits for, or how long it rests)."""
    parts = [_e(a.label or DASH, TITLE_MAX)]
    if model := _model(a.model, a.effort):
        parts.append(_e(model, MODEL_MAX))
    if a.context_percent is not None:
        parts.append(f"ctx {a.context_percent}%")
    if a.state in (RunState.WORKING, RunState.STALLED):
        fragment = _now(a.state, a.activity, now)
        if fragment != DASH and now_max:
            parts.append(_e(fragment, now_max))
    elif a.state.is_waiting:
        parts.append(WORD[a.state])
    else:
        parts.append(_ago(a.last_activity, now))
    nested = "↳ " if a.depth is not None and a.depth >= 2 else ""
    return f" {nested}{ICON[a.state]} " + " · ".join(parts)


def _summary(s: StatusSummary | None, plan: Plan) -> list[str]:
    """`В процессе`, then (after an empty line) `Дальше` when the reports say what comes next."""
    if s is None:
        return [SUMMARY_UNAVAILABLE]
    lines = ["📝 <b>В процессе:</b> " + html.escape(clip(s.progress, plan.progress_max))]
    if s.next_steps:
        lines += ["", "➡️ <b>Дальше:</b> " + html.escape(clip(s.next_steps, plan.next_max))]
    return lines


def _now(state: RunState, activity: Activity | None, now: int) -> str:
    """What the thread does right now. Built from the kind of its newest item only, never from text."""
    if state.is_waiting:
        return WORD[state]
    if activity is None:
        return DASH
    if not activity.turn_running:
        return f"ход завершён {_ago(activity.completed_at, now)}" if activity.completed_at else DASH
    return _item_phrase(activity.item) if activity.item else DASH


def _item_phrase(item: ItemBrief) -> str:
    done = int(item.finished)
    match item.kind:
        case ItemKind.COMMAND:
            return f"{NOW_COMMAND[done]}: {item.detail}" if item.detail else NOW_COMMAND[done]
        case ItemKind.FILES:
            count = f" ({item.count})" if item.count is not None else ""
            return NOW_FILES[done] + count
        case ItemKind.TOOL:
            return f"{NOW_TOOL[done]} {item.detail}" if item.detail else DASH
        case ItemKind.MESSAGE:
            return "пишет ответ"
        case ItemKind.REASONING:
            return "думает"
        case ItemKind.SPAWN:
            return NOW_SPAWN[done]
        case ItemKind.COLLAB:
            return (NOW_WAIT if item.detail == "wait" else NOW_COLLAB)[done]
        case ItemKind.WEB:
            return "ищет в сети"
    return DASH


def _limits(lim: RateLimits | None) -> str:
    """`📈 лимит 30% нед. · сброс 12.11 09:00 UTC`: the reset is the only absolute time on the card."""
    if lim is None or lim.used_percent is None:
        return f"📈 лимит {DASH}"
    reset = f" · сброс {_utc(lim.resets_at, '%d.%m %H:%M')} UTC" if lim.resets_at else ""
    return f"📈 лимит {lim.used_percent}% {_window(lim.window_minutes)}{reset}"


def _window(minutes: int | None) -> str:
    if not minutes:
        return DASH
    if minutes == 10080:
        return "нед."
    return f"{minutes // 60}ч" if minutes % 60 == 0 else f"{minutes}м"


def _utc(ts: int, fmt: str) -> str:
    return time.strftime(fmt, time.gmtime(ts))


def _minutes(seconds: int | None) -> str:
    if seconds is None:
        return DASH
    h, m = divmod(seconds // 60, 60)
    return f"{h}ч{m:02d}м" if h and m else f"{h}ч" if h else f"{m}м"


def _ago(ts: int | None, now: int, *, fresh: str = "только что", suffix: str = " назад") -> str:
    if not ts:
        return DASH
    secs = max(now - ts, 0)
    if secs < 60:
        return fresh
    if secs < 3600:
        return f"{secs // 60}м{suffix}"
    if secs < 172800:
        return f"{secs // 3600}ч{suffix}"
    return f"{secs // 86400}д{suffix}"


def tokens(n: int) -> str:
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        return f"{n / 1000:.1f}".removesuffix(".0") + "k"
    return f"{n / 1_000_000:.1f}".removesuffix(".0") + "M"
