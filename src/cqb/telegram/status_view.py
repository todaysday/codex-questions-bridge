"""Status card view: renders a StatusReport into plain text (at most one Telegram message)."""

import time

from cqb.domain.enums import RunState
from cqb.domain.status import AgentLine, GoalSummary, RateLimits, StatusReport, UsageSnapshot
from cqb.telegram.fmt import TEXT_LIMIT, clip, hhmm

DASH = "—"
ICON = {
    RunState.WORKING: "🟢", RunState.WAITING_INPUT: "🟡", RunState.WAITING_APPROVAL: "🟡",
    RunState.IDLE: "⚪", RunState.NOT_LOADED: "💤", RunState.ERROR: "🔴",
    RunState.STALLED: "🟠", RunState.UNKNOWN: "❔",
}
WORD = {
    RunState.WORKING: "работает", RunState.WAITING_INPUT: "ждёт ответа", RunState.WAITING_APPROVAL: "ждёт одобрения",
    RunState.IDLE: "простаивает", RunState.NOT_LOADED: "не загружен", RunState.ERROR: "ошибка",
    RunState.UNKNOWN: "неизвестно",
}
GOAL_WORD = {
    "active": "активна", "paused": "пауза", "blocked": "заблокирована", "usageLimited": "лимит использования",
    "budgetLimited": "лимит бюджета", "complete": "выполнена",
}
ACTIVE = (RunState.WORKING, RunState.STALLED, RunState.WAITING_INPUT, RunState.WAITING_APPROVAL)


def render_status(r: StatusReport) -> str:
    lines = [f"📊 Статус: {clip(r.title, 60) if r.title else DASH}", _root_line(r)]
    lines.append(f"Контекст: {_context(r.usage)}")
    lines.append(f"Активность: {_ago(r.last_activity, r.now)}")
    if r.goal:
        lines.append(_goal(r.goal))
    lines.append(_agents_header(r))
    lines.extend(_agent_line(a, r.now) for a in r.agents)
    if r.agents_total is not None and r.agents_total > len(r.agents):
        lines.append(f"+{r.agents_total - len(r.agents)} ещё" + ("+" if r.agents_more else ""))
    if r.narrative:
        lines.append(clip(r.narrative, 300))
    lines.append(f"Лимит: {_limits(r.limits)}")
    lines.append(f"снимок {hhmm(r.now)}")
    return clip("\n".join(lines), TEXT_LIMIT)


def _root_line(r: StatusReport) -> str:
    word = f"тихо {r.quiet_minutes} мин" if r.state is RunState.STALLED else WORD[r.state]
    parts = [f"{ICON[r.state]} {word}", r.model or DASH, f"effort {r.effort or DASH}"]
    if r.state in ACTIVE:
        parts.append(f"ход {_minutes(r.turn_elapsed_s)}")
    return " · ".join(parts)


def _context(u: UsageSnapshot | None) -> str:
    if u is None:
        return f"{DASH} · кэш {DASH}"
    window = tokens(u.context_window) if u.context_window else DASH
    fill = f" ({u.fill_percent}%)" if u.fill_percent is not None else ""
    cache = f"{u.cache_percent}%" if u.cache_percent is not None else DASH
    return f"{tokens(u.context_tokens)} / {window}{fill} · кэш {cache}"


def _goal(g: GoalSummary) -> str:
    spent = tokens(g.tokens_used) if g.tokens_used is not None else DASH
    budget = f" / {tokens(g.token_budget)}" if g.token_budget is not None else ""
    return f"Цель: {g.objective or DASH} · {GOAL_WORD.get(g.status, g.status or DASH)} · {spent}{budget} ток."


def _agents_header(r: StatusReport) -> str:
    if r.agents_total is None:
        return f"Субагенты: {DASH}"
    if r.agents_total == 0:
        return "Субагенты: нет"
    plus = "+" if r.agents_more else ""
    return (f"Субагенты: {r.agents_total}{plus} · 🟢 {r.agents_working} работают · "
            f"🟡 {r.agents_waiting} ждут · ⚪ {r.agents_idle} простаивают")


def _agent_line(a: AgentLine, now: int) -> str:
    ctx = f"{a.context_percent}%" if a.context_percent is not None else DASH
    return (f" • {clip(a.label, 24)} · {a.model or DASH} {a.effort or DASH} · {ICON[a.state]} · "
            f"ctx {ctx} · {_ago(a.last_activity, now)}")


def _limits(lim: RateLimits | None) -> str:
    if lim is None or lim.used_percent is None:
        return DASH
    reset = f" · сброс {_reset(lim.resets_at)}" if lim.resets_at else ""
    return f"{lim.used_percent}% ({_window(lim.window_minutes)}){reset}"


def _window(minutes: int | None) -> str:
    if not minutes:
        return DASH
    if minutes == 10080:
        return "неделя"
    return f"{minutes // 60} ч" if minutes % 60 == 0 else f"{minutes} мин"


def _reset(ts: int) -> str:
    same_day = time.localtime(ts)[:3] == time.localtime()[:3]
    return time.strftime("%H:%M" if same_day else "%d.%m %H:%M", time.localtime(ts))


def _minutes(seconds: int | None) -> str:
    return f"{seconds // 60} мин" if seconds is not None else DASH


def _ago(ts: int | None, now: int) -> str:
    if not ts:
        return DASH
    secs = max(now - ts, 0)
    if secs < 60:
        return "только что"
    if secs < 3600:
        return f"{secs // 60} мин назад"
    if secs < 172800:
        return f"{secs // 3600} ч назад"
    return f"{secs // 86400} дн назад"


def tokens(n: int) -> str:
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        return f"{n / 1000:.1f}".removesuffix(".0") + "k"
    return f"{n / 1_000_000:.1f}".removesuffix(".0") + "M"
