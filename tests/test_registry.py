from cqb.dao.consultants import ConsultantDao
from cqb.dao.questions import QuestionDao
from cqb.dao.threads import ThreadDao
import dataclasses
import asyncio

from cqb.codex.events import Connected, ThreadStatusChanged
from cqb.domain.enums import ArchiveState, ConsultantState, RunState
from cqb.services.live_state import LiveState
from cqb.services.registry import RegistryQuery
from tg_stack import NOW, thread
from test_db_dao import question


def test_registry_page_orders_filters_and_paginates(conn):
    threads = ThreadDao(conn)
    threads.upsert_many([
        *(thread(f"r{i}", updated=NOW - i * 100) for i in range(7)),
        thread("worker", parent="r0"),
        thread("stale", updated=NOW - 40 * 86400),
        thread("arch", state=ArchiveState.ARCHIVED),
    ])
    q = RegistryQuery(threads, ConsultantDao(conn), QuestionDao(conn), "s1", 30, lambda: NOW)
    p0 = q.page(0, 3)
    assert [r.thread.thread_id for r in p0.rows] == ["r0", "r1", "r2"] and p0.has_next
    p2 = q.page(2, 3)
    assert [r.thread.thread_id for r in p2.rows] == ["r6"] and not p2.has_next
    assert q.page(-5, 3).page == 0
    ids = {r.thread.thread_id for page in (q.page(0, 50),) for r in page.rows}
    assert "worker" not in ids and "stale" not in ids and "arch" not in ids
    assert [r.thread.thread_id for r in q.page(0, 50, archived=True).rows] == ["arch"]


def test_registry_rows_carry_consultant_state_and_pending_questions(conn):
    threads = ThreadDao(conn)
    threads.upsert_many([thread("r1"), thread("r2")])
    consultants = ConsultantDao(conn)
    c = consultants.reserve("s1", "r1", -100, "k", 1)
    consultants.open(c.consultant_id, 9, 2)
    questions = QuestionDao(conn)
    questions.insert_if_new(question("a", tool_use_id="a", root_thread_id="r1"))
    questions.insert_if_new(question("b", tool_use_id="b", root_thread_id="r1"))
    q = RegistryQuery(threads, consultants, questions, "s1", 30, lambda: NOW)
    rows = {r.thread.thread_id: r for r in q.page(0, 10).rows}
    assert (rows["r1"].consultant_state, rows["r1"].topic_id, rows["r1"].pending_questions) == (ConsultantState.OPEN, 9, 2)
    assert (rows["r2"].consultant_state, rows["r2"].topic_id, rows["r2"].pending_questions) == (None, None, 0)
    assert q.describe("r1").pending_questions == 2 and q.describe("zz") is None


def build(conn, live=None):
    return RegistryQuery(ThreadDao(conn), ConsultantDao(conn), QuestionDao(conn), "s1", 30, lambda: NOW, live=live)


def with_state(t, state):
    return dataclasses.replace(t, run_state=state)


def test_order_questions_first_then_working_then_activity_with_stable_ties(conn):
    threads = ThreadDao(conn)
    threads.upsert_many([
        thread("a_idle_new", updated=NOW - 10),
        thread("b_idle_old", updated=NOW - 900),
        with_state(thread("c_working_old", updated=NOW - 800), RunState.WORKING),
        with_state(thread("d_working_new", updated=NOW - 20), RunState.WORKING),
        thread("e_question_old", updated=NOW - 5000),
        with_state(thread("f_both", updated=NOW - 4000), RunState.WORKING),
        thread("g_tie_2", updated=NOW - 300), thread("g_tie_1", updated=NOW - 300),
        with_state(thread("h_waiting", updated=NOW - 5), RunState.WAITING_INPUT),
    ])
    questions = QuestionDao(conn)
    for root in ("e_question_old", "f_both"):
        questions.insert_if_new(question(root, tool_use_id=root, root_thread_id=root))
    q = build(conn)
    ids = [r.thread.thread_id for r in q.page(0, 50).rows]
    assert ids == ["f_both", "e_question_old",  # questions first (by activity, the two markers do not stack)
                   "d_working_new", "c_working_old",  # then working threads
                   "h_waiting", "a_idle_new", "g_tie_1", "g_tie_2", "b_idle_old"]  # rest: updatedAt desc, ties by id
    both = q.page(0, 50).rows[0]
    assert both.pending_questions == 1 and both.working
    assert not [r for r in q.page(0, 50).rows if r.thread.thread_id == "h_waiting"][0].working  # waiting != working
    assert [r.thread.thread_id for r in q.page(1, 4).rows] == ["h_waiting", "a_idle_new", "g_tie_1", "g_tie_2"]
    assert q.page(1, 4).has_next and not q.page(2, 4).has_next


def test_working_state_prefers_the_live_broadcast_and_falls_back_to_the_stored_poll_status(conn):
    ThreadDao(conn).upsert_many([
        with_state(thread("stored_working"), RunState.WORKING),
        with_state(thread("stored_idle"), RunState.IDLE),
        with_state(thread("live_overrides"), RunState.WORKING),
        thread("never_reported"),
    ])
    live = LiveState()
    q = build(conn, live)
    working = lambda: {r.thread.thread_id for r in q.page(0, 50).rows if r.working}  # noqa: E731
    assert working() == {"stored_working", "live_overrides"}
    asyncio.run(live.handle(ThreadStatusChanged("stored_idle", False, False, RunState.WORKING)))
    asyncio.run(live.handle(ThreadStatusChanged("live_overrides", False, False, RunState.IDLE)))
    asyncio.run(live.handle(ThreadStatusChanged("stored_working", True, False, RunState.WAITING_INPUT)))
    assert working() == {"stored_idle"}
    assert q.describe("stored_idle").working and q.is_root("stored_idle") and not q.is_root("nope")


def test_live_flip_hook_fires_only_when_working_actually_flips():
    flips = []
    live = LiveState(on_working_flip=flips.append)
    send = lambda state: asyncio.run(live.handle(ThreadStatusChanged("t", False, False, state)))  # noqa: E731
    send(RunState.IDLE)       # first broadcast: unknown before, counts once
    send(RunState.IDLE)
    send(RunState.WORKING)    # flip
    send(RunState.WORKING)
    send(RunState.WORKING)
    send(RunState.WAITING_INPUT)  # stops working: flip
    send(RunState.IDLE)       # waiting -> idle: nothing visible changes
    send(RunState.WORKING)    # flip
    assert flips == ["t", "t", "t", "t"]
    asyncio.run(live.handle(Connected()))  # facts are forgotten: the next broadcast is news again
    flips.clear()
    send(RunState.WORKING)
    assert flips == ["t"]
