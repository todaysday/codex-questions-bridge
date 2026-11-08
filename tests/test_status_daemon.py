"""Status reads and broadcasts over the real client against the fake daemon."""

import asyncio
import json

import pytest

import cqb.codex.appserver as appserver
from conftest import connected_client
from cqb.codex.appserver import Inbound
from cqb.codex.events import GoalChanged, ThreadStatusChanged, TokenUsageUpdated, normalize
from cqb.codex.rollout_tail import read_usage
from cqb.codex.status_reader import DaemonStatusReader, snapshot_from_wire
from cqb.codex.stream import dispatch
from cqb.domain.enums import RunState as R
from cqb.domain.status import ThreadSnapshot
from cqb.services.live_state import LiveState
from cqb.services.status import StatusService
from cqb.telegram.status_view import render_status
from fake_appserver import wait_until

NOW = 1_800_000_000
LAST = {"inputTokens": 40_000, "cachedInputTokens": 30_000, "outputTokens": 400, "reasoningOutputTokens": 0,
        "totalTokens": 40_400, "cacheWriteInputTokens": 0}


def arun(coro):
    return asyncio.run(coro)


def test_run_state_mapping_from_wire():
    def st(status):
        return normalize(Inbound("notification", "thread/status/changed", {"threadId": "t", "status": status}))
    assert st({"type": "active", "activeFlags": []}).state is R.WORKING
    assert st({"type": "active", "activeFlags": ["waitingOnUserInput"]}).state is R.WAITING_INPUT
    assert st({"type": "active", "activeFlags": ["waitingOnApproval"]}).state is R.WAITING_APPROVAL
    both = st({"type": "active", "activeFlags": ["waitingOnApproval", "waitingOnUserInput"]})
    assert both.state is R.WAITING_INPUT and both.waiting_on_approval and both.waiting_on_user_input
    assert st({"type": "idle"}).state is R.IDLE
    assert st({"type": "notLoaded"}).state is R.NOT_LOADED
    assert st({"type": "systemError"}).state is R.ERROR
    assert st({"type": "future"}).state is R.UNKNOWN


def test_new_notifications_are_normalized_and_bad_payloads_ignored():
    ev = normalize(Inbound("notification", "thread/tokenUsage/updated", {
        "threadId": "t1", "turnId": "u", "tokenUsage": {
            "last": LAST, "total": {"totalTokens": 10**7, "inputTokens": 8_000_000, "cachedInputTokens": 6_000_000},
            "modelContextWindow": 100_000}}))
    assert isinstance(ev, TokenUsageUpdated)
    assert (ev.usage.context_tokens, ev.usage.context_window, ev.usage.cache_permille) == (40_400, 100_000, 750)
    bare = normalize(Inbound("notification", "thread/tokenUsage/updated", {"threadId": "t", "tokenUsage": {"last": LAST}}))
    assert bare.usage.cache_permille is None  # the latest call alone says nothing honest about the cache
    assert normalize(Inbound("notification", "thread/goal/updated", {"threadId": "t", "goal": {}})) == GoalChanged("t", True)
    assert normalize(Inbound("notification", "thread/goal/cleared", {"threadId": "t"})) == GoalChanged("t", False)
    no_window = normalize(Inbound("notification", "thread/tokenUsage/updated", {
        "threadId": "t1", "tokenUsage": {"last": LAST, "modelContextWindow": None}}))
    assert no_window.usage.context_window is None and no_window.usage.fill_percent is None
    assert normalize(Inbound("notification", "thread/tokenUsage/updated", {"threadId": "t"})) is None
    assert normalize(Inbound("notification", "thread/tokenUsage/updated", {
        "threadId": "t", "tokenUsage": {"last": {"totalTokens": "x"}}})) is None


def test_snapshot_from_wire_tolerates_missing_fields():
    s = snapshot_from_wire({"id": "t", "status": {"type": "idle"}})
    assert (s.model, s.effort, s.parent_id, s.updated_at, s.path, s.name) == (None, None, None, 0, None, None)
    assert snapshot_from_wire({"status": {}}) is None
    s = snapshot_from_wire({"id": "t", "preview": "  many   words\nhere  ", "path": 5, "agentNickname": "Ada"})
    assert s.name == "many words here" and s.path is None and s.nickname == "Ada"


def test_cwd_comes_from_the_same_thread_read_and_odd_values_are_none():
    assert snapshot_from_wire({"id": "t", "cwd": "/srv/projects/fox"}).cwd == "/srv/projects/fox"
    for odd in (None, "", 5, ["/a"], {"p": "/a"}):
        assert snapshot_from_wire({"id": "t", "cwd": odd}).cwd is None
    assert snapshot_from_wire({"id": "t"}).cwd is None


def test_agent_path_and_depth_are_parsed_defensively():
    def spawn(source):
        return snapshot_from_wire({"id": "t", "source": source})
    s = spawn({"subAgent": {"thread_spawn": {"agent_path": "/root/a_b/c_d", "depth": 2, "agent_nickname": "N"}}})
    assert (s.agent_path, s.depth, s.title) == ("/root/a_b/c_d", 2, "C d")
    for odd in ("cli", None, 5, [], {"subAgent": "review"}, {"subAgent": {"thread_spawn": "x"}},
                {"subAgent": {"thread_spawn": {"agent_path": 7, "depth": "2"}}},
                {"subAgent": {"thread_spawn": {"agent_path": None, "depth": True}}}):
        s = spawn(odd)
        assert (s.agent_path, s.depth) == (None, None), odd
    assert snapshot_from_wire({"id": "t"}).agent_path is None


def test_title_derivation_and_fallbacks():
    def title(path, nick=None, role=None):
        return ThreadSnapshot("t", None, R.IDLE, None, None, nick, role, None, 0, None, agent_path=path).title
    assert title("/root/tl_008_oracle_run_lifecycle") == "Tl 008 oracle run lifecycle"
    assert title("/root/x/ex_007_admission/") == "Ex 007 admission"  # trailing slash
    assert title("/root/__a__b__") == "A b" and title("single") == "Single" and title("/root/éa_B") == "Éa B"
    assert title("/root/mixed_CaSe_Words") == "Mixed CaSe Words"  # only the first letter changes
    for odd in (None, "", "/", "///", "/ /  /", "/root/___"):
        assert title(odd, "Nick", "role") == "Nick" and title(odd, None, "role") == "role", odd
        assert title(odd) is None


def test_descendants_pages_up_to_the_cap():
    async def main():
        async with connected_client() as (client, fake):
            fake.add_thread("r1", NOW)
            for i in range(250):
                fake.add_thread(f"a{i:03}", NOW - i - 1, parent="r1", nickname=f"N{i}")
            reader = DaemonStatusReader(client)
            fake.calls.clear()
            kids = await reader.descendants("r1", 300)
            assert len(kids.threads) == 250 and not kids.more
            lists = [p for m, p in fake.calls if m == "thread/list"]
            assert [p["limit"] for p in lists] == [100, 100, 100]  # 250 rows: the third page ends the list
            assert all(p["ancestorThreadId"] == "r1" and p["useStateDbOnly"] is True for p in lists)
            assert [p.get("cursor") for p in lists] == [None, "100", "200"]
            for i in range(250, 400):
                fake.add_thread(f"a{i:03}", NOW - i - 1, parent="r1")
            fake.calls.clear()
            kids = await reader.descendants("r1", 300)
            assert len(kids.threads) == 300 and kids.more is True
            assert sum(m == "thread/list" for m in fake.methods()) == 3  # never a fourth page
            assert len({t.thread_id for t in kids.threads}) == 300
            assert [p["limit"] for m, p in fake.calls if m == "thread/list"] == [100, 100, 100]
            fake.calls.clear()
            kids = await reader.descendants("r1", 150)  # page sizes follow the remaining room
            assert len(kids.threads) == 150 and kids.more is True
            assert [p["limit"] for _, p in fake.calls] == [100, 50]
    arun(main())


def test_archived_descendants_are_fetched_separately():
    async def main():
        async with connected_client() as (client, fake):
            fake.add_thread("r1", NOW)
            fake.add_thread("active", NOW - 1, parent="r1")
            fake.add_thread("archived", NOW - 2, parent="r1", archived=True)
            reader = DaemonStatusReader(client)
            assert [t.thread_id for t in (await reader.descendants("r1", 100)).threads] == ["active"]
            assert [t.thread_id for t in (await reader.archived_descendants("r1", 100)).threads] == ["archived"]
    arun(main())


def test_descendants_second_page_failure_keeps_what_was_read():
    class Flaky:
        def __init__(self):
            self.n = 0

        async def call(self, method, params=None):
            self.n += 1
            if self.n == 2:
                raise RuntimeError("down")
            return {"data": [{"id": f"a{i}", "status": {"type": "idle"}} for i in range(100)], "nextCursor": "c"}
    kids = arun(DaemonStatusReader(Flaky()).descendants("r", 300))
    assert len(kids.threads) == 100 and kids.more is True

    class Dead:
        async def call(self, method, params=None):
            raise RuntimeError("down")
    assert arun(DaemonStatusReader(Dead()).descendants("r", 300)) is None


def test_reader_against_the_daemon():
    async def main():
        async with connected_client() as (client, fake):
            fake.add_thread("r1", NOW - 30, name="Root", model="gpt-6.1", effort="high", path="/x/r.jsonl")
            fake.add_thread("a1", NOW - 20, parent="r1", nickname="Ada", role="luna-low")
            fake.add_thread("a2", NOW - 10, parent="a1", nickname="Bob", agent_path="/root/ex_007/admission_check",
                            depth=2)  # grandchild: any depth
            fake.add_thread("other", NOW)
            fake.threads["a1"]["status"] = {"type": "active", "activeFlags": []}
            fake.turns["r1"] = [{"status": "inProgress", "startedAt": NOW - 90}, {"status": "completed", "startedAt": 1}]
            fake.turns["a1"] = [{"status": "completed", "startedAt": 5}]
            fake.goals["r1"] = {"objective": "  Ship\nthe   page  " + "x" * 100, "status": "active",
                                "tokenBudget": None, "tokensUsed": 5}
            api = appserver.ALLOWED_METHODS
            reader = DaemonStatusReader(client)
            root = await reader.thread("r1")
            assert (root.model, root.effort, root.path, root.state) == ("gpt-6.1", "high", "/x/r.jsonl", R.IDLE)
            assert await reader.thread("nope") is None
            kids = await reader.descendants("r1", 50)
            assert {t.thread_id for t in kids.threads} == {"a1", "a2"} and not kids.more
            assert {t.state for t in kids.threads} == {R.WORKING, R.IDLE}
            by_id = {t.thread_id: t for t in kids.threads}
            assert (by_id["a2"].agent_path, by_id["a2"].depth, by_id["a2"].title) == (
                "/root/ex_007/admission_check", 2, "Admission check")
            assert (by_id["a1"].agent_path, by_id["a1"].depth, by_id["a1"].title) == (None, 1, "Ada")
            assert (await reader.descendants("r1", 1)).more is True
            running = await reader.activity("r1", with_item=False)
            assert (running.turn_running, running.started_at, running.item) == (True, NOW - 90, None)
            finished = await reader.activity("a1", with_item=True)
            assert (finished.turn_running, finished.item) == (False, None)  # last turn finished: no item read
            assert await reader.activity("nope", with_item=True) is None
            goal = await reader.goal("r1")
            assert goal.objective.startswith("Ship the page xxx") and len(goal.objective) == 60 and goal.tokens_used == 5
            assert goal.token_budget is None and await reader.goal("a1") is None
            limits = await reader.rate_limits()
            assert (limits.used_percent, limits.resets_at, limits.window_minutes) == (30, 1_800_100_000, 10080)
            assert set(fake.methods()) <= api
    arun(main())


def test_rate_limits_prefer_the_weekly_window_and_tolerate_oddities():
    async def main():
        async with connected_client() as (client, fake):
            reader = DaemonStatusReader(client)
            fake.rate_limits = {"rateLimits": {"primary": {"usedPercent": 7, "windowDurationMins": 300, "resetsAt": 5},
                                               "secondary": {"usedPercent": 41, "windowDurationMins": 10080, "resetsAt": 9}}}
            assert (await reader.rate_limits()).used_percent == 41
            fake.rate_limits = {"rateLimits": {"primary": {"usedPercent": 3}, "secondary": None}}
            lim = await reader.rate_limits()
            assert (lim.used_percent, lim.resets_at, lim.window_minutes) == (3, None, None)
            fake.rate_limits = {"rateLimits": {"primary": None, "secondary": None}}
            assert await reader.rate_limits() is None
            fake.rate_limits = {}
            assert await reader.rate_limits() is None
    arun(main())


def test_reader_fails_soft_when_the_daemon_is_gone():
    async def main():
        async with connected_client() as (client, fake):
            reader = DaemonStatusReader(client)
            await fake.stop()
            await wait_until(lambda: not client.connected)
            assert await reader.thread("r1") is None and await reader.rate_limits() is None
            assert await reader.descendants("r1", 5) is None and await reader.goal("r1") is None
            fake.conns.clear()
    arun(main())


def test_live_map_is_fed_by_broadcasts_without_subscribing_and_whole_card_uses_only_whitelisted_reads(tmp_path):
    async def main():
        home = tmp_path
        (home / "sessions").mkdir()
        rollout = home / "sessions" / "r.jsonl"
        rollout.write_text(json.dumps({"type": "event_msg", "payload": {"type": "token_count", "info": {
            "last_token_usage": {"input_tokens": 50_000, "cached_input_tokens": 25_000, "total_tokens": 51_000},
            "total_token_usage": {"input_tokens": 400_000, "cached_input_tokens": 396_000, "total_tokens": 1},
            "model_context_window": 200_000}}}) + "\n")
        async with connected_client() as (client, fake):
            fake.add_thread("r1", NOW - 60, name="Root", path=str(rollout))
            fake.add_thread("a1", NOW - 20, parent="r1", nickname="Ada", model="luna", effort="low")
            live = LiveState()
            pump = asyncio.create_task(dispatch(client, [live.handle]))
            svc = StatusService(DaemonStatusReader(client), live, lambda p: read_usage(p, str(home)), "local")
            try:
                await fake.set_status("a1", [])  # active
                await fake.token_usage("a1", LAST, window=100_000)
                await fake.broadcast("thread/goal/updated", {"threadId": "r1", "goal": {"objective": "secret text"}})
                await wait_until(lambda: (e := live.get("a1")) is not None and e.usage is not None
                                 and (g := live.get("r1")) is not None and g.has_goal)
                fake.goals["r1"] = {"objective": "Ship", "status": "active", "tokenBudget": 100, "tokensUsed": 10}
                fake.turns["r1"] = [{"id": "t1", "status": "inProgress", "startedAt": int(asyncio.get_running_loop().time())}]
                report = await svc.collect("local", "r1")
                text = render_status(report)
                assert " <b>∑1 (404k) · ctx</b> 51k/200k (26%) · 💾 99.0%" in text  # root: rollout tail
                assert " 🟢 Ada · luna·low · ctx 40%" in text  # subagent: broadcast usage
                assert "🎯 Ship · 10/100\n" in text and "📈" not in text
                assert set(fake.methods()) <= {"thread/read", "thread/list", "thread/goal/get",
                                               "account/rateLimits/read", "thread/turns/list"}
                assert not any(c.subscribed for c in fake.conns)
                assert not {"thread/resume", "thread/unsubscribe"} & set(fake.methods())
            finally:
                pump.cancel()
                await asyncio.gather(pump, return_exceptions=True)
    arun(main())


@pytest.mark.parametrize("method", ["account/rateLimitResetCredit/consume", "thread/goal/set", "turn/start"])
def test_forbidden_methods_never_reach_the_daemon(method):
    async def main():
        async with connected_client() as (client, fake):
            with pytest.raises(appserver.MethodNotAllowed):
                await client.call(method, {"threadId": "t"})
            assert fake.calls == []
            assert await DaemonStatusReader(client).thread("t") is None  # reader never uses them either
    arun(main())
