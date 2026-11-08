"""Subagent context, cache hit rate and compaction count come from each shown subagent's rollout, so they survive
a Bridge restart (an empty live map); the live value only fills in where the rollout has nothing."""

import asyncio

from cqb.codex.events import TokenUsageUpdated
from cqb.domain.enums import RunState as R
from cqb.domain.status import Descendants, UsageSnapshot
from cqb.services.live_state import LiveState
from cqb.services.status import StatusService
from cqb.telegram.status_view import render_status
from tg_stack import Clock, StatusSourceStub, snapshot


def run(coro):
    return asyncio.run(coro)


def usage(fill, window=200_000, total=1000, cached=870):
    return UsageSnapshot(fill, window, 10, 10, total, cached)


class Env:
    def __init__(self, tails=None, counts=None, live=None):
        self.src, self.clock = StatusSourceStub(), Clock()
        self.live = live or LiveState(4096, self.clock)
        self.tail_calls, self.count_calls = [], []
        tails, counts = tails or {}, counts or {}

        def tail(path):
            self.tail_calls.append(path)
            return tails.get(path)

        async def count(path):
            self.count_calls.append(path)
            if isinstance(counts.get(path), Exception):
                raise counts[path]
            return counts.get(path)

        self.svc = StatusService(self.src, self.live, tail, "s1", clock=self.clock, compactions=count)

    def card(self):
        return render_status(run(self.svc.collect("s1", "r1")))

    def agent_lines(self):
        return [x for x in self.card().splitlines() if x.startswith((" 🟢", " 🟡", " ⚪"))]


def family(**paths):
    root = snapshot("r1", path=paths.get("r1"))
    kids = tuple(snapshot(t, R.IDLE, parent="r1", nickname=t.upper(), path=p) for t, p in paths.items() if t != "r1")
    return root, kids


def test_subagent_lines_show_context_cache_and_compactions_from_the_rollouts_with_an_empty_live_map():
    env = Env(tails={"/h/sessions/r1": usage(160_000, 475_000, 1000, 994), "/h/sessions/a1": usage(70_000, 200_000, 1000, 870)},
              counts={"/h/sessions/r1": 5, "/h/sessions/a1": 2, "/h/sessions/a2": 0})
    root, kids = family(r1="/h/sessions/r1", a1="/h/sessions/a1", a2="/h/sessions/a2")
    env.src.threads["r1"], env.src.children["r1"] = root, kids
    assert len(env.live) == 0  # a fresh Bridge: no broadcast seen yet
    card = env.card()
    assert " <b>ctx</b> 160k/475k (34%)<b> · </b>💾99.4%<b> · </b>🗜️5\n" in card
    lines = env.agent_lines()
    assert " ⚪A1<b> · </b>gpt-6 low<b> · </b>ctx 35%<b> · </b>💾87%<b> · </b>1м назад<b> · </b>🗜️2" in lines  # the compaction count is appended
    a2 = next(x for x in lines if "A2<b>" in x)
    assert "ctx" not in a2 and "💾" not in a2 and "🗜️" not in a2  # no usage in its rollout, zero compactions: nothing


def test_the_rollout_wins_over_the_live_value_which_only_fills_in_where_there_is_no_rollout_usage():
    env = Env(tails={"/h/sessions/a1": usage(70_000)}, counts={})
    root, kids = family(a1="/h/sessions/a1", a2="/h/sessions/a2", a3=None)
    env.src.threads["r1"], env.src.children["r1"] = root, kids
    for tid in ("a1", "a2", "a3"):
        run(env.live.handle(TokenUsageUpdated(tid, UsageSnapshot(100_000, 200_000, 10, 10, 1000, 500))))
    lines = env.agent_lines()
    assert "</b>ctx 35%<b> · </b>💾87%" in next(x for x in lines if "A1<b>" in x)  # the rollout, not the live 50%
    assert "</b>ctx 50%<b> · </b>💾50%" in next(x for x in lines if "A2<b>" in x)  # unreadable rollout: the live value
    assert "</b>ctx 50%<b> · </b>💾50%" in next(x for x in lines if "A3<b>" in x)  # no path at all: the live value
    assert env.tail_calls.count("/h/sessions/a1") == 1 and "None" not in env.tail_calls  # only paths that exist are read


def test_all_subagent_usage_is_read_but_compactions_only_for_shown_lines():
    env = Env(counts={"/h/sessions/a1": RuntimeError("boom")})
    paths = {f"a{i}": f"/h/sessions/a{i}" for i in range(1, 10)}
    root, kids = family(**paths)
    env.src.threads["r1"], env.src.children["r1"] = root, kids
    card = env.card()
    assert len(env.tail_calls) == 9 and len(env.count_calls) == 6
    assert "🗜️" not in card and "Субагенты 9" in card


def test_token_totals_include_more_than_a_thousand_subagents_without_extra_display_lines():
    env = Env()
    env.src.threads["r1"] = snapshot("r1", path="/root")
    kids = tuple(snapshot(f"a{i}", R.IDLE, parent="r1", path=f"/a{i}") for i in range(1205))
    calls = []

    async def descendants(root, limit):
        calls.append(limit)
        return Descendants(kids[:limit], len(kids) > limit)

    env.src.descendants = descendants
    env.svc._tail_usage = lambda path: UsageSnapshot(100, 1000, 10, 0, total_tokens=100 if path == "/root" else 3)
    report = run(env.svc.collect("s1", "r1"))
    assert calls == [300, 10000]
    assert report.usage.total_tokens == 100 and report.subagent_tokens == 1205 * 3
    assert not report.subagent_tokens_incomplete and len(report.agents) == 6
    assert "🌀<b>∑100 (3.6k) · ctx</b>" in render_status(report)


def test_unreadable_or_truncated_subagent_totals_are_marked_incomplete():
    env = Env(tails={"/root": UsageSnapshot(100, 1000, 10, 0, total_tokens=500),
                     "/a1": UsageSnapshot(100, 1000, 10, 0, total_tokens=75)})
    root, kids = family(r1="/root", a1="/a1", a2="/missing")
    env.src.threads["r1"], env.src.children["r1"] = root, kids
    report = run(env.svc.collect("s1", "r1"))
    assert report.subagent_tokens == 75 and report.subagent_tokens_incomplete
    assert "🌀<b>∑500 (≥75) · ctx</b>" in render_status(report)


def test_archived_subagents_are_included_in_the_token_total_not_the_visible_list():
    env = Env(tails={"/root": UsageSnapshot(100, 1000, 10, 0, total_tokens=500),
                     "/a1": UsageSnapshot(100, 1000, 10, 0, total_tokens=75),
                     "/a2": UsageSnapshot(100, 1000, 10, 0, total_tokens=25)})
    root, kids = family(r1="/root", a1="/a1", a2="/a2")
    env.src.threads["r1"] = root
    env.src.children["r1"] = kids[:1]
    env.src.archived_children["r1"] = kids[1:]
    report = run(env.svc.collect("s1", "r1"))
    assert report.agents_total == 1 and len(report.agents) == 1
    assert report.subagent_tokens == 100 and not report.subagent_tokens_incomplete


def test_without_a_counter_or_with_unreadable_rollouts_the_card_has_no_compaction_marks():
    env = Env()
    env.svc._compactions = None
    root, kids = family(r1="/h/sessions/r1", a1="/h/sessions/a1")
    env.src.threads["r1"], env.src.children["r1"] = root, kids
    assert "🗜️" not in env.card() and "💾" not in env.card()
