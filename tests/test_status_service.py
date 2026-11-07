"""StatusService, LiveState and the card view: pure logic over ports, no daemon and no network."""

import asyncio
import dataclasses

import pytest

from cqb.codex.events import Connected, Disconnected, GoalChanged, ThreadStatusChanged, TokenUsageUpdated
from cqb.domain.enums import RunState as R
from cqb.domain.status import Activity, AgentLine, GoalSummary, RateLimits, StatusReport, StatusSummary, UsageSnapshot
from cqb.services import status as status_mod
from cqb.services.live_state import LiveState
from cqb.services.status import StatusService, StatusUnavailable
from cqb.telegram.fmt import TEXT_LIMIT
from cqb.telegram.status_view import ICON, render_status, tokens
from tg_stack import NOW, Clock, StatusSourceStub, snapshot


def run(coro):
    return asyncio.run(coro)


def make(usage=None, narrator=None, max_live=4096):
    src, clock = StatusSourceStub(), Clock()
    live = LiveState(max_live, clock)
    svc = StatusService(src, live, lambda path: usage, "s1", narrator, clock)
    return svc, src, live, clock


def collect(svc, root="r1"):
    return run(svc.collect("s1", root))


@pytest.mark.parametrize("state,icon,word", [
    (R.WORKING, "🟢", "работает"), (R.WAITING_INPUT, "🟡", "ждёт ответа"),
    (R.WAITING_APPROVAL, "🟡", "ждёт одобрения"), (R.IDLE, "⚪", "простаивает"),
    (R.NOT_LOADED, "💤", "не загружен"), (R.ERROR, "🔴", "ошибка"),
])
def test_root_state_icon_and_word(state, icon, word):
    svc, src, *_ = make()
    src.threads["r1"] = snapshot("r1", state, updated=NOW - 5)
    text = render_status(collect(svc))
    assert f"{icon} <b>{word}</b>" in text.splitlines()[1]


@pytest.mark.parametrize("quiet,stalled", [(0, False), (599, False), (600, False), (601, True), (3600, True)])
def test_stalled_boundaries_use_updated_at(quiet, stalled):
    svc, src, *_ = make()
    src.threads["r1"] = snapshot("r1", R.WORKING, updated=NOW - quiet)
    report = collect(svc)
    assert (report.state is R.STALLED) == stalled
    if stalled:
        assert report.quiet_minutes == quiet // 60
        assert f"🟠 <b>тихо {'1ч' if quiet == 3600 else f'{quiet // 60}м'}</b>" in render_status(report)


def test_stalled_uses_the_latest_broadcast_and_never_applies_to_waiting_or_idle():
    svc, src, live, clock = make()
    src.threads["r1"] = snapshot("r1", R.WORKING, updated=NOW - 1000)
    clock.now = NOW - 100
    run(live.handle(ThreadStatusChanged("r1", False, False, R.WORKING)))  # a broadcast 100 s ago
    clock.now = NOW
    assert collect(svc).state is R.WORKING
    for state in (R.WAITING_INPUT, R.WAITING_APPROVAL, R.IDLE):
        src.threads[state.value] = snapshot(state.value, state, updated=NOW - 5000)
        assert collect(svc, state.value).state is state


def test_turn_elapsed_only_for_an_active_thread_and_never_negative():
    svc, src, *_ = make()
    src.turn_start = NOW - 754
    src.threads["r1"] = snapshot("r1", R.WORKING, updated=NOW - 5)
    report = collect(svc)
    assert report.turn_elapsed_s == 754 and "ход 12м" in render_status(report)
    src.threads["r2"] = snapshot("r2", R.IDLE)
    src.activities["r2"] = Activity(False, NOW - 900, NOW - 800, None)
    report = collect(svc, "r2")
    assert report.turn_elapsed_s is None and "ход" not in render_status(report).splitlines()[1]  # finished turn: no elapsed
    src.turn_start = NOW + 50  # clock skew
    assert collect(svc, "r1").turn_elapsed_s == 0


def test_context_and_cache_math_use_last_call_only():
    usage = UsageSnapshot(context_tokens=29_500, context_window=475_000, input_tokens=29_000, cached_input_tokens=23_200)
    svc, src, *_ = make(usage)
    src.threads["r1"] = snapshot("r1", path="/p")
    report = collect(svc)
    assert report.usage.fill_percent == 6 and report.usage.cache_percent == 80
    assert "🧠 ctx 29.5k/475k (6%) · 💾 кэш 80%" in render_status(report)


def test_usage_edge_cases():
    assert UsageSnapshot(10, None, 0, 0).fill_percent is None
    assert UsageSnapshot(10, 0, 0, 0).fill_percent is None
    assert UsageSnapshot(10, 100, 0, 0).cache_percent is None
    assert UsageSnapshot(100, 100, 50, 50).fill_percent == 100


def test_tokens_formatting():
    assert [tokens(n) for n in (0, 999, 1000, 29_500, 475_000, 1_000_000, 1_250_000)] == [
        "0", "999", "1k", "29.5k", "475k", "1M", "1.2M"]


def test_missing_data_is_a_dash_never_invented():
    svc, src, *_ = make(usage=None)
    src.threads["r1"] = snapshot("r1", R.WORKING, updated=NOW - 5, model=None, effort=None, name=None)
    src.limits = None
    src.turn_start = None
    text = render_status(collect(svc))
    lines = text.splitlines()
    assert lines[0] == "📊 <b>—</b>"
    assert lines[1] == "🟢 <b>работает</b> · — · ход — · 🕒 только что"
    assert "🧠 ctx — · 💾 кэш —" in text and "📈 лимит —" in text and "снимок" not in text
    assert "🎯" not in text  # no goal: the line is skipped, not dashed


def test_unreadable_children_are_a_dash_but_no_children_is_none():
    svc, src, *_ = make()
    src.threads["r1"] = snapshot("r1")
    assert "🤖 <b>Субагенты</b> · нет" in render_status(collect(svc))
    src.fail = False
    report = dataclasses.replace(collect(svc, "r1"), agents_total=None)
    assert "🤖 <b>Субагенты</b> · —" in render_status(report)


def test_root_unreadable_raises_and_unknown_source_is_refused():
    svc, src, *_ = make()
    with pytest.raises(StatusUnavailable):
        collect(svc, "missing")
    with pytest.raises(StatusUnavailable):
        run(svc.collect("other-source", "r1"))
    src.threads["r1"] = snapshot("r1")
    src.fail = True
    with pytest.raises(StatusUnavailable):
        collect(svc)


def test_collect_times_out_instead_of_hanging(monkeypatch):
    svc, src, *_ = make()

    async def hang(_):
        await asyncio.sleep(10)
    src.thread = hang
    monkeypatch.setattr(status_mod, "COLLECT_TIMEOUT", 0.05)
    with pytest.raises(StatusUnavailable):
        collect(svc)


def children(src, n, **kw):
    src.children["r1"] = tuple(
        snapshot(f"a{i}", kw.get("state", R.IDLE), updated=NOW - 100 - i, parent="r1", nickname=f"Agent{i}",
                 model="luna", effort="high", role="luna-high") for i in range(n))


def test_subagent_counting_and_the_six_line_cap():
    svc, src, live, _ = make()
    src.threads["r1"] = snapshot("r1")
    children(src, 11)
    kids = list(src.children["r1"])
    kids[0] = dataclasses.replace(kids[0], state=R.WORKING)
    kids[1] = dataclasses.replace(kids[1], state=R.WAITING_INPUT)
    kids[2] = dataclasses.replace(kids[2], state=R.WAITING_APPROVAL)
    kids[10] = dataclasses.replace(kids[10], state=R.WORKING)  # oldest, still ranked before idle ones
    src.children["r1"] = tuple(kids)
    report = collect(svc)
    assert (report.agents_total, report.agents_working, report.agents_waiting, report.agents_idle) == (11, 2, 2, 7)
    assert len(report.agents) == 6
    assert [a.state for a in report.agents[:4]] == [R.WORKING, R.WORKING, R.WAITING_INPUT, R.WAITING_APPROVAL]
    text = render_status(report)
    assert "🤖 <b>Субагенты 11</b> · 🟢 2 · 🟡 2 · ⚪ 7\n" in text
    assert sum(x.startswith((" 🟢", " 🟡", " ⚪")) for x in text.splitlines()) == 6
    assert "\n … и ещё 5\n" in text and "+" not in text.split("Субагенты")[1].split("📝")[0]
    assert len(text.splitlines()) <= 20


def test_more_pages_than_fetched_is_marked():
    svc, src, *_ = make()
    src.threads["r1"] = snapshot("r1")
    children(src, 9)
    src.more = True
    text = render_status(collect(svc))
    assert "Субагенты 9+</b>" in text and "\n … и ещё 3\n" in text and "ещё+" not in text


def test_agent_line_label_context_from_broadcast_and_dashes():
    svc, src, live, _ = make()
    src.threads["r1"] = snapshot("r1")
    src.children["r1"] = (
        snapshot("a1", R.WORKING, parent="r1", nickname="Lovelace", model="luna", effort="low"),
        snapshot("a2", parent="r1", role="luna-high", model=None, effort=None),
        snapshot("abcdefgh99", parent="r1"),
    )
    run(live.handle(TokenUsageUpdated("a1", UsageSnapshot(50_000, 200_000, 49_000, 0))))
    lines = render_status(collect(svc)).splitlines()
    agent_lines = [x for x in lines if x.startswith((" 🟢", " 🟡", " ⚪"))]
    assert any(x.startswith(" 🟢 Lovelace · luna·low · ctx 25%") for x in agent_lines)
    assert any(x.startswith(" ⚪ luna-high · ") and "ctx" not in x and "—" not in x.split(" · ")[1] for x in agent_lines)
    assert any(x.startswith(" ⚪ — · gpt-6·low · ") for x in agent_lines)  # no path, nickname or role: a dash, never an id


def test_goal_line_present_and_skipped_when_live_says_none():
    svc, src, live, _ = make()
    src.threads["r1"] = snapshot("r1")
    src.goals["r1"] = GoalSummary("Ship the profile page", "active", 12_300, 50_000)
    assert "🎯 Ship the profile page · 12.3k/50k\n" in render_status(collect(svc))
    run(live.handle(GoalChanged("r1", False)))
    calls = src.calls.count("goal")
    assert collect(svc).goal is None and src.calls.count("goal") == calls  # no daemon call needed
    run(live.handle(GoalChanged("r1", True)))
    assert collect(svc).goal is not None


def test_rate_limits_are_cached_for_thirty_seconds():
    svc, src, _, clock = make()
    src.threads["r1"] = snapshot("r1")
    collect(svc)
    clock.now += 10
    collect(svc)
    assert src.calls.count("limits") == 1
    clock.now += 21
    collect(svc)
    assert src.calls.count("limits") == 2
    src.limits = None
    clock.now += 31
    assert render_status(collect(svc)).endswith("📈 лимит —")


def test_limits_rendering_weekly_primary_only():
    svc, src, *_ = make()
    src.threads["r1"] = snapshot("r1")
    text = render_status(collect(svc))
    assert "📈 лимит 30% нед. · сброс " in text and text.endswith(" UTC") and "снимок" not in text
    src.limits = RateLimits(55, None, 300)
    svc._limits = None
    assert render_status(collect(svc)).endswith("📈 лимит 55% 5ч")


def test_every_snapshot_reads_fresh_data():
    svc, src, *_ = make()
    src.threads["r1"] = snapshot("r1")
    collect(svc)
    collect(svc)
    assert src.calls.count("thread") == 2  # a static message per request: no cached report is ever re-posted


def test_card_never_exceeds_the_telegram_limit():
    long = "я" * 5000
    agents = tuple(AgentLine(long, long, long, R.WORKING, 99, NOW) for _ in range(8))
    report = StatusReport(
        NOW, long, R.STALLED, 99, long, long, 10**6, UsageSnapshot(10**9, 10**9, 10**9, 10**9), NOW,
        GoalSummary(long, long, 10**9, 10**9), 10**4, True, 3, 3, 4, agents, RateLimits(100, NOW, 10080), None, StatusSummary(long, long))
    text = render_status(report)
    assert len(text) <= TEXT_LIMIT and "\n 🟢" in text
    assert set(ICON) == set(R)


def test_live_map_is_bounded_oldest_first_and_cleared_on_reconnect():
    clock = Clock()
    live = LiveState(3, clock)
    for i in range(5):
        clock.now = NOW + i
        run(live.handle(ThreadStatusChanged(f"t{i}", False, False, R.IDLE)))
    assert len(live) == 3 and live.get("t0") is None and live.get("t1") is None and live.get("t4").seen_at == NOW + 4
    run(live.handle(ThreadStatusChanged("t2", False, False, R.WORKING)))  # refreshed: now the newest
    run(live.handle(ThreadStatusChanged("t5", False, False, R.IDLE)))
    assert live.get("t2").state is R.WORKING and live.get("t3") is None
    run(live.handle(TokenUsageUpdated("t5", UsageSnapshot(1, 2, 3, 4))))
    assert live.get("t5").usage.context_tokens == 1 and live.get("t5").state is R.IDLE  # fields merge
    run(live.handle(Disconnected()))
    assert len(live) == 0
    run(live.handle(ThreadStatusChanged("t1", False, False, R.IDLE)))
    run(live.handle(Connected()))
    assert len(live) == 0


def header(report):
    return next(x for x in render_status(report).splitlines() if x.startswith("🤖"))


def test_zero_counts_are_not_shown_in_the_header():
    svc, src, *_ = make()
    src.threads["r1"] = snapshot("r1")
    children(src, 4)
    assert header(collect(svc)) == "🤖 <b>Субагенты 4</b> · ⚪ 4"
    kids = [dataclasses.replace(k, state=R.NOT_LOADED) for k in src.children["r1"]]
    kids[0] = dataclasses.replace(kids[0], state=R.WORKING)
    src.children["r1"] = tuple(kids)
    assert header(collect(svc)) == "🤖 <b>Субагенты 4</b> · 🟢 1 · ⚪ 3"
    src.children["r1"] = tuple(dataclasses.replace(k, state=R.WAITING_INPUT) for k in kids)
    assert header(collect(svc)) == "🤖 <b>Субагенты 4</b> · 🟡 4"
    src.children["r1"] = ()
    assert header(collect(svc)) == "🤖 <b>Субагенты</b> · нет"


def test_the_cap_counts_and_shows_the_plus_only_in_the_header():
    svc, src, *_ = make()
    src.threads["r1"] = snapshot("r1")
    children(src, 300)
    src.more = True
    report = collect(svc)
    assert (report.agents_total, report.agents_more) == (300, True)
    assert header(report) == "🤖 <b>Субагенты 300+</b> · ⚪ 300"
    assert "\n … и ещё 294\n" in render_status(report)


def test_the_six_lines_are_working_then_waiting_then_most_recent():
    svc, src, *_ = make()
    src.threads["r1"] = snapshot("r1")
    kids = [snapshot(f"i{i}", R.IDLE, updated=NOW - 10 * (i + 1), parent="r1", nickname=f"Idle{i}") for i in range(6)]
    kids.append(snapshot("w_old", R.WORKING, updated=NOW - 5000, parent="r1", nickname="WorkOld"))
    kids.append(snapshot("w_new", R.WORKING, updated=NOW - 20, parent="r1", nickname="WorkNew"))
    kids.append(snapshot("q", R.WAITING_APPROVAL, updated=NOW - 4000, parent="r1", nickname="Waits"))
    src.children["r1"] = tuple(kids)
    labels = [a.label for a in collect(svc).agents]
    assert labels == ["WorkNew", "WorkOld", "Waits", "Idle0", "Idle1", "Idle2"]


def test_title_from_the_agent_path_and_the_depth_prefix():
    svc, src, *_ = make()
    src.threads["r1"] = snapshot("r1")
    src.children["r1"] = (
        snapshot("a1", R.WORKING, parent="r1", nickname="Faraday the 2nd", role="worker", depth=1,
                 agent_path="/root/tl_008_oracle_known_lifecycle"),
        snapshot("a2", R.WORKING, parent="r1", nickname="Ada", depth=2,
                 agent_path="/root/tl_008_oracle_known_lifecycle/oracle_run_lifecycle"),
        snapshot("a3", R.WORKING, parent="r1", nickname="Bob", role="worker", agent_path="//", depth=0),
    )
    text = render_status(collect(svc))
    assert "\n 🟢 Tl 008 oracle known lifecycle · " in text
    assert "\n ↳ 🟢 Oracle run lifecycle · " in text
    assert "\n 🟢 Bob · " in text and "↳ Bob" not in text and "↳ 🟢 Tl" not in text
    assert "/root" not in text and "Faraday" not in text


def test_long_title_is_clipped():
    svc, src, *_ = make()
    src.threads["r1"] = snapshot("r1")
    src.children["r1"] = (snapshot("a1", parent="r1", agent_path="/root/" + "word_" * 20),)
    line = next(x for x in render_status(collect(svc)).splitlines() if x.startswith(" ⚪"))
    assert "Word word" in line and "… ·" in line and len(line.split(" · ")[0]) <= 3 + 32
