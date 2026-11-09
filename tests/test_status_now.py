"""'Now' lines: read-only reads of the newest item, the item-kind mapping, redaction, and that no text ever leaks."""

import asyncio
import dataclasses

import pytest

from conftest import connected_client
from cqb.codex.status_reader import DaemonStatusReader, brief_from_item
from cqb.domain.enums import ItemKind as K, RunState as R
from cqb.domain.status import Activity, ItemBrief
from cqb.services.live_state import LiveState
from cqb.services.status import DETAIL_MAX, NOW_FETCHES, StatusService
from cqb.telegram.status_view import render_status
from tg_stack import NOW, Clock, StatusSourceStub, snapshot

SECRET_TEXT = "TOPSECRET-message-text-4711"


def run(coro):
    return asyncio.run(coro)


def make():
    src, clock = StatusSourceStub(), Clock()
    return StatusService(src, LiveState(4096, clock), lambda path: None, "s1", clock=clock), src


def card_of(svc, root="r1"):
    return render_status(run(svc.collect("s1", root)))


def now_of(svc, root="r1"):
    """The `💭сейчас` fragment of the status line (the root's second card line); `💭—` when the line has none."""
    line = card_of(svc, root).splitlines()[1]
    return "💭" + line.partition(" 💭")[2] if " 💭" in line else "💭—"


def running(item, started=NOW - 60):
    return Activity(True, started, None, item)


# --- wire item -> ItemBrief (reader) -------------------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    ({"type": "commandExecution", "status": "inProgress", "command": "/bin/bash -lc \"pytest -q tests\""},
     ItemBrief(K.COMMAND, False, "pytest -q tests")),
    ({"type": "commandExecution", "status": "completed", "command": "ls   -la\n/tmp"}, ItemBrief(K.COMMAND, True, "ls -la /tmp")),
    ({"type": "commandExecution", "status": "failed", "command": None}, ItemBrief(K.COMMAND, True, None)),
    ({"type": "fileChange", "status": "inProgress", "changes": [{"path": "a"}, {"path": "b"}]},
     ItemBrief(K.FILES, False, None, 2)),
    ({"type": "mcpToolCall", "status": "inProgress", "server": "cqb", "tool": "list_agents"},
     ItemBrief(K.TOOL, False, "cqb.list_agents")),
    ({"type": "dynamicToolCall", "status": "completed", "namespace": "ns", "tool": "t"}, ItemBrief(K.TOOL, True, "ns.t")),
    ({"type": "dynamicToolCall", "status": "completed", "tool": "t"}, ItemBrief(K.TOOL, True, "t")),
    ({"type": "agentMessage", "text": SECRET_TEXT, "phase": "final"}, ItemBrief(K.MESSAGE, False)),
    ({"type": "reasoning", "summary": [SECRET_TEXT], "content": [SECRET_TEXT]}, ItemBrief(K.REASONING, False)),
    ({"type": "collabAgentToolCall", "status": "inProgress", "tool": "spawnAgent", "prompt": SECRET_TEXT},
     ItemBrief(K.SPAWN, False, "spawnAgent")),
    ({"type": "collabAgentToolCall", "status": "completed", "tool": "wait"}, ItemBrief(K.COLLAB, True, "wait")),
    ({"type": "webSearch", "query": SECRET_TEXT}, ItemBrief(K.WEB, False)),
    ({"type": "userMessage", "content": [{"type": "text", "text": SECRET_TEXT}]}, ItemBrief(K.OTHER, False)),
    ({"type": "somethingNew"}, ItemBrief(K.OTHER, False)),
])
def test_item_mapping_keeps_kind_and_short_detail_but_never_text(raw, expected):
    brief = brief_from_item(raw)
    assert brief == expected and SECRET_TEXT not in repr(brief)


def test_malformed_items_are_none_and_a_long_command_head_is_bounded():
    assert brief_from_item(None) is None and brief_from_item("x") is None
    assert len(brief_from_item({"type": "commandExecution", "command": "x" * 5000}).detail) == 200


# --- the reads (fake daemon): two cheap calls, never the whole turn, never resume --------------

def test_activity_reads_the_newest_item_with_two_cheap_calls():
    async def main():
        async with connected_client() as (client, fake):
            fake.add_thread("r1", NOW)
            fake.turns["r1"] = [{"id": "t9", "status": "inProgress", "startedAt": NOW - 30}]
            fake.items["r1"] = [
                {"turnId": "t9", "item": {"type": "agentMessage", "id": "a", "text": SECRET_TEXT}},
                {"turnId": "t9", "item": {"type": "commandExecution", "id": "c", "status": "inProgress",
                                          "command": "make test", "aggregatedOutput": SECRET_TEXT}},
            ]
            reader = DaemonStatusReader(client)
            got = await reader.activity("r1", with_item=True)
            assert got == Activity(True, NOW - 30, None, ItemBrief(K.COMMAND, False, "make test"))
            turns, items = fake.calls
            assert turns == ("thread/turns/list", {"threadId": "r1", "limit": 1, "itemsView": "notLoaded"})
            assert items == ("thread/items/list", {"threadId": "r1", "turnId": "t9", "limit": 1, "sortDirection": "desc"})
            fake.calls.clear()
            assert (await reader.activity("r1", with_item=False)).item is None  # a waiting thread: the turn only
            assert fake.methods() == ["thread/turns/list"]
            fake.turns["r1"] = [{"id": "t9", "status": "completed", "startedAt": 1, "completedAt": NOW - 90}]
            fake.calls.clear()
            done = await reader.activity("r1", with_item=True)
            assert (done.turn_running, done.completed_at, done.item) == (False, NOW - 90, None)
            assert fake.methods() == ["thread/turns/list"]  # idle: no item read
            assert await reader.activity("none", with_item=True) is None
    run(main())


# --- the card ------------------------------------------------------------------------------

@pytest.mark.parametrize("item, phrase", [
    (ItemBrief(K.COMMAND, False, "pytest -q"), "💭выполняет: pytest -q"),
    (ItemBrief(K.COMMAND, True, "pytest -q"), "💭выполнил: pytest -q"),
    (ItemBrief(K.FILES, False, None, 3), "💭правит файлы (3)"),
    (ItemBrief(K.FILES, True, None, 1), "💭правил файлы (1)"),
    (ItemBrief(K.TOOL, False, "cqb.list_agents"), "💭вызывает cqb.list_agents"),
    (ItemBrief(K.TOOL, True, "cqb.list_agents"), "💭вызывал cqb.list_agents"),
    (ItemBrief(K.MESSAGE, False), "💭пишет ответ"),
    (ItemBrief(K.REASONING, False), "💭думает"),
    (ItemBrief(K.SPAWN, False, "spawnAgent"), "💭запускает субагента"),
    (ItemBrief(K.COLLAB, False, "wait"), "💭ждёт субагентов"),
    (ItemBrief(K.COLLAB, False, "sendInput"), "💭общается с субагентами"),
    (ItemBrief(K.WEB, False), "💭ищет в сети"),
    (ItemBrief(K.OTHER, False), "💭—"),
    (ItemBrief(K.TOOL, False, None), "💭—"),
    (None, "💭—"),
])
def test_root_now_line_per_item_kind(item, phrase):
    svc, src = make()
    src.threads["r1"] = snapshot("r1", R.WORKING, updated=NOW - 5)
    src.activities["r1"] = running(item)
    assert now_of(svc) == phrase


def test_waiting_states_idle_age_and_missing_data():
    svc, src = make()
    for state, phrase in ((R.WAITING_INPUT, "💭ждёт ответа"), (R.WAITING_APPROVAL, "💭ждёт одобрения")):
        src.threads["r1"] = snapshot("r1", state, updated=NOW - 5)
        src.activities["r1"] = running(ItemBrief(K.COMMAND, False, "never shown"))
        assert now_of(svc) == phrase
    src.threads["r1"] = snapshot("r1", R.IDLE, updated=NOW - 5)
    src.activities["r1"] = Activity(False, NOW - 900, NOW - 720, None)
    assert now_of(svc) == "💭ход завершён 12м назад"
    src.activities["r1"] = Activity(False, None, None, None)
    assert now_of(svc) == "💭—"
    del src.activities["r1"]  # the daemon could not say
    assert now_of(svc) == "💭—"


def test_waiting_flag_costs_no_item_read_and_only_working_threads_read_items():
    svc, src = make()
    src.threads["r1"] = snapshot("r1", R.WAITING_INPUT, updated=NOW - 5)
    card_of(svc)
    assert src.item_reads == []
    src.threads["r1"] = snapshot("r1", R.WORKING, updated=NOW - 5)
    card_of(svc)
    assert src.item_reads == ["r1"]


def test_command_head_is_redacted_then_cut_to_eighty_characters():
    svc, src = make()
    src.threads["r1"] = snapshot("r1", R.WORKING, updated=NOW - 5)
    token = "ghp_" + "a" * 36
    src.activities["r1"] = running(ItemBrief(K.COMMAND, False, f"curl -H 'Authorization: Bearer {token}' https://x.test"))
    line = now_of(svc)
    assert token not in line and "[REDACTED]" in line
    src.activities["r1"] = running(ItemBrief(K.COMMAND, False, "echo " + "word " * 60))
    line = now_of(svc)
    detail = line.removeprefix("💭выполняет: ")
    assert len(detail) == DETAIL_MAX and detail.endswith("…")
    # a secret that straddles the cut is redacted before the cut, never half shown
    src.activities["r1"] = running(ItemBrief(K.COMMAND, False, "x" * 70 + " password=hunter2hunter2 tail"))
    assert "hunter2" not in card_of(svc)


def test_no_message_text_or_output_reaches_the_card_through_the_real_reader():
    async def main():
        async with connected_client() as (client, fake):
            fake.add_thread("r1", NOW - 5, name="Root")
            fake.add_thread("a1", NOW - 5, parent="r1", nickname="Ada")
            fake.threads["r1"]["status"] = {"type": "active", "activeFlags": []}
            fake.threads["a1"]["status"] = {"type": "active", "activeFlags": []}
            for tid, kind in (("r1", "agentMessage"), ("a1", "reasoning")):
                fake.turns[tid] = [{"id": "t", "status": "inProgress", "startedAt": NOW - 10}]
                fake.items[tid] = [{"turnId": "t", "item": {
                    "type": kind, "id": "i", "text": SECRET_TEXT, "summary": [SECRET_TEXT], "content": [SECRET_TEXT]}}]
            svc = StatusService(DaemonStatusReader(client), LiveState(), lambda p: None, "local", clock=lambda: NOW)
            text = render_status(await svc.collect("local", "r1"))
            assert "💭пишет ответ" in text and "<b> · </b>думает" in text and SECRET_TEXT not in text
            assert set(fake.methods()) <= {"thread/read", "thread/list", "thread/goal/get", "account/rateLimits/read",
                                           "thread/turns/list", "thread/items/list"}
    run(main())


def test_subagent_lines_get_now_data_concurrently_with_a_small_semaphore_and_only_for_lines_shown():
    svc, src = make()
    src.threads["r1"] = snapshot("r1", R.IDLE, updated=NOW - 5)
    kids = [snapshot(f"a{i}", R.WORKING if i < 10 else R.WAITING_INPUT, updated=NOW - 100 - i, parent="r1",
                     nickname=f"A{i}") for i in range(12)]
    src.children["r1"] = tuple(kids)
    live = {"now": 0, "peak": 0}

    async def slow(thread_id, with_item):
        live["now"] += 1
        live["peak"] = max(live["peak"], live["now"])
        await asyncio.sleep(0.01)
        live["now"] -= 1
        src.calls.append("turn")
        return running(ItemBrief(K.REASONING, False))

    src.activity = slow
    report = run(svc.collect("s1", "r1"))
    assert len(report.agents) == 8
    assert 1 < live["peak"] <= NOW_FETCHES
    assert src.calls.count("turn") <= 1 + 8  # the root and the lines shown; the other four are never read
    assert render_status(report).count("<b> · </b>думает") >= 1


def test_waiting_subagent_line_needs_no_read_and_says_what_it_waits_for():
    svc, src = make()
    src.threads["r1"] = snapshot("r1", R.IDLE, updated=NOW - 5)
    src.children["r1"] = (snapshot("a1", R.WAITING_APPROVAL, parent="r1", nickname="Ada"),)
    reads = []
    real = src.activity

    async def spy(thread_id, with_item):
        reads.append(thread_id)
        return await real(thread_id, with_item)

    src.activity = spy
    text = card_of(svc)
    assert "<b> · </b>ждёт одобрения" in text and reads == ["r1"]


def test_item_brief_dto_is_immutable_and_slotted():
    brief = ItemBrief(K.COMMAND, False, "x")
    with pytest.raises(dataclasses.FrozenInstanceError):
        brief.detail = "y"  # type: ignore[misc]
    assert not hasattr(brief, "__dict__")
