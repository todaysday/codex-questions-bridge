"""Question card flows (cards live in the consultant topic) with the real QuestionService and a fake Telegram."""

import asyncio

from cqb.codex.events import TurnEnded
from cqb.domain.enums import ArchiveState, QuestionLifecycle as L
from cqb.domain.models import QuestionItem
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
            assert text.startswith("❓ FoxProfile — User Access") and "Where store profile settings?" in text
            assert "A. Existing table" in text and "pending_confirmed" in text

            press(st, qid, "B. Separate")  # stages the decision, nothing is sent yet
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
            press(st, qid, "A. Existing")
            await st.pump()
            press(st, qid, "Отмена")
            await st.pump()
            assert st.question_dao.get(qid).lifecycle is L.PENDING_CONFIRMED
            assert st.fake.button(st.card_of(qid), "A. Existing") and st.client.responses == []
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
            key = st.fake.button(st.card_of(qid), "A. Existing")["callback_data"]
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
            press(st, qid, "A. Existing")
            await st.pump()
            await st.settle()
            key = st.fake.button(st.card_of(qid), "Отправить")["callback_data"]
            st.fake.push_callback(key, st.card_of(qid))
            st.fake.push_callback(key, st.card_of(qid))
            await st.pump()
            assert len(st.client.responses) == 1
    run(main())


def test_other_answer_by_replying_to_the_card(conn):
    async def main():
        async with make_stack(conn, thread("r1")) as st:
            qid = await posted(st, items=OTHER_ITEM)
            card = st.card_of(qid)
            q = press(st, qid, "Другое")
            await st.pump()
            assert any(a["callback_query_id"] == q and "Ответьте на сообщение" in a["text"]
                       for a in st.fake.callback_answers)
            st.fake.push_message("my own option", await topic_of(st), reply_to=card)
            await st.pump()
            assert st.relay.got == []  # a reply to a card is an answer, never consultant chat
            assert "Выбрано: my own option" in card_text(st, qid)
            press(st, qid, "Отправить в Codex")
            await st.pump()
            (rid, result), = st.client.responses
            assert result == {"answers": {"name": {"answers": ["my own option"]}}}
    run(main())


def test_replies_to_cards_are_answers_or_refused_but_never_chat(conn):
    async def main():
        async with make_stack(conn, thread("r1")) as st:
            topic = await topic_of(st)
            plain = await posted(st, item_id="i1")
            secret = await posted(st, item_id="i2", secret=True, request_id=2)
            other = await posted(st, item_id="i3", items=OTHER_ITEM, request_id=3)
            st.fake.push_message("x", topic, reply_to=st.card_of(plain))
            st.fake.push_message("hunter2", topic, reply_to=st.card_of(secret))
            st.fake.push_message("x", topic, reply_to=st.card_of(other), user_id=999)
            await st.pump()
            for qid in (plain, secret, other):
                assert st.question_dao.get(qid).staged is None
            assert st.relay.got == []  # in particular the "secret" never reaches the consultant model
            texts = [m["text"] for m in st.fake.live_messages(topic)]
            assert any("Секретный ввод" in t for t in texts) and any("используйте кнопки" in t for t in texts)
            st.fake.push_message("hello consultant", topic, reply_to=5)  # reply to something that is no card
            await st.pump()
            assert st.relay.got == [("r1", "hello consultant")]
            st.fake.push_message("y" * 1500, topic, reply_to=st.card_of(other))
            await st.pump()
            assert st.question_dao.get(other).staged is None
            assert "Слишком длинный" in st.fake.live_messages(topic)[-1]["text"]
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
            st.fake.delete_message(gone)
            await st.resolve("r1")
            await st.cards.refresh_card(qid)
            assert st.card_of(qid) == gone  # nothing actionable to repost
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
            press(st, qid, "A. Existing")
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
