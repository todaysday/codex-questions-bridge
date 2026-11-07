"""CardPoster: a card is one message or several, the keyboard sits on the last one, and nothing is ever cut."""

import asyncio
import re

from cqb.telegram.api import TelegramError
from cqb.telegram.card_post import PART_LIMIT, CardPoster, card_parts
from cqb.telegram.models import ActionSpec, Button
from tg_stack import make_stack, thread

KEYBOARD = ((Button("A", callback_data="k:a"), Button("B", callback_data="k:b")), (Button("✍️ Ответить текстом", callback_data="k:t"),))


def run(coro):
    return asyncio.run(coro)


def long_text(n=3, tag="x"):
    return "\n".join(f"line {i} {tag} " + "word " * 120 for i in range(n * 8))


def test_parts_are_bounded_labelled_and_lose_no_text():
    text = long_text(4)
    parts = card_parts(text, "Q7")
    assert len(parts) >= 3 and all(len(p) <= 4096 for p in parts) and parts[0] == parts[0].lstrip()
    assert all(p.startswith(f"↪ Q7 · {n}/{len(parts)}\n\n") for n, p in enumerate(parts[1:], start=2))
    rebuilt = " ".join(" ".join(re.sub(r"↪ Q7 · \d+/\d+", "", p).split()) for p in parts)
    assert rebuilt == " ".join(text.split()) and "…" not in "".join(parts)
    assert card_parts("short", "Q1") == ["short"] and card_parts("x" * 5000, "Q1")[0] == "x" * PART_LIMIT  # hard cut only without spaces


def test_one_message_for_a_short_card_and_keys_only_on_the_last_message_of_a_long_one(conn):
    async def main():
        async with make_stack(conn, thread("r1")) as st:
            topic = await st.open_consultant("r1")
            poster, ids_seen = CardPoster(st.api, st.fake.chat_id), []
            ids = await poster.sync([], ["one"], KEYBOARD, topic, lambda ids: ids_seen.append(list(ids)))
            assert len(ids) == 1 and st.fake.buttons(ids[0]) and ids_seen == [ids]
            long_ids = await poster.sync([], ["a", "b", "c"], KEYBOARD, topic, lambda ids: None)
            assert [bool(st.fake.buttons(m)) for m in long_ids] == [False, False, True]
            assert [st.fake.messages[m]["text"] for m in long_ids] == ["a", "b", "c"]
            assert all(st.fake.messages[m]["thread_id"] == topic for m in long_ids)
    run(main())


def test_a_card_that_grows_moves_the_keyboard_and_one_that_shrinks_drops_the_surplus(conn):
    async def main():
        async with make_stack(conn, thread("r1")) as st:
            topic = await st.open_consultant("r1")
            poster = CardPoster(st.api, st.fake.chat_id)
            stored: list[int] = []
            ids = await poster.sync([], ["a", "b"], KEYBOARD, topic, lambda i: stored.__setitem__(slice(None), i))
            grown = await poster.sync(ids, ["a", "b", "c"], KEYBOARD, topic, lambda i: stored.__setitem__(slice(None), i))
            assert grown[:2] == ids and len(grown) == 3 and stored == grown
            assert [bool(st.fake.buttons(m)) for m in grown] == [False, False, True]  # the old last lost its keys
            shrunk = await poster.sync(grown, ["only"], (), topic, lambda i: stored.__setitem__(slice(None), i))
            assert shrunk == grown[:1] and stored == shrunk and st.fake.messages[shrunk[0]]["text"] == "only"
            assert st.fake.messages[grown[1]].get("deleted") and st.fake.messages[grown[2]].get("deleted")
            assert not st.fake.buttons(shrunk[0])
    run(main())


def test_a_deleted_message_reposts_the_whole_card_but_only_when_posting_is_allowed(conn):
    async def main():
        async with make_stack(conn, thread("r1")) as st:
            topic = await st.open_consultant("r1")
            poster = CardPoster(st.api, st.fake.chat_id)
            ids = await poster.sync([], ["a", "b", "c"], KEYBOARD, topic, lambda i: None)
            st.fake.delete_message(ids[1])
            stored: list[int] = []
            again = await poster.sync(ids, ["a", "b", "c"], KEYBOARD, topic, lambda i: stored.__setitem__(slice(None), i))
            assert len(again) == 3 and not set(again) & set(ids) and stored == again
            assert all(st.fake.messages[m].get("deleted") for m in ids)  # the rest of the old card was removed
            st.fake.delete_message(again[0])
            assert await poster.sync(again, ["a", "b", "c"], KEYBOARD, None, lambda i: None) == []  # closed: no repost
    run(main())


def test_a_failure_half_way_keeps_what_was_sent_and_the_next_try_continues_the_same_card(conn):
    async def main():
        async with make_stack(conn, thread("r1")) as st:
            topic = await st.open_consultant("r1")
            poster = CardPoster(st.api, st.fake.chat_id)
            stored: list[int] = []

            def bind(ids):
                stored[:] = ids
            st.fake.fail("sendMessage", 400, "Bad Request: boom", times=1)
            try:
                await poster.sync([], ["a", "b"], KEYBOARD, topic, bind)
            except TelegramError:
                pass
            assert stored == []  # the first send failed: nothing to continue
            await poster.sync([], ["a"], (), topic, bind)  # (a card of one message went out)
            first = list(stored)
            st.fake.fail("sendMessage", 400, "Bad Request: boom", times=1)
            try:
                await poster.sync(first, ["a", "b", "c"], KEYBOARD, topic, bind)
            except TelegramError:
                pass
            assert stored == first  # the failed continuation is not recorded
            done = await poster.sync(stored, ["a", "b", "c"], KEYBOARD, topic, bind)
            assert done[0] == first[0] and len(done) == 3 and stored == done
    run(main())
