"""`/questions` in a consultant topic: the old card messages of every pending question of the thread are deleted and fresh
full cards are posted in number order (both kinds, rich and classic); the command message is removed; with nothing pending
a short notice answers and is removed after a few seconds. Failed deletes are ignored."""

import asyncio

import pytest

from cqb.domain.models import QuestionItem, QuestionOption
from tg_stack import agent_item, make_stack, thread

AGENT_Q = [{"title": "Agent asks?", "options": ["Yes", "No"]}]


def run(coro):
    return asyncio.run(coro)


async def three_cards(st):
    """Q1 (request), Q2 and Q3 (agent message); returns (topic, [message ids of the cards in number order])."""
    topic = await st.open_consultant("r1")
    await st.ask("r1", "i1")
    st.agent_client.items["r1"] = [agent_item("m1", AGENT_Q + [{"title": "Second agent?", "options": None}])]
    await st.agent.scan_thread("r1")
    await st.settle()
    return topic, card_ids(st)


def card_ids(st):
    q = st.question_dao.get(st.qid_of("i1"))
    agent = sorted(st.agent.open_questions("r1"), key=lambda a: a.display_no)
    return [[q.card_message_id, *q.card_extra_ids]] + [
        [a.card_message_id, *st.agent_dao.get(a.agent_question_id).card_extra_ids] for a in agent]


def cards_in_topic(st, topic):
    return [i for i, m in st.fake.messages.items() if m["thread_id"] == topic and not m.get("deleted") and m["text"].startswith("❓ Q")]


@pytest.mark.parametrize("rich", [True, False])
def test_questions_reposts_every_pending_card_in_number_order_and_deletes_the_old_ones(conn, rich):
    async def main():
        async with make_stack(conn, thread("r1", name="Fox"), rich=rich) as st:
            topic, old = await three_cards(st)
            fake = st.fake
            assert len(old) == 3
            cmd = fake.push_message("/questions", topic)
            await st.pump()
            assert all(fake.messages[m].get("deleted") for ids in old for m in ids)
            assert cmd in fake.deleted_user_messages  # the owner's command message is removed
            fresh = card_ids(st)
            assert all(set(a).isdisjoint(set(b)) for a, b in zip(old, fresh)) and all(ids for ids in fresh)
            flat = [ids[0] for ids in fresh]
            assert flat == sorted(flat) and cards_in_topic(st, topic) == flat  # fresh, one each, in number order
            texts = [fake.messages[m]["text"].split("\n")[0] for m in flat]
            assert texts == ["❓ Q1 · Fox", "❓ Q2 · Fox", "❓ Q3 · Fox"]
            assert ("rich" in fake.messages[flat[0]]) is rich  # the same renderer as for a new question
            # the stored ids point at the new messages: later edits hit them, and the buttons work
            qid = st.qid_of("i1")
            assert st.card_of(qid) == flat[0]
            qkey = fake.button(flat[0], "B")
            assert qkey["callback_data"].startswith("a:")
            fake.press(flat[0], "B")
            await st.pump()
            await st.settle()
            assert "Выбрано: Separate table" in fake.messages[flat[0]]["text"] and fake.messages[flat[0]].get("edits") == 1
            fake.press(flat[1], "A")
            await st.pump()
            await st.settle()
            assert "Выбрано: Yes" in fake.messages[flat[1]]["text"]
    run(main())


def test_questions_deletes_all_parts_of_a_split_classic_card(conn):
    long_option = "Use the long approach: " + "very detailed words, " * 400
    items = (QuestionItem("a", "", "Which?", False, (QuestionOption(long_option), QuestionOption("B"))),)

    async def main():
        async with make_stack(conn, thread("r1", name="Fox"), rich=False) as st:
            topic = await st.open_consultant("r1")
            await st.ask("r1", "i1", items=items)
            await st.settle()
            q = st.question_dao.get(st.qid_of("i1"))
            parts = [q.card_message_id, *q.card_extra_ids]
            assert len(parts) >= 2
            st.fake.push_message("/questions", topic)
            await st.pump()
            assert all(st.fake.messages[m].get("deleted") for m in parts)
            q2 = st.question_dao.get(st.qid_of("i1"))
            new_parts = [q2.card_message_id, *q2.card_extra_ids]
            assert len(new_parts) == len(parts) and set(new_parts).isdisjoint(parts)
            assert [bool(st.fake.buttons(m)) for m in new_parts] == [False] * (len(parts) - 1) + [True]  # keys on the last
    run(main())


def test_failed_deletes_are_ignored_and_the_fresh_cards_still_come(conn):
    async def main():
        async with make_stack(conn, thread("r1", name="Fox"), rich=True) as st:
            topic, old = await three_cards(st)
            st.fake.fail("deleteMessage", 400, "Bad Request: message can't be deleted", times=50)
            st.fake.push_message("/questions", topic)
            await st.pump()
            fresh = card_ids(st)
            assert all(set(a).isdisjoint(set(b)) for a, b in zip(old, fresh))
            assert not any(st.fake.messages[m].get("deleted") for ids in old for m in ids)  # the old ones stay, harmlessly
            assert len(cards_in_topic(st, topic)) == 6 and st.fake.methods("deleteMessage")
            st.fake.press(fresh[0][0], "A")  # and the fresh card works
            await st.pump()
            assert st.question_dao.get(st.qid_of("i1")).staged
    run(main())


def test_nothing_pending_answers_with_a_notice_that_is_removed_after_a_few_seconds(conn):
    async def main():
        async with make_stack(conn, thread("r1", name="Fox")) as st:
            topic = await st.open_consultant("r1")
            cmd = st.fake.push_message("/questions", topic)
            await st.pump()
            notice = [i for i, m in st.fake.messages.items() if m["thread_id"] == topic and m["text"] == "Нет неотвеченных вопросов"]
            assert len(notice) == 1 and cmd in st.fake.deleted_user_messages
            assert not st.fake.messages[notice[0]].get("deleted") and len(st.router._notices) == 1
            await asyncio.sleep(0.3)  # notice_seconds is 0.05 in the test stack
            assert st.fake.messages[notice[0]].get("deleted") and not st.router._notices
    run(main())


def test_notice_timers_are_bounded_cancelled_on_close_and_a_failed_delete_is_harmless(conn):
    async def main():
        async with make_stack(conn, thread("r1", name="Fox")) as st:
            topic = await st.open_consultant("r1")
            st.router._notice_seconds = 3600
            for _ in range(25):
                st.fake.push_message("/questions", topic)
            await st.pump()
            assert len(st.router._notices) == 20  # at most 20 timers; the other notices simply stay
            await st.router.close()
            assert not st.router._notices
            st.router._notice_seconds = 0.01
            st.fake.fail("deleteMessage", 400, "Bad Request: message can't be deleted", times=5)
            st.fake.push_message("/questions", topic)
            await st.pump()
            await asyncio.sleep(0.2)  # the delete fails (after the command's own delete): nothing breaks, nothing leaks
            assert not st.router._notices
    run(main())


def test_questions_of_another_thread_are_not_touched_and_secret_cards_repost_as_notices(conn):
    async def main():
        async with make_stack(conn, thread("r1", name="Fox"), thread("r2", name="Bee")) as st:
            topic = await st.open_consultant("r1")
            await st.open_consultant("r2")
            await st.ask("r1", "i1", secret=True)
            await st.ask("r2", "j1", request_id=5)
            await st.settle()
            other = st.card_of(st.qid_of("j1"))
            secret_old = st.card_of(st.qid_of("i1"))
            st.fake.push_message("/questions", topic)
            await st.pump()
            assert not st.fake.messages[other].get("deleted") and st.card_of(st.qid_of("j1")) == other
            fresh = st.card_of(st.qid_of("i1"))
            assert fresh != secret_old and "🔒" in st.fake.messages[fresh]["text"] and st.fake.buttons(fresh) == []
    run(main())
