"""Registry, service topics, consultants, archive mirroring and authorization over a fake Telegram."""

import asyncio
import time


from cqb.domain.enums import ArchiveState, ConsultantState as S
from cqb.telegram.keys import ACTION_TTL
from fake_telegram import OWNER
from tg_stack import NOW, Stack, make_stack, snapshot, thread

STRANGER = 4242


def run(coro):
    return asyncio.run(coro)


def click(st: Stack, message_id, part, user=OWNER):
    return st.fake.push_callback(st.fake.button(message_id, part)["callback_data"], message_id, user)


def answer_for(st: Stack, query_id):
    return next((a for a in st.fake.callback_answers if a["callback_query_id"] == query_id), None)


def test_control_topic_created_once_message_pinned_and_no_inbox_topic(conn):
    async def main():
        async with make_stack(conn, thread("r1", name="FoxProfile")) as st:
            fake = st.fake
            assert fake.topic_named("📋 Треды Codex") == [st.control_topic]
            assert fake.topic_named("📥 Inbox") == [] and len(fake.topics) == 1
            msg = st.registry_message
            assert fake.messages[msg]["thread_id"] == st.control_topic and fake.pinned == [msg]
            assert "FoxProfile" in fake.messages[msg]["text"]
            again = Stack(conn, fake)  # restart: ids come from bot_state
            await again.api.start()
            await again.topics.ensure()
            assert len(fake.topics) == 1
            await again.api.close()
    run(main())


def test_registry_is_edited_only_when_content_changes(conn):
    async def main():
        async with make_stack(conn, thread("r1", name="FoxProfile")) as st:
            msg, fake = st.registry_message, st.fake
            st.clock.now += 60  # no clock in the registry, and the age text has not changed yet
            assert await st.publisher.refresh() is False
            assert fake.messages[msg].get("edits", 0) == 0
            st.threads.upsert_many([thread("r2", name="Second", updated=NOW)])
            assert await st.publisher.refresh() is True
            assert fake.messages[msg]["edits"] == 1 and "Second" in fake.messages[msg]["text"]
            assert await st.publisher.refresh() is False
            assert fake.messages[msg]["edits"] == 1
            # a message older than the callback ttl window is re-rendered so its buttons stay valid
            st.clock.now += 7 * 3600
            assert await st.publisher.refresh() is True
    run(main())


def test_refresh_marks_are_coalesced_into_one_edit(conn):
    async def main():
        async with make_stack(conn, thread("r1")) as st:
            msg = st.registry_message
            st.threads.upsert_many([thread("r2", name="New")])
            for _ in range(10):
                st.publisher.request_refresh()
            await st.settle()
            assert st.fake.messages[msg]["edits"] == 1
            await st.settle()  # nothing marked since: no pass at all
            assert st.fake.messages[msg]["edits"] == 1
    run(main())


def test_running_publisher_makes_at_most_one_edit_per_interval(conn):
    async def main():
        async with make_stack(conn, thread("r1", updated=NOW - 10)) as st:
            msg, fake = st.registry_message, st.fake
            st.publisher._interval = 0.3
            worker = asyncio.create_task(st.publisher.run())
            try:
                await asyncio.sleep(0.05)  # no mark yet: no pass, no edit
                assert fake.messages[msg].get("edits", 0) == 0
                for i in range(6):  # a burst of real changes, each marked dirty
                    st.threads.upsert_many([thread(f"n{i}", name=f"Burst {i}", updated=NOW - i)])
                    st.publisher.request_refresh()
                    await asyncio.sleep(0.02)
                assert fake.messages[msg]["edits"] == 1  # the first mark is served at once...
                assert "Burst 4" not in fake.messages[msg]["text"]  # ...the others wait for the window to end
                await asyncio.sleep(0.4)
                assert fake.messages[msg]["edits"] == 2 and "Burst 4" in fake.messages[msg]["text"]  # one edit
                await asyncio.sleep(0.4)
                assert fake.messages[msg]["edits"] == 2  # idle: no edits without marks
            finally:
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)
    run(main())


def test_unchanged_render_is_not_edited_even_when_marked_or_after_the_safety_period(conn):
    async def main():
        async with make_stack(conn, thread("r1")) as st:
            msg, fake = st.registry_message, st.fake
            st.publisher._safety = 0.05
            st.publisher._interval = 0
            st.clock.now += 120  # only the time header would differ
            worker = asyncio.create_task(st.publisher.run())
            try:
                st.publisher.request_refresh()
                await asyncio.sleep(0.25)  # several marked and periodic passes
                assert fake.messages[msg].get("edits", 0) == 0 and not fake.methods("editMessageText")
            finally:
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)
    run(main())


def test_deleted_registry_message_is_recreated_and_pinned(conn):
    async def main():
        async with make_stack(conn, thread("r1")) as st:
            old = st.registry_message
            st.fake.delete_message(old)
            assert await st.publisher.refresh(force=True) is True
            new = st.registry_message
            assert new != old and st.fake.pinned == [old, new]
            assert not st.fake.messages[new].get("deleted")
    run(main())


def test_deleted_control_topic_is_recreated(conn):
    async def main():
        async with make_stack(conn, thread("r1")) as st:
            old_topic = st.control_topic
            st.fake.delete_topic(old_topic)
            await st.publisher.refresh(force=True)
            assert st.control_topic != old_topic
            assert st.fake.messages[st.registry_message]["thread_id"] == st.control_topic
            assert len(st.fake.topic_named("📋 Треды Codex")) == 1
    run(main())


def test_create_double_click_makes_exactly_one_topic(conn):
    async def main():
        async with make_stack(conn, thread("r1", name="FoxProfile — User Access")) as st:
            msg, fake = st.registry_message, st.fake
            q1 = click(st, msg, "Создать")
            q2 = click(st, msg, "Создать")
            await st.pump()
            assert fake.topic_named("FoxProfile — User Access") and len(fake.topics) == 2  # control + 1
            assert "создан" in answer_for(st, q1)["text"]
            assert "устарела" in answer_for(st, q2)["text"]  # the one-time key is used up
            topic = st.consultant_dao.get("s1", "r1").tg_topic_id
            opening = fake.live_messages(topic)
            assert len(opening) == 1 and "Консультант: FoxProfile — User Access" in opening[0]["text"]
            await st.settle()
            url = fake.button(msg, "Открыть")["url"]
            assert url == f"https://t.me/c/1234567890/{topic}"
            assert not [b for b in fake.buttons(msg) if "Создать" in b["text"]]
    run(main())


def test_concurrent_creates_from_two_batches_still_one_topic(conn):
    async def main():
        async with make_stack(conn, thread("r1")) as st:
            await asyncio.gather(st.router._create("r1"), st.router._create("r1"))
            assert len(st.fake.methods("createForumTopic")) == 2  # control + one consultant
    run(main())


def test_callback_expiry_and_stale_buttons_heal_the_message(conn):
    async def main():
        async with make_stack(conn, thread("r1")) as st:
            msg = st.registry_message
            q = click(st, msg, "Создать")
            st.clock.now += ACTION_TTL + 1
            st.threads.upsert_many([thread("r1", updated=int(st.clock.now) - 60)])  # still a relevant (recent) thread
            await st.pump()
            assert "устарела" in answer_for(st, q)["text"]
            assert st.consultant_dao.get("s1", "r1") is None and len(st.fake.topics) == 1
            await st.settle()  # healed: re-rendered with fresh keys
            q = click(st, msg, "Создать")
            await st.pump()
            assert "создан" in answer_for(st, q)["text"]
    run(main())


def test_navigation_buttons_are_reusable_until_expiry(conn):
    async def main():
        async with make_stack(conn, thread("r1", name="Fox")) as st:
            msg = st.registry_message
            q1, q2 = click(st, msg, "Архивные"), click(st, msg, "Архивные")  # navigation is not one-time
            await st.pump()
            for q in (q1, q2):
                assert answer_for(st, q) is not None and answer_for(st, q).get("text") is None
    run(main())


def test_unauthorized_input_never_reaches_services(conn):
    async def main():
        async with make_stack(conn, thread("r1")) as st:
            fake, msg = st.fake, st.registry_message
            # a consultant exists, so there is a registered topic to attack
            await st.router._create("r1")
            topic = st.consultant_dao.get("s1", "r1").tg_topic_id
            await st.settle()
            fake.push_callback(fake.button(msg, "Архивные")["callback_data"], msg, user_id=STRANGER)
            fake.push_callback(fake.button(msg, "Архивные")["callback_data"], msg, chat_id=-100555)
            fake.push_message("hi", topic, user_id=STRANGER)
            fake.push_message("hi", topic, chat_id=-100555)
            fake.push_message("hi", topic, forwarded=True)
            fake.push_message("hi", topic, sender_chat=True)
            fake.push_message("hi", topic, user_id=OWNER, is_bot=True)
            fake.push_message("/threads", st.control_topic, user_id=STRANGER)
            fake.push_message("/questions", topic, user_id=STRANGER)
            await st.pump()
            assert st.relay.got == [] and st.inventory.polls == 0 and st.publisher.view() == (0, False)
            assert fake.callback_answers == []  # strangers get no confirmation at all
            assert st.router.rejected == 9
    run(main())


def test_unknown_topics_and_general_topic_are_ignored(conn):
    async def main():
        async with make_stack(conn, thread("r1")) as st:
            st.fake.push_message("hello", 9999)
            st.fake.push_message("hello", None)
            st.fake.push_message("/thread", None)
            st.fake.push_message("free text", st.control_topic)
            st.fake.push_message("/questions", st.control_topic)
            await st.pump()
            assert st.relay.got == [] and len(st.fake.methods("sendMessage")) == 1  # only the registry
    run(main())


def test_commands_in_the_right_topics_and_relay_of_free_text(conn):
    async def main():
        async with make_stack(conn, thread("r1", name="Fox"), thread("r2", name="Other")) as st:
            fake = st.fake
            topic = await st.open_consultant("r1")
            await st.settle()
            edits_before = fake.messages[st.registry_message].get("edits", 0)
            st.clock.now += 3600  # the forced re-render shows the new ages
            cmd = fake.push_message("/threads", st.control_topic)
            fake.push_message("/thread@cqb_test_bot", topic)
            fake.push_message("/thread@other_bot", topic)
            fake.push_message("why option A?", topic)
            fake.push_message("/status", topic)
            fake.push_message("/questions", topic)
            await st.pump()
            assert st.inventory.polls == 1
            assert fake.messages[st.registry_message]["edits"] == edits_before + 1  # /threads re-rendered it
            assert cmd in fake.deleted_user_messages  # and removed the command message (as it did the /questions one)
            texts = [m["text"] for m in fake.live_messages(topic)]
            assert not any(t.startswith("Тред:") for t in texts)  # /thread is disabled: nothing is answered by the Bridge
            assert any(t == "Нет неотвеченных вопросов" for t in texts)
            # /status is handled by the Bridge, not relayed; /thread (ours or not) is like any unknown slash text
            assert st.relay.got == [("r1", "/thread@cqb_test_bot"), ("r1", "why option A?")]
            await st.ask("r2", "i1")
            await st.ask("r1", "i2", request_id=2)
            fake.push_message("/questions", topic)
            fake.push_message("/questions", st.control_topic)  # not a consultant topic: ignored
            await st.pump()
            mine = [m["text"] for m in fake.live_messages(topic)][-1]
            assert mine.startswith("❓") and "Fox" in mine and "Other" not in mine
            assert len(st.control_messages()) == 1
    run(main())


def test_threads_command_survives_missing_delete_rights(conn):
    async def main():
        async with make_stack(conn, thread("r1")) as st:
            st.fake.fail("deleteMessage", 400, "Bad Request: message can't be deleted")
            st.clock.now += 3600
            st.fake.push_message("/threads", st.control_topic)
            await st.pump()
            assert st.inventory.polls == 1 and len(st.control_messages()) == 1
            assert st.fake.messages[st.registry_message]["edits"] == 1  # still re-rendered
    run(main())


def test_messages_in_a_closed_consultant_topic_are_ignored(conn):
    async def main():
        async with make_stack(conn, thread("r1")) as st:
            await st.router._create("r1")
            topic = st.consultant_dao.get("s1", "r1").tg_topic_id
            st.threads.set_archive_state("s1", "r1", ArchiveState.ARCHIVED, 1)
            await st.consultants.reconcile()
            st.fake.push_message("anyone there?", topic)
            await st.pump()
            assert st.relay.got == []
    run(main())


def test_archive_closes_unarchive_reopens_same_topic_and_no_consultant_no_topic(conn):
    async def main():
        async with make_stack(conn, thread("r1", name="Fox"), thread("r2", name="Plain")) as st:
            fake = st.fake
            await st.router._create("r1")
            topic = st.consultant_dao.get("s1", "r1").tg_topic_id
            before = set(fake.topics)
            for tid in ("r1", "r2"):
                st.threads.set_archive_state("s1", tid, ArchiveState.ARCHIVED, 1)
            await st.consultants.reconcile()
            assert fake.topics[topic]["closed"] is True
            assert st.consultant_dao.get("s1", "r1").state is S.ARCHIVED
            for tid in ("r1", "r2"):
                st.threads.set_archive_state("s1", tid, ArchiveState.ACTIVE, 2)
            await st.consultants.reconcile()
            assert fake.topics[topic]["closed"] is False
            assert set(fake.topics) == before  # r2 never got a topic
            assert fake.methods("closeForumTopic") and len(fake.methods("closeForumTopic")) == 1
    run(main())


def test_paging_and_archived_toggle(conn):
    async def main():
        many = [thread(f"r{i:02d}", updated=NOW - i, name=f"Thread {i:02d}") for i in range(7)]
        async with make_stack(conn, *many, thread("old", name="Gone", state=ArchiveState.ARCHIVED)) as st:
            msg, fake = st.registry_message, st.fake
            assert "Thread 00" in fake.messages[msg]["text"] and "Thread 06" not in fake.messages[msg]["text"]
            click(st, msg, "▶️")
            await st.pump()
            await st.settle()
            text = fake.messages[msg]["text"]
            assert "Thread 05" in text and "Thread 00" not in text
            click(st, msg, "Архивные")
            await st.pump()
            await st.settle()
            assert "Gone" in fake.messages[msg]["text"] and fake.messages[msg]["text"].startswith("📦")
    run(main())


def test_poll_offset_is_persisted_and_updates_are_not_replayed(conn):
    async def main():
        async with make_stack(conn, thread("r1")) as st:
            st.fake.push_message("/threads", st.control_topic)
            await st.pump()
            assert st.inventory.polls == 1 and st.state.get_int("poll_offset") > 1
            await st.pump()
            restarted = Stack(conn, st.fake)
            await restarted.api.start()
            await restarted.pump()
            assert restarted.inventory.polls == 0 and st.inventory.polls == 1
            await restarted.api.close()
    run(main())


def test_poller_survives_handler_errors_and_still_advances(conn):
    async def main():
        async with make_stack(conn, thread("r1")) as st:
            calls = []

            async def boom(update):
                calls.append(update.update_id)
                raise RuntimeError("x")
            st.poller._handle = boom
            st.fake.push_message("a", st.control_topic)
            st.fake.push_message("b", st.control_topic)
            await st.pump()
            assert len(calls) == 2 and st.state.get_int("poll_offset") == calls[-1] + 1
    run(main())
