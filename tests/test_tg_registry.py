"""The single self-updating registry message: markers, order, event-driven refresh, paging, one message only."""

import asyncio

from cqb.codex.events import ThreadStatusChanged
from cqb.domain.enums import ArchiveState, RunState as R
from cqb.telegram.keys import ONE_TIME
from tg_stack import NOW, Stack, make_stack, thread
from test_tg_control import click


def run(coro):
    return asyncio.run(coro)


def rows(st):
    """(marker prefix, name) of each row in the registry text."""
    out = []
    for line in st.fake.messages[st.registry_message]["text"].splitlines():
        head, _, rest = line.partition(". ")
        if head.isdigit():
            marks = rest.split(" ")[0] if rest[:1] in ("❓", "⚙") else ""
            out.append((marks, rest[len(marks):].strip()))
    return out


async def status(st, thread_id, state):
    await st.live.handle(ThreadStatusChanged(thread_id, state is R.WAITING_INPUT, False, state))


def test_markers_and_order_follow_questions_and_working_threads(conn):
    async def main():
        async with make_stack(conn, thread("r1", name="Idle", updated=NOW - 10), thread("r2", name="Old", updated=NOW - 900),
                              thread("r3", name="Busy", updated=NOW - 500)) as st:
            assert rows(st) == [("", "Idle"), ("", "Busy"), ("", "Old")]
            await st.ask("r2", "i1")  # no consultant: only the marker, no card, no alert
            await st.settle()
            assert rows(st)[0] == ("❓", "Old") and len(st.control_messages()) == 1
            await status(st, "r3", R.WORKING)
            await st.settle()
            assert rows(st) == [("❓", "Old"), ("⚙️", "Busy"), ("", "Idle")]
            await st.ask("r3", "i2", request_id=2)  # both markers on one row, and it moves up by the question rule
            await st.settle()
            assert rows(st)[:2] == [("❓⚙️", "Busy"), ("❓", "Old")]
            await st.resolve("r2")
            await status(st, "r3", R.IDLE)
            await st.resolve("r3", request_id=2)
            await st.settle()
            assert rows(st) == [("", "Idle"), ("", "Busy"), ("", "Old")]
    run(main())


def test_only_a_working_flip_of_a_root_marks_the_registry_dirty(conn):
    async def main():
        async with make_stack(conn, thread("r1"), thread("w1", parent="r1")) as st:
            wake = st.publisher._wake
            wake.clear()
            await status(st, "r1", R.IDLE)  # first broadcast after (re)connect: may differ from the stored status
            assert wake.is_set()
            wake.clear()
            await status(st, "r1", R.IDLE)
            await status(st, "r1", R.NOT_LOADED)
            assert not wake.is_set()
            await status(st, "r1", R.WORKING)
            assert wake.is_set()
            wake.clear()
            for _ in range(5):  # a busy thread broadcasts all the time: still nothing to do
                await status(st, "r1", R.WORKING)
            await status(st, "w1", R.WORKING)  # a subagent is not a registry row
            await status(st, "ghost", R.WORKING)  # unknown thread
            assert not wake.is_set()
            await status(st, "r1", R.WAITING_INPUT)
            assert wake.is_set()
    run(main())


def test_stored_poll_status_is_the_fallback_for_the_working_marker(conn):
    async def main():
        async with make_stack(conn, thread("r1", name="Fox")) as st:
            import dataclasses
            st.threads.upsert_many([dataclasses.replace(thread("r1", name="Fox"), run_state=R.WORKING)])
            await st.publisher.refresh()
            assert rows(st) == [("⚙️", "Fox")]
            await status(st, "r1", R.IDLE)  # the live broadcast wins over the stored one
            await st.settle()
            assert rows(st) == [("", "Fox")]
    run(main())


def test_changes_that_alter_the_text_mark_it_and_unchanged_content_is_not_written(conn):
    async def main():
        async with make_stack(conn, thread("r1", name="Fox")) as st:
            msg, fake = st.registry_message, st.fake
            edits = lambda: fake.messages[msg].get("edits", 0)  # noqa: E731
            await st.ask("r1", "i1")
            await st.settle()
            assert edits() == 1
            await st.ask("r1", "i2", request_id=2)  # a second question: the row looks the same
            st.clock.now += 3600
            await st.settle()
            assert edits() == 1
            await st.consultants.create("r1")
            st.publisher.request_refresh()  # what the create button does
            await st.settle()
            assert edits() == 2 and "консультант: ✅" in fake.messages[msg]["text"]
            st.threads.set_archive_state("s1", "r1", ArchiveState.ARCHIVED, 9)
            st.publisher.request_refresh()
            await st.consultants.reconcile()
            await st.settle()
            assert edits() == 3 and "Нет тредов" in fake.messages[msg]["text"]  # archived: gone from the active list
    run(main())


def test_paging_edits_the_same_message_and_the_page_is_remembered(conn):
    async def main():
        many = [thread(f"r{i:02d}", updated=NOW - i, name=f"Thread {i:02d}") for i in range(7)]
        async with make_stack(conn, *many) as st:
            msg, fake = st.registry_message, st.fake
            sent_before = len(fake.methods("sendMessage"))
            click(st, msg, "▶️")
            await st.pump()  # no settle: the page switch itself edits the message
            assert "Thread 05" in fake.messages[msg]["text"] and st.registry_message == msg
            assert st.state.get("registry.view") == "1:0" and st.publisher.view() == (1, False)
            assert len(fake.methods("sendMessage")) == sent_before and fake.messages[msg]["edits"] == 1
            await st.settle()
            assert st.registry_message == msg
            again = Stack(conn, fake)  # restart: the page comes from bot_state
            await again.api.start()
            assert again.publisher.view() == (1, False)
            await again.publisher.refresh(force=True)
            assert "Thread 05" in fake.messages[msg]["text"] and "Thread 00" not in fake.messages[msg]["text"]
            await again.api.close()
            click(st, msg, "◀️")
            await st.pump()
            assert "Thread 00" in fake.messages[msg]["text"] and st.publisher.view() == (0, False)
            # a page that no longer exists falls back to the first one
            st.publisher.set_view(9, False)
            await st.publisher.refresh()
            assert st.publisher.view() == (0, False) and "Thread 00" in fake.messages[msg]["text"]
    run(main())


def test_no_refresh_or_status_button_anywhere_and_key_kinds(conn):
    async def main():
        many = [thread(f"r{i}", updated=NOW - i) for i in range(7)]
        async with make_stack(conn, *many) as st:
            msg = st.registry_message
            await st.consultants.create("r0")
            for nav in ("▶️", "◀️", None):
                texts = [b["text"] for b in st.fake.buttons(msg)]
                assert not [t for t in texts if "🔄" in t or "Обновить" in t or "ℹ️" in t or "Подробнее" in t], texts
                assert not [b for b in st.fake.buttons(msg) if "callback_data" in b and
                            st.keys._actions.get(b["callback_data"], 0).action in ("refresh", "status")]
                if nav:
                    click(st, msg, nav)
                    await st.pump()
    assert ONE_TIME == {"create", "opt", "send", "cancel", "aopt", "asend", "acancel", "unarm"}
    run(main())


def test_one_message_in_the_control_topic_across_a_full_scenario(conn):
    async def main():
        async with make_stack(conn, thread("r1", name="Fox"), thread("r2", name="Two", updated=NOW - 900),
                              thread("old", name="Gone", state=ArchiveState.ARCHIVED)) as st:
            fake, control = st.fake, st.control_topic
            await st.ask("r1", "i1")                       # question before any consultant
            await st.settle()
            topic = await st.open_consultant("r1")         # consultant created: card appears in ITS topic
            await st.settle()
            card = st.card_of(st.qid_of("i1"))
            assert fake.messages[card]["thread_id"] == topic
            fake.push_callback(fake.button(card, "A")["callback_data"], card)
            await st.pump()
            fake.push_callback(fake.button(card, "Отправить")["callback_data"], card)
            await st.pump()
            await st.resolve("r1")                         # answered
            await status(st, "r2", R.WORKING)
            await st.settle()
            for text, where in (("/threads", control), ("/status", control), ("/questions", control), ("/thread", control),
                                ("free text", control), ("/status", topic), ("/questions", topic)):
                fake.push_message(text, where)
            click(st, st.registry_message, "Архивные")
            await st.pump()
            click(st, st.registry_message, "Активные")
            await st.pump()
            st.threads.set_archive_state("s1", "r1", ArchiveState.ARCHIVED, 9)
            await st.consultants.reconcile()               # consultant closed with its thread
            st.publisher.request_refresh()
            await st.settle()
            sent_to_control = [p for p in fake.methods("sendMessage") if p.get("message_thread_id") == control]
            assert len(sent_to_control) == 1               # the registry itself, once, never anything else
            assert len(st.control_messages()) == 1 and fake.pinned == [st.registry_message]
            assert [i for i, m in fake.messages.items() if m["thread_id"] == control] == [st.registry_message]
            assert fake.topic_named("📥 Inbox") == [] and len(fake.topics) == 2  # control + the consultant
    run(main())
