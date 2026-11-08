"""Agent-message question detection: the watcher reads thread items of a fake daemon (no resume, no subscribe)."""

import asyncio
import contextlib
import json
import logging


from conftest import connected_client
from cqb.codex.agent_reply import answered_questions, build_reply
from cqb.codex.events import ThreadArchiveChanged, ThreadStatusChanged, agent_questions_from_item
from cqb.dao import db
from cqb.dao.agent_questions import AgentQuestionDao
from cqb.dao.threads import ThreadDao
from cqb.domain.enums import AgentQuestionState as S, ArchiveState, RunState
from cqb.services import agent_questions as aq
from cqb.services.agent_questions import AgentQuestionService
from fake_appserver import agent_message, other_item, user_message, wait_until
from tg_stack import NOW, Clock, thread

Q1 = [{"title": "Where to store the settings?", "options": ["Existing table", "Separate table"]}]
Q3 = [{"title": f"Question {i}", "options": [f"A{i}", f"B{i}"]} for i in range(3)]


def run(coro):
    return asyncio.run(coro)


def entry(item, turn="t1"):
    return {"turnId": turn, "item": item}


@contextlib.asynccontextmanager
async def watcher(conn, *threads, **kw):
    async with connected_client() as (client, fake):
        clock = Clock()
        dao, threads_dao = AgentQuestionDao(conn), ThreadDao(conn)
        threads_dao.upsert_many(threads or (thread("r1"),))
        svc = AgentQuestionService(client, dao, threads_dao, "s1", clock=clock, debounce=0, **kw)
        events = []

        async def opened(q):
            events.append(("pending", q.agent_question_id))

        async def closed(q):
            events.append((q.state.value, q.agent_question_id))

        svc.subscribe(on_question=opened, on_closed=closed)
        yield svc, fake, dao, events


def put(fake, thread_id, *items, turn="t1"):
    fake.items.setdefault(thread_id, []).extend(entry(i, turn) for i in items)


def item_calls(fake):
    return [p for m, p in fake.calls if m == "thread/items/list"]


def test_pending_question_from_items_keeps_titles_and_options_only(conn):
    async def main():
        async with watcher(conn) as (svc, fake, dao, events):
            put(fake, "r1", other_item("a"), agent_message("call_1", Q1), other_item("b"))
            await svc.scan_thread("r1")
            [q] = svc.open_questions("r1")
            assert (q.thread_id, q.root_thread_id, q.item_id, q.turn_id, q.state) == ("r1", "r1", "call_1", "t1", S.PENDING)
            assert q.title == Q1[0]["title"] and q.options == ("Existing table", "Separate table") and q.index == 0
            assert q.answerable and q.card_message_id is None and q.sent_at is None
            assert events == [("pending", q.agent_question_id)]
            dump = "\n".join(conn.iterdump())
            assert "SECRET-TEXT" not in dump  # the message text is never stored
            assert svc.open_count() == 1 and svc.get(q.agent_question_id) == q
            assert fake.methods() == ["thread/items/list"]  # no resume, no subscribe, nothing but one read
    run(main())


def test_dedup_and_the_newest_item_is_not_rescanned(conn):
    async def main():
        async with watcher(conn) as (svc, fake, dao, events):
            put(fake, "r1", agent_message("call_1", Q1), other_item("b"))
            await svc.scan_thread("r1")
            await svc.scan_thread("r1")
            await svc.scan_thread("r1")
            assert len(events) == 1
            calls = item_calls(fake)
            assert [c["limit"] for c in calls] == [aq.PAGE_SIZE, 1, 1]  # later looks are a one-item probe
            put(fake, "r1", other_item("c"))  # a new item: one page read, which stops at the item seen last time
            await svc.scan_thread("r1")
            assert [c["limit"] for c in item_calls(fake)][-2:] == [1, aq.PAGE_SIZE]
            assert len(events) == 1 and len(svc.open_questions()) == 1  # same key: still one question
    run(main())


def test_answered_by_a_later_user_message(conn):
    async def main():
        async with watcher(conn) as (svc, fake, dao, events):
            put(fake, "r1", agent_message("call_1", Q1))
            await svc.scan_thread("r1")
            qid = svc.open_questions()[0].agent_question_id
            put(fake, "r1", other_item("x"), user_message("answer"))
            await svc.scan_thread("r1")
            assert svc.get(qid).state is S.ANSWERED and svc.open_questions() == [] and svc.open_count() == 0
            assert events == [("pending", qid), ("answered", qid)]
            await svc.scan_thread("r1")
            assert len(events) == 2
    run(main())


def test_a_user_message_after_the_question_means_nothing_older_is_pending(conn):
    async def main():
        async with watcher(conn) as (svc, fake, dao, events):
            put(fake, "r1", agent_message("old", Q1), user_message("answer"), other_item("x"))
            await svc.scan_thread("r1")
            assert events == [] and len(item_calls(fake)) == 1  # the walk stopped at the userMessage
            put(fake, "r1", agent_message("call_2", Q1))
            await svc.scan_thread("r1")
            assert [e[0] for e in events] == ["pending"] and svc.open_questions()[0].item_id == "call_2"
    run(main())


def test_new_question_after_our_answer_closes_the_old_one_and_stays_pending_itself(conn):
    async def main():
        async with watcher(conn) as (svc, fake, dao, events):
            put(fake, "r1", agent_message("call_1", Q1))
            await svc.scan_thread("r1")
            put(fake, "r1", user_message("answer"), other_item("x"), agent_message("call_2", Q1))
            await svc.scan_thread("r1")
            states = {q.item_id: q.state for q in map(svc.get, [e[1] for e in events]) if q}
            assert states == {"call_1": S.ANSWERED, "call_2": S.PENDING}
    run(main())


def test_two_unanswered_questions_stay_pending_and_are_capped_per_thread(conn):
    async def main():
        async with watcher(conn) as (svc, fake, dao, events):
            put(fake, "r1", *[agent_message(f"c{i}", Q1) for i in range(5)])
            await svc.scan_thread("r1")
            assert sorted(q.item_id for q in svc.open_questions()) == ["c2", "c3", "c4"]  # the newest three
    run(main())


def test_the_walk_pages_back_to_the_page_cap(conn):
    async def main():
        async with watcher(conn) as (svc, fake, dao, events):
            put(fake, "r1", agent_message("deep", Q1), *[other_item(f"f{i}") for i in range(aq.PAGE_SIZE * 2 + 5)])
            await svc.scan_thread("r1")
            assert [q.item_id for q in svc.open_questions()] == ["deep"] and len(item_calls(fake)) == 3
        async with watcher(conn, thread("r2")) as (svc, fake, dao, events):
            put(fake, "r2", agent_message("too-deep", Q1), *[other_item(f"g{i}") for i in range(aq.PAGE_SIZE * aq.MAX_PAGES)])
            await svc.scan_thread("r2")
            assert svc.open_questions("r2") == [] and len(item_calls(fake)) == aq.MAX_PAGES  # a hard cap on reading
    run(main())


def test_only_roots_are_scanned_and_status_events_coalesce(conn):
    async def main():
        async with watcher(conn, thread("r1"), thread("sub", parent="r1"), thread("r2")) as (svc, fake, dao, events):
            put(fake, "r1", agent_message("c1", Q1))
            put(fake, "sub", agent_message("c2", Q1))
            put(fake, "r2", agent_message("c3", Q1))
            for tid in ("sub", "r1", "r1", "r1", "ghost"):
                await svc.handle(ThreadStatusChanged(tid, False, False, RunState.IDLE))
            await svc.process_dirty()
            assert [p["threadId"] for p in item_calls(fake)] == ["r1"]  # one read: subagents and unknowns are ignored
            await svc.sweep()
            assert sorted(q.thread_id for q in svc.open_questions()) == ["r1", "r2"]
            assert "sub" not in [q.thread_id for q in svc.open_questions()]
    run(main())


def test_sweep_picks_working_recent_and_pending_roots_only(conn):
    async def main():
        old = NOW - 3600
        async with watcher(
            conn, thread("fresh"), thread("stale-idle", updated=old), thread("old-working", updated=old),
            thread("archived", state=ArchiveState.ARCHIVED),
        ) as (svc, fake, dao, events):
            conn.execute("UPDATE codex_threads SET run_state='working' WHERE thread_id='old-working'")
            for t in ("fresh", "stale-idle", "old-working", "archived"):
                put(fake, t, other_item("x"))
            await svc.sweep()
            assert sorted(p["threadId"] for p in item_calls(fake)) == ["fresh", "old-working"]
            # a pending question keeps its thread in the sweep however old it is
            put(fake, "stale-idle", agent_message("c1", Q1))
            await svc.scan_thread("stale-idle")
            fake.calls.clear()
            await svc.sweep()
            assert "stale-idle" in [p["threadId"] for p in item_calls(fake)]
    run(main())


def test_sweep_is_capped_and_rotates_oldest_first(conn, monkeypatch):
    async def main():
        ids = [f"r{i:02d}" for i in range(aq.SWEEP_MAX_THREADS + 5)]
        async with watcher(conn, *[thread(t, updated=NOW - i) for i, t in enumerate(ids)]) as (svc, fake, dao, events):
            for t in ids:
                put(fake, t, other_item("x"))
            await svc.sweep()
            first = [p["threadId"] for p in item_calls(fake)]
            assert len(first) == aq.SWEEP_MAX_THREADS == len(set(first))
            fake.calls.clear()
            await svc.sweep()
            second = [p["threadId"] for p in item_calls(fake)]
            assert set(ids) - set(first) <= set(second[:5])  # the five never read come first
            assert len(second) == aq.SWEEP_MAX_THREADS
    run(main())


def test_archive_event_and_archived_sweep_make_questions_stale(conn):
    async def main():
        async with watcher(conn, thread("r1"), thread("r2")) as (svc, fake, dao, events):
            put(fake, "r1", agent_message("c1", Q1))
            put(fake, "r2", agent_message("c2", Q1))
            await svc.scan_thread("r1")
            await svc.scan_thread("r2")
            await svc.handle(ThreadArchiveChanged("r1", True))
            assert [q.thread_id for q in svc.open_questions()] == ["r2"]
            ThreadDao(conn).set_archive_state("s1", "r2", ArchiveState.ARCHIVED, NOW)
            await svc.sweep()  # the inventory noticed the archive: stale without an event
            assert svc.open_questions() == [] and sorted(e[0] for e in events) == ["pending", "pending", "stale", "stale"]
            await svc.handle(ThreadArchiveChanged("r1", False))  # unarchive does not reopen anything
            assert svc.open_questions() == []
    run(main())


def test_a_deleted_thread_makes_its_question_stale_but_a_flaky_read_does_not(conn):
    async def main():
        async with watcher(conn) as (svc, fake, dao, events):
            fake.add_thread("r1", NOW)
            put(fake, "r1", agent_message("c1", Q1))
            await svc.scan_thread("r1")
            fake.gone.add("r1")  # items/list fails, but thread/read still answers: not gone
            put(fake, "r1", other_item("x"))
            await svc.scan_thread("r1")
            assert len(svc.open_questions()) == 1
            del fake.threads["r1"]  # now thread/read fails too
            await svc.scan_thread("r1")
            assert svc.open_questions() == [] and events[-1][0] == "stale"
    run(main())


def test_no_connection_is_quiet(conn):
    async def main():
        from cqb.codex.appserver import AppServerClient
        svc = AgentQuestionService(AppServerClient("/nonexistent.sock"), AgentQuestionDao(conn), ThreadDao(conn), "s1")
        await svc.scan_thread("r1")
        await svc.sweep()
        assert svc.open_count() == 0
    run(main())


def test_logs_have_ids_and_counts_but_never_text(conn, caplog):
    async def main():
        caplog.set_level(logging.DEBUG)
        async with watcher(conn) as (svc, fake, dao, events):
            put(fake, "r1", agent_message("call_1", Q1, text="PRIVATE-MESSAGE"))
            await svc.scan_thread("r1")
            put(fake, "r1", user_message("PRIVATE-ANSWER"))
            await svc.scan_thread("r1")
    run(main())
    log = caplog.text
    assert "pending" in log and "answered" in log and "index=0" in log
    for secret in ("PRIVATE", "Where to store", "Existing table"):
        assert secret not in log


def test_questions_over_the_limits_are_cut_and_not_answerable(conn):
    async def main():
        async with watcher(conn) as (svc, fake, dao, events):
            long = [{"title": "T" * 25000, "options": ["o" * 9000, *[f"{i}" for i in range(30)]]}]
            put(fake, "r1", agent_message("c1", long))
            await svc.scan_thread("r1")
            [q] = svc.open_questions()
            assert not q.answerable and len(q.title) == 20000 and len(q.options) == 26 and len(q.options[0]) == 8000
            # a question within the bounds is stored whole, however long for one Telegram message
            put(fake, "r1", other_item("x"), agent_message("c2", [{"title": "L" * 19000, "options": ["o" * 7900]}]))
            await svc.scan_thread("r1")
            whole = next(x for x in svc.open_questions() if x.item_id == "c2")
            assert whole.answerable and len(whole.title) == 19000 and len(whole.options[0]) == 7900
    run(main())


def test_item_parser_shapes():
    ok = agent_message("i", [{"title": "t", "options": None}])
    (item,) = agent_questions_from_item(ok)
    assert (item.title, item.options, item.complete) == ("t", (), True)
    for bad in (
        agent_message("i", None), agent_message("i", []), other_item("i"), None, "x",
        agent_message("i", [{"title": 1, "options": []}]), agent_message("i", [{"title": "t", "options": [1]}]),
        agent_message("i", ["t"]), agent_message("i", [{"title": "t", "options": "ab"}]),
    ):
        assert agent_questions_from_item(bad) is None
    many = agent_message("i", [{"title": str(n), "options": []} for n in range(25)])
    items = agent_questions_from_item(many)
    assert len(items) == 20 and [i.title for i in items] == [str(n) for n in range(20)]  # positions are indexes


def test_memory_stays_bounded(conn, monkeypatch):
    async def main():
        monkeypatch.setattr(aq, "MAX_TRACKED", 3)
        monkeypatch.setattr(aq, "MAX_DIRTY", 4)
        ids = [f"r{i}" for i in range(8)]
        async with watcher(conn, *[thread(t) for t in ids]) as (svc, fake, dao, events):
            for t in ids:
                put(fake, t, other_item("x"))
                await svc.scan_thread(t)
                await svc.handle(ThreadStatusChanged(t, False, False, RunState.WORKING))
            assert len(svc._seen) == 3 and len(svc._dirty) == 4
            assert list(svc._seen) == ids[-3:] and all(isinstance(v, tuple) and len(v) == 2 for v in svc._seen.values())
    run(main())


def test_run_loop_reacts_to_status_events_and_sweeps_periodically(conn):
    async def main():
        async with watcher(conn, thread("r1"), thread("r2"), scan_seconds=0.05) as (svc, fake, dao, events):
            task = asyncio.create_task(svc.run())
            try:
                put(fake, "r1", agent_message("c1", Q1))
                await wait_until(lambda: any(q.thread_id == "r1" for q in svc.open_questions()))  # the sweep
                put(fake, "r2", agent_message("c2", Q1))
                await svc.handle(ThreadStatusChanged("r2", False, False, RunState.IDLE))
                await wait_until(lambda: any(q.thread_id == "r2" for q in svc.open_questions()))
                put(fake, "r2", user_message("answer"))
                await wait_until(lambda: [q for q in svc.open_questions() if q.thread_id == "r2"] == [])
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    run(main())


def test_dao_unique_key_counts_postable_and_prune(conn):
    dao = AgentQuestionDao(conn)

    async def main():
        async with watcher(conn) as (svc, fake, _, events):
            put(fake, "r1", agent_message("c1", Q1))
            await svc.scan_thread("r1")
            q = svc.open_questions()[0]
            assert dao.insert_if_new(q) is False  # (source, thread, item, index) is the key
            assert dao.open_counts_by_root("s1") == {"r1": 1}
            assert dao.list_postable("s1") == []  # no open consultant topic
            conn.execute("INSERT INTO consultants VALUES (1,'s1','r1',1,5,'cqb-x','open',1,1,NULL)")
            assert [x.agent_question_id for x in dao.list_postable("s1")] == [q.agent_question_id]
            dao.set_card_messages(q.agent_question_id, [77, 78, 79])
            assert dao.find_by_card_message(77).agent_question_id == q.agent_question_id and dao.list_postable("s1") == []
            assert dao.find_by_card_message(79).card_extra_ids == (78, 79) and dao.find_by_card_message(7) is None
            dao.set_state(q.agent_question_id, S.ANSWERED, 10)
            assert dao.prune_closed("s1", 5) == 0 and dao.prune_closed("s1", 11) == 1
    run(main())


def test_migration_is_forward_only_and_adds_the_table():
    c = db.connect(":memory:")
    c.executescript(f"BEGIN;\n{db.MIGRATIONS[0]}\n{db.MIGRATIONS[1]}\n{db.MIGRATIONS[2]}\nPRAGMA user_version = 3;\nCOMMIT;")
    c.execute("INSERT INTO codex_sources VALUES ('s1','S1','app_server',1)")
    assert db.migrate(c) == len(db.MIGRATIONS) >= 4
    cols = {r[1] for r in c.execute("PRAGMA table_info(agent_questions)")}
    assert {"agent_question_id", "source_id", "thread_id", "root_thread_id", "turn_id", "item_id", "item_index", "title",
            "options_json", "state", "staged", "sent_at", "card_message_id", "card_extra_ids", "display_no",
            "created_at", "updated_at"} <= cols
    assert c.execute("SELECT COUNT(*) FROM questions").fetchone()[0] == 0  # request_user_input table untouched
    assert db.migrate(c) == len(db.MIGRATIONS)


# --- one question per row: a message with several questions ------------------------------------------------

def reply(item_id, *answers):
    """The user message the Codex app (and the Bridge) writes for answered questions: (index, title, answer)."""
    reply.n += 1
    return user_message("".join(build_reply(item_id, i, t, a) for i, t, a in answers), item_id=f"um-reply-{reply.n}")


reply.n = 0


def test_a_message_with_several_questions_gives_one_pending_question_each(conn):
    async def main():
        async with watcher(conn) as (svc, fake, dao, events):
            put(fake, "r1", agent_message("m1", Q3))
            await svc.scan_thread("r1")
            qs = sorted(svc.open_questions("r1"), key=lambda q: q.display_no)
            assert [(q.index, q.title, q.display_no) for q in qs] == [(i, f"Question {i}", i + 1) for i in range(3)]
            assert len({q.agent_question_id for q in qs}) == 3 and all(q.item_id == "m1" for q in qs)
            assert [e[0] for e in events] == ["pending"] * 3 and svc.open_count() == 3
            await svc.scan_thread("r1")  # a rescan finds the same three: nothing new, numbers stay
            put(fake, "r1", other_item("x"))
            await svc.scan_thread("r1")
            assert len(events) == 3 and sorted(q.display_no for q in svc.open_questions("r1")) == [1, 2, 3]
            put(fake, "r1", agent_message("m2", Q1))
            await svc.scan_thread("r1")
            assert max(q.display_no for q in svc.open_questions("r1")) == 4
    run(main())


def test_a_reply_naming_a_question_answers_exactly_that_one_and_the_walk_goes_on(conn):
    async def main():
        async with watcher(conn) as (svc, fake, dao, events):
            put(fake, "r1", agent_message("m1", Q3))
            await svc.scan_thread("r1")
            by_index = {q.index: q for q in svc.open_questions("r1")}
            put(fake, "r1", other_item("x"), reply("m1", (1, "Question 1", "B1")))
            await svc.scan_thread("r1")
            assert svc.get(by_index[1].agent_question_id).state is S.ANSWERED
            assert svc.get(by_index[0].agent_question_id).state is S.PENDING and svc.get(by_index[2].agent_question_id).state is S.PENDING
            put(fake, "r1", reply("m1", (0, "Question 0", "A0")))
            await svc.scan_thread("r1")
            assert [q.index for q in svc.open_questions("r1")] == [2]
            assert [e for e in events if e[0] == "answered"] == [("answered", by_index[1].agent_question_id), ("answered", by_index[0].agent_question_id)]
    run(main())


def test_a_fresh_service_reads_replies_through_to_the_older_message_and_never_reopens_answered_ones(conn):
    async def main():
        async with watcher(conn) as (svc, fake, dao, events):
            put(fake, "r1", agent_message("m1", Q3), reply("m1", (0, "Question 0", "A0")), other_item("x"),
                reply("m1", (2, "Question 2", "A2")))
            await svc.scan_thread("r1")  # first look: the walk meets the replies first, then the message
            assert [q.index for q in svc.open_questions("r1")] == [1] and [e[0] for e in events] == ["pending"]
    run(main())


def test_a_plain_user_message_answers_every_question_before_it_but_not_the_ones_after(conn):
    async def main():
        async with watcher(conn) as (svc, fake, dao, events):
            put(fake, "r1", agent_message("m1", Q3))
            await svc.scan_thread("r1")
            put(fake, "r1", user_message("just text, no question id"), agent_message("m2", Q3[:2]))
            await svc.scan_thread("r1")
            assert sorted((q.item_id, q.index) for q in svc.open_questions("r1")) == [("m2", 0), ("m2", 1)]
            assert [e[0] for e in events].count("answered") == 3
    run(main())


def test_reply_parsing_names_questions_and_ignores_garbage():
    text = build_reply("m1", 2, 'Title with "quotes"', "A\nB") + build_reply("m9", 0, "t", "x")
    assert answered_questions(text) == {("m1", 2), ("m9", 0)}
    assert answered_questions("plain") is None and answered_questions("") is None
    assert answered_questions("<send_user_message_question_reply>\nnot json\n</send_user_message_question_reply>") is None
    broken = '<send_user_message_question_reply>\n[{"questionItemId":"[\\"other_tag\\",\\"m\\",0]"}]\n</send_user_message_question_reply>'
    assert answered_questions(broken) is None
    assert build_reply("m1", 1, "T", "A") == (
        '<send_user_message_question_reply>\n[{"questionItemId":'
        '"[\\"request_user_input_async\\",\\"m1\\",1]","question":"T","answer":"A"}]\n'
        '</send_user_message_question_reply>\n')


def test_reply_is_the_apps_shape_and_parsing_accepts_both_key_orders_and_trailing_newline():
    app = ('<send_user_message_question_reply>\n[{"questionItemId":"[\\"request_user_input_async\\",\\"m1\\",0]",'
           '"question":"Где?","answer":"B"}]\n</send_user_message_question_reply>\n')
    assert build_reply("m1", 0, "Где?", "B") == app
    old = ('<send_user_message_question_reply>\n[{"question":"Где?","answer":"B","questionItemId":'
           '"[\\"request_user_input_async\\",\\"m1\\",0]"}]\n</send_user_message_question_reply>')
    for text in (app, app.rstrip("\n"), old, old + "\n"):
        assert answered_questions(text) == {("m1", 0)}
    assert list(json.loads(app.split("\n")[1])[0]) == ["questionItemId", "question", "answer"]


def test_migration_6_splits_the_table_into_one_row_per_question(tmp_path):
    c = db.connect(str(tmp_path / "v5.db"))
    c.executescript("BEGIN;\n" + "\n".join(db.MIGRATIONS[:5]) + "\nPRAGMA user_version = 5;\nCOMMIT;")
    c.execute("INSERT INTO codex_sources VALUES ('s1','S1','app_server',1)")
    row = ("INSERT INTO agent_questions(agent_question_id, source_id, thread_id, root_thread_id, turn_id, item_id, items_json, "
           "answerable, state, staged_json, sent_at, card_message_id, display_no, created_at, updated_at) "
           "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)")
    one = '[{"title":"Где?","options":["А","Б"]}]'
    two = '[{"title":"a","options":[]},{"title":"b","options":[]}]'
    c.execute(row, ("a_one", "s1", "r1", "r1", "u", "m1", one, 1, "pending", '{"0":"Б"}', None, 77, 3, 1, 1))
    c.execute(row, ("a_two", "s1", "r1", "r1", "u", "m2", two, 1, "pending", None, None, 78, 4, 1, 1))
    assert db.migrate(c) == len(db.MIGRATIONS) >= 6
    dao = AgentQuestionDao(c)
    assert dao.get("a_two") is None  # a merged row is dropped: the next scan finds its questions again
    q = dao.get("a_one")
    assert (q.title, q.options, q.index, q.staged, q.card_message_id, q.display_no) == ("Где?", ("А", "Б"), 0, "Б", 77, 3)
    assert dao.find_by_card_message(77).agent_question_id == "a_one"
    assert {r[1] for r in c.execute("PRAGMA index_list(agent_questions)")} >= {"idx_agent_question_root", "idx_agent_question_card"}
    assert db.migrate(c) == len(db.MIGRATIONS)
