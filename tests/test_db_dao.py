import dataclasses
import sqlite3

import pytest

from cqb.dao import db
from cqb.dao.actions import ActionDao
from cqb.dao.questions import QuestionDao
from cqb.dao.threads import ThreadDao
from cqb.domain.enums import ArchiveState, ConsultantState, QuestionLifecycle, RunState, VerificationSource
from cqb.domain.models import Question, QuestionItem, QuestionOption, TelegramAction, Thread


def thread(tid="t1", parent=None, root=None, updated=100, state=ArchiveState.UNKNOWN, verified=None):
    return Thread("s1", tid, root or parent or tid, parent, "cli", "name", "/w", updated, state, verified, 1)


def question(qid="q1", **kw):
    base = dict(
        question_id=qid, source_id="s1", root_thread_id="t1", origin_thread_id="t1", turn_id="u1",
        tool_use_id=qid, request_id="7",
        items=(QuestionItem("a", "H", "Pick?", False, (QuestionOption("A", "da"), QuestionOption("B"))),),
        is_secret=False, is_blocking=True, lifecycle=QuestionLifecycle.PENDING_CONFIRMED,
        verification_source=VerificationSource.HOST_RPC, staged=None, delivered_to_codex=False,
        created_at=1, updated_at=1,
    )
    return Question(**{**base, **kw})


def test_pragmas_and_migration_idempotent(tmp_path):
    conn = db.connect(str(tmp_path / "p.db"))
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1
    assert conn.execute("PRAGMA cache_size").fetchone()[0] == -512
    assert (conn.execute("PRAGMA mmap_size").fetchone() or [0])[0] == 0
    db.migrate(conn)
    assert db.migrate(conn) == len(db.MIGRATIONS)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(db.MIGRATIONS)


def test_newer_schema_is_refused(tmp_path):
    c = db.connect(str(tmp_path / "x.db"))
    c.execute("PRAGMA user_version = 99")
    with pytest.raises(RuntimeError, match="newer"):
        db.migrate(c)


def test_transaction_rolls_back(conn):
    with pytest.raises(ValueError), db.transaction(conn):
        conn.execute("INSERT INTO codex_sources VALUES ('s2','x','app_server',1)")
        raise ValueError
    assert conn.execute("SELECT COUNT(*) FROM codex_sources WHERE source_id='s2'").fetchone()[0] == 0


def test_threads_foreign_key_to_source(conn):
    with pytest.raises(sqlite3.IntegrityError):
        ThreadDao(conn).upsert_many([Thread("nope", "t", "t", None, None, None, None, 1, ArchiveState.UNKNOWN, None, 1)])


def test_threads_upsert_keeps_verified_archive_state(conn):
    dao = ThreadDao(conn)
    dao.upsert_many([thread(state=ArchiveState.ARCHIVED, verified=50)])
    dao.upsert_many([thread(updated=200)])  # unknown must not erase the verified state
    t = dao.get("s1", "t1")
    assert (t.updated_at, t.archive_state, t.archive_verified_at) == (200, ArchiveState.ARCHIVED, 50)
    dao.set_archive_state("s1", "t1", ArchiveState.ACTIVE, 60)
    assert dao.get("s1", "t1").archive_state is ArchiveState.ACTIVE
    assert dao.set_archive_state("s1", "missing", ArchiveState.ARCHIVED, 1) is False


def test_threads_roots_and_descendants(conn):
    dao = ThreadDao(conn)
    dao.upsert_many([thread("r", updated=10), thread("c", parent="r", root="r", updated=20), thread("r2", updated=30)])
    assert [t.thread_id for t in dao.list_roots("s1", None, 10)] == ["r2", "r"]
    assert [t.thread_id for t in dao.list_roots("s1", None, 1, offset=1)] == ["r"]
    assert [t.thread_id for t in dao.list_descendants("s1", "r", 10)] == ["c"]


def test_questions_roundtrip_dedup_and_open_queries(conn):
    dao = QuestionDao(conn)
    assert dao.insert_if_new(question()) is True
    assert dao.insert_if_new(question(turn_id="u1")) is False  # same dedup key (qid is the same too)
    assert dao.insert_if_new(question("q1b", tool_use_id="q1")) is False  # unique dedup tuple
    got = dao.get("q1")
    assert got.items[0].options[0] == QuestionOption("A", "da") and got.request_id == "7"
    assert dao.find_open_by_request("s1", "t1", "7").question_id == "q1"
    assert dao.open_thread_ids("s1") == ["t1"]
    dao.stage("q1", {"a": ("A",)}, 5)
    assert dao.get("q1").staged == {"a": ("A",)}
    dao.set_lifecycle("q1", QuestionLifecycle.RESOLVED_OBSERVED, 6)
    assert dao.list_open("s1") == [] and dao.find_open_by_request("s1", "t1", "7") is None


def test_questions_detach_demotes_only_unconfirmed(conn):
    dao = QuestionDao(conn)
    dao.insert_if_new(question("a", tool_use_id="a"))
    dao.insert_if_new(question("b", tool_use_id="b", lifecycle=QuestionLifecycle.DECISION_STAGED))
    dao.detach_requests("s1", 9)
    a, b = dao.get("a"), dao.get("b")
    assert (a.lifecycle, a.request_id) == (QuestionLifecycle.CAPTURED, None)
    assert (b.lifecycle, b.request_id) == (QuestionLifecycle.DECISION_STAGED, None)


def test_secret_question_has_no_text_in_storage(conn):
    dao = QuestionDao(conn)
    dao.insert_if_new(question(items=(), is_secret=True))
    raw = conn.execute("SELECT questions_json, is_secret FROM questions").fetchone()
    assert (raw["questions_json"], raw["is_secret"]) == ("[]", 1)


def test_actions_are_one_time_and_expire(conn):
    dao = ActionDao(conn)
    dao.put(TelegramAction("k1", "answer", "s1", "t1", "{}", expires_at=100))
    assert dao.consume("k1", now=50).action == "answer"
    assert dao.consume("k1", now=50) is None
    dao.put(TelegramAction("k2", "answer", None, None, None, expires_at=100))
    assert dao.consume("k2", now=100) is None  # expired
    dao.put(TelegramAction("k3", "answer", None, None, None, expires_at=10))
    assert dao.delete_expired(now=11) == 1


def test_migration_2_upgrades_a_v1_database_in_place(tmp_path):
    c = db.connect(str(tmp_path / "v1.db"))
    c.executescript(f"BEGIN;\n{db.MIGRATIONS[0]}\nPRAGMA user_version = 1;\nCOMMIT;")
    c.execute("INSERT INTO codex_sources VALUES ('s1','S1','app_server',1)")
    QuestionDao(c).insert_if_new(question())
    assert db.migrate(c) == len(db.MIGRATIONS) >= 2
    assert QuestionDao(c).get("q1").card_message_id is None  # existing rows survive
    tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"consultants", "bot_state"} <= tables


def test_migration_3_renames_the_card_column_and_adds_run_state(tmp_path):
    c = db.connect(str(tmp_path / "v2.db"))
    for version in range(2):
        c.executescript(f"BEGIN;\n{db.MIGRATIONS[version]}\nPRAGMA user_version = {version + 1};\nCOMMIT;")
    c.execute("INSERT INTO codex_sources VALUES ('s1','S1','app_server',1)")
    c.execute("INSERT INTO codex_threads VALUES ('s1','t1','t1',NULL,'cli','n','/w',100,'active',1,1)")
    QuestionDao(c).insert_if_new(question("a"))
    c.execute("UPDATE questions SET inbox_message_id=77 WHERE question_id='a'")  # a card posted before the upgrade
    assert db.migrate(c) == len(db.MIGRATIONS) >= 3
    assert "inbox_message_id" not in {r[1] for r in c.execute("PRAGMA table_info(questions)")}
    dao = QuestionDao(c)
    assert dao.get("a").card_message_id == 77 and dao.find_by_card_message(77).question_id == "a"
    assert ThreadDao(c).get("s1", "t1").run_state is None  # old rows: not reported yet
    ThreadDao(c).upsert_many([dataclasses.replace(thread("t1"), run_state=RunState.WORKING)])
    ThreadDao(c).upsert_many([thread("t1")])  # a poll without status keeps the known one
    assert ThreadDao(c).get("s1", "t1").run_state is RunState.WORKING
    assert db.migrate(c) == len(db.MIGRATIONS)  # idempotent


def test_threads_recency_window(conn):
    dao = ThreadDao(conn)
    dao.upsert_many([thread("a", updated=10), thread("b", updated=100)])
    assert [t.thread_id for t in dao.list_roots("s1", None, 10, since=50)] == ["b"]


def test_actions_get_does_not_consume(conn):
    dao = ActionDao(conn)
    dao.put(TelegramAction("k", "info", "s1", "t1", None, expires_at=100))
    assert dao.get("k", 50).action == "info" and dao.get("k", 50) is not None
    assert dao.get("k", 100) is None
    dao.put(TelegramAction("k", "info", "s1", "t1", None, expires_at=500))  # re-minting extends it
    assert dao.get("k", 200) is not None


def test_bot_state_roundtrip(conn):
    from cqb.dao.bot_state import BotStateDao
    s = BotStateDao(conn)
    assert s.get("x") is None and s.get_int("x") is None
    s.set("x", 5)
    s.set("x", 7)
    assert s.get("x") == "7" and s.get_int("x") == 7
    s.delete("x")
    assert s.get("x") is None


def test_consultant_dao_uniqueness_and_states(conn):
    from cqb.dao.consultants import ConsultantDao
    from cqb.domain.enums import ConsultantState as S
    ThreadDao(conn).upsert_many([thread("r1"), thread("r2")])
    dao = ConsultantDao(conn)
    a = dao.reserve("s1", "r1", -100, "cqb-a", 1)
    assert a.state is S.CREATING and a.tg_topic_id is None
    assert dao.reserve("s1", "r1", -100, "cqb-other", 2) == a  # one row per root
    dao.open(a.consultant_id, 5, 3)
    b = dao.reserve("s1", "r2", -100, "cqb-b", 1)
    with pytest.raises(sqlite3.IntegrityError):
        dao.open(b.consultant_id, 5, 3)  # UNIQUE(tg_chat_id, tg_topic_id)
    assert dao.get_by_topic(-100, 5).root_thread_id == "r1" and dao.get_by_topic(-100, 6) is None
    assert set(dao.get_many("s1", ["r1", "r2", "zz"])) == {"r1", "r2"} and dao.get_many("s1", []) == {}
    assert dao.count_open("s1") == 1
    assert dao.fail_stale_creating("s1", 9) == 1 and dao.get("s1", "r2").state is S.ERROR
    assert [c.root_thread_id for c in dao.list_by_states("s1", [S.OPEN])] == ["r1"]
    with pytest.raises(sqlite3.IntegrityError):
        dao.reserve("s1", "ghost", -100, "k", 1)  # FK to codex_threads


def test_question_card_helpers(conn):
    from cqb.dao.consultants import ConsultantDao
    ThreadDao(conn).upsert_many([thread("r1"), thread("r2")])
    dao = QuestionDao(conn)
    dao.insert_if_new(question("a", tool_use_id="a", root_thread_id="r1"))
    dao.insert_if_new(question("b", tool_use_id="b", root_thread_id="r1"))
    dao.insert_if_new(question("c", tool_use_id="c", root_thread_id="r2"))
    assert dao.open_counts_by_root("s1") == {"r1": 2, "r2": 1}
    assert dao.list_postable("s1") == []  # no consultant anywhere: nothing can be posted
    consultants = ConsultantDao(conn)
    one = consultants.reserve("s1", "r1", -100, "k1", 1)
    assert dao.list_postable("s1") == []  # still creating
    consultants.open(one.consultant_id, 5, 2)
    assert {q.question_id for q in dao.list_postable("s1")} == {"a", "b"}  # only r1 has an open topic
    dao.set_card_message("a", 77)
    assert dao.find_by_card_message(77).question_id == "a" and dao.get("a").card_message_id == 77
    assert {q.question_id for q in dao.list_postable("s1")} == {"b"}
    consultants.set_state(one.consultant_id, ConsultantState.ARCHIVED, 3)
    assert dao.list_postable("s1") == []  # closed consultants get no new cards
    dao.stage("a", {"a": ("A",)}, 5)
    dao.unstage("a", QuestionLifecycle.PENDING_CONFIRMED, 6)
    got = dao.get("a")
    assert got.staged is None and got.lifecycle is QuestionLifecycle.PENDING_CONFIRMED
