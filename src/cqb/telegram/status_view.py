"""Status card view: renders a StatusReport into Telegram HTML (at most one message of TEXT_LIMIT characters).

Every dynamic string is clipped first and escaped after (an entity is never cut), and only the fixed tags below
are ever written, so the text always parses. When the full card is too long it shrinks in a fixed order: fewer
subagent lines (down to MIN_AGENT_LINES), shorter 'сейчас' fragments, no goal line, and only then a shorter summary.
The limits line says what is LEFT and the reset as a countdown; there is no absolute time on the card. With a configured link template the card ends with one
line that opens the thread (the very last thing the shrink gives up, after the summary).

Header (three lines, then the goal line):
    {state icon} {thread name} · 📁{project folder, the last path component of the cwd; left out when unknown}
    🧠{model short}·{effort} · 🕒{turn duration} · 🔄{last activity} 💭{сейчас}
     <b>ctx</b> {used}/{window} ({pct}%) · 💾{cache pct}%
"""

import html
import re
from dataclasses import dataclass
from urllib.parse import quote

from cqb.domain.enums import ItemKind, RunState
from cqb.domain.status import (
    AGENT_LINES, NEXT_MAX, PROGRESS_MAX, Activity, AgentLine, GoalSummary, ItemBrief, RateLimits, StatusReport,
    StatusSummary, StatusSummaryError, UsageSnapshot,
)
from cqb.telegram.api import strip_html
from cqb.telegram.fmt import TEXT_LIMIT, clip

DASH = "—"
SEP = "<b> · </b>"
LINK_TEXT = "Открыть в Codex"
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
SUMMARY_UNAVAILABLE = "📝резюме недоступно"
MIN_AGENT_LINES = 3
TITLE_MAX = 32
FOLDER_MAX = 32  # the project folder on the first line
NOW_MAX = 40  # a subagent's 'сейчас' fragment
ROOT_NOW_MAX = 120  # the service already cut a command head at 80
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
    agent_cache: bool = True  # the `💾N%` part of a subagent line
    progress_max: int = PROGRESS_MAX  # characters of the summary parts, before escaping
    next_max: int = NEXT_MAX
    link: bool = True  # the open-in-Codex line: the very last thing to go


PLANS = (
    Plan(),
    *(Plan(agent_lines=n, agent_cache=False) for n in range(AGENT_LINES, MIN_AGENT_LINES - 1, -1)),  # cache part first
    Plan(MIN_AGENT_LINES, 24, agent_cache=False), Plan(MIN_AGENT_LINES, 12, agent_cache=False),
    Plan(MIN_AGENT_LINES, 12, False, False),
    Plan(MIN_AGENT_LINES, 12, False, False, 450, 250), Plan(MIN_AGENT_LINES, 12, False, False, 300, 150),  # summary last
    Plan(0, 0, False, False, 150, 100),
    Plan(0, 0, False, False, 150, 100, link=False),  # unreachable in practice, but sized for the worst escaped card: the guarantee is unconditional
)


def render_status(r: StatusReport, open_link: str = "") -> str:
    """The first plan whose card fits one message; the last one is returned even if it did not (it always does).

    `open_link` is the configured URL template (`{thread_id}` is replaced by the root's id): when set, the last line
    of the card opens the thread."""
    for plan in PLANS:
        text = _card(r, plan, open_link)
        if len(strip_html(text)) <= TEXT_LIMIT:
            break
    return text


def _card(r: StatusReport, plan: Plan, open_link: str = "") -> str:
    top = [_title_line(r), _status_line(r), _context(r.usage, r.compactions, r.subagent_tokens, r.subagent_tokens_incomplete)]
    agents = _agents(r, plan)
    if len(agents) > 1:
        agents = [agents[0], "<blockquote expandable>\n" + "\n".join(agents[1:]) + "\n</blockquote>"]
    blocks = ["\n".join(top), "\n".join(agents), "\n".join(_summary(r.summary, plan))]
    text = "\n\n".join(b for b in blocks if b)
    link = _link(r, open_link) if plan.link else None
    text = f"{text}\n{link}" if link else text
    if r.goal and plan.goal:
        available = TEXT_LIMIT - len(strip_html(text)) - 1
        low, high = 0, len(r.goal.objective or DASH)
        while low < high:
            mid = (low + high + 1) // 2
            if len(strip_html(_goal(r.goal, mid))) <= available:
                low = mid
            else:
                high = mid - 1
        if low:
            header = "\n".join(top)
            text = header + "\n" + _goal(r.goal, low) + text[len(header):]
    return text


def _link(r: StatusReport, template: str) -> str | None:
    """`🔗<a href="https://…">Открыть в Codex</a>` on the last line; the text is fixed and the href is
    the template with the root's own id (percent-encoded) and nothing else, HTML-escaped."""
    if not template or not r.thread_id:
        return None
    url = template.replace("{thread_id}", quote(r.thread_id, safe=""))
    return f'🔗<a href="{html.escape(url, quote=True)}">{LINK_TEXT}</a>'


def _e(text: str, limit: int) -> str:
    return html.escape(clip(text, limit))


def _title_line(r: StatusReport) -> str:
    """`🟢 Fox Profile\n📁gh-runner-dispatcher`: the folder part only when the working directory is known."""
    folder = folder_name(r.cwd)
    tail = f"\n📁{_e(folder, FOLDER_MAX)}" if folder else ""
    return f"{ICON[r.state]}{_e(r.title or DASH, 60)}{tail}"


def folder_name(cwd: str | None) -> str | None:
    """The last component of a working directory, never a path: None for nothing usable (unknown, `/`, `.`, `..`)."""
    parts = [p.strip() for p in re.split(r"[/\\]+", cwd or "")]
    name = next((p for p in reversed(parts) if p), None)
    return name if name and name not in (".", "..") else None


def _status_line(r: StatusReport) -> str:
    """`🧠sol 6.1 low · 🕒25м · 🔄2м 💭выполняет: pytest`; the turn and 'сейчас' parts only when there are any."""
    parts = [f"🧠{_e(_model(r.model, r.effort) or DASH, MODEL_MAX)}"]
    if r.state in ACTIVE and r.turn_elapsed_s is not None:
        parts.append(f"🕒{_minutes(r.turn_elapsed_s)}")
    line = SEP.join([*parts, f"🔄{_ago(r.last_activity, r.now, fresh=NOW_WORD, suffix='')}"])
    now = _now(r.state, r.activity, r.now)
    return line if now == DASH else f"{line} 💭{_e(now, ROOT_NOW_MAX)}"


def _model(model: str | None, effort: str | None) -> str | None:
    """`gpt-6.1-sol` + `high` -> `sol 6.1 high`, `gpt-6-luna` -> `luna 6`; any other name is kept whole."""
    m = MODEL_NAME.match(model) if model else None
    name = f"{m.group(2)} {m.group(1)}" if m else model
    return " ".join(x for x in (name, effort) if x) or None


def _cache(permille: int) -> str:
    """`97%`, and from 99% up one decimal (`99.4%`); never rounded up, `100%` only when every token was cached."""
    return "100%" if permille >= 1000 else f"{permille / 10:.1f}%" if permille >= 990 else f"{permille // 10}%"


def _compacted(n: int | None) -> str:
    """` · 🗜️5`: how many times the context was compacted; nothing for zero or unknown."""
    return f"{SEP}🗜️{n}" if n else ""


def _context(u: UsageSnapshot | None, compactions: int | None, subagents: int | None = None,
             incomplete: bool = False) -> str:
    """` <b>ctx</b> 160k/475k(34%) · 💾80% · 🗜️5`: starts with one space; the cache and 🗜️ parts are left out when unknown."""
    if u is None:
        return f" <b>ctx</b> {DASH}{_compacted(compactions)}"
    total = ""
    if u.total_tokens is not None:
        children = f"{'≥' if incomplete else ''}{tokens(subagents)}" if subagents is not None else DASH
        total = f"∑{tokens(u.total_tokens)} ({children}) · "
    window = tokens(u.context_window) if u.context_window else DASH
    fill = f"({u.fill_percent}%)" if u.fill_percent is not None else ""
    cache = f"{SEP}💾{_cache(u.cache_permille)}" if u.cache_permille is not None else ""
    icon = "🌀" if total else ""
    return f" {icon}<b>{total}ctx</b> {tokens(u.context_tokens)}/{window}{fill}{cache}{_compacted(compactions)}"


def _goal(g: GoalSummary, max_chars: int) -> str:
    state = f"{SEP}{_e(GOAL_WORD.get(g.status, g.status), 20)} " if g.status and g.status != GOAL_ACTIVE else ""
    return f"<blockquote expandable>🎯{state}{_e(g.objective or DASH, max_chars)}</blockquote>"


def _agents(r: StatusReport, plan: Plan) -> list[str]:
    if r.agents_total is None:
        return [f"🤖<b>Субагенты</b>{SEP}{DASH}"]
    if r.agents_total == 0:
        return [f"🤖<b>Субагенты</b>{SEP}нет"]
    counts = ((ICON[RunState.WORKING], r.agents_working), ("🟡", r.agents_waiting), (ICON[RunState.IDLE], r.agents_idle))
    total = f"{r.agents_total}{'+' if r.agents_more else ''}"
    lines = [SEP.join([f"🤖<b>Субагенты {total}</b>", *(f"{icon}{n}" for icon, n in counts if n)])]
    shown = r.agents[: plan.agent_lines]
    lines += [_agent_line(a, r.now, plan.now_max, plan.agent_cache) for a in shown]
    if r.agents_total > len(shown):
        lines.append(f" … и ещё {r.agents_total - len(shown)}")
    return lines


def _agent_line(a: AgentLine, now: int, now_max: int, cache: bool = True) -> str:
    """One line: icon, title, model, context and cache hit, compactions, and what it does now (or what it waits
    for, or how long it rests)."""
    parts = [_e(a.label or DASH, TITLE_MAX)]
    if model := _model(a.model, a.effort):
        parts.append(_e(model, MODEL_MAX))
    if a.context_percent is not None:
        parts.append(f"ctx {a.context_percent}%")
    if cache and a.cache_permille is not None:
        parts.append(f"💾{_cache(a.cache_permille)}")
    if a.state in (RunState.WORKING, RunState.STALLED):
        fragment = _now(a.state, a.activity, now)
        if fragment != DASH and now_max:
            parts.append(_e(fragment, now_max))
    elif a.state.is_waiting:
        parts.append(WORD[a.state])
    else:
        parts.append(_ago(a.last_activity, now))
    if a.compactions:
        parts.append(f"🗜️{a.compactions}")
    nested = "↳ " if a.depth is not None and a.depth >= 2 else ""
    return f" {nested}{ICON[a.state]}" + SEP.join(parts)


def _summary(s: StatusSummary | StatusSummaryError | None, plan: Plan) -> list[str]:
    """`В процессе`, then (after an empty line) `Дальше` when the reports say what comes next."""
    if s is None:
        return [SUMMARY_UNAVAILABLE]
    if isinstance(s, StatusSummaryError):
        return ["📝ошибка резюме: " + html.escape(clip(s.reason, plan.progress_max))]
    lines = ["🔛<b>:</b> " + html.escape(clip(s.progress, plan.progress_max))]
    if s.next_steps:
        lines += ["", "🔜<b>:</b> " + html.escape(clip(s.next_steps, plan.next_max))]
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


def limits_line(lim: RateLimits | None, now: int) -> str:
    """`📈 осталось 49% нед. · сброс через 1д 10ч 05м`: what is left, and the reset as a countdown. Empty (no line at
    all) when the usage is unknown; no countdown when the reset is unknown or already past."""
    if lim is None or lim.used_percent is None:
        return ""
    left = min(max(100 - lim.used_percent, 0), 100)
    reset = f" · сброс через {_countdown(lim.resets_at - now)}" if lim.resets_at and lim.resets_at > now else ""
    return f"📈 осталось {left}% {_window(lim.window_minutes)}{reset}"


def _countdown(seconds: int) -> str:
    """`1д 10ч 05м`, `10ч 05м`, `45м` (never below `1м`)."""
    days, rest = divmod(max(seconds, 0) // 60, 1440)
    hours, minutes = divmod(rest, 60)
    if days:
        return f"{days}д {hours}ч {minutes:02d}м"
    return f"{hours}ч {minutes:02d}м" if hours else f"{max(minutes, 1)}м"


def _window(minutes: int | None) -> str:
    if not minutes:
        return DASH
    if minutes == 10080:
        return "нед."
    return f"{minutes // 60}ч" if minutes % 60 == 0 else f"{minutes}м"


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
    if n < 1_000_000_000:
        return f"{n / 1_000_000:.1f}".removesuffix(".0") + "M"
    if n < 1_000_000_000_000:
        return f"{n / 1_000_000_000:.1f}".removesuffix(".0") + "B"
    return f"{n / 1_000_000_000_000:.1f}".removesuffix(".0") + "T"
