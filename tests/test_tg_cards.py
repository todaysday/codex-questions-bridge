"""Question card flows (cards live in the consultant topic) with the real QuestionService and a fake Telegram."""

import asyncio
import re

from cqb.codex.events import TurnEnded
from cqb.domain.enums import ArchiveState, QuestionLifecycle as L
from cqb.domain.models import QuestionItem, QuestionOption
from tg_stack import make_stack, thread

OTHER_ITEM = (QuestionItem("name", "", "Pick a name", True, ()),)


def run(coro):
    return asyncio.run(coro)


def card_text(st, qid):
    return st.fake.messages[st.card_of(qid)]["text"]


def press(st, qid, part):
    return st.fake.push_callback(st.fake.button(st.card_of(qid), part)["callback_data"], st.card_of(qid))


async def topic_of(st, root="r1"):
    """The root's consultant topic; the consultant is created on first use."""
    if st.consultant_dao.get("s1", root) is None:
        await st.open_consultant(root)
    return st.consultant_dao.get("s1", root).tg_topic_id


async def posted(st, thread_id="r1", item_id="i1", **kw):
    await topic_of(st, thread_id)
    await st.ask(thread_id, item_id, **kw)
    await st.settle()
    return st.qid_of(item_id)


def test_normal_question_stage_confirm_answer_then_resolved_card(conn):
    async def main():
        async with make_stack(conn, thread("r1", name="FoxProfile — User Access")) as st:
            qid = await posted(st)
            fake, card = st.fake, st.card_of(qid)
            assert fake.messages[card]["thread_id"] == st.consultant_dao.get("s1", "r1").tg_topic_id
            text = card_text(st, qid)
            assert text.startswith("❓ Q1 · FoxProfile — User Access") and "Where store profile settings?" in text
            assert "A. Existing table" in text and "pending_confirmed" not in text

            press(st, qid, "B")  # stages the decision, nothing is sent yet
            await st.pump()
            assert st.question_dao.get(qid).lifecycle is L.DECISION_STAGED and st.client.responses == []
            assert "Выбрано: Separate table" in card_text(st, qid)
            assert fake.button(card, "Отправить в Codex") and fake.button(card, "Отмена")

            press(st, qid, "Отправить в Codex")
            await st.pump()
            assert st.client.responses == [("1", {"answers": {"pick": {"answers": ["Separate table"]}}})] \
                or st.client.responses == [(1, {"answers": {"pick": {"answers": ["Separate table"]}}})]
            sent = card_text(st, qid)
            assert "ждём подтверждения" in sent and "Доставлено" not in sent
            assert not [b for b in fake.buttons(card) if "Отправить" in b["text"]]

            await st.resolve("r1")
            await st.settle()
            done = card_text(st, qid)
            assert "✅ Доставлено" in done and "Separate table" in done
            assert fake.buttons(card) == []
    run(main())


def test_cancel_returns_to_the_options(conn):
    async def main():
        async with make_stack(conn, thread("r1")) as st:
            qid = await posted(st)
            press(st, qid, "A")
            await st.pump()
            press(st, qid, "Отмена")
            await st.pump()
            assert st.question_dao.get(qid).lifecycle is L.PENDING_CONFIRMED
            assert st.fake.button(st.card_of(qid), "A") and st.client.responses == []
    run(main())


def test_secret_question_card_has_no_text_and_no_buttons(conn):
    async def main():
        async with make_stack(conn, thread("r1", name="Fox")) as st:
            qid = await posted(st, secret=True)
            card = st.card_of(qid)
            text = card_text(st, qid)
            assert "секретный ввод" in text and "Codex" in text
            assert "Where" not in text and qid not in text
            assert st.fake.buttons(card) == []
            assert st.fake.messages[card]["markup"] is None
    run(main())


def test_answered_elsewhere_card(conn):
    async def main():
        async with make_stack(conn, thread("r1")) as st:
            qid = await posted(st)
            await st.resolve("r1")  # Desktop answered first
            await st.settle()
            text = card_text(st, qid)
            assert "отвечено в другом месте" in text and "Доставлено" not in text
            assert st.fake.buttons(st.card_of(qid)) == []
    run(main())


def test_stale_card(conn):
    async def main():
        async with make_stack(conn, thread("r1")) as st:
            qid = await posted(st)
            await st.questions.handle(TurnEnded("r1", "u1"))
            await st.settle()
            assert "устарел" in card_text(st, qid)
            assert st.fake.buttons(st.card_of(qid)) == []
    run(main())


def test_late_click_on_a_finished_question_is_refused_and_heals_the_card(conn):
    async def main():
        async with make_stack(conn, thread("r1")) as st:
            qid = await posted(st)
            key = st.fake.button(st.card_of(qid), "A")["callback_data"]
            await st.resolve("r1")  # card not refreshed yet: the old button is still on screen
            q = st.fake.push_callback(key, st.card_of(qid))
            await st.pump()
            answer = next(a for a in st.fake.callback_answers if a["callback_query_id"] == q)
            assert "question is resolved_observed" in answer["text"] and answer["show_alert"]
            assert st.client.responses == []
    run(main())


def test_confirming_twice_sends_only_one_answer(conn):
    async def main():
        async with make_stack(conn, thread("r1")) as st:
            qid = await posted(st)
            press(st, qid, "A")
            await st.pump()
            await st.settle()
            key = st.fake.button(st.card_of(qid), "Отправить")["callback_data"]
            st.fake.push_callback(key, st.card_of(qid))
            st.fake.push_callback(key, st.card_of(qid))
            await st.pump()
            assert len(st.client.responses) == 1
    run(main())


def toasts(st):
    return [a.get("text") for a in st.fake.callback_answers]


def test_text_answer_needs_the_button_then_the_reply_is_the_answer(conn):
    async def main():
        async with make_stack(conn, thread("r1")) as st:
            qid = await posted(st, items=OTHER_ITEM)
            card, topic = st.card_of(qid), await topic_of(st)
            assert "обсужда" in card_text(st, qid) and "следующий ответ" not in card_text(st, qid)
            press(st, qid, "Ответить текстом")
            await st.pump()
            assert toasts(st)[-1] == "Ответьте на это сообщение: текст уйдёт в Codex"
            text = card_text(st, qid)
            assert "следующий ответ на это сообщение уйдёт в Codex" in text
            assert st.fake.button(card, "Отмена") and not [b for b in st.fake.buttons(card) if "Ответить текстом" in b["text"]]
            st.fake.push_message("my own option", topic, reply_to=card)
            await st.pump()
            assert st.relay.got == []  # the armed reply is the answer, not chat
            assert "Выбрано: my own option" in card_text(st, qid) and "следующий ответ" not in card_text(st, qid)
            press(st, qid, "Отправить в Codex")
            await st.pump()
            (rid, result), = st.client.responses
            assert result == {"answers": {"name": {"answers": ["my own option"]}}}
            # armed state is used up: a second reply is a discussion again
            st.fake.push_message("and one more thing", topic, reply_to=card)
            await st.pump()
            assert len(st.relay.got) == 1 and st.relay.got[0][1].endswith("] and one more thing")
    run(main())


def test_armed_card_expires_after_ten_minutes_and_can_be_cancelled(conn):
    async def main():
        async with make_stack(conn, thread("r1")) as st:
            qid = await posted(st, items=OTHER_ITEM)
            card, topic = st.card_of(qid), await topic_of(st)
            press(st, qid, "Ответить текстом")
            await st.pump()
            st.clock.now += 599
            st.fake.push_message("still armed", topic, reply_to=card)
            await st.pump()
            assert st.question_dao.get(qid).staged == {"name": ("still armed",)} and st.relay.got == []
            press(st, qid, "Отмена")  # un-stage (the answer was staged), then arm again
            await st.pump()
            press(st, qid, "Ответить текстом")
            await st.pump()
            st.clock.now += 601  # ten minutes pass: the hint leaves the card by itself and a reply is chat
            await st.settle()
            assert "следующий ответ" not in card_text(st, qid)
            st.fake.push_message("too late", topic, reply_to=card)
            await st.pump()
            assert st.question_dao.get(qid).staged is None
            assert len(st.relay.got) == 1 and st.relay.got[0][1].endswith("] too late")

            press(st, qid, "Ответить текстом")
            await st.pump()
            assert "следующий ответ" in card_text(st, qid)
            press(st, qid, "Отмена")
            await st.pump()
            assert "следующий ответ" not in card_text(st, qid) and toasts(st)[-1] == "Отменено"
            st.fake.push_message("after cancel", topic, reply_to=card)
            await st.pump()
            assert st.question_dao.get(qid).staged is None and len(st.relay.got) == 2
    run(main())


def test_armed_state_is_dropped_when_the_question_resolves_or_an_option_is_pressed(conn):
    async def main():
        async with make_stack(conn, thread("r1")) as st:
            qid = await posted(st, items=OTHER_ITEM)
            press(st, qid, "Ответить текстом")
            await st.pump()
            await st.resolve("r1")
            await st.settle()
            assert "следующий ответ" not in card_text(st, qid)
            st.fake.push_message("late", await topic_of(st), reply_to=st.card_of(qid))
            await st.pump()
            (rid, text), = st.relay.got
            assert "(already answered)" in text and text.endswith("] late")  # stale card: discussion with the note
    run(main())


def test_ordinary_replies_are_discussion_with_a_bounded_context_prefix(conn):
    async def main():
        async with make_stack(conn, thread("r1")) as st:
            topic = await topic_of(st)
            long_title = "tall " * 100
            many = tuple(QuestionOption(f"opt{i}" + "x" * 100) for i in range(9))
            plain = await posted(st, item_id="i1")
            big = await posted(st, item_id="i2", request_id=2, items=(QuestionItem("a", "", long_title, False, many),))
            st.fake.push_message("what is this about?", topic, reply_to=st.card_of(plain))
            st.fake.push_message("/clear", topic, reply_to=st.card_of(plain))
            st.fake.push_message("explain", topic, reply_to=st.card_of(big))
            await st.pump()
            assert st.question_dao.get(plain).staged is None and st.question_dao.get(big).staged is None
            first, second, third = (t for _, t in st.relay.got)
            assert first == ("[Context: the user replies about question Q1 (Storage: Where store profile settings?; "
                             "options: Existing table, Separate table)] what is this about?")
            assert second == "/clear"  # a command is not a discussion: it goes as today and the relay escapes it
            assert third.startswith("[Context: the user replies about question Q2 (tall tall")
            prefix = third.removesuffix("explain")
            assert len(prefix) < 700 and "opt5" in prefix and "opt6" not in prefix and prefix.count("tall") <= 40 and "…" in prefix
    run(main())


def test_replies_about_secret_questions_are_refused_and_never_forwarded(conn):
    async def main():
        async with make_stack(conn, thread("r1")) as st:
            topic = await topic_of(st)
            secret = await posted(st, item_id="i2", secret=True, request_id=2)
            st.fake.push_message("hunter2", topic, reply_to=st.card_of(secret))
            await st.pump()
            assert st.relay.got == [] and st.question_dao.get(secret).staged is None
            assert "Секретный ввод" in st.fake.live_messages(topic)[-1]["text"]
            assert "hunter2" not in repr(st.fake.messages)
    run(main())


def test_replies_from_strangers_and_to_other_topics_cards_never_reach_the_consultant_as_context(conn):
    async def main():
        async with make_stack(conn, thread("r1"), thread("r2")) as st:
            topic = await topic_of(st)
            other_topic = await topic_of(st, "r2")
            plain = await posted(st, item_id="i1")
            theirs = await posted(st, thread_id="r2", item_id="i9", request_id=9)
            st.fake.push_message("x", topic, reply_to=st.card_of(plain), user_id=999)
            st.fake.push_message("sneaky", topic, reply_to=st.card_of(theirs))  # another thread's card
            st.fake.push_message("hello consultant", topic, reply_to=5)  # reply to something that is no card
            await st.pump()
            assert st.relay.got == [("r1", "sneaky"), ("r1", "hello consultant")]  # no foreign question text
            assert other_topic != topic
    run(main())


def test_too_long_armed_answer_keeps_the_card_armed(conn):
    async def main():
        async with make_stack(conn, thread("r1")) as st:
            topic = await topic_of(st)
            qid = await posted(st, items=OTHER_ITEM)
            press(st, qid, "Ответить текстом")
            await st.pump()
            st.fake.push_message("y" * 1500, topic, reply_to=st.card_of(qid))
            await st.pump()
            assert st.question_dao.get(qid).staged is None and "следующий ответ" in card_text(st, qid)
            assert "Слишком длинный" in st.fake.live_messages(topic)[-1]["text"] and st.relay.got == []
    run(main())


def test_request_card_never_cuts_text_and_continues_in_more_messages(conn):
    async def main():
        async with make_stack(conn, thread("r1", name="Fox")) as st:
            topic = await topic_of(st)
            long_option = "Take the long road: " + "every detail matters, " * 220  # over 4800 characters
            title = "Which road? " + "background sentence. " * 100
            qid = await posted(st, items=(QuestionItem("a", "Road", title, True, (
                QuestionOption("Short", "quick"), QuestionOption(long_option, "slow " * 300), QuestionOption("Third")),),))
            q = st.question_dao.get(qid)
            ids = [q.card_message_id, *q.card_extra_ids]
            assert len(ids) >= 2 and all(len(st.fake.messages[m]["text"]) <= 4096 for m in ids)
            assert st.fake.messages[ids[0]]["text"].startswith("❓ Q1 · Fox\n\n")
            assert st.fake.messages[ids[1]]["text"].startswith("↪ Q1 · 2/")
            texts = [st.fake.messages[m]["text"] for m in ids]
            assert "…" not in "".join(texts)
            joined = " ".join(" ".join(re.sub(r"↪ Q1 · \d+/\d+", "", t).split()) for t in texts)
            assert " ".join(long_option.split()) in joined and " ".join(title.split()) in joined
            assert " ".join(("slow " * 300).split()) in joined and "C. Third" in joined
            assert "request_user_input" not in joined and qid not in joined and "pending" not in joined
            assert [m for m in ids if st.fake.buttons(m)] == [ids[-1]]  # the one-time keys: on the last message only
            assert [b["text"] for b in st.fake.buttons(ids[-1])] == ["A", "B", "C", "✍️ Ответить текстом"]
            # a reply to any message of the card is a reply to the card
            st.fake.push_message("is B really slow?", topic, reply_to=ids[1])
            await st.pump()
            assert st.relay.got[-1][1].startswith("[Context: the user replies about question Q1 (Road: Which road?")
            # the button answers with the full option text
            press_key = st.fake.button(ids[-1], "B")["callback_data"]
            st.fake.push_callback(press_key, ids[-1])
            await st.pump()
            q = st.question_dao.get(qid)
            assert q.staged == {"a": (long_option,)} and [m for m in [q.card_message_id, *q.card_extra_ids] if st.fake.buttons(m)] == [q.card_extra_ids[-1]]
            assert [b["text"] for b in st.fake.buttons(q.card_extra_ids[-1])] == ["✅ Отправить в Codex", "↩️ Отмена"]
            await st.resolve("r1")
            await st.settle()
            q = st.question_dao.get(qid)
            assert not [m for m in [q.card_message_id, *q.card_extra_ids] if st.fake.buttons(m)]
            assert st.fake.messages[q.card_message_id]["text"].splitlines()[0].endswith("· отвечено в другом месте")
    run(main())


def test_cards_are_updated_by_the_background_worker_without_blocking_events(conn):
    async def main():
        async with make_stack(conn, thread("r1")) as st:
            await topic_of(st)
            worker = asyncio.create_task(st.cards.run())
            try:
                st.publisher._wake.clear()
                await st.ask("r1", "i1")
                for _ in range(100):
                    await asyncio.sleep(0.02)
                    if st.question_dao.get(st.open_ids()[0]).card_message_id:
                        break
                qid = st.open_ids()[0]
                assert "Where store profile settings?" in card_text(st, qid)
                await st.resolve("r1")
                for _ in range(100):
                    await asyncio.sleep(0.02)
                    if "другом месте" in card_text(st, qid):
                        break
                assert "отвечено в другом месте" in card_text(st, qid)
                assert st.publisher._wake.is_set()  # the registry row loses its ❓
            finally:
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)
    run(main())


def test_question_captured_before_telegram_was_up_gets_its_card_at_startup(conn):
    async def main():
        async with make_stack(conn, thread("r1")) as st:
            await topic_of(st)
            st.cards.mark = lambda qid: None  # simulate Telegram down: nothing marked
            await st.ask("r1", "i1")
            qid = st.open_ids()[0]
            assert st.card_of(qid) is None
            del st.cards.mark
            worker = asyncio.create_task(st.cards.run())
            try:
                for _ in range(100):
                    await asyncio.sleep(0.02)
                    if st.question_dao.get(qid).card_message_id:
                        break
                assert st.card_of(qid) is not None
            finally:
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)
    run(main())


def test_deleted_card_is_posted_again_while_open_but_not_when_finished(conn):
    async def main():
        async with make_stack(conn, thread("r1")) as st:
            qid = await posted(st)
            old = st.card_of(qid)
            st.fake.delete_message(old)
            await st.cards.refresh_card(qid)
            assert st.card_of(qid) != old and "Where" in card_text(st, qid)
            gone = st.card_of(qid)
            sent = len(st.fake.methods("sendMessage"))
            st.fake.delete_message(gone)
            await st.resolve("r1")
            await st.cards.refresh_card(qid)
            assert st.card_of(qid) is None and len(st.fake.methods('sendMessage')) == sent  # nothing is reposted
    run(main())


def test_no_open_consultant_means_no_card_and_a_question_mark_in_the_registry(conn):
    async def main():
        async with make_stack(conn, thread("r1", name="Fox")) as st:
            before = len(st.fake.methods("sendMessage"))
            await st.ask("r1", "i1")
            await st.settle()
            qid = st.qid_of("i1")
            assert st.card_of(qid) is None and len(st.fake.methods("sendMessage")) == before  # nothing anywhere
            assert "❓ Fox" in st.fake.messages[st.registry_message]["text"]
            assert len(st.control_messages()) == 1  # no alert next to the registry
            await st.resolve("r1")
            await st.settle()
            assert st.card_of(qid) is None and len(st.fake.methods("sendMessage")) == before
            assert "❓" not in st.fake.messages[st.registry_message]["text"]
    run(main())


def test_pending_cards_are_posted_when_a_consultant_is_created_later(conn):
    async def main():
        async with make_stack(conn, thread("r1", name="Fox"), thread("r2", name="Other")) as st:
            await st.ask("r1", "i1")
            await st.ask("r1", "i2", request_id=2)
            await st.ask("r2", "i3", request_id=3)
            await st.resolve("r1", request_id=2)  # already finished: no card for it
            await st.settle()
            assert [st.card_of(st.qid_of(i)) for i in ("i1", "i2", "i3")] == [None] * 3
            topic = await st.open_consultant("r1")
            await st.settle()
            assert "Where store profile settings?" in card_text(st, st.qid_of("i1"))
            assert st.fake.messages[st.card_of(st.qid_of("i1"))]["thread_id"] == topic
            assert st.card_of(st.qid_of("i2")) is None and st.card_of(st.qid_of("i3")) is None
            assert len(st.control_messages()) == 1
    run(main())


def test_cards_follow_the_consultant_state_archived_gets_none_reopened_gets_them(conn):
    async def main():
        async with make_stack(conn, thread("r1", name="Fox")) as st:
            topic = await topic_of(st)
            st.threads.set_archive_state("s1", "r1", ArchiveState.ARCHIVED, 1)
            await st.consultants.reconcile()  # the consultant (and its topic) is closed
            assert st.fake.topics[topic]["closed"] is True
            before = len(st.fake.methods("sendMessage"))
            await st.ask("r1", "i1")
            await st.settle()
            assert st.card_of(st.qid_of("i1")) is None and len(st.fake.methods("sendMessage")) == before
            st.threads.set_archive_state("s1", "r1", ArchiveState.ACTIVE, 2)
            await st.consultants.reconcile()  # reopened: the pending question gets its card in the same topic
            await st.settle()
            assert st.fake.messages[st.card_of(st.qid_of("i1"))]["thread_id"] == topic
    run(main())


def test_card_of_an_unfinished_consultant_is_not_posted_while_creating(conn):
    async def main():
        async with make_stack(conn, thread("r1")) as st:
            st.consultant_dao.reserve("s1", "r1", -1001234567890, "k", 1)  # CREATING: no topic yet
            await st.ask("r1", "i1")
            await st.settle()
            assert st.card_of(st.qid_of("i1")) is None
    run(main())


def test_failure_while_answering_is_reported_not_swallowed(conn):
    async def main():
        async with make_stack(conn, thread("r1")) as st:
            qid = await posted(st)
            press(st, qid, "A")
            await st.pump()

            async def boom(request_id, result):
                raise ConnectionError("daemon gone")
            st.client.respond = boom
            q = press(st, qid, "Отправить в Codex")
            await st.pump()
            answer = next(a for a in st.fake.callback_answers if a["callback_query_id"] == q)
            assert answer["show_alert"] and "Внутренняя ошибка" in answer["text"]
            assert not st.question_dao.get(qid).delivered_to_codex
    run(main())
