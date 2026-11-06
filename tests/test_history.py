import asyncio

import pytest

from cqb.codex.history import HistoryReader, HistoryUnavailable, event_from_entry, make_ref, split_ref
from conftest import connected_client


def entry(kind, turn="t1", item_id="i1", **fields):
    return {"turnId": turn, "startedAtMs": 10, "completedAtMs": 20, "item": {"type": kind, "id": item_id, **fields}}


def ev(kind, **fields):
    return event_from_entry("th", entry(kind, **fields))


def test_refs_round_trip_even_when_the_item_id_has_a_colon():
    assert split_ref(make_ref("th", "t1", "call:abc:1")) == ("th", "t1", "call:abc:1")
    assert split_ref("a:b") is None and split_ref("a::c") is None and split_ref("") is None


def test_normalization_of_each_kind_is_short_and_has_no_tool_output():
    e = ev("userMessage", content=[{"type": "text", "text": "hi"}, {"type": "image", "url": "x"}])
    assert (e.kind, e.text, e.ref, e.at_ms) == ("userMessage", "hi\n[image]", "th:t1:i1", 20)
    assert ev("agentMessage", text="done", phase="final").status == "final"
    e = ev("commandExecution", command="pytest -q", status="failed", exitCode=1, aggregatedOutput="BOOM " * 1000)
    assert e.text == "$ pytest -q  (exit 1)" and e.status == "failed"  # output only in the detail view
    assert ev("fileChange", changes=[{"path": "a.py"}, {"path": "b.py"}], status="completed").text == "files: a.py, b.py"
    assert ev("mcpToolCall", server="s", tool="t", arguments={"a": 1}, result={"big": "x" * 999}).text == 's/t {"a": 1}'
    assert ev("webSearch", query="q").text == "search: q"
    assert ev("reasoning", summary=["s1", "s2"], content=["raw thoughts"]).text == "s1 s2"  # raw reasoning never
    assert ev("collabAgentToolCall", tool="spawnAgent", receiverThreadIds=["c1"]).text == "spawnAgent -> c1"
    assert ev("contextCompaction").text == "context compacted"
    assert ev("somethingNew").text == "somethingNew"
    assert ev("hookPrompt", fragments=[]) is None


def test_detail_view_adds_a_capped_output_tail():
    e = event_from_entry("th", entry("commandExecution", command="make", aggregatedOutput="x " * 5000), max_chars=100, detail=True)
    assert e.text.startswith("$ make\n…") and len(e.text) <= 110 and e.text_truncated is False or e.text_truncated


def test_long_text_is_cut_and_flagged_and_malformed_entries_are_skipped():
    e = ev("agentMessage", text="w " * 2000)
    assert e.text_truncated and len(e.text) == 601
    for bad in ({}, {"item": {}}, {"turnId": "t", "item": {"type": "agentMessage"}}, {"turnId": "t", "item": "x"}):
        assert event_from_entry("th", bad) is None
    assert ev("agentMessage", text="x").at_ms == 20
    assert event_from_entry("th", {**entry("agentMessage", text="x"), "completedAtMs": None, "startedAtMs": True}).at_ms is None


def test_reader_pages_and_failure_mapping():
    async def main():
        async with connected_client() as (client, fake):
            fake.items["th"] = [entry("agentMessage", item_id=f"i{n}", turn=f"t{n // 4}", text=str(n)) for n in range(10)]
            reader = HistoryReader(client)
            rows, more = await reader.recent("th", 3)
            assert [r["item"]["text"] for r in rows] == ["7", "8", "9"] and more is True  # chronological, newest last
            rows, more = await reader.recent("th", 50)
            assert len(rows) == 10 and more is False
            assert (await reader.find("th", "t2", "i9"))["item"]["text"] == "9"
            assert await reader.find("th", "t2", "i0") is None
            assert await reader.find("nope", "t", "i") is None
            assert "thread/items/list" in fake.methods()
        with pytest.raises(HistoryUnavailable):
            await reader.recent("th", 1)  # connection gone
    asyncio.run(main())
