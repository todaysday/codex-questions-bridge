"""A new consultant topic starts with its opening message, pinned; cards of pending questions come only after it."""

import asyncio

from cqb.domain.enums import ArchiveState
from tg_stack import make_stack, thread


def run(coro):
    return asyncio.run(coro)


def topic_calls(st, topic):
    """(method, message id) of what the Bridge did in the topic since it was created, in order."""
    calls = st.fake.calls
    start = max(i for i, (m, _) in enumerate(calls) if m == "createForumTopic")
    out, sent = [], iter(sorted(i for i, m in st.fake.messages.items() if m["thread_id"] == topic))
    for method, p in calls[start:]:
        if method == "sendMessage" and p.get("message_thread_id") == topic:
            out.append((method, next(sent)))
        elif method == "pinChatMessage":
            out.append((method, p["message_id"]))
    return out


def texts(st, topic):
    return [m["text"] for _, m in sorted(st.fake.messages.items()) if m["thread_id"] == topic and not m.get("deleted")]


async def two_pending(st):
    await st.ask("r1", "i1")
    await st.ask("r1", "i2", request_id=2)
    await st.settle()  # no consultant yet: no card anywhere


def test_opening_message_goes_first_and_is_pinned_then_cards_follow(conn):
    async def main():
        async with make_stack(conn, thread("r1", name="Fox")) as st:
            await st.ask("r1", "i1")
            topic = await st.open_consultant("r1")
            await st.settle()
            opening, card = sorted(i for i, m in st.fake.messages.items() if m["thread_id"] == topic)
            assert card == st.card_of(st.qid_of("i1"))
            assert topic_calls(st, topic) == [("sendMessage", opening), ("pinChatMessage", opening), ("sendMessage", card)]
            assert st.fake.pinned[1:] == [opening] and card not in st.fake.pinned  # [0] is the registry message
            first = st.fake.messages[opening]["text"]
            assert first.startswith("Консультант: Fox") and "/status" in first and "/questions" in first
            assert "/thread" not in first  # disabled by owner request
    run(main())


def test_two_pending_questions_give_two_cards_after_the_pinned_opening(conn):
    async def main():
        async with make_stack(conn, thread("r1", name="Fox")) as st:
            await two_pending(st)
            topic = await st.open_consultant("r1")
            await st.settle()
            opening, *cards = sorted(i for i, m in st.fake.messages.items() if m["thread_id"] == topic)
            assert len(cards) == 2 and set(cards) == {st.card_of(st.qid_of("i1")), st.card_of(st.qid_of("i2"))}
            assert topic_calls(st, topic) == [
                ("sendMessage", opening), ("pinChatMessage", opening), *(("sendMessage", c) for c in cards)]
            assert st.fake.pinned[1:] == [opening]
    run(main())


def test_a_failed_pin_does_not_hold_back_the_cards(conn):
    async def main():
        async with make_stack(conn, thread("r1", name="Fox")) as st:
            await two_pending(st)
            st.fake.fail("pinChatMessage", 400, "Bad Request: not enough rights to pin a message")
            topic = await st.open_consultant("r1")
            await st.settle()
            opening, *cards = sorted(i for i, m in st.fake.messages.items() if m["thread_id"] == topic)
            assert len(cards) == 2 and texts(st, topic)[0].startswith("Консультант: Fox")
            assert [m for m, _ in topic_calls(st, topic)] == ["sendMessage", "pinChatMessage", "sendMessage", "sendMessage"]
            assert len(st.fake.pinned) == 1  # the registry only: nothing was pinned, and no card in particular
    run(main())


def test_a_failed_opening_message_does_not_hold_back_the_cards_and_nothing_is_pinned(conn):
    async def main():
        async with make_stack(conn, thread("r1", name="Fox")) as st:
            await st.ask("r1", "i1")
            st.fake.fail("sendMessage", 400, "Bad Request: message text is empty")
            topic = await st.open_consultant("r1")
            await st.settle()
            [card] = [i for i, m in st.fake.messages.items() if m["thread_id"] == topic]
            assert card == st.card_of(st.qid_of("i1")) and len(st.fake.pinned) == 1
            assert not texts(st, topic)[0].startswith("Консультант")
    run(main())


def test_reopening_posts_cards_after_the_existing_pinned_message_and_adds_no_opening(conn):
    async def main():
        async with make_stack(conn, thread("r1", name="Fox")) as st:
            topic = await st.open_consultant("r1")
            [opening] = [i for i, m in st.fake.messages.items() if m["thread_id"] == topic]
            st.threads.set_archive_state("s1", "r1", ArchiveState.ARCHIVED, 5)
            await st.consultants.reconcile()
            await st.ask("r1", "i1")  # a question while the topic is closed
            await st.settle()
            assert [i for i, m in st.fake.messages.items() if m["thread_id"] == topic] == [opening]
            st.threads.set_archive_state("s1", "r1", ArchiveState.ACTIVE, 6)
            await st.consultants.reconcile()
            await st.settle()
            ids = [i for i, m in sorted(st.fake.messages.items()) if m["thread_id"] == topic]
            assert ids == [opening, st.card_of(st.qid_of("i1"))]
            assert st.fake.pinned[1:] == [opening]  # still the one pin, never a card
            assert texts(st, topic)[0].startswith("Консультант: Fox")
    run(main())


def test_the_pin_uses_the_opening_message_id_even_with_other_messages_in_flight(conn):
    async def main():
        async with make_stack(conn, thread("r1", name="Fox"), thread("r2", name="Two")) as st:
            await st.ask("r2", "j1")
            await st.settle()
            t1 = await st.open_consultant("r1")
            t2 = await st.open_consultant("r2")
            await st.settle()
            first = {t: min(i for i, m in st.fake.messages.items() if m["thread_id"] == t) for t in (t1, t2)}
            assert st.fake.pinned[1:] == [first[t1], first[t2]]
            assert st.card_of(st.qid_of("j1")) not in st.fake.pinned
    run(main())
