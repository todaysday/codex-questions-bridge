"""/status in consultant topics and the registry "ℹ️ Подробнее" button: wiring, edit in place, errors."""

import asyncio

from cqb.domain.enums import ArchiveState, RunState as R
from cqb.telegram.fmt import TEXT_LIMIT
from fake_telegram import OWNER
from test_tg_control import STRANGER, answer_for, click
from tg_stack import NOW, make_stack, snapshot, thread


def run(coro):
    return asyncio.run(coro)


def cards(st, topic):
    return [(i, m) for i, m in st.fake.messages.items() if m["thread_id"] == topic and not m.get("deleted")
            and m["text"].startswith("📊")]


async def consultant_topic(st, root="r1"):
    await st.router._create(root)
    return st.consultant_dao.get("s1", root).tg_topic_id


def test_status_command_posts_one_card_and_edits_it_in_place(conn):
    async def main():
        async with make_stack(conn, thread("r1", name="Fox")) as st:
            st.status_source.threads["r1"] = snapshot("r1", R.WORKING, updated=NOW - 5, name="Fox", model="gpt-6.1")
            topic = await consultant_topic(st)
            st.fake.push_message("/status", topic)
            await st.pump()
            [(mid, card)] = cards(st, topic)
            assert "🟢 работает · gpt-6.1" in card["text"] and len(card["text"]) <= TEXT_LIMIT
            assert st.relay.got == []  # never relayed to the consultant
            assert st.state.get("status.msg.r1") == f"{topic}:{mid}"
            st.clock.now += 120
            st.status_source.threads["r1"] = snapshot("r1", R.IDLE, updated=NOW - 5, name="Fox")
            st.fake.push_message("/status@cqb_test_bot", topic)
            await st.pump()
            [(mid2, card2)] = cards(st, topic)
            assert mid2 == mid and card2["edits"] == 1 and "⚪ простаивает" in card2["text"]
    run(main())


def test_deleted_card_is_replaced_and_other_topics_get_their_own(conn):
    async def main():
        async with make_stack(conn, thread("r1", name="Fox")) as st:
            st.status_source.threads["r1"] = snapshot("r1")
            topic = await consultant_topic(st)
            st.fake.push_message("/status", topic)
            await st.pump()
            [(old, _)] = cards(st, topic)
            st.fake.delete_message(old)
            st.clock.now += 10
            st.fake.push_message("/status", topic)
            await st.pump()
            [(new, _)] = cards(st, topic)
            assert new != old and st.state.get("status.msg.r1") == f"{topic}:{new}"
            # the registry button asks from the control topic: a separate message, the consultant's stays
            click(st, st.registry_message, "Подробнее")
            await st.pump()
            assert len(cards(st, st.control_topic)) == 1 and len(cards(st, topic)) == 1
    run(main())


def test_registry_button_sends_card_to_control_topic_without_a_consultant(conn):
    async def main():
        async with make_stack(conn, thread("r1", name="Fox", updated=NOW - 10), thread("r2", name="Two")) as st:
            st.status_source.threads["r1"] = snapshot("r1", R.WAITING_INPUT)
            assert st.consultant_dao.get("s1", "r1") is None
            q = click(st, st.registry_message, "1. ℹ️ Подробнее")
            await st.pump()
            assert answer_for(st, q)["text"] == "Статус отправлен"
            [(mid, card)] = cards(st, st.control_topic)
            assert "🟡 ждёт ответа" in card["text"]
            assert st.relay.got == [] and st.consultant_dao.get("s1", "r1") is None
            q = click(st, st.registry_message, "Подробнее")  # again: edited, not duplicated
            await st.pump()
            assert [i for i, _ in cards(st, st.control_topic)] == [mid]
            assert st.status_source.calls.count("thread") == 1  # per-root rate limit absorbs the second press
    run(main())


def test_unavailable_data_is_reported_not_crashed(conn):
    async def main():
        async with make_stack(conn, thread("r1", name="Fox")) as st:
            topic = await consultant_topic(st)
            st.status_source.fail = True
            q = click(st, st.registry_message, "Подробнее")
            st.fake.push_message("/status", topic)
            await st.pump()
            a = answer_for(st, q)
            assert a["show_alert"] and "Нет данных" in a["text"]
            assert any("Нет данных" in m["text"] for m in st.fake.live_messages(topic))
            assert cards(st, topic) == [] and st.state.get("status.msg.r1") is None
    run(main())


def test_status_for_unknown_thread_and_unauthorized_users(conn):
    async def main():
        async with make_stack(conn, thread("r1", name="Fox")) as st:
            st.status_source.threads["r1"] = snapshot("r1")
            topic = await consultant_topic(st)
            before = len(st.fake.methods("sendMessage"))
            st.fake.push_message("/status", topic, user_id=STRANGER)
            st.fake.push_callback(st.fake.button(st.registry_message, "Подробнее")["callback_data"],
                                  st.registry_message, user_id=STRANGER)
            await st.pump()
            assert len(st.fake.methods("sendMessage")) == before and st.status_source.calls == []
            # a registry row whose thread vanished
            msg = st.registry_message
            data = st.fake.button(msg, "Подробнее")["callback_data"]
            st.conn.execute("DELETE FROM consultants")
            st.conn.execute("DELETE FROM codex_threads WHERE thread_id='r1'")
            q = st.fake.push_callback(data, msg)
            await st.pump()
            assert answer_for(st, q)["text"] == "Тред не найден"
    run(main())


def test_status_in_other_topics_and_closed_consultants_is_ignored(conn):
    async def main():
        async with make_stack(conn, thread("r1", name="Fox")) as st:
            st.status_source.threads["r1"] = snapshot("r1")
            topic = await consultant_topic(st)
            st.fake.push_message("/status", st.control_topic)
            st.fake.push_message("/status", st.inbox_topic)
            st.fake.push_message("/status", 9999)
            st.threads.set_archive_state("s1", "r1", ArchiveState.ARCHIVED, 1)
            await st.consultants.reconcile()
            st.fake.push_message("/status", topic)
            await st.pump()
            assert st.status_source.calls == [] and st.relay.got == []
    run(main())


def test_opening_message_mentions_status(conn):
    async def main():
        async with make_stack(conn, thread("r1", name="Fox")) as st:
            topic = await consultant_topic(st)
            assert any("/status" in m["text"] for m in st.fake.live_messages(topic))
    run(main())
