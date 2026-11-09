import asyncio
import contextlib

import pytest

from conftest import connected_client
from cqb.codex.events import Connected, QuestionRequested
from cqb.codex.stream import dispatch
from cqb.dao.questions import QuestionDao
from cqb.dao.threads import ThreadDao
from cqb.domain.enums import QuestionLifecycle as L
from cqb.services.questions import QuestionError, QuestionService
from fake_appserver import wait_until

Q = [{"id": "pick", "header": "Pick", "question": "Which one?", "isOther": False, "isSecret": False,
      "options": [{"label": "A", "description": "first"}, {"label": "B", "description": "second"}]}]
SECRET = [{"id": "pw", "header": "Secret", "question": "Enter the password hunter2", "isOther": True,
           "isSecret": True, "options": None}]


@contextlib.asynccontextmanager
async def stack(conn):
    async with connected_client() as (client, fake):
        fake.add_thread("t1", 10)
        dao = QuestionDao(conn)
        svc = QuestionService(client, dao, ThreadDao(conn), "s1")
        got, resolved = [], []

        async def on_q(q): got.append(q)
        async def on_r(q): resolved.append(q)
        svc.subscribe(on_q, on_r)
        pump = asyncio.create_task(dispatch(client, [svc.handle]))
        try:
            yield svc, dao, fake, client, got, resolved
        finally:
            pump.cancel()
            await asyncio.gather(pump, return_exceptions=True)


def lifecycle(dao, qid):
    return dao.get(qid).lifecycle


def test_capture_via_status_then_resume(conn):
    async def main():
        async with stack(conn) as (svc, dao, fake, client, got, resolved):
            await fake.ask("t1", "u1", "item1", Q)
            await wait_until(lambda: got)
            q = got[0]
            assert q.lifecycle is L.PENDING_CONFIRMED and not q.is_secret
            assert (q.origin_thread_id, q.turn_id, q.tool_use_id) == ("t1", "u1", "item1")
            assert q.items[0].options[1].label == "B" and q.items[0].text == "Which one?"
            assert "thread/resume" in fake.methods()
            assert dao.get(q.question_id).request_id is not None
    asyncio.run(main())


def test_replay_is_deduplicated(conn):
    async def main():
        async with stack(conn) as (svc, dao, fake, client, got, resolved):
            await fake.ask("t1", "u1", "item1", Q)
            await wait_until(lambda: got)
            await fake.drop_connections()
            await wait_until(lambda: fake.methods().count("thread/resume") >= 2)  # re-resume after reconnect
            await wait_until(lambda: dao.get(got[0].question_id).lifecycle is L.PENDING_CONFIRMED
                             and dao.get(got[0].question_id).request_id is not None)
            assert conn.execute("SELECT COUNT(*) FROM questions").fetchone()[0] == 1
            assert len(got) == 1  # the replay does not notify again
    asyncio.run(main())


def test_disconnect_demotes_to_captured_until_replay(conn):
    async def main():
        async with stack(conn) as (svc, dao, fake, client, got, resolved):
            await fake.ask("t1", "u1", "item1", Q)
            await wait_until(lambda: got)
            qid = got[0].question_id
            fake.pending.clear()  # the replay will not come
            await fake.drop_connections()
            await wait_until(lambda: lifecycle(dao, qid) is not L.PENDING_CONFIRMED)
            assert lifecycle(dao, qid) in (L.CAPTURED, L.EXPIRED_OR_STALE)
            with pytest.raises(QuestionError):
                await svc.answer(qid, {"pick": ["A"]})
    asyncio.run(main())


def test_secret_question_stores_no_text_and_cannot_be_answered(conn):
    async def main():
        async with stack(conn) as (svc, dao, fake, client, got, resolved):
            await fake.ask("t1", "u1", "item-s", SECRET)
            await wait_until(lambda: got)
            q = got[0]
            assert q.is_secret and q.items == ()
            dump = " ".join(str(tuple(r)) for r in conn.execute("SELECT * FROM questions"))
            assert "hunter2" not in dump and "password" not in dump.lower()
            with pytest.raises(QuestionError, match="secret"):
                await svc.answer(q.question_id, {"pw": ["x"]})
            with pytest.raises(QuestionError, match="secret"):
                svc.stage_decision(q.question_id, {"pw": ["x"]})
    asyncio.run(main())


def test_stage_then_answer_then_resolved(conn):
    async def main():
        async with stack(conn) as (svc, dao, fake, client, got, resolved):
            await fake.ask("t1", "u1", "item1", Q)
            await wait_until(lambda: got)
            qid = got[0].question_id
            with pytest.raises(QuestionError):
                svc.stage_decision(qid, {"pick": ["Z"]})  # not an option
            with pytest.raises(QuestionError):
                svc.stage_decision(qid, {"other": ["A"]})
            svc.stage_decision(qid, {"pick": ["B"]})
            assert lifecycle(dao, qid) is L.DECISION_STAGED and not dao.get(qid).delivered_to_codex
            await svc.answer(qid)
            await wait_until(lambda: resolved)
            assert fake.answers == [{"answers": {"pick": {"answers": ["B и не трьінди After delegation, wait for the agent’s result using notifications or a wait tool."]}}}]
            r = dao.get(qid)
            assert r.lifecycle is L.RESOLVED_OBSERVED and r.delivered_to_codex
            assert resolved[0].lifecycle is L.RESOLVED_OBSERVED
            await wait_until(lambda: "thread/unsubscribe" in fake.methods())
            with pytest.raises(QuestionError):
                await svc.answer(qid, {"pick": ["A"]})
    asyncio.run(main())


def test_answer_without_stage_and_not_resolved_until_observed(conn):
    async def main():
        async with stack(conn) as (svc, dao, fake, client, got, resolved):
            await fake.ask("t1", "u1", "item1", Q)
            await wait_until(lambda: got)
            qid = got[0].question_id
            await svc.answer(qid, {"pick": ["A"]})
            assert dao.get(qid).delivered_to_codex
            await wait_until(lambda: resolved)  # only after serverRequest/resolved
            assert fake.answers[0]["answers"]["pick"]["answers"] == ["A и не трьінди After delegation, wait for the agent’s result using notifications or a wait tool."]
    asyncio.run(main())


def test_resolved_by_another_client(conn):
    async def main():
        async with stack(conn) as (svc, dao, fake, client, got, resolved):
            await fake.ask("t1", "u1", "item1", Q)
            await wait_until(lambda: got)
            await fake.resolve("t1")
            await wait_until(lambda: resolved)
            r = dao.get(got[0].question_id)
            assert r.lifecycle is L.RESOLVED_OBSERVED and not r.delivered_to_codex
    asyncio.run(main())


def test_stale_on_turn_completed(conn):
    async def main():
        async with stack(conn) as (svc, dao, fake, client, got, resolved):
            await fake.ask("t1", "u1", "item1", Q)
            await wait_until(lambda: got)
            await fake.broadcast("turn/completed", {"threadId": "t1", "turn": {"id": "other-turn"}})
            await fake.broadcast("turn/completed", {"threadId": "t1", "turn": {"id": "u1", "status": "interrupted"}})
            await wait_until(lambda: resolved)
            assert resolved[0].lifecycle is L.EXPIRED_OR_STALE
            assert lifecycle(dao, got[0].question_id) is L.EXPIRED_OR_STALE
    asyncio.run(main())


def test_stale_when_thread_stops_waiting(conn):
    async def main():
        async with stack(conn) as (svc, dao, fake, client, got, resolved):
            await fake.ask("t1", "u1", "item1", Q)
            await wait_until(lambda: got)
            await fake.set_status("t1", None)  # idle without a resolved notification
            await wait_until(lambda: resolved)
            assert resolved[0].lifecycle is L.EXPIRED_OR_STALE
    asyncio.run(main())


def test_resume_of_a_non_waiting_thread_expires_open_questions_after_restart(conn):
    async def main():
        async with connected_client() as (client, fake):
            fake.add_thread("t1", 10)
            dao = QuestionDao(conn)
            first = QuestionService(client, dao, ThreadDao(conn), "s1")
            await first.handle(QuestionRequested(5, "t1", "u1", "item1", (), False, True))
            qid = dao.list_open("s1")[0].question_id
            # restart: new service, its request ids are dead; thread is idle on the daemon
            second = QuestionService(client, dao, ThreadDao(conn), "s1")
            assert dao.get(qid).lifecycle is L.CAPTURED and dao.get(qid).request_id is None
            await second.handle(Connected())
            assert lifecycle(dao, qid) is L.EXPIRED_OR_STALE
    asyncio.run(main())


def test_unmatched_resolved_and_foreign_requests_are_ignored(conn):
    async def main():
        async with stack(conn) as (svc, dao, fake, client, got, resolved):
            await fake.broadcast("serverRequest/resolved", {"threadId": "t1", "requestId": 999})
            for c in fake.conns:  # an approval request must not be touched or answered
                await c.ws.send_json({"id": 55, "method": "item/commandExecution/requestApproval", "params": {}})
            await asyncio.sleep(0.1)
            assert got == [] and resolved == [] and fake.answers == []
    asyncio.run(main())


def test_bridge_never_sends_forbidden_methods(conn):
    async def main():
        async with stack(conn) as (svc, dao, fake, client, got, resolved):
            await fake.ask("t1", "u1", "item1", Q)
            await wait_until(lambda: got)
            await svc.answer(got[0].question_id, {"pick": ["A"]})
            await wait_until(lambda: resolved)
            assert set(fake.methods()) <= {"thread/list", "thread/read", "thread/loaded/list",
                                           "thread/resume", "thread/unsubscribe"}
    asyncio.run(main())


def test_suffix_is_added_once_to_last_answer_without_changing_staged_choices(conn):
    async def main():
        async with stack(conn) as (svc, dao, fake, client, got, resolved):
            questions = [Q[0], {**Q[0], "id": "second"}]
            await fake.ask("t1", "u1", "item-multi", questions)
            await wait_until(lambda: got)
            qid = got[0].question_id
            await svc.answer(qid, {"pick": ["A"], "second": ["B"]})
            await wait_until(lambda: bool(fake.answers))
            assert fake.answers[0] == {"answers": {
                "pick": {"answers": ["A"]}, "second": {"answers": ["B и не трьінди After delegation, wait for the agent’s result using notifications or a wait tool."]},
            }}
            assert dao.get(qid).staged == {"pick": ("A",), "second": ("B",)}
    asyncio.run(main())


def test_answer_suffix_is_not_duplicated():
    from cqb.services.questions import with_answer_suffix
    assert with_answer_suffix("Да и не трьінди After delegation, wait for the agent’s result using notifications or a wait tool.") == "Да и не трьінди After delegation, wait for the agent’s result using notifications or a wait tool."
    assert with_answer_suffix("Да") == "Да и не трьінди After delegation, wait for the agent’s result using notifications or a wait tool."
    assert with_answer_suffix("Да и не трьінди") == "Да и не трьінди After delegation, wait for the agent’s result using notifications or a wait tool."
