"""Agent-message question cards: detection output -> Telegram card -> one combined answer as a user message."""

import asyncio
import json

from cqb.codex.appserver import RpcError
from cqb.codex.events import ThreadArchiveChanged
from cqb.domain.enums import AgentQuestionState as S
from cqb.services.questions import QuestionError
from tg_stack import agent_item, make_stack, thread, user_item

Q1 = [{"title": "Where to store the settings?", "options": ["Existing table", "Separate table"]}]
Q3 = [
    {"title": "First?", "options": ["Yes", "No"]},
    {"title": "Second?", "options": None},
    {"title": "Third?", "options": ["Red", "Green", "Blue"]},
]


def run(coro):
    return asyncio.run(coro)


def text_of(st, aqid):
    return st.fake.messages[st.agent_dao.get(aqid).card_message_id]["text"]


def card_of(st, aqid):
    return st.agent_dao.get(aqid).card_message_id


def press(st, aqid, part):
    return st.fake.push_callback(st.fake.button(card_of(st, aqid), part)["callback_data"], card_of(st, aqid))


def toasts(st):
    return [a.get("text") for a in st.fake.callback_answers]


async def posted(st, questions=Q1, item_id="call_1", consultant=True):
    """Put an agentMessage with questions into r1, let the watcher see it and (with a consultant) post the card."""
    if consultant and st.consultant_dao.get("s1", "r1") is None:
        await st.open_consultant("r1")
    st.agent_client.items.setdefault("r1", []).append(agent_item(item_id, questions))
    await st.agent.scan_thread("r1")
    await st.settle()
    return st.agent.open_questions("r1")[0].agent_question_id


def expected(item_id, entries):
    body = ",".join(
        '{"question":%s,"answer":%s,"questionItemId":%s}' % (
            json.dumps(t, ensure_ascii=False), json.dumps(a, ensure_ascii=False),
            json.dumps(json.dumps(["request_user_input_async", item_id, i], separators=(",", ":"))))
        for i, (t, a) in enumerate(entries))
    return f"<send_user_message_question_reply>\n[{body}]\n</send_user_message_question_reply>"


def test_single_question_stage_confirm_send_and_delivered_only_after_the_user_message(conn):
    async def main():
        async with make_stack(conn, thread("r1", name="FoxProfile")) as st:
            aqid = await posted(st)
            fake, card = st.fake, card_of(st, aqid)
            assert fake.messages[card]["thread_id"] == st.consultant_dao.get("s1", "r1").tg_topic_id
            text = text_of(st, aqid)
            assert text.startswith("❓ FoxProfile") and "Where to store the settings?" in text
            assert "A. Existing table" in text and "B. Separate table" in text and "Или ответьте" in text

            press(st, aqid, "B. Separate")
            await st.pump()
            assert "Выбрано: Separate table" in text_of(st, aqid) and st.agent_client.sent == []
            assert fake.button(card, "Отправить в Codex") and fake.button(card, "Отмена")

            press(st, aqid, "Отправить в Codex")
            await st.pump()
            assert st.agent_client.sent == [("r1", expected("call_1", [(Q1[0]["title"], "Separate table")]))]
            sent = text_of(st, aqid)
            assert "ждём подтверждения" in sent and "Доставлено" not in sent
            assert fake.buttons(card) == [] and st.agent_dao.get(aqid).sent_at is not None
            assert st.agent_dao.get(aqid).state is S.PENDING  # not delivered until a userMessage is seen

            st.agent_client.items["r1"].append(user_item("um1", "whatever the app stored"))
            await st.agent.scan_thread("r1")
            await st.settle()
            done = text_of(st, aqid)
            assert "✅ Доставлено" in done and "Separate table" in done and fake.buttons(card) == []
            assert st.agent_dao.get(aqid).state is S.ANSWERED
    run(main())


def test_several_questions_collect_one_choice_each_then_one_confirmation_and_one_message(conn):
    async def main():
        async with make_stack(conn, thread("r1")) as st:
            aqid = await posted(st, Q3)
            card, fake = card_of(st, aqid), st.fake
            text = text_of(st, aqid)
            assert "1. First?" in text and "2. Second?" in text and "3. Third?" in text and "«2: ваш текст»" in text
            assert [b["text"] for b in fake.buttons(card)] == [
                "1A. Yes", "1B. No", "✍️ Другое 1", "✍️ Другое 2", "3A. Red", "3B. Green", "3C. Blue", "✍️ Другое 3"]

            press(st, aqid, "1B. No")
            await st.pump()
            assert "Осталось ответить: 2, 3" in text_of(st, aqid)
            assert not [b for b in fake.buttons(card) if b["text"].startswith("1")]  # answered: no buttons left for it
            assert fake.button(card, "Отмена")
            press(st, aqid, "3C. Blue")
            await st.pump()
            assert "Осталось ответить: 2" in text_of(st, aqid) and not [b for b in fake.buttons(card) if "Отправить" in b["text"]]

            # a premature confirmation is refused by the service too
            try:
                await st.agent.send(aqid)
                raise AssertionError("must refuse")
            except QuestionError as exc:
                assert "все вопросы" in str(exc)
            assert st.agent_client.sent == []

            fake.push_message("2: my own answer, with «quotes»", st.consultant_dao.get("s1", "r1").tg_topic_id, reply_to=card)
            await st.pump()
            assert "Подтвердите" in toasts(st)[-1] or "Отправить этот ответ" in text_of(st, aqid)
            assert fake.button(card, "Отправить в Codex")
            press(st, aqid, "Отправить в Codex")
            await st.pump()
            assert st.agent_client.sent == [("r1", expected("call_1", [
                ("First?", "No"), ("Second?", "my own answer, with «quotes»"), ("Third?", "Blue")]))]
            assert st.relay.got == []  # the reply to a card never reached the consultant model
    run(main())


def test_free_text_reply_for_a_single_question_and_hints(conn):
    async def main():
        async with make_stack(conn, thread("r1")) as st:
            aqid = await posted(st)
            topic, card = st.consultant_dao.get("s1", "r1").tg_topic_id, card_of(st, aqid)
            press(st, aqid, "Другое")
            await st.pump()
            assert toasts(st)[-1] == "Ответьте на сообщение карточки своим текстом"
            st.fake.push_message("my own option", topic, reply_to=card)
            await st.pump()
            assert "Выбрано: my own option" in text_of(st, aqid)
            press(st, aqid, "Отправить в Codex")
            await st.pump()
            assert st.agent_client.sent[0][1] == expected("call_1", [(Q1[0]["title"], "my own option")])
            assert st.relay.got == []
    run(main())


def test_reply_validation_numbers_and_length(conn):
    async def main():
        async with make_stack(conn, thread("r1")) as st:
            aqid = await posted(st, Q3)
            topic, card = st.consultant_dao.get("s1", "r1").tg_topic_id, card_of(st, aqid)
            for bad in ("no number", "9: nope", "0: nope"):
                st.fake.push_message(bad, topic, reply_to=card)
            st.fake.push_message("2: " + "y" * 1500, topic, reply_to=card)
            st.fake.push_message("2: ok", topic, reply_to=card, user_id=999)  # a stranger
            await st.pump()
            assert st.agent_dao.get(aqid).staged == {}
            said = [m["text"] for m in st.fake.messages.values() if m["thread_id"] == topic]
            assert sum("Укажите номер вопроса" in t for t in said) == 3 and any("Слишком длинный" in t for t in said)
            assert st.relay.got == []
    run(main())


def test_answered_elsewhere_before_confirmation(conn):
    async def main():
        async with make_stack(conn, thread("r1")) as st:
            aqid = await posted(st)
            card = card_of(st, aqid)
            press(st, aqid, "A. Existing")
            await st.pump()
            send_key = st.fake.button(card, "Отправить")["callback_data"]
            st.agent_client.items["r1"].append(user_item("um1"))  # the owner answered in the Codex app
            await st.agent.scan_thread("r1")
            await st.settle()
            text = text_of(st, aqid)
            assert "отвечено в другом месте" in text and "Доставлено" not in text and st.fake.buttons(card) == []
            st.fake.push_callback(send_key, card)  # a stale button must not send anything
            await st.pump()
            assert st.agent_client.sent == [] and "уже отвечен" in toasts(st)[-1]
    run(main())


def test_confirmation_rereads_the_thread_so_an_unnoticed_answer_is_not_duplicated(conn):
    async def main():
        async with make_stack(conn, thread("r1")) as st:
            aqid = await posted(st)
            press(st, aqid, "A. Existing")
            await st.pump()
            st.agent_client.items["r1"].append(user_item("um1"))  # answered, but the watcher has not looked yet
            press(st, aqid, "Отправить")
            await st.pump()
            assert st.agent_client.sent == []
            assert "уже отвечен" in toasts(st)[-1] and "отвечено в другом месте" in text_of(st, aqid)
    run(main())


def test_stale_card_when_the_thread_is_archived(conn):
    async def main():
        async with make_stack(conn, thread("r1")) as st:
            aqid = await posted(st)
            await st.agent.handle(ThreadArchiveChanged("r1", True))
            await st.settle()
            assert "⌛" in text_of(st, aqid) and st.fake.buttons(card_of(st, aqid)) == []
            assert st.agent_dao.get(aqid).state is S.STALE
    run(main())


def test_send_failures_keep_the_question_open_and_say_so(conn):
    async def main():
        async with make_stack(conn, thread("r1")) as st:
            aqid = await posted(st)
            press(st, aqid, "A. Existing")
            await st.pump()
            st.agent_client.send_error = RpcError("turn/start", {"message": "boom with private text"})
            press(st, aqid, "Отправить")
            await st.pump()
            assert toasts(st)[-1] == "Не удалось отправить в Codex" and "private" not in toasts(st)[-1]
            assert st.agent_dao.get(aqid).sent_at is None and st.fake.button(card_of(st, aqid), "Отправить")
            st.agent_client.send_error = TimeoutError()
            press(st, aqid, "Отправить")
            await st.pump()
            assert "проверьте тред" in toasts(st)[-1] and st.agent_dao.get(aqid).sent_at is None
            st.agent_client.send_error = None
            press(st, aqid, "Отправить")
            await st.pump()
            assert len(st.agent_client.sent) == 1 and st.agent_dao.get(aqid).sent_at is not None
            assert st.fake.buttons(card_of(st, aqid)) == []
            try:
                await st.agent.send(aqid)
                raise AssertionError("must refuse")
            except Exception as exc:
                assert "уже отправлен" in str(exc)
            assert len(st.agent_client.sent) == 1
    run(main())


def test_cancel_clears_the_choices(conn):
    async def main():
        async with make_stack(conn, thread("r1")) as st:
            aqid = await posted(st, Q3)
            press(st, aqid, "1A. Yes")
            await st.pump()
            press(st, aqid, "Отмена")
            await st.pump()
            assert st.agent_dao.get(aqid).staged == {} and st.fake.button(card_of(st, aqid), "1A. Yes")
    run(main())


def test_no_consultant_means_marker_only_then_card_when_the_consultant_opens(conn):
    async def main():
        async with make_stack(conn, thread("r1", name="Fox")) as st:
            before = len(st.fake.methods("sendMessage"))
            aqid = await posted(st, consultant=False)
            assert card_of(st, aqid) is None and len(st.fake.methods("sendMessage")) == before
            assert st.registry.describe("r1").pending_questions == 1  # the registry row shows ❓
            await st.settle()
            assert "❓ Fox" in st.fake.messages[st.registry_message]["text"]

            await st.open_consultant("r1")
            await st.settle()
            assert card_of(st, aqid) is not None and "Where to store" in text_of(st, aqid)

            # request_user_input questions of the same root are counted together with these
            await st.ask("r1", "i9")
            assert st.registry.describe("r1").pending_questions == 2
    run(main())


def test_registry_counts_clear_when_answered(conn):
    async def main():
        async with make_stack(conn, thread("r1", name="Fox")) as st:
            await posted(st, consultant=False)
            st.agent_client.items["r1"].append(user_item("um1"))
            await st.agent.scan_thread("r1")
            await st.settle()
            assert st.registry.describe("r1").pending_questions == 0
            assert "❓" not in st.fake.messages[st.registry_message]["text"]
    run(main())


def test_questions_command_lists_agent_questions(conn):
    async def main():
        async with make_stack(conn, thread("r1", name="Fox")) as st:
            await posted(st)
            topic = st.consultant_dao.get("s1", "r1").tg_topic_id
            st.fake.push_message("/questions", topic)
            await st.pump()
            assert any("Where to store the settings?" in m["text"] for m in st.fake.live_messages(topic))
    run(main())


def test_unanswerable_question_gets_a_card_without_buttons(conn):
    async def main():
        async with make_stack(conn, thread("r1")) as st:
            aqid = await posted(st, [{"title": "T" * 3000, "options": ["a"]}])
            assert st.fake.buttons(card_of(st, aqid)) == [] and "ответьте в Codex" in text_of(st, aqid)
            topic, card = st.consultant_dao.get("s1", "r1").tg_topic_id, card_of(st, aqid)
            st.fake.push_message("x", topic, reply_to=card)
            await st.pump()
            assert st.agent_client.sent == [] and st.agent_dao.get(aqid).staged == {}
    run(main())


def test_card_survives_a_deleted_message_and_a_stale_key_heals(conn):
    async def main():
        async with make_stack(conn, thread("r1")) as st:
            aqid = await posted(st)
            old = card_of(st, aqid)
            st.fake.push_callback("a:unknownkey000", old)  # an expired key on the card: re-render with fresh keys
            await st.pump()
            await st.settle()
            assert "Кнопка устарела" in toasts(st)[-1] and st.fake.button(old, "A. Existing")
    run(main())
