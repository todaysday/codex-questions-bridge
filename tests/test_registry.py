from cqb.dao.consultants import ConsultantDao
from cqb.dao.questions import QuestionDao
from cqb.dao.threads import ThreadDao
from cqb.domain.enums import ArchiveState, ConsultantState
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
