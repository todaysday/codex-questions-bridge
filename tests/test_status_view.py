"""The status card layout (Telegram HTML): header, snapshot texts, escaping, the 4096-character shrink order, model names."""

import dataclasses
import re

import pytest

from cqb.domain.enums import ItemKind as K, RunState as R
from cqb.domain.status import (
    NEXT_MAX, PROGRESS_MAX, Activity, AgentLine, GoalSummary, ItemBrief, RateLimits, StatusReport, StatusSummary, UsageSnapshot,
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
        "🟢 Fox Profile\n"
        "🧠 sol 6·low · 🕒 25м · 🔄 2м 💭 выполняет: pytest -q\n"
        " <b>ctx</b> 160k/475k (34%) · 💾 80%\n"
        "🎯 Ship the profile page · 1.2M\n"
        "\n"
        "🤖 <b>Субагенты 9</b> · 🟢 1 · 🟡 1 · ⚪ 2\n"
        " 🟢 Oracle run lifecycle · luna 6·high · ctx 40% · выполняет: pytest -q\n"
        " 🟡 Spec review · luna 6·high · ctx 12% · ждёт ответа\n"
        " ↳ ⚪ Parser · luna 6.1·low · 12м назад\n"
        " 💤 Old one · mystery-1 · ctx 7% · 2ч назад\n"
        " … и ещё 5\n"
        "\n"
        "📝 <b>В процессе:</b> Правит тесты статуса.\n"
        "\n"
        "➡️ <b>Дальше:</b> прогнать e2e\n"
        "\n"
        "📈 лимит 41% нед. · сброс 20.01 09:00 UTC")


def test_minimal_card_has_no_goal_agents_next_or_summary():
    text = valid(render_status(report(
        title=None, state=R.IDLE, model=None, effort=None, turn_elapsed_s=None, usage=None, last_activity=None, goal=None,
        limits=None, activity=None)))
    assert text == (
        "⚪ —\n"
        "🧠 — · 🔄 —\n"
        " <b>ctx</b> —\n"
        "\n"
        "🤖 <b>Субагенты</b> · нет\n"
        "\n"
        "📝 резюме недоступно\n"
        "\n"
        "📈 лимит —")


def test_no_next_line_without_next_steps_and_unreadable_agents_are_a_dash():
    text = valid(render_status(report(agents_total=None, summary=StatusSummary("Идёт сборка."))))
    assert "📝 <b>В процессе:</b> Идёт сборка.\n" in text and "➡️" not in text and "🤖 <b>Субагенты</b> · —" in text
    multi = valid(render_status(report(summary=StatusSummary("раз\nдва\nтри\nчетыре\nпять", "а\nб\nв\nг"))))
    assert "📝 <b>В процессе:</b> раз\nдва\nтри\nчетыре\nпять\n\n➡️ <b>Дальше:</b> а\nб\nв\nг\n" in multi


def test_one_empty_line_separates_the_progress_from_the_next_steps():
    text = render_status(report(summary=StatusSummary("раз", "два")))
    assert "📝 <b>В процессе:</b> раз\n\n➡️ <b>Дальше:</b> два\n\n📈" in text and "\n\n\n" not in text


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
    assert " 🟢 D1 · luna 6·high · ctx 40% · думает" in lines and " ↳ 🟢 D2 · luna 6·high · ctx 40% · думает" in lines
    assert " ↳ 🟢 D3 · luna 6·high · ctx 40% · думает" in lines and " 🟢 D0 · luna 6·high · ctx 40% · думает" in lines


def test_agent_fragments_are_clipped_and_a_missing_one_is_left_out():
    long = agent("т" * 50, now=Activity(True, NOW - 9, None, ItemBrief(K.COMMAND, False, "x" * 70)))
    line = next(x for x in render_status(with_agents([long])).splitlines() if x.startswith(" 🟢"))
    title, model, ctx, now = line.removeprefix(" 🟢 ").split(" · ")
    assert title == "т" * 31 + "…" and len(now) == 40 and now.endswith("…")
    bare = agent(now=Activity(True, NOW, None, None))  # a running turn without an item: nothing to say, no dash
    assert next(x for x in render_status(with_agents([bare])).splitlines() if x.startswith(" 🟢")).endswith("ctx 40%")


@pytest.mark.parametrize("model,effort,short", [
    ("gpt-6-luna", "high", "luna 6·high"), ("gpt-6-sol", "low", "sol 6·low"), ("gpt-6.1-sol", "medium", "sol 6.1·medium"),
    ("gpt-6.12-luna", None, "luna 6.12"), ("gpt-6", "low", "gpt-6·low"), ("gpt-6.1", None, "gpt-6.1"),
    ("claude-x", "high", "claude-x·high"), ("gpt-60-luna", None, "luna 60"), (None, "high", "high"), (None, None, "—"),
])
def test_model_short_is_name_version_and_effort_and_other_names_are_kept_whole(model, effort, short):
    line = render_status(report(model=model, effort=effort)).splitlines()[1]
    assert line.startswith(f"🧠 {short} · 🕒 ")
    kid = render_status(with_agents([agent(model=model, effort=effort)])).splitlines()
    parts = next(x for x in kid if x.startswith(" 🟢")).split(" · ")
    assert (parts[1] == short) if model or effort else parts[1].startswith("ctx")  # the same format on a subagent line


def test_the_state_is_an_icon_before_the_thread_name_and_no_english_state_word_appears_anywhere():
    icons = {R.WORKING: "🟢", R.WAITING_INPUT: "🟡", R.WAITING_APPROVAL: "🟡", R.IDLE: "⚪", R.NOT_LOADED: "💤",
             R.ERROR: "🔴", R.STALLED: "🟠", R.UNKNOWN: "❔"}
    for state, icon in icons.items():
        text = valid(render_status(with_agents([agent(state=s) for s in icons], activity=None, state=state,
                                               quiet_minutes=75, summary=StatusSummary("ок", "дальше"))))
        assert text.splitlines()[0] == f"{icon} Fox Profile"
        assert not [w for w in ENGLISH if re.search(rf"\b{w}\b", re.sub(r"<[^>]+>", "", text))], text
    waits = render_status(report(state=R.WAITING_INPUT, activity=None, turn_elapsed_s=90)).splitlines()
    assert waits[1] == "🧠 sol 6·low · 🕒 1м · 🔄 2м 💭 ждёт ответа"
    stalled = render_status(report(state=R.STALLED, quiet_minutes=75, activity=None, last_activity=NOW - 4500)).splitlines()
    assert stalled[0] == "🟠 Fox Profile" and stalled[1] == "🧠 sol 6·low · 🕒 25м · 🔄 1ч"


@pytest.mark.parametrize("cwd,folder", [
    ("/srv/projects/gh-runner-dispatcher", "gh-runner-dispatcher"), ("/srv/projects/gh-runner-dispatcher/", "gh-runner-dispatcher"),
    ("/a/b///", "b"), ("/home/me", "me"), ("relative/dir", "dir"), ("single", "single"), ("C:\\work\\proj\\", "proj"),
    ("/path with space/my project", "my project"), ("/", None), ("//", None), ("", None), (None, None), ("   ", None),
    ("/a/..", None), ("/a/b/.", None), (".", None),
])
def test_folder_is_only_the_last_path_component_and_never_a_path(cwd, folder):
    assert view.folder_name(cwd) == folder
    first = render_status(report(cwd=cwd)).splitlines()[0]
    assert first == ("🟢 Fox Profile" + (f" · 📁 {folder}" if folder else ""))
    assert "/" not in (folder or "") and "\\" not in (folder or "")


def test_folder_is_escaped_and_clipped_to_32_characters_and_name_and_folder_are_independent():
    first = lambda **kw: render_status(report(**kw)).splitlines()[0]
    assert first(cwd="/w/<b>&x") == "🟢 Fox Profile · 📁 &lt;b&gt;&amp;x"
    assert first(cwd="/w/" + "д" * 40) == "🟢 Fox Profile · 📁 " + "д" * 31 + "…"
    assert first(cwd="/w/" + "д" * 32) == "🟢 Fox Profile · 📁 " + "д" * 32  # exactly the limit: no ellipsis
    assert first(cwd="/w/" + "&" * 40) == "🟢 Fox Profile · 📁 " + "&amp;" * 31 + "…"  # clipped before escaping
    assert first(cwd="/w/fox", title=None) == "🟢 — · 📁 fox"
    long = first(cwd="/w/" + "я" * 99, title="т" * 99)
    assert long == "🟢 " + "т" * 59 + "… · 📁 " + "я" * 31 + "…"
    valid(render_status(report(cwd="/w/<x>", title=NASTY)))


def test_the_header_with_a_folder_matches_the_documented_layout():
    text = valid(render_status(report(cwd="/srv/projects/gh-runner-dispatcher")))
    assert text.splitlines()[:3] == [
        "🟢 Fox Profile · 📁 gh-runner-dispatcher",
        "🧠 sol 6·low · 🕒 25м · 🔄 2м 💭 выполняет: pytest -q",
        " <b>ctx</b> 160k/475k (34%) · 💾 80%"]


def test_status_line_segments_turn_duration_last_activity_and_now():
    line = lambda **kw: render_status(report(**kw)).splitlines()[1]
    assert line(turn_elapsed_s=8100) == "🧠 sol 6·low · 🕒 2ч15м · 🔄 2м 💭 выполняет: pytest -q"
    assert line(turn_elapsed_s=2700).startswith("🧠 sol 6·low · 🕒 45м · ") and "🕒 1ч · " in line(turn_elapsed_s=3600)
    assert "🕒 2ч05м" in line(turn_elapsed_s=7500)
    no_turn = line(state=R.IDLE, turn_elapsed_s=None, activity=None)  # no turn: no duration; nothing 'now': no 💭
    assert no_turn == "🧠 sol 6·low · 🔄 2м"
    assert line(state=R.IDLE, turn_elapsed_s=500, activity=None) == no_turn  # an idle thread has no running turn
    assert line(state=R.WORKING, activity=None).endswith("🔄 2м") and "💭" not in line(state=R.WORKING, activity=None)
    assert line(last_activity=NOW - 5).endswith("🔄 now 💭 выполняет: pytest -q")
    assert "🔄 now " in line(last_activity=NOW - 59) and "🔄 1м " in line(last_activity=NOW - 60)
    assert "🔄 59м " in line(last_activity=NOW - 3599) and "🔄 1ч " in line(last_activity=NOW - 3600)
    assert "🔄 5ч " in line(last_activity=NOW - 5 * 3600 - 59) and "🔄 3д " in line(last_activity=NOW - 3 * 86400)
    assert "🔄 — " in line(last_activity=None) and "назад" not in line()
    done = render_status(report(state=R.IDLE, turn_elapsed_s=None, activity=Activity(False, NOW - 900, NOW - 300, None)))
    assert done.splitlines()[1].endswith("💭 ход завершён 5м назад")


def test_context_line_starts_with_one_space_has_bold_ctx_and_leaves_out_an_unknown_cache():
    ctx = lambda usage: render_status(report(usage=usage)).splitlines()[2]
    assert ctx(UsageSnapshot(160_000, 475_000, 100, 80)) == " <b>ctx</b> 160k/475k (34%) · 💾 80%"
    assert ctx(UsageSnapshot(160_000, 475_000, 0, 0)) == " <b>ctx</b> 160k/475k (34%)"
    assert ctx(UsageSnapshot(900, None, 10, 1)) == " <b>ctx</b> 900/— · 💾 10%"
    assert ctx(None) == " <b>ctx</b> —"


def test_time_and_duration_words_are_abbreviated():
    assert render_status(report(limits=RateLimits(55, None, 300))).endswith("📈 лимит 55% 5ч")
    assert "лимит 7% 90м · сброс" in render_status(report(limits=RateLimits(7, RESET, 90)))
    idle = render_status(with_agents([agent(state=R.IDLE, age=5), agent(state=R.IDLE, age=7200)])).splitlines()
    assert any(x.endswith("только что") for x in idle) and any(x.endswith("2ч назад") for x in idle)  # subagents as before


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
    assert text.count("<b>") == text.count("</b>") == 4
    assert strip_html(text.splitlines()[0]) == f"🟢 {NASTY}"


def test_the_card_bold_tags_are_exactly_the_fixed_ones():
    text = render_status(with_agents([agent()], summary=StatusSummary("а", "б")))
    assert re.findall(r"<[^>]+>", text) == ["<b>", "</b>"] * 4  # ctx, subagents header, В процессе, Дальше


def test_clipping_happens_before_escaping_so_an_entity_is_never_cut():
    text = valid(render_status(report(title="&" * 100)))
    first = text.splitlines()[0]
    assert first == "🟢 " + "&amp;" * 59 + "…"
    assert not re.search(r"&[^;]{0,5}…", text)


# --- 4096 ----------------------------------------------------------------------------------------------------

def huge(**kw):
    long = "я" * 400
    kids = [agent(long, model=long, effort=long, now=Activity(True, NOW, None, ItemBrief(K.COMMAND, False, long)))
            for _ in range(6)]
    base = dict(title=long, model=long, effort=long, activity=Activity(True, NOW, None, ItemBrief(K.COMMAND, False, long)),
                goal=GoalSummary(long, "x" * 99, 10**9, 10**9), summary=StatusSummary(long * 2, long))
    base.update(kw)
    return with_agents(kids, total=300, **base)


def test_a_normal_card_is_not_shrunk():
    kids = [agent(f"Agent {i}") for i in range(6)]
    text = render_status(with_agents(kids, summary=StatusSummary("в", "д")))
    assert sum(x.startswith(" 🟢") for x in text.splitlines()) == 6 and "🎯" in text and "думает" in text


def test_a_huge_card_shrinks_to_fit_and_never_cuts_a_tag_or_an_entity():
    for fill in ("я", "&", "<", '"', "'"):  # escaped, the last ones are 4 to 6 times longer: only they need the shrink
        r = dataclasses.replace(huge(), title=fill * 400, summary=StatusSummary(fill * PROGRESS_MAX, fill * NEXT_MAX), agents=tuple(
            AgentLine(fill * 400, fill * 400, fill * 400, R.WORKING, 5, NOW,
                      Activity(True, NOW, None, ItemBrief(K.COMMAND, False, fill * 400))) for _ in range(6)))
        text = valid(render_status(r))
        assert text.count("<b>") == text.count("</b>") and not re.search(r"&[#\w]*$|&[^;\s]{0,6}…", text)
        shown = sum(x.startswith(" 🟢") for x in text.splitlines())
        assert (shown < 6) is (len(view._card(r, view.PLANS[0])) > TEXT_LIMIT)
        assert "📝 <b>В процессе:</b>" in text and "➡️ <b>Дальше:</b>" in text


def test_the_last_plan_fits_the_worst_escaped_card_whatever_the_fields_hold():
    """Every field at its maximum, each character expanding to six (`&#x27;`): the last plan alone must fit."""
    for fill in ("'", '"', "<", "&"):
        r = dataclasses.replace(huge(), title=fill * 400, model=fill * 400, effort=fill * 400, activity=Activity(
            True, NOW, None, ItemBrief(K.COMMAND, False, fill * 400)), summary=StatusSummary(fill * 5000, fill * 5000))
        r = dataclasses.replace(r, cwd="/w/" + fill * 400)
        text = view._card(r, view.PLANS[-1])
        assert len(text) <= TEXT_LIMIT, (fill, len(text))
        check_html(text)
        assert len(render_status(r)) <= TEXT_LIMIT


def test_a_long_summary_is_kept_whole_while_the_card_fits():
    summary = StatusSummary("\n".join(["п" * 125] * 5), "\n".join(["д" * 98] * 4))  # about 650 + 400 characters
    text = valid(render_status(with_agents([agent(f"A{i}") for i in range(6)], summary=summary)))
    assert summary.progress in text and summary.next_steps in text and "🎯" in text


def test_plans_shrink_in_the_documented_order():
    plans = view.PLANS
    assert plans[0] == view.Plan(6, 40, True)
    lines = [p.agent_lines for p in plans]
    assert lines == sorted(lines, reverse=True) and MIN_AGENT_LINES in lines
    first_short = next(i for i, p in enumerate(plans) if p.now_max < 40)
    assert all(p.agent_lines > MIN_AGENT_LINES for p in plans[: first_short - 1]) and plans[first_short - 1].agent_lines == 3
    assert all(p.progress_max == PROGRESS_MAX and p.next_max == NEXT_MAX for p in plans[: first_short + 3])
    first_no_goal = next(i for i, p in enumerate(plans) if not p.goal)
    assert all(p.goal for p in plans[:first_no_goal]) and all(not p.goal for p in plans[first_no_goal:])
    assert plans[first_no_goal - 1].agent_lines == MIN_AGENT_LINES and plans[first_no_goal - 1].now_max <= 12
    caps = [(p.progress_max, p.next_max) for p in plans]  # the summary is shortened last, after lines, fragments and goal
    first_cut = next(i for i, c in enumerate(caps) if c != (PROGRESS_MAX, NEXT_MAX))
    assert first_cut > first_no_goal and caps == sorted(caps, reverse=True) and not plans[first_cut].goal


def test_render_picks_the_first_plan_that_fits(monkeypatch):
    r = huge()
    size = lambda *plan: len(view._card(r, view.Plan(*plan)))
    fragments = lambda text: [len(x.split(" · ")[-1]) for x in text.splitlines() if x.startswith(" 🟢")]
    full = render_status(r)
    assert len(full) <= TEXT_LIMIT  # a huge fixture does not fit with six lines: it was shrunk already
    cases = [  # (limit just under this plan, what must be left)
        (size(6, 40, True), 5, 40, True), (size(3, 40, True), 3, 24, True),
        (size(3, 24, True), 3, 12, True), (size(3, 12, True), 3, 12, False)]
    monkeypatch.setattr(view, "TEXT_LIMIT", size(3, 12, False) - 1)  # only now is the summary cut, not before
    text = render_status(r)
    assert len(text) < size(3, 12, False) and len(fragments(text)) == 3 and "🎯" not in text
    assert len(text.split("📝 <b>В процессе:</b> ")[1].split("\n")[0]) < len("я" * PROGRESS_MAX * 2)
    for limit, count, now_len, goal in cases:
        monkeypatch.setattr(view, "TEXT_LIMIT", limit - 1)
        text = render_status(r)
        assert len(text) <= limit - 1 and len(fragments(text)) == count and ("🎯" in text) is goal, (limit, count)
        assert max(fragments(text)) == now_len if count else True
        check_html(text)


def test_the_folder_survives_every_shrink_step():
    r = dataclasses.replace(huge(), cwd="/srv/projects/gh-runner-dispatcher")
    for plan in view.PLANS:
        assert view._card(r, plan).splitlines()[0].endswith(" · 📁 gh-runner-dispatcher")  # never the first thing to go
    assert valid(render_status(r)).splitlines()[0].endswith("📁 gh-runner-dispatcher")


def test_the_shrunk_card_keeps_the_summary_and_the_footer():
    text = valid(render_status(huge()))
    assert "📝 <b>В процессе:</b>" in text and "➡️ <b>Дальше:</b>" in text and text.endswith(" UTC")
