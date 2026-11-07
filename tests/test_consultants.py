import asyncio
import sqlite3

import pytest

from cqb.dao.consultants import ConsultantDao
from cqb.dao.threads import ThreadDao
from cqb.domain.enums import ArchiveState, ConsultantState as S
from cqb.domain.models import Thread
from cqb.services.consultants import (
    ConsultantError, ConsultantService, TopicGone, session_key, topic_name,
)

CHAT = -1001234567890


def thread(tid="r1", state=ArchiveState.ACTIVE, parent=None, name="FoxProfile — User Access"):
    return Thread("s1", tid, parent or tid, parent, "cli", name, "/w", 100, state, 1, 1)


class FakeTopics:
    def __init__(self):
        self.created: list[str] = []
        self.closed: dict[int, bool] = {}
        self.calls: list[tuple[int, bool]] = []
        self.fail_create = False
        self.gone: set[int] = set()
        self.delay = 0.0

    async def create_topic(self, name):
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail_create:
            raise RuntimeError("boom")
        self.created.append(name)
        return 100 + len(self.created)

    async def set_topic_closed(self, topic_id, closed):
        self.calls.append((topic_id, closed))
        if topic_id in self.gone:
            raise TopicGone()
        self.closed[topic_id] = closed


@pytest.fixture
def env(conn):
    threads = ThreadDao(conn)
    threads.upsert_many([thread("r1"), thread("w1", parent="r1"), thread("old", ArchiveState.ARCHIVED)])
    topics = FakeTopics()
    dao = ConsultantDao(conn)
    svc = ConsultantService(dao, threads, topics, "s1", CHAT, persist_delay=0)
    return svc, topics, dao, threads, conn


def run(coro):
    return asyncio.run(coro)


def test_double_click_creates_exactly_one_topic(env):
    svc, topics, dao, *_ = env
    topics.delay = 0.05

    async def main():
        return await asyncio.gather(svc.create("r1"), svc.create("r1"), svc.create("r1"))
    results = run(main())
    assert len(topics.created) == 1
    assert {c.tg_topic_id for c in results} == {101} and all(c.state is S.OPEN for c in results)
    assert svc._locks == {}  # no lock leaks


def test_create_is_idempotent_and_names_the_topic(env):
    svc, topics, *_ = env
    first = run(svc.create("r1"))
    again = run(svc.create("r1"))
    assert first == again and topics.created == ["FoxProfile — User Access"]
    assert first.pico_session_key == session_key("s1", "r1") and first.tg_chat_id == CHAT


def test_validation_rejects_unknown_nonroot_and_archived(env):
    svc, topics, *_ = env
    for root in ("nope", "w1", "old"):
        with pytest.raises(ConsultantError):
            run(svc.create(root))
    assert topics.created == []


def test_unverified_archive_state_blocks_creation(env):
    svc, topics, dao, threads, conn = env
    threads.upsert_many([thread("u1", ArchiveState.UNKNOWN)])
    with pytest.raises(ConsultantError):
        run(svc.create("u1"))
    assert topics.created == []


def test_topic_failure_marks_error_and_click_retries(env):
    svc, topics, dao, *_ = env
    topics.fail_create = True
    with pytest.raises(ConsultantError):
        run(svc.create("r1"))
    assert dao.get("s1", "r1").state is S.ERROR
    topics.fail_create = False
    c = run(svc.create("r1"))
    assert c.state is S.OPEN and len(topics.created) == 1


def test_transient_db_failure_after_topic_creation_does_not_duplicate(env, monkeypatch):
    svc, topics, dao, *_ = env
    real, calls = dao.open, []

    def flaky(*a):
        calls.append(a)
        if len(calls) < 3:
            raise sqlite3.OperationalError("database is locked")
        return real(*a)
    monkeypatch.setattr(dao, "open", flaky)
    c = run(svc.create("r1"))
    assert c.state is S.OPEN and len(topics.created) == 1 and len(calls) == 3


def test_persistent_db_failure_keeps_topic_for_the_next_click(env, monkeypatch):
    svc, topics, dao, *_ = env
    real = dao.open
    monkeypatch.setattr(dao, "open", lambda *a: (_ for _ in ()).throw(sqlite3.OperationalError("disk")))
    with pytest.raises(ConsultantError):
        run(svc.create("r1"))
    assert dao.get("s1", "r1").state is S.CREATING and len(topics.created) == 1
    monkeypatch.setattr(dao, "open", real)
    c = run(svc.create("r1"))
    assert c.state is S.OPEN and c.tg_topic_id == 101 and len(topics.created) == 1


def test_restart_with_interrupted_creation_marks_error_and_never_auto_creates(env):
    svc, topics, dao, threads, conn = env
    dao.reserve("s1", "r1", CHAT, session_key("s1", "r1"), 1)  # a crash left CREATING behind
    fresh = ConsultantService(dao, threads, topics, "s1", CHAT)
    assert fresh.recover() == 1
    assert dao.get("s1", "r1").state is S.ERROR and topics.created == []
    assert run(fresh.reconcile()) == 0 and topics.created == []
    assert run(fresh.create("r1")).state is S.OPEN  # only an explicit click retries


def test_archive_closes_and_unarchive_reopens_the_same_topic(env):
    svc, topics, dao, threads, conn = env
    c = run(svc.create("r1"))
    assert run(svc.reconcile()) == 0 and topics.calls == []
    threads.set_archive_state("s1", "r1", ArchiveState.ARCHIVED, 5)
    assert run(svc.reconcile()) == 1
    assert topics.calls == [(c.tg_topic_id, True)] and dao.get("s1", "r1").state is S.ARCHIVED
    assert run(svc.reconcile()) == 0  # stable
    threads.set_archive_state("s1", "r1", ArchiveState.ACTIVE, 6)
    assert run(svc.reconcile()) == 1
    assert topics.calls[-1] == (c.tg_topic_id, False) and dao.get("s1", "r1").state is S.OPEN
    assert len(topics.created) == 1


def test_no_consultant_means_no_topic_ever(env):
    svc, topics, dao, threads, conn = env
    threads.set_archive_state("s1", "r1", ArchiveState.ARCHIVED, 5)
    threads.set_archive_state("s1", "r1", ArchiveState.ACTIVE, 6)
    assert run(svc.reconcile()) == 0
    assert topics.created == [] and topics.calls == []
    assert conn.execute("SELECT COUNT(*) FROM consultants").fetchone()[0] == 0


def test_unverified_or_missing_thread_never_closes_anything(env):
    svc, topics, dao, threads, conn = env
    run(svc.create("r1"))
    threads.upsert_many([thread("r1", ArchiveState.UNKNOWN)])  # unknown never overwrites a verified state
    assert run(svc.reconcile()) == 0
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("DELETE FROM codex_threads WHERE thread_id='r1'")  # thread merely missing
    assert run(svc.reconcile()) == 0 and topics.calls == []


def test_deleted_topic_becomes_error_not_recreated(env):
    svc, topics, dao, threads, conn = env
    c = run(svc.create("r1"))
    topics.gone.add(c.tg_topic_id)
    threads.set_archive_state("s1", "r1", ArchiveState.ARCHIVED, 5)
    assert run(svc.reconcile()) == 0
    assert dao.get("s1", "r1").state is S.ERROR and len(topics.created) == 1


def test_session_key_is_deterministic_lowercase_and_separator_free():
    k = session_key("s1", "019ABC-Def")
    assert k == session_key("s1", "019ABC-Def") and k != session_key("s1", "019abc-def")
    assert k != session_key("s2", "019ABC-Def")
    assert k.startswith("cqb-") and k == k.lower() and k[4:].isalnum() and len(k) == 30


def test_topic_name_limits():
    assert len(topic_name(thread(name="x" * 300))) == 128
    assert topic_name(thread(name="  a \n b ")) == "a b"
    assert topic_name(thread("abcdef012345", name=None)) == "Thread abcdef01"


def test_on_open_fires_for_create_and_reopen_but_not_for_repeats_or_closing(env):
    svc, topics, dao, threads, conn = env
    opened = []
    svc._on_open = opened.append
    run(svc.create("r1"))
    run(svc.create("r1"))  # already open: not news
    assert opened == ["r1"]
    threads.set_archive_state("s1", "r1", ArchiveState.ARCHIVED, 5)
    run(svc.reconcile())  # closing
    assert opened == ["r1"]
    threads.set_archive_state("s1", "r1", ArchiveState.ACTIVE, 6)
    run(svc.reconcile())  # reopening
    assert opened == ["r1", "r1"]
