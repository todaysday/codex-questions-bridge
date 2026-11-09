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


def agent(label="Ada", state=R.WORKING, model="gpt-6-luna", effort="high", ctx=40, age=30, now=None, depth=1,
          compactions=None, cache_permille=None):
    activity = Activity(True, NOW - 60, None, ItemBrief(K.REASONING, False)) if now is None and state is R.WORKING else now
    return AgentLine(label, model, effort, state, ctx, NOW - age, activity, depth, compactions, cache_permille)


def report(**kw):
    base = dict(
        now=NOW, title="Fox Profile", state=R.WORKING, quiet_minutes=None, model="gpt-6-sol", effort="low",
        turn_elapsed_s=1500, usage=UsageSnapshot(160_000, 475_000, 100, 100, 1000, 800), last_activity=NOW - 120,
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
    assert len(strip_html(text)) <= TEXT_LIMIT
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
        "🟢Fox Profile\n"
        "🧠sol 6 low<b> · </b>🕒25м<b> · </b>🔄2м 💭выполняет: pytest -q\n"
        " <b>ctx</b> 160k/475k(34%)<b> · </b>💾80%\n"
        "<blockquote expandable>🎯Ship the profile page</blockquote>\n"
        "\n"
        "🤖<b>Субагенты 9</b><b> · </b>🟢1<b> · </b>🟡1<b> · </b>⚪2\n"
        "<blockquote expandable>\n"
        " 🟢Oracle run lifecycle<b> · </b>luna 6 high<b> · </b>ctx 40%<b> · </b>выполняет: pytest -q\n"
        " 🟡Spec review<b> · </b>luna 6 high<b> · </b>ctx 12%<b> · </b>ждёт ответа\n"
        " ↳ ⚪Parser<b> · </b>luna 6.1 low<b> · </b>12м назад\n"
        " 💤Old one<b> · </b>mystery-1<b> · </b>ctx 7%<b> · </b>2ч назад\n"
        " … и ещё 5\n"
        "</blockquote>\n"
        "\n"
        "🔛<b>:</b> Правит тесты статуса.\n"
        "\n"
        "🔜<b>:</b> прогнать e2e")


def test_minimal_card_has_no_goal_agents_next_or_summary():
    text = valid(render_status(report(
        title=None, state=R.IDLE, model=None, effort=None, turn_elapsed_s=None, usage=None, last_activity=None, goal=None,
        limits=None, activity=None)))
    assert text == (
        "⚪—\n"
        "🧠—<b> · </b>🔄—\n"
        " <b>ctx</b> —\n"
        "\n"
        "🤖<b>Субагенты</b><b> · </b>нет\n"
        "\n"
        "📝резюме недоступно")  # usage unknown: no limits line at all


def test_no_next_line_without_next_steps_and_unreadable_agents_are_a_dash():
    text = valid(render_status(report(agents_total=None, summary=StatusSummary("Идёт сборка."))))
    assert "🔛<b>:</b> Идёт сборка." in text and "➡️" not in text and "🤖<b>Субагенты</b><b> · </b>—" in text
    multi = valid(render_status(report(summary=StatusSummary("раз\nдва\nтри\nчетыре\nпять", "а\nб\nв\nг"))))
    assert "🔛<b>:</b> раз\nдва\nтри\nчетыре\nпять\n\n🔜<b>:</b> а\nб\nв\nг" in multi


def test_one_empty_line_separates_the_progress_from_the_next_steps():
    text = render_status(report(summary=StatusSummary("раз", "два")))
    assert "🔛<b>:</b> раз\n\n🔜<b>:</b> два" in text and "\n\n\n" not in text


def test_zero_counts_are_hidden_in_the_header():
    header = lambda kids: next(x for x in render_status(with_agents(kids)).splitlines() if x.startswith("🤖"))
    assert header([agent(state=R.IDLE)] * 2) == "🤖<b>Субагенты 2</b><b> · </b>⚪2"
    assert header([agent(), agent(state=R.IDLE)]) == "🤖<b>Субагенты 2</b><b> · </b>🟢1<b> · </b>⚪1"
    assert header([agent(state=R.WAITING_APPROVAL)]) == "🤖<b>Субагенты 1</b><b> · </b>🟡1"
    assert header([agent(), agent(state=R.WAITING_INPUT)]) == "🤖<b>Субагенты 2</b><b> · </b>🟢1<b> · </b>🟡1"


def test_at_most_eight_subagent_lines_and_the_rest_is_counted():
    text = valid(render_status(with_agents([agent(f"A{i}") for i in range(8)], total=40)))
    assert sum(x.startswith(" 🟢") for x in text.splitlines()) == 8 and "\n … и ещё 32\n" in text
    text = valid(render_status(with_agents([agent(f"A{i}") for i in range(4)])))
    assert sum(x.startswith(" 🟢") for x in text.splitlines()) == 4 and "и ещё" not in text


def test_depth_two_and_deeper_get_an_arrow_and_depth_one_or_unknown_do_not():
    kids = [agent("D1", depth=1), agent("D2", depth=2), agent("D3", depth=3), agent("D0", depth=None)]
    lines = render_status(with_agents(kids)).splitlines()
    assert " 🟢D1<b> · </b>luna 6 high<b> · </b>ctx 40%<b> · </b>думает" in lines and " ↳ 🟢D2<b> · </b>luna 6 high<b> · </b>ctx 40%<b> · </b>думает" in lines
    assert " ↳ 🟢D3<b> · </b>luna 6 high<b> · </b>ctx 40%<b> · </b>думает" in lines and " 🟢D0<b> · </b>luna 6 high<b> · </b>ctx 40%<b> · </b>думает" in lines


def test_agent_fragments_are_clipped_and_a_missing_one_is_left_out():
    long = agent("т" * 50, now=Activity(True, NOW - 9, None, ItemBrief(K.COMMAND, False, "x" * 70)))
    line = next(x for x in render_status(with_agents([long])).splitlines() if x.startswith(" 🟢"))
    title, model, ctx, now = line.removeprefix(" 🟢").split("<b> · </b>")
    assert title == "т" * 31 + "…" and len(now) == 40 and now.endswith("…")
    bare = agent(now=Activity(True, NOW, None, None))  # a running turn without an item: nothing to say, no dash
    assert next(x for x in render_status(with_agents([bare])).splitlines() if x.startswith(" 🟢")).endswith("ctx 40%")


@pytest.mark.parametrize("model,effort,short", [
    ("gpt-6-luna", "high", "luna 6 high"), ("gpt-6-sol", "low", "sol 6 low"), ("gpt-6.1-sol", "medium", "sol 6.1 medium"),
    ("gpt-6.12-luna", None, "luna 6.12"), ("gpt-6", "low", "gpt-6 low"), ("gpt-6.1", None, "gpt-6.1"),
    ("claude-x", "high", "claude-x high"), ("gpt-60-luna", None, "luna 60"), (None, "high", "high"), (None, None, "—"),
])
def test_model_short_is_name_version_and_effort_and_other_names_are_kept_whole(model, effort, short):
    line = render_status(report(model=model, effort=effort)).splitlines()[1]
    assert line.startswith(f"🧠{short}<b> · </b>🕒")
    kid = render_status(with_agents([agent(model=model, effort=effort)])).splitlines()
    parts = next(x for x in kid if x.startswith(" 🟢")).split("<b> · </b>")
    assert (parts[1] == short) if model or effort else parts[1].startswith("ctx")  # the same format on a subagent line


def test_the_state_is_an_icon_before_the_thread_name_and_no_english_state_word_appears_anywhere():
    icons = {R.WORKING: "🟢", R.WAITING_INPUT: "🟡", R.WAITING_APPROVAL: "🟡", R.IDLE: "⚪", R.NOT_LOADED: "💤",
             R.ERROR: "🔴", R.STALLED: "🟠", R.UNKNOWN: "❔"}
    for state, icon in icons.items():
        text = valid(render_status(with_agents([agent(state=s) for s in icons], activity=None, state=state,
                                               quiet_minutes=75, summary=StatusSummary("ок", "дальше"))))
        assert text.splitlines()[0] == f"{icon}Fox Profile"
        assert not [w for w in ENGLISH if re.search(rf"\b{w}\b", re.sub(r"<[^>]+>", "", text))], text
    waits = render_status(report(state=R.WAITING_INPUT, activity=None, turn_elapsed_s=90)).splitlines()
    assert waits[1] == "🧠sol 6 low<b> · </b>🕒1м<b> · </b>🔄2м 💭ждёт ответа"
    stalled = render_status(report(state=R.STALLED, quiet_minutes=75, activity=None, last_activity=NOW - 4500)).splitlines()
    assert stalled[0] == "🟠Fox Profile" and stalled[1] == "🧠sol 6 low<b> · </b>🕒25м<b> · </b>🔄1ч"


@pytest.mark.parametrize("cwd,folder", [
    ("/srv/projects/gh-runner-dispatcher", "gh-runner-dispatcher"), ("/srv/projects/gh-runner-dispatcher/", "gh-runner-dispatcher"),
    ("/a/b///", "b"), ("/home/me", "me"), ("relative/dir", "dir"), ("single", "single"), ("C:\\work\\proj\\", "proj"),
    ("/path with space/my project", "my project"), ("/", None), ("//", None), ("", None), (None, None), ("   ", None),
    ("/a/..", None), ("/a/b/.", None), (".", None),
])
def test_folder_is_only_the_last_path_component_and_never_a_path(cwd, folder):
    assert view.folder_name(cwd) == folder
    first = view._title_line(report(cwd=cwd))
    assert first == ("🟢Fox Profile" + (f"\n📁{folder}" if folder else ""))
    assert "/" not in (folder or "") and "\\" not in (folder or "")


def test_folder_is_escaped_and_clipped_to_32_characters_and_name_and_folder_are_independent():
    first = lambda **kw: view._title_line(report(**kw))
    assert first(cwd="/w/<b>&x") == "🟢Fox Profile\n📁&lt;b&gt;&amp;x"
    assert first(cwd="/w/" + "д" * 40) == "🟢Fox Profile\n📁" + "д" * 31 + "…"
    assert first(cwd="/w/" + "д" * 32) == "🟢Fox Profile\n📁" + "д" * 32  # exactly the limit: no ellipsis
    assert first(cwd="/w/" + "&" * 40) == "🟢Fox Profile\n📁" + "&amp;" * 31 + "…"  # clipped before escaping
    assert first(cwd="/w/fox", title=None) == "🟢—\n📁fox"
    long = first(cwd="/w/" + "я" * 99, title="т" * 99)
    assert long == "🟢" + "т" * 59 + "…\n📁" + "я" * 31 + "…"
    valid(render_status(report(cwd="/w/<x>", title=NASTY)))


def test_the_header_with_a_folder_matches_the_documented_layout():
    text = valid(render_status(report(cwd="/srv/projects/gh-runner-dispatcher")))
    assert text.splitlines()[:4] == [
        "🟢Fox Profile",
        "📁gh-runner-dispatcher",
        "🧠sol 6 low<b> · </b>🕒25м<b> · </b>🔄2м 💭выполняет: pytest -q",
        " <b>ctx</b> 160k/475k(34%)<b> · </b>💾80%"]


def test_status_line_segments_turn_duration_last_activity_and_now():
    line = lambda **kw: render_status(report(**kw)).splitlines()[1]
    assert line(turn_elapsed_s=8100) == "🧠sol 6 low<b> · </b>🕒2ч15м<b> · </b>🔄2м 💭выполняет: pytest -q"
    assert line(turn_elapsed_s=2700).startswith("🧠sol 6 low<b> · </b>🕒45м<b> · </b>") and "🕒1ч<b> · </b>" in line(turn_elapsed_s=3600)
    assert "🕒2ч05м" in line(turn_elapsed_s=7500)
    no_turn = line(state=R.IDLE, turn_elapsed_s=None, activity=None)  # no turn: no duration; nothing 'now': no 💭
    assert no_turn == "🧠sol 6 low<b> · </b>🔄2м"
    assert line(state=R.IDLE, turn_elapsed_s=500, activity=None) == no_turn  # an idle thread has no running turn
    assert line(state=R.WORKING, activity=None).endswith("🔄2м") and "💭" not in line(state=R.WORKING, activity=None)
    assert line(last_activity=NOW - 5).endswith("🔄now 💭выполняет: pytest -q")
    assert "🔄now " in line(last_activity=NOW - 59) and "🔄1м " in line(last_activity=NOW - 60)
    assert "🔄59м " in line(last_activity=NOW - 3599) and "🔄1ч " in line(last_activity=NOW - 3600)
    assert "🔄5ч " in line(last_activity=NOW - 5 * 3600 - 59) and "🔄3д " in line(last_activity=NOW - 3 * 86400)
    assert "🔄— " in line(last_activity=None) and "назад" not in line()
    done = render_status(report(state=R.IDLE, turn_elapsed_s=None, activity=Activity(False, NOW - 900, NOW - 300, None)))
    assert done.splitlines()[1].endswith("💭ход завершён 5м назад")


def test_context_line_starts_with_one_space_has_bold_ctx_and_leaves_out_an_unknown_cache():
    ctx = lambda usage: render_status(report(usage=usage)).splitlines()[2]
    assert ctx(UsageSnapshot(160_000, 475_000, 100, 80, 1000, 800)) == " <b>ctx</b> 160k/475k(34%)<b> · </b>💾80%"
    assert ctx(UsageSnapshot(160_000, 475_000, 0, 0, 0, 0)) == " <b>ctx</b> 160k/475k(34%)"
    assert ctx(UsageSnapshot(900, None, 10, 1, 100, 10)) == " <b>ctx</b> 900/—<b> · </b>💾10%"
    assert ctx(UsageSnapshot(160_000, 475_000, 100, 100)) == " <b>ctx</b> 160k/475k(34%)"  # last call only: no figure
    assert ctx(None) == " <b>ctx</b> —"


@pytest.mark.parametrize("permille,shown", [
    (0, "0%"), (7, "0%"), (10, "1%"), (500, "50%"), (876, "87%"), (979, "97%"), (985, "98%"), (989, "98%"),  # integers, down
    (990, "99.0%"), (994, "99.4%"), (995, "99.5%"), (999, "99.9%"),  # one decimal from 99%, never rounded up
    (1000, "100%"),  # exactly everything cached
])
def test_cache_rate_is_rounded_down_with_one_decimal_from_99_percent_and_100_only_when_exact(permille, shown):
    assert view._cache(permille) == shown
    usage = UsageSnapshot(1, 2, 3, 3, 1000, permille)
    assert usage.cache_permille == permille
    assert ctx_line(usage).endswith(f"💾{shown}")


def ctx_line(usage):
    return render_status(report(usage=usage)).splitlines()[2]


def test_cache_rate_is_the_whole_session_never_the_last_call_and_unknown_shows_nothing():
    assert UsageSnapshot(1, 2, 1000, 1000).cache_permille is None  # a last-call figure of 100% is not shown
    assert UsageSnapshot(1, 2, 1000, 1000, 5_000_000, 4_970_000).cache_permille == 994
    assert UsageSnapshot(1, 2, 3, 3, 1999, 1000).cache_permille == 500  # 50.02: down
    assert UsageSnapshot(1, 2, 3, 3, 999_999, 999_999).cache_permille == 1000 and UsageSnapshot(1, 2, 3, 3, 1_000_000, 999_999).cache_permille == 999  # one token short: 99.9, never 100
    assert UsageSnapshot(1, 2, 3, 3, 0, 0).cache_permille is None and UsageSnapshot(1, 2, 3, 3, 10, 11).cache_permille is None
    assert UsageSnapshot(1, 2, 3, 3, 10, None).cache_permille is None
    assert "💾" not in ctx_line(UsageSnapshot(160_000, 475_000, 100, 100))


def test_compactions_are_shown_after_the_cache_on_the_root_and_at_the_end_of_a_subagent_line_and_zero_is_hidden():
    usage = UsageSnapshot(160_000, 475_000, 100, 80, 1000, 994)
    text = render_status(with_agents([agent(compactions=2), agent("B", compactions=0), agent("C", compactions=None)],
                                     usage=usage, compactions=5))
    assert text.splitlines()[2] == " <b>ctx</b> 160k/475k(34%)<b> · </b>💾99.4%<b> · </b>🗜️5"
    lines = [x for x in text.splitlines() if x.startswith(" 🟢")]
    assert lines[0].endswith("</b>думает<b> · </b>🗜️2") and lines[1].endswith("</b>думает") and lines[2].endswith("</b>думает")
    assert "🗜️" not in render_status(report(usage=usage, compactions=0))
    assert "🗜️" not in render_status(report(usage=usage, compactions=None))
    assert render_status(report(usage=None, compactions=3)).splitlines()[2] == " <b>ctx</b> —<b> · </b>🗜️3"
    assert render_status(report(usage=UsageSnapshot(1, None, 1, 1), compactions=3)).splitlines()[2].endswith("</b>🗜️3")


def test_subagent_lines_show_the_cache_rate_right_after_the_context():
    line = lambda **kw: next(x for x in render_status(with_agents([agent(**kw)])).splitlines() if x.startswith(" 🟢"))
    assert line(cache_permille=874) == " 🟢Ada<b> · </b>luna 6 high<b> · </b>ctx 40%<b> · </b>💾87%<b> · </b>думает"
    assert line(cache_permille=997) == " 🟢Ada<b> · </b>luna 6 high<b> · </b>ctx 40%<b> · </b>💾99.7%<b> · </b>думает"
    assert line(cache_permille=None) == " 🟢Ada<b> · </b>luna 6 high<b> · </b>ctx 40%<b> · </b>думает"
    assert line(cache_permille=874, ctx=None) == " 🟢Ada<b> · </b>luna 6 high<b> · </b>💾87%<b> · </b>думает"
    assert line(cache_permille=874, compactions=1).endswith("ctx 40%<b> · </b>💾87%<b> · </b>думает<b> · </b>🗜️1")


def test_the_subagent_cache_part_is_the_first_thing_the_shrink_drops():
    plans = view.PLANS
    assert plans[0].agent_cache and not any(p.agent_cache for p in plans[1:])
    assert plans[1].agent_lines == plans[0].agent_lines and plans[1].now_max == plans[0].now_max and plans[1].goal  # nothing else yet
    r = with_agents([agent(f"Agent {i}", cache_permille=874) for i in range(6)], summary=StatusSummary("в", "д"))
    full = view._card(r, plans[0])
    kid_marks = lambda text: sum("💾" in x for x in text.splitlines() if x.startswith(" 🟢"))
    assert kid_marks(full) == 6 and kid_marks(view._card(r, plans[1])) == 0 and "💾80%" in view._card(r, plans[1])  # root keeps it
    assert len(view._card(r, plans[1])) < len(full)


def test_time_and_duration_words_are_abbreviated():
    assert view.limits_line(RateLimits(55, None, 300), NOW).endswith("📈 осталось 45% 5ч")
    assert "осталось 93% 90м · сброс через" in view.limits_line(RateLimits(7, RESET, 90), NOW)
    idle = render_status(with_agents([agent(state=R.IDLE, age=5), agent(state=R.IDLE, age=7200)])).splitlines()
    assert any(x.endswith("только что") for x in idle) and any(x.endswith("2ч назад") for x in idle)  # subagents as before


def test_goal_is_complete_and_marks_a_goal_that_is_not_active():
    text = render_status(report(goal=GoalSummary("ц" * 90, "paused", 900, 50_000)))
    assert f"<blockquote expandable>🎯<b> · </b>пауза {'ц' * 90}</blockquote>" in text and "900/50k" not in text


def test_status_omits_limits_even_when_available():
    assert "📈" not in render_status(report())
    assert view.limits_line(RateLimits(41, RESET, 10080), NOW) == "📈 осталось 59% нед. · сброс через 5д 1ч 00м"


def test_countdown_formats():
    count = lambda s: view._countdown(s)  # noqa: E731
    assert count(5 * 86400 + 3600) == "5д 1ч 00м" and count(86400 + 10 * 3600 + 5 * 60) == "1д 10ч 05м"
    assert count(10 * 3600 + 5 * 60) == "10ч 05м" and count(3600) == "1ч 00м" and count(45 * 60) == "45м"
    assert count(59) == "1м" and count(60) == "1м" and count(0) == "1м"


# --- escaping ------------------------------------------------------------------------------------------------

NASTY = "<b>&amp; \"q\" 'a' </i><script>"


def test_every_dynamic_string_is_escaped_and_the_card_still_parses():
    kid = agent(NASTY, model=NASTY, effort=NASTY, now=Activity(True, NOW, None, ItemBrief(K.COMMAND, False, NASTY)))
    text = valid(render_status(with_agents(
        [kid], title=NASTY, model=NASTY, effort=NASTY, activity=Activity(True, NOW, None, ItemBrief(K.TOOL, False, NASTY)),
        goal=GoalSummary(NASTY, NASTY, 1, 2), summary=StatusSummary(NASTY, NASTY))))
    stripped = re.sub(r"</?b>|<blockquote expandable>|</blockquote>", "", text)
    assert "<" not in stripped and ">" not in stripped  # only our fixed formatting tags are present
    assert "&lt;b&gt;&amp;amp; &quot;q&quot; &#x27;a&#x27; &lt;/i&gt;&lt;script&gt;" in text
    assert text.count("<b>") == text.count("</b>") == 12
    assert strip_html(text.splitlines()[0]) == f"🟢{NASTY}"


def test_the_card_bold_tags_are_exactly_the_fixed_ones():
    text = render_status(with_agents([agent()], summary=StatusSummary("а", "б")))
    assert text.count("<b> · </b>") == 7
    assert text.count("<blockquote expandable>") == 2


def test_clipping_happens_before_escaping_so_an_entity_is_never_cut():
    text = valid(render_status(report(title="&" * 100)))
    first = text.splitlines()[0]
    assert first == "🟢" + "&amp;" * 59 + "…"
    assert not re.search(r"&[^;]{0,5}…", text)


# --- 4096 ----------------------------------------------------------------------------------------------------

def huge(**kw):
    long = "я" * 400
    kids = [agent(long, model=long, effort=long, now=Activity(True, NOW, None, ItemBrief(K.COMMAND, False, long)),
                  compactions=10**6, cache_permille=999) for _ in range(6)]
    base = dict(title=long, model=long, effort=long, activity=Activity(True, NOW, None, ItemBrief(K.COMMAND, False, long)),
                goal=GoalSummary(long, "x" * 99, 10**9, 10**9), summary=StatusSummary(long * 2, long),
                compactions=10**9, usage=UsageSnapshot(10**9, 10**9, 10**9, 10**9, 10**12, 10**12 - 1))
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
        assert (shown < 6) is (len(strip_html(view._card(r, view.PLANS[0]))) > TEXT_LIMIT)
        assert "🔛<b>:</b>" in text and "🔜<b>:</b>" in text


def test_the_last_plan_fits_the_worst_escaped_card_whatever_the_fields_hold():
    """Every field at its maximum, each character expanding to six (`&#x27;`): the last plan alone must fit."""
    for fill in ("'", '"', "<", "&"):
        r = dataclasses.replace(huge(), title=fill * 400, model=fill * 400, effort=fill * 400, activity=Activity(
            True, NOW, None, ItemBrief(K.COMMAND, False, fill * 400)), summary=StatusSummary(fill * 5000, fill * 5000))
        r = dataclasses.replace(r, cwd="/w/" + fill * 400)
        text = view._card(r, view.PLANS[-1])
        assert len(strip_html(text)) <= TEXT_LIMIT, (fill, len(text))
        check_html(text)
        assert len(strip_html(render_status(r))) <= TEXT_LIMIT


def test_a_long_summary_is_kept_whole_while_the_card_fits():
    summary = StatusSummary("\n".join(["п" * 125] * 5), "\n".join(["д" * 98] * 4))  # about 650 + 400 characters
    text = valid(render_status(with_agents([agent(f"A{i}") for i in range(6)], summary=summary)))
    assert summary.progress in text and summary.next_steps in text and "🎯" in text


def test_plans_shrink_in_the_documented_order():
    plans = view.PLANS
    assert plans[0] == view.Plan(8, 40, True)
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
    r = with_agents([agent(f"Agent {i}", cache_permille=874) for i in range(6)], total=40, cwd="/w/fox",
                    summary=StatusSummary("в" * PROGRESS_MAX, "д" * NEXT_MAX), goal=GoalSummary("ц" * 60, "paused", 1, 2))
    plans = view.PLANS
    sizes = [len(strip_html(view._card(r, p))) for p in plans]
    assert sizes == sorted(sizes, reverse=True) and sizes[-1] < sizes[0]  # no step grows the card
    kids = lambda text: [x for x in text.splitlines() if x.startswith(" 🟢")]
    for i in range(1, len(plans)):
        if sizes[i] == sizes[i - 1]:
            continue  # a plan that changes nothing for this card
        monkeypatch.setattr(view, "TEXT_LIMIT", sizes[i - 1] - 1)  # just too small for the previous plan
        text = render_status(r)
        expected = next(view._card(r, p) for p in plans if len(strip_html(view._card(r, p))) <= view.TEXT_LIMIT)
        assert text == expected and len(strip_html(text)) <= sizes[i - 1] - 1
        check_html(text)
    monkeypatch.setattr(view, "TEXT_LIMIT", 10)  # nothing fits: the last plan is still returned
    assert render_status(r) == view._card(r, plans[-1])


def test_the_folder_survives_every_shrink_step():
    r = dataclasses.replace(huge(), cwd="/srv/projects/gh-runner-dispatcher")
    for plan in view.PLANS:
        assert view._card(r, plan).splitlines()[1] == "📁gh-runner-dispatcher"  # never the first thing to go
    assert valid(render_status(r)).splitlines()[1] == "📁gh-runner-dispatcher"


def test_the_shrunk_card_keeps_the_summary():
    text = valid(render_status(huge()))
    assert "🔛<b>:</b>" in text and "🔜<b>:</b>" in text and "📈" not in text


# --- the open-in-Codex link: the very last line --------------------------------------------

LINK = "https://chatgpt.com/codex/tasks/{thread_id}"
ANCHOR = '🔗<a href="https://chatgpt.com/codex/tasks/019a-77ee-r1">Открыть в Codex</a>'


def test_no_link_line_without_a_configured_template_or_a_thread_id():
    assert "🔗" not in render_status(report(thread_id="r1")) and "<a" not in render_status(report(thread_id="r1"), "")
    assert "🔗" not in render_status(report(thread_id=None), LINK) and "🔗" not in render_status(report(thread_id=""), LINK)


def test_the_link_is_one_fixed_text_anchor_on_the_last_line():
    text = valid(render_status(report(thread_id="019a-77ee-r1"), LINK))
    lines = text.splitlines()
    assert lines[-1] == ANCHOR and "📈" not in text and text.count("🔗") == 1
    assert text.removesuffix("\n" + ANCHOR) == render_status(report(thread_id="019a-77ee-r1"))  # nothing else changed
    assert re.findall(r"<[^>]+>", lines[-1]) == ['<a href="https://chatgpt.com/codex/tasks/019a-77ee-r1">', "</a>"]
    other = render_status(report(thread_id="r9"), "https://example.org/open?thread={thread_id}&source=cqb").splitlines()[-1]
    assert other == '🔗<a href="https://example.org/open?thread=r9&amp;source=cqb">Открыть в Codex</a>'


def test_only_the_root_id_goes_into_the_url_and_it_is_encoded_and_the_href_is_escaped():
    r = report(thread_id='a b/"<&>\'x', title="<b>Title</b>", cwd="/w/secret-dir", model="secret-model")
    last = render_status(r, 'https://h/p?q="{thread_id}"&z=1').splitlines()[-1]
    assert last == '🔗<a href="https://h/p?q=&quot;a%20b%2F%22%3C%26%3E%27x&quot;&amp;z=1">Открыть в Codex</a>'
    for leaked in ("secret", "Title", "Fox"):
        assert leaked not in last
    assert "{thread_id}" not in last and "<" not in re.sub(r"</?a[^>]*>", "", last)
    valid(render_status(r, 'https://h/p?q="{thread_id}"&z=1'))


def test_the_link_is_the_last_thing_the_shrink_gives_up_and_its_length_is_budgeted():
    r = dataclasses.replace(huge(), thread_id="r1")
    link = "https://h/" + "&" * 150 + "{thread_id}"  # escapes to 5 characters each
    plans = view.PLANS
    assert all(p.link for p in plans[:-1]) and not plans[-1].link  # only the very last plan drops it
    assert plans[-2].agent_lines == 0 and plans[-2].goal is False and (plans[-2].progress_max, plans[-2].next_max) == (150, 100)
    for i, plan in enumerate(plans[:-1]):
        assert view._card(r, plan, link).splitlines()[-1].startswith('🔗<a href="https://h/&amp;')
        assert view._card(r, plan, link).endswith("</a>")
    assert "🔗" not in view._card(r, plans[-1], link)
    text = valid(render_status(r, link))
    assert len(strip_html(text)) <= TEXT_LIMIT and text.splitlines()[-1].endswith("</a>") and "📈" not in text
    assert len(render_status(r, link)) > len(render_status(r)) - 1  # the link took part in the size


def test_the_link_makes_the_card_shrink_further_and_a_card_that_fits_with_it_keeps_everything(monkeypatch):
    r = dataclasses.replace(with_agents([agent(f"Agent {i}", cache_permille=874) for i in range(6)],
                                        summary=StatusSummary("в" * 600, "д" * 300)), thread_id="r1")
    plain, linked = view._card(r, view.PLANS[0]), view._card(r, view.PLANS[0], LINK)
    assert len(strip_html(linked)) > len(strip_html(plain))
    monkeypatch.setattr(view, "TEXT_LIMIT", len(strip_html(plain)))  # fits without the link only: the link forces one more step
    with_link = render_status(r, LINK)
    assert len(strip_html(with_link)) <= len(strip_html(plain)) and with_link.endswith("</a>") and with_link != linked
    assert render_status(r) == plain  # without the link nothing shrinks
    monkeypatch.setattr(view, "TEXT_LIMIT", len(strip_html(linked)))
    assert render_status(r, LINK) == linked  # with room for the link nothing is dropped


def test_the_worst_escaped_card_with_the_longest_link_still_fits():
    for fill in ("'", '"', "<", "&"):
        r = dataclasses.replace(huge(), thread_id=fill * 100, title=fill * 400, cwd="/w/" + fill * 400,
                                summary=StatusSummary(fill * 5000, fill * 5000))
        link = "https://h/" + fill.replace("<", "x") * 100 + "{thread_id}"  # 200 characters in all
        text = render_status(r, link)
        assert len(strip_html(text)) <= TEXT_LIMIT, fill
        check_html(text)


def test_long_goal_uses_remaining_space_without_removing_other_content():
    objective = "т" * 6000
    text = render_status(report(goal=GoalSummary(objective, "active", 10, None), summary=StatusSummary("текущая работа", "дальше")))
    assert len(strip_html(text)) <= TEXT_LIMIT and "текущая работа" in text and "дальше" in text
    assert "<blockquote expandable>🎯" + "т" * 500 in text and "…</blockquote>" in text


def test_context_totals_format_matches_the_requested_layout():
    usage = UsageSnapshot(302300, 475000, 0, 0, 1000, 993, 999600000)
    assert view._context(usage, 9, 4400000000) == (
        " 🌀<b>∑999.6M (4.4B) · ctx</b> 302.3k/475k(64%)<b> · </b>💾99.3%<b> · </b>🗜️9"
    )


def test_goal_fills_rendered_limit_without_counting_html_or_escaped_entities():
    objective = "<&🎯" * 3000
    r = report(goal=GoalSummary(objective, "active", 0, None), summary=StatusSummary("идёт работа", "проверки"))
    text = render_status(r)
    visible = strip_html(text)
    assert len(text) > TEXT_LIMIT
    assert len(visible) == TEXT_LIMIT
    assert "идёт работа" in visible and "проверки" in visible
    assert "<&🎯" * 100 in visible and "…" in visible
    check_html(text)
