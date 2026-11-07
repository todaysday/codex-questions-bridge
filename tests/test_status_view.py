"""The status card layout (Telegram HTML): snapshot texts, escaping, the 4096-character shrink order, model names."""

import dataclasses
import re

import pytest

from cqb.domain.enums import ItemKind as K, RunState as R
from cqb.domain.status import (
    Activity, AgentLine, GoalSummary, ItemBrief, RateLimits, StatusReport, StatusSummary, UsageSnapshot,
)
from cqb.telegram.api import strip_html
from cqb.telegram.fmt import TEXT_LIMIT
from cqb.telegram import status_view as view
from cqb.telegram.status_view import MIN_AGENT_LINES, render_status
from fake_telegram import check_html

NOW = 1_800_000_000  # 2027-01-15 08:00:00 UTC
RESET = NOW + 5 * 86400 + 3600  # 20.01 09:00 UTC
TURN = Activity(True, NOW - 1500, None, ItemBrief(K.COMMAND, False, "pytest -q"))
ENGLISH = ("working", "idle", "completed", "waiting", "stalled", "running")


def agent(label="Ada", state=R.WORKING, model="gpt-6-luna", effort="high", ctx=40, age=30, now=None, depth=1):
    activity = Activity(True, NOW - 60, None, ItemBrief(K.REASONING, False)) if now is None and state is R.WORKING else now
    return AgentLine(label, model, effort, state, ctx, NOW - age, activity, depth)


def report(**kw):
    base = dict(
        now=NOW, title="Fox Profile", state=R.WORKING, quiet_minutes=None, model="gpt-6-sol", effort="low",
        turn_elapsed_s=1500, usage=UsageSnapshot(160_000, 475_000, 100, 80), last_activity=NOW - 120,
        goal=GoalSummary("Ship the profile page", "active", 1_250_000, None), agents_total=0, agents_more=False,
        agents_working=0, agents_waiting=0, agents_idle=0, agents=(), limits=RateLimits(41, RESET, 10080),
        activity=TURN, summary=None)
    base.update(kw)
    return StatusReport(**base)


def with_agents(agents, total=None, **kw):
    return report(
        agents=tuple(agents), agents_total=total if total is not None else len(agents),
        agents_working=sum(a.state is R.WORKING for a in agents), agents_waiting=sum(a.state.is_waiting for a in agents),
        agents_idle=sum(a.state not in (R.WORKING, R.WAITING_INPUT, R.WAITING_APPROVAL) for a in agents), **kw)


def valid(text):
    check_html(text)  # raises when Telegram would refuse it
    assert len(text) <= TEXT_LIMIT
    return text


def test_full_card_layout():
    kids = [
        agent("Oracle run lifecycle", R.WORKING, now=Activity(True, NOW - 9, None, ItemBrief(K.COMMAND, False, "pytest -q"))),
        agent("Spec review", R.WAITING_INPUT, ctx=12),
        agent("Parser", R.IDLE, "gpt-6.1-luna", "low", None, age=720, depth=2),
        agent("Old one", R.NOT_LOADED, "mystery-1", None, 7, age=7200),
    ]
    text = valid(render_status(with_agents(kids, total=9, summary=StatusSummary("Правит тесты статуса.", "прогнать e2e"))))
    assert text == (
        "📊 <b>Fox Profile</b>\n"
        "🟢 <b>работает</b> · sol·low · ход 25м · 🕒 2м назад\n"
        "💭 выполняет: pytest -q\n"
        "🧠 ctx 160k/475k (34%) · 💾 кэш 80%\n"
        "🎯 Ship the profile page · 1.2M\n"
        "\n"
        "🤖 <b>Субагенты 9</b> · 🟢 1 · 🟡 1 · ⚪ 2\n"
        " 🟢 Oracle run lifecycle · luna·high · ctx 40% · выполняет: pytest -q\n"
        " 🟡 Spec review · luna·high · ctx 12% · ждёт ответа\n"
        " ↳ ⚪ Parser · luna·low · 12м назад\n"
        " 💤 Old one · mystery-1 · ctx 7% · 2ч назад\n"
        " … и ещё 5\n"
        "\n"
        "📝 <b>В процессе:</b> Правит тесты статуса.\n"
        "➡️ <b>Дальше:</b> прогнать e2e\n"
        "\n"
        "📈 лимит 41% нед. · сброс 20.01 09:00 UTC")


def test_minimal_card_has_no_goal_agents_next_or_summary():
    text = valid(render_status(report(
        title=None, state=R.IDLE, model=None, effort=None, turn_elapsed_s=None, usage=None, last_activity=None, goal=None,
        limits=None, activity=None)))
    assert text == (
        "📊 <b>—</b>\n"
        "⚪ <b>простаивает</b> · — · 🕒 —\n"
        "💭 —\n"
        "🧠 ctx — · 💾 кэш —\n"
        "\n"
        "🤖 <b>Субагенты</b> · нет\n"
        "\n"
        "📝 резюме недоступно\n"
        "\n"
        "📈 лимит —")


def test_no_next_line_without_next_steps_and_unreadable_agents_are_a_dash():
    text = valid(render_status(report(agents_total=None, summary=StatusSummary("Идёт сборка."))))
    assert "📝 <b>В процессе:</b> Идёт сборка.\n" in text and "➡️" not in text and "🤖 <b>Субагенты</b> · —" in text
    multi = valid(render_status(report(summary=StatusSummary("раз\nдва\nтри", "а\nб"))))
    assert "📝 <b>В процессе:</b> раз\nдва\nтри\n➡️ <b>Дальше:</b> а\nб\n" in multi


def test_zero_counts_are_hidden_in_the_header():
    header = lambda kids: next(x for x in render_status(with_agents(kids)).splitlines() if x.startswith("🤖"))
    assert header([agent(state=R.IDLE)] * 2) == "🤖 <b>Субагенты 2</b> · ⚪ 2"
    assert header([agent(), agent(state=R.IDLE)]) == "🤖 <b>Субагенты 2</b> · 🟢 1 · ⚪ 1"
    assert header([agent(state=R.WAITING_APPROVAL)]) == "🤖 <b>Субагенты 1</b> · 🟡 1"
    assert header([agent(), agent(state=R.WAITING_INPUT)]) == "🤖 <b>Субагенты 2</b> · 🟢 1 · 🟡 1"


def test_at_most_six_subagent_lines_and_the_rest_is_counted():
    text = valid(render_status(with_agents([agent(f"A{i}") for i in range(8)], total=40)))
    assert sum(x.startswith(" 🟢") for x in text.splitlines()) == 6 and "\n … и ещё 34\n" in text
    text = valid(render_status(with_agents([agent(f"A{i}") for i in range(4)])))
    assert sum(x.startswith(" 🟢") for x in text.splitlines()) == 4 and "и ещё" not in text


def test_depth_two_and_deeper_get_an_arrow_and_depth_one_or_unknown_do_not():
    kids = [agent("D1", depth=1), agent("D2", depth=2), agent("D3", depth=3), agent("D0", depth=None)]
    lines = render_status(with_agents(kids)).splitlines()
    assert " 🟢 D1 · luna·high · ctx 40% · думает" in lines and " ↳ 🟢 D2 · luna·high · ctx 40% · думает" in lines
    assert " ↳ 🟢 D3 · luna·high · ctx 40% · думает" in lines and " 🟢 D0 · luna·high · ctx 40% · думает" in lines


def test_agent_fragments_are_clipped_and_a_missing_one_is_left_out():
    long = agent("т" * 50, now=Activity(True, NOW - 9, None, ItemBrief(K.COMMAND, False, "x" * 70)))
    line = next(x for x in render_status(with_agents([long])).splitlines() if x.startswith(" 🟢"))
    title, model, ctx, now = line.removeprefix(" 🟢 ").split(" · ")
    assert title == "т" * 31 + "…" and len(now) == 40 and now.endswith("…")
    bare = agent(now=Activity(True, NOW, None, None))  # a running turn without an item: nothing to say, no dash
    assert next(x for x in render_status(with_agents([bare])).splitlines() if x.startswith(" 🟢")).endswith("ctx 40%")


@pytest.mark.parametrize("model,effort,short", [
    ("gpt-6-luna", "high", "luna·high"), ("gpt-6-sol", "low", "sol·low"), ("gpt-6.1-luna", "medium", "luna·medium"),
    ("gpt-6.12-sol", None, "sol"), ("gpt-6", "low", "gpt-6·low"), ("gpt-6.1", None, "gpt-6.1"),
    ("claude-x", "high", "claude-x·high"), ("gpt-60-luna", None, "gpt-60-luna"), (None, "high", "high"), (None, None, "—"),
])
def test_model_short_names_keep_the_full_name_only_when_the_prefix_does_not_match(model, effort, short):
    line = render_status(report(model=model, effort=effort)).splitlines()[1]
    assert line.split(" · ")[1] == short


def test_state_words_and_icons_are_russian_and_no_english_state_word_appears_anywhere():
    states = {R.WORKING: "🟢 <b>работает", R.WAITING_INPUT: "🟡 <b>ждёт ответа", R.WAITING_APPROVAL: "🟡 <b>ждёт одобрения",
              R.IDLE: "⚪ <b>простаивает", R.NOT_LOADED: "💤 <b>не загружен", R.ERROR: "🔴 <b>ошибка", R.UNKNOWN: "❔ <b>неизвестно"}
    for state, head in states.items():
        text = valid(render_status(with_agents([agent(state=s) for s in states], activity=None, state=state,
                                               summary=StatusSummary("ок", "дальше"))))
        assert text.splitlines()[1].startswith(head)
        assert not [w for w in ENGLISH if re.search(rf"\b{w}\b", re.sub(r"<[^>]+>", "", text))], text
    quiet = render_status(report(state=R.STALLED, quiet_minutes=75))
    assert quiet.splitlines()[1].startswith("🟠 <b>тихо 1ч15м</b> · sol·low · ход 25м")
    waits = render_status(report(state=R.WAITING_INPUT, activity=None, turn_elapsed_s=90)).splitlines()
    assert waits[1].startswith("🟡 <b>ждёт ответа</b>") and waits[2] == "💭 ждёт ответа" and "ход 1м" in waits[1]


def test_time_and_duration_words_are_abbreviated():
    first = lambda **kw: render_status(report(**kw)).splitlines()[1]
    assert first(turn_elapsed_s=3600).endswith("ход 1ч · 🕒 2м назад") and "ход 2ч05м" in first(turn_elapsed_s=7500)
    assert first(last_activity=NOW - 5).endswith("🕒 только что") and first(last_activity=NOW - 7200).endswith("🕒 2ч назад")
    assert first(last_activity=NOW - 3 * 86400).endswith("🕒 3д назад")
    assert render_status(report(limits=RateLimits(55, None, 300))).endswith("📈 лимит 55% 5ч")
    assert "лимит 7% 90м · сброс" in render_status(report(limits=RateLimits(7, RESET, 90)))


def test_goal_line_is_clipped_and_marks_a_goal_that_is_not_active():
    text = render_status(report(goal=GoalSummary("ц" * 90, "paused", 900, 50_000)))
    assert f"🎯 {'ц' * 59}… · 900/50k · пауза\n" in text


def test_the_limit_reset_is_the_only_absolute_time_and_is_labelled_utc():
    text = render_status(report())
    assert text.count("UTC") == 1 and text.endswith("📈 лимит 41% нед. · сброс 20.01 09:00 UTC")
    assert "снимок" not in text and not re.search(r"\b\d\d:\d\d\b", text.replace("09:00 UTC", ""))
    assert "UTC" not in render_status(report(limits=RateLimits(41, None, 10080)))


# --- escaping ------------------------------------------------------------------------------------------------

NASTY = "<b>&amp; \"q\" 'a' </i><script>"


def test_every_dynamic_string_is_escaped_and_the_card_still_parses():
    kid = agent(NASTY, model=NASTY, effort=NASTY, now=Activity(True, NOW, None, ItemBrief(K.COMMAND, False, NASTY)))
    text = valid(render_status(with_agents(
        [kid], title=NASTY, model=NASTY, effort=NASTY, activity=Activity(True, NOW, None, ItemBrief(K.TOOL, False, NASTY)),
        goal=GoalSummary(NASTY, NASTY, 1, 2), summary=StatusSummary(NASTY, NASTY))))
    stripped = re.sub(r"</?(b)>", "", text)
    assert "<" not in stripped and ">" not in stripped  # only our own <b> tags are left
    assert "&lt;b&gt;&amp;amp; &quot;q&quot; &#x27;a&#x27; &lt;/i&gt;&lt;script&gt;" in text
    assert text.count("<b>") == text.count("</b>") == 5
    assert strip_html(text.splitlines()[0]) == f"📊 {NASTY}"


def test_the_card_bold_tags_are_exactly_the_fixed_ones():
    text = render_status(with_agents([agent()], summary=StatusSummary("а", "б")))
    assert re.findall(r"<[^>]+>", text) == ["<b>", "</b>"] * 5  # title, state, header, В процессе, Дальше


def test_clipping_happens_before_escaping_so_an_entity_is_never_cut():
    text = valid(render_status(report(title="&" * 100)))
    first = text.splitlines()[0]
    assert first == "📊 <b>" + "&amp;" * 59 + "…</b>"
    assert not re.search(r"&[^;]{0,5}…", text)


# --- 4096 ----------------------------------------------------------------------------------------------------

def huge(**kw):
    long = "я" * 400
    kids = [agent(long, model=long, effort=long, now=Activity(True, NOW, None, ItemBrief(K.COMMAND, False, long)))
            for _ in range(6)]
    base = dict(title=long, model=long, effort=long, activity=Activity(True, NOW, None, ItemBrief(K.COMMAND, False, long)),
                goal=GoalSummary(long, "x" * 99, 10**9, 10**9), summary=StatusSummary(long, long))
    base.update(kw)
    return with_agents(kids, total=300, **base)


def test_a_normal_card_is_not_shrunk():
    kids = [agent(f"Agent {i}") for i in range(6)]
    text = render_status(with_agents(kids, summary=StatusSummary("в", "д")))
    assert sum(x.startswith(" 🟢") for x in text.splitlines()) == 6 and "🎯" in text and "думает" in text


def test_a_huge_card_shrinks_to_fit_and_never_cuts_a_tag_or_an_entity():
    for fill in ("я", "&", "<", '"', "'"):  # escaped, the last ones are 4 to 6 times longer: only they need the shrink
        r = dataclasses.replace(huge(), title=fill * 400, summary=StatusSummary(fill * 230, fill * 120), agents=tuple(
            AgentLine(fill * 400, fill * 400, fill * 400, R.WORKING, 5, NOW,
                      Activity(True, NOW, None, ItemBrief(K.COMMAND, False, fill * 400))) for _ in range(6)))
        text = valid(render_status(r))
        assert text.count("<b>") == text.count("</b>") and not re.search(r"&[#\w]*$|&[^;\s]{0,6}…", text)
        shown = sum(x.startswith(" 🟢") for x in text.splitlines())
        assert (shown < 6) is (fill in "<'&\"" and len(view._card(r, view.PLANS[0])) > TEXT_LIMIT)


def test_plans_shrink_in_the_documented_order():
    plans = view.PLANS
    assert plans[0] == view.Plan(6, 40, True)
    lines = [p.agent_lines for p in plans]
    assert lines == sorted(lines, reverse=True) and MIN_AGENT_LINES in lines
    first_short = next(i for i, p in enumerate(plans) if p.now_max < 40)
    assert all(p.agent_lines > MIN_AGENT_LINES for p in plans[: first_short - 1]) and plans[first_short - 1].agent_lines == 3
    first_no_goal = next(i for i, p in enumerate(plans) if not p.goal)
    assert all(p.goal for p in plans[:first_no_goal]) and all(not p.goal for p in plans[first_no_goal:])
    assert plans[first_no_goal - 1].agent_lines == MIN_AGENT_LINES and plans[first_no_goal - 1].now_max <= 12


def test_render_picks_the_first_plan_that_fits(monkeypatch):
    r = huge()
    size = lambda *plan: len(view._card(r, view.Plan(*plan)))
    fragments = lambda text: [len(x.split(" · ")[-1]) for x in text.splitlines() if x.startswith(" 🟢")]
    full = render_status(r)
    assert len(full) <= TEXT_LIMIT  # a huge fixture does not fit with six lines: it was shrunk already
    cases = [  # (limit just under this plan, what must be left)
        (size(6, 40, True), 5, 40, True), (size(3, 40, True), 3, 24, True),
        (size(3, 24, True), 3, 12, True), (size(3, 12, True), 3, 12, False)]
    for limit, count, now_len, goal in cases:
        monkeypatch.setattr(view, "TEXT_LIMIT", limit - 1)
        text = render_status(r)
        assert len(text) <= limit - 1 and len(fragments(text)) == count and ("🎯" in text) is goal, (limit, count)
        assert max(fragments(text)) == now_len if count else True
        check_html(text)


def test_the_shrunk_card_keeps_the_summary_and_the_footer():
    text = valid(render_status(huge()))
    assert "📝 <b>В процессе:</b>" in text and "➡️ <b>Дальше:</b>" in text and text.endswith(" UTC")
