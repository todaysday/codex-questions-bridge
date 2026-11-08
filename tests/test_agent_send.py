"""The one write path into a thread: AppServerClient.send_user_message (and nothing else of turn/*)."""

import asyncio
import uuid

import pytest

from conftest import connected_client
from cqb.codex import appserver
from cqb.codex.appserver import ALLOWED_METHODS, MAX_USER_MESSAGE_CHARS, MethodNotAllowed, RpcError


def arun(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _no_race_pause(monkeypatch):
    monkeypatch.setattr(appserver, "RACE_PAUSE", 0)


def assert_uuid4(value):
    assert isinstance(value, str) and value == str(uuid.UUID(value)) and uuid.UUID(value).version == 4


def idle_thread(fake, tid="t1", turns=()):
    fake.add_thread(tid, 1)
    fake.turns[tid] = list(turns)


TEXT = '<send_user_message_question_reply>\n[{"question":"Q","answer":"Да"}]\n</send_user_message_question_reply>'


@pytest.mark.parametrize("method", ["turn/steer", "turn/start", "turn/interrupt", "turn/delete", "thread/archive"])
def test_call_never_passes_a_write_whatever_the_params(method):
    async def main():
        async with connected_client() as (client, fake):
            with pytest.raises(MethodNotAllowed):
                await client.call(method, {"threadId": "t1", "input": [], "expectedTurnId": "x"})
            assert fake.calls == []
    arun(main())


def test_the_read_whitelist_is_unchanged_by_the_write_path():
    assert not {m for m in ALLOWED_METHODS if m.startswith("turn/")}
    assert {appserver.STEER, appserver.START} == {"turn/steer", "turn/start"}


def test_idle_thread_resumes_starts_and_unsubscribes_with_exactly_these_params():
    async def main():
        async with connected_client() as (client, fake):
            idle_thread(fake, turns=[{"id": "t9", "status": "completed"}])
            assert await client.send_user_message("t1", TEXT) == "start"
            assert fake.calls == [
                ("thread/turns/list", {"threadId": "t1", "limit": 1, "itemsView": "notLoaded"}),
                ("thread/resume", {"threadId": "t1", "excludeTurns": True}),
                ("turn/start", {"threadId": "t1", "input": [{"type": "text", "text": TEXT}],
                                "clientUserMessageId": fake.calls[2][1]["clientUserMessageId"],
                                "turnTrigger": "send_user_message_async_question"}),
                ("thread/unsubscribe", {"threadId": "t1"}),
            ]
            assert_uuid4(fake.calls[2][1]["clientUserMessageId"])
            assert fake.conns[0].subscribed == set()
            [msg] = [e["item"] for e in fake.items["t1"]]
            assert msg["type"] == "userMessage" and msg["content"][0]["text"] == TEXT  # exactly our text
    arun(main())


def test_running_turn_is_steered_without_resume_and_with_the_expected_turn_id():
    async def main():
        async with connected_client() as (client, fake):
            idle_thread(fake, turns=[{"id": "t2", "status": "inProgress"}, {"id": "t1", "status": "completed"}])
            assert await client.send_user_message("t1", TEXT) == "steer"
            assert fake.calls[1:] == [
                ("turn/steer", {"threadId": "t1", "input": [{"type": "text", "text": TEXT}], "expectedTurnId": "t2",
                                "clientUserMessageId": fake.calls[1][1]["clientUserMessageId"]}),
            ]
            assert_uuid4(fake.calls[1][1]["clientUserMessageId"])
            assert "turnTrigger" not in fake.calls[1][1]
            assert fake.items["t1"][0]["item"]["content"][0]["text"] == TEXT
    arun(main())


FORBIDDEN = {"approvalPolicy", "sandbox", "model", "cwd", "effort", "approvalsReviewer", "collaborationMode",
             "outputSchema", "environments", "personality", "serviceTier", "permissions", "summary"}


@pytest.mark.parametrize("turns", [[], [{"id": "t2", "status": "inProgress"}]])
def test_no_override_can_be_sent_whatever_the_path(turns):
    async def main():
        async with connected_client() as (client, fake):
            idle_thread(fake, turns=turns)
            await client.send_user_message("t1", TEXT)
            for method, params in fake.calls:
                assert not FORBIDDEN & set(params), (method, params)
                if method == "turn/steer":
                    assert set(params) == {"threadId", "input", "expectedTurnId", "clientUserMessageId"}
                if method == "turn/start":
                    assert set(params) == {"threadId", "input", "clientUserMessageId", "turnTrigger"}
                if method.startswith("turn/"):
                    assert [set(i) for i in params["input"]] == [{"type", "text"}]
            assert {m for m, _ in fake.calls if m.startswith("turn/")} <= {"turn/steer", "turn/start"}
    arun(main())


@pytest.mark.parametrize("turns, method", [([], "turn/start"), ([{"id": "t2", "status": "inProgress"}], "turn/steer")])
def test_client_message_id_is_a_fresh_uuid4_per_call_and_turn_trigger_is_start_only(turns, method):
    async def main():
        async with connected_client() as (client, fake):
            idle_thread(fake, turns=turns)
            await client.send_user_message("t1", TEXT)
            for turn in fake.turns["t1"]:
                turn["status"] = "completed" if method == "turn/start" else "inProgress"
            await client.send_user_message("t1", TEXT)
            sent = [p for m, p in fake.calls if m == method]
            assert len(sent) == 2
            ids = [p["clientUserMessageId"] for p in sent]
            for i in ids:
                assert_uuid4(i)
            assert ids[0] != ids[1]
            assert all(("turnTrigger" in p) == (method == "turn/start") for p in sent)
            assert all(p.get("turnTrigger", "send_user_message_async_question") == "send_user_message_async_question" for p in sent)
    arun(main())


def test_steer_race_the_turn_ended_meanwhile_rereads_and_starts():
    async def main():
        async with connected_client() as (client, fake):
            idle_thread(fake, turns=[{"id": "t2", "status": "inProgress"}])
            fake.steer_races = 1
            assert await client.send_user_message("t1", TEXT) == "start"
            assert fake.methods() == ["thread/turns/list", "turn/steer", "thread/turns/list", "thread/resume",
                                      "turn/start", "thread/unsubscribe"]
            assert len(fake.items["t1"]) == 1  # sent once
    arun(main())


def test_start_race_a_turn_began_meanwhile_unsubscribes_then_steers():
    async def main():
        async with connected_client() as (client, fake):
            idle_thread(fake)
            fake.start_races = 1
            assert await client.send_user_message("t1", TEXT) == "steer"
            assert fake.methods() == ["thread/turns/list", "thread/resume", "turn/start", "thread/unsubscribe",
                                      "thread/turns/list", "turn/steer"]
            assert len(fake.items["t1"]) == 1
    arun(main())


def test_a_failure_after_the_retry_is_raised_and_nothing_is_sent_twice():
    async def main():
        async with connected_client() as (client, fake):
            with pytest.raises(RpcError):
                await client.send_user_message("unknown-thread", TEXT)  # resume fails both times
            assert fake.methods().count("thread/resume") == 2 and "turn/start" not in fake.methods()
            assert fake.items == {}
    arun(main())


@pytest.mark.parametrize("text", ["", "   \n", "x" * (MAX_USER_MESSAGE_CHARS + 1)])
def test_empty_or_oversized_text_is_rejected_before_any_call(text):
    async def main():
        async with connected_client() as (client, fake):
            idle_thread(fake)
            with pytest.raises(ValueError):
                await client.send_user_message("t1", text)
            with pytest.raises(ValueError):
                await client.send_user_message("", "hello")
            assert fake.calls == []
        async with connected_client() as (client, fake):
            idle_thread(fake)
            await client.send_user_message("t1", "x" * MAX_USER_MESSAGE_CHARS)  # the limit itself is fine
    arun(main())
