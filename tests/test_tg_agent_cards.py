"""Agent-message question cards: ONE card per question, each with its own number, buttons and answer message."""

import asyncio
import re

from cqb.codex.agent_reply import build_reply
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
LONG_OPTION = "Use the long approach: " + "very detailed words, " * 200  # about 4200 characters


def run(coro):
    return asyncio.run(coro)


def q_of(st, aqid):
    return st.agent_dao.get(aqid)


def card_ids(st, aqid):
    q = q_of(st, aqid)
    return [q.card_message_id, *q.card_extra_ids]


def text_of(st, aqid):
    """The whole card: every message of it, in order."""
    return "\n".join(st.fake.messages[m]["text"] for m in card_ids(st, aqid))


def card_of(st, aqid):
    return q_of(st, aqid).card_message_id


def last_of(st, aqid):
    return card_ids(st, aqid)[-1]  # the message that carries the keyboard


def press(st, aqid, part):
    return st.fake.push_callback(st.fake.button(last_of(st, aqid), part)["callback_data"], last_of(st, aqid))


def toasts(st):
    return [a.get("text") for a in st.fake.callback_answers]


def topic_of(st):
    return st.consultant_dao.get("s1", "r1").tg_topic_id


async def posted(st, questions=Q1, item_id="call_1", consultant=True):
    """Put an agentMessage with questions into r1, let the watcher see it and (with a consultant) post the cards.
    Returns the question ids in number order (one per question)."""
    if consultant and st.consultant_dao.get("s1", "r1") is None:
        await st.open_consultant("r1")
    st.agent_client.items.setdefault("r1", []).append(agent_item(item_id, questions))
    await st.agent.scan_thread("r1")
    await st.settle()
    found = [q for q in st.agent.open_questions("r1") if q.item_id == item_id]
    return [q.agent_question_id for q in sorted(found, key=lambda q: q.display_no)]


def reply_text(item_id, index, title, answer):
    return build_reply(item_id, index, title, answer)


def test_single_question_stage_confirm_send_and_delivered_only_after_the_user_message(conn):
    async def main():
        async with make_stack(conn, thread("r1", name="FoxProfile")) as st:
            [aqid] = await posted(st)
            fake, card = st.fake, card_of(st, aqid)
            assert fake.messages[card]["thread_id"] == topic_of(st)
            text = text_of(st, aqid)
            assert text.splitlines()[0] == "❓ Q1 · FoxProfile" and text.splitlines()[1] == ""  # no state tag, no clock, own line
            assert "agent question" not in text and "a_" + aqid[2:] not in text and "pending" not in text
            assert "Where to store the settings?" in text and "A. Existing table" in text and "B. Separate table" in text
            assert "обсужда" in text
            assert [b["text"] for b in fake.buttons(card)] == ["A", "B", "✍️ Ответить текстом"]

            press(st, aqid, "B")
            await st.pump()
            assert "Выбрано: Separate table" in text_of(st, aqid) and st.agent_client.sent == []
            assert fake.button(card, "Отправить в Codex") and fake.button(card, "Отмена")

            press(st, aqid, "Отправить в Codex")
            await st.pump()
            assert st.agent_client.sent == [("r1", reply_text("call_1", 0, Q1[0]["title"], "Separate table и не трьінди After delegation, wait for the agent’s result using notifications or a wait tool."))]
            sent = text_of(st, aqid)
            assert "ждём подтверждения" in sent and "Доставлено" not in sent
            assert fake.buttons(card) == [] and q_of(st, aqid).sent_at is not None
            assert q_of(st, aqid).state is S.PENDING  # not delivered until a userMessage is seen

            st.agent_client.items["r1"].append(user_item("um1", "whatever the app stored"))
            await st.agent.scan_thread("r1")
            await st.settle()
            done = text_of(st, aqid)
            assert "✅ Доставлено" in done and "Separate table" in done and fake.buttons(card) == []
            assert done.splitlines()[0].endswith("· ✅ отвечено") and q_of(st, aqid).state is S.ANSWERED
    run(main())


def test_a_message_with_several_questions_gives_separate_cards_and_separate_answers(conn):
    async def main():
        async with make_stack(conn, thread("r1", name="Fox")) as st:
            ids = await posted(st, Q3)
            fake = st.fake
            assert len(ids) == 3 and len({card_of(st, i) for i in ids}) == 3  # three cards, three messages
            heads = [text_of(st, i).splitlines()[0] for i in ids]
            assert [h.split(" · ")[0] for h in heads] == ["❓ Q1", "❓ Q2", "❓ Q3"]
            bodies = [text_of(st, i) for i in ids]
            assert "First?" in bodies[0] and "Second?" not in bodies[0] and "Third?" not in bodies[0]  # never merged
            assert "Third?" in bodies[2] and "C. Blue" in bodies[2] and "1. First?" not in bodies[0]
            assert [b["text"] for b in fake.buttons(card_of(st, ids[0]))] == ["A", "B", "✍️ Ответить текстом"]
            assert [b["text"] for b in fake.buttons(card_of(st, ids[1]))] == ["✍️ Ответить текстом"]
            assert [b["text"] for b in fake.buttons(card_of(st, ids[2]))] == ["A", "B", "C", "✍️ Ответить текстом"]

            # Q2 is answered on its own: stage, confirm, and one message that names only its question
            press(st, ids[0], "B")
            await st.pump()
            assert "Выбрано: No" in text_of(st, ids[0]) and "Выбрано" not in text_of(st, ids[2])
            press(st, ids[0], "Отправить в Codex")
            await st.pump()
            assert st.agent_client.sent == [("r1", reply_text("call_1", 0, "First?", "No и не трьінди After delegation, wait for the agent’s result using notifications or a wait tool."))]
            assert q_of(st, ids[1]).staged is None and q_of(st, ids[2]).staged is None

            press(st, ids[2], "C")
            await st.pump()
            press(st, ids[2], "Отправить в Codex")
            await st.pump()
            assert st.agent_client.sent[1] == ("r1", reply_text("call_1", 2, "Third?", "Blue и не трьінди After delegation, wait for the agent’s result using notifications or a wait tool."))  # its own message
            assert len(st.agent_client.sent) == 2

            # the app's reply naming Q1 answers exactly that question; the others keep waiting
            st.agent_client.items["r1"].append(user_item("um1", reply_text("call_1", 0, "First?", "No")))
            await st.agent.scan_thread("r1")
            await st.settle()
            assert q_of(st, ids[0]).state is S.ANSWERED and q_of(st, ids[1]).state is S.PENDING and q_of(st, ids[2]).state is S.PENDING
            assert fake.button(card_of(st, ids[1]), "Ответить текстом")
            st.agent_client.items["r1"].append(user_item("um2", reply_text("call_1", 2, "Third?", "Blue")))
            await st.agent.scan_thread("r1")
            await st.settle()
            assert [q.display_no for q in st.agent.open_questions("r1")] == [2]
            assert text_of(st, ids[2]).splitlines()[0].endswith("· ✅ отвечено")
            assert st.relay.got == []  # no discussion happened
    run(main())


def test_free_text_answer_needs_the_button_and_goes_out_as_that_questions_own_message(conn):
    async def main():
        async with make_stack(conn, thread("r1")) as st:
            ids = await posted(st, Q3)
            topic, card = topic_of(st), card_of(st, ids[1])
            st.fake.push_message("not armed, so a discussion", topic, reply_to=card)
            await st.pump()
            assert q_of(st, ids[1]).staged is None and len(st.relay.got) == 1
            assert st.relay.got[0][1].startswith('[Context: the user replies about question Q2 (Second?)] ')
            press(st, ids[1], "Ответить текстом")
            await st.pump()
            assert toasts(st)[-1] == "Ответьте на это сообщение: текст уйдёт в Codex"
            assert "следующий ответ на это сообщение уйдёт в Codex" in text_of(st, ids[1])
            assert "следующий ответ" not in text_of(st, ids[0])  # armed: only this card
            st.fake.push_message("my own answer, with «quotes»", topic, reply_to=card)
            await st.pump()
            assert "Выбрано: my own answer, with «quotes»" in text_of(st, ids[1])
            press(st, ids[1], "Отправить в Codex")
            await st.pump()
            assert st.agent_client.sent == [("r1", reply_text("call_1", 1, "Second?", "my own answer, with «quotes» и не трьінди After delegation, wait for the agent’s result using notifications or a wait tool."))]
            assert len(st.relay.got) == 1  # only the unarmed reply went to the consultant
    run(main())


def test_armed_reply_validation_expiry_and_cancel(conn):
    async def main():
        async with make_stack(conn, thread("r1")) as st:
            [aqid] = await posted(st)
            topic, card = topic_of(st), card_of(st, aqid)
            press(st, aqid, "Ответить текстом")
            await st.pump()
            assert [b["text"] for b in st.fake.buttons(card)].count("↩️ Отмена") == 1
            assert not [b for b in st.fake.buttons(card) if "Ответить текстом" in b["text"]]
            st.fake.push_message("y" * 1500, topic, reply_to=card)
            st.fake.push_message("ok", topic, reply_to=card, user_id=999)  # a stranger: ignored
            await st.pump()
            assert q_of(st, aqid).staged is None and "Слишком длинный" in st.fake.live_messages(topic)[-1]["text"]
            assert "следующий ответ" in text_of(st, aqid)  # still armed after a refused reply

            st.clock.now += 601  # expiry: the hint leaves the card and the next reply is a discussion again
            await st.settle()
            assert "следующий ответ" not in text_of(st, aqid)
            st.fake.push_message("late", topic, reply_to=card)
            await st.pump()
            assert q_of(st, aqid).staged is None and len(st.relay.got) == 1

            press(st, aqid, "Ответить текстом")
            await st.pump()
            press(st, aqid, "Отмена")
            await st.pump()
            assert "следующий ответ" not in text_of(st, aqid) and toasts(st)[-1] == "Отменено"
            st.fake.push_message("after cancel", topic, reply_to=card)
            await st.pump()
            assert q_of(st, aqid).staged is None and len(st.relay.got) == 2
    run(main())


def test_option_press_and_closing_drop_the_armed_state_and_closed_cards_are_discussed(conn):
    async def main():
        async with make_stack(conn, thread("r1")) as st:
            [aqid] = await posted(st)
            topic, card = topic_of(st), card_of(st, aqid)
            press(st, aqid, "Ответить текстом")
            await st.pump()
            press(st, aqid, "A")
            await st.pump()
            assert "следующий ответ" not in text_of(st, aqid) and q_of(st, aqid).staged == "Existing table"
            st.fake.push_message("not an answer", topic, reply_to=card)
            await st.pump()
            assert q_of(st, aqid).staged == "Existing table" and len(st.relay.got) == 1

            press(st, aqid, "Отмена")
            await st.pump()
            press(st, aqid, "Ответить текстом")
            await st.pump()
            st.agent_client.items["r1"].append(user_item("um1"))  # answered in the Codex app
            await st.agent.scan_thread("r1")
            await st.settle()
            assert "следующий ответ" not in text_of(st, aqid)
            assert text_of(st, aqid).splitlines()[0].endswith("· отвечено в другом месте")
            st.fake.push_message("why did I pick that?", topic, reply_to=card)
            await st.pump()
            assert q_of(st, aqid).staged is None
            assert st.relay.got[-1][1].startswith("[Context: the user replies about question Q1 (already answered) (Where to store")
    run(main())


def test_nothing_is_cut_a_very_long_option_continues_in_more_messages_with_the_keyboard_on_the_last(conn):
    async def main():
        async with make_stack(conn, thread("r1", name="Fox")) as st:
            long_title = "Which approach should the migration take? " + "context sentence. " * 120
            [aqid] = await posted(st, [{"title": long_title, "options": ["Short", LONG_OPTION, "Last option"]}])
            ids = card_ids(st, aqid)
            assert len(ids) >= 2 and len(set(ids)) == len(ids)  # continuation messages
            messages = [st.fake.messages[m] for m in ids]
            assert all(len(m["text"]) <= 4096 for m in messages) and all(m["thread_id"] == topic_of(st) for m in messages)
            assert "…" not in "".join(m["text"] for m in messages)  # nothing is cut with an ellipsis
            joined = " ".join(" ".join(re.sub(r"↪ Q1 · \d+/\d+", "", m["text"]).split()) for m in messages)
            assert " ".join(LONG_OPTION.split()) in joined and " ".join(long_title.split()) in joined  # whole texts
            assert "A. Short" in joined and "C. Last option" in joined
            assert messages[0]["text"].startswith("❓ Q1 · Fox\n\n") and messages[1]["text"].startswith("↪ Q1 · 2/")
            # one-time keys live on exactly one message: the last; buttons are the short labels
            with_keys = [m for m in ids if st.fake.buttons(m)]
            assert with_keys == [ids[-1]]
            assert [b["text"] for b in st.fake.buttons(ids[-1])] == ["A", "B", "C", "✍️ Ответить текстом"]
            # a reply to a continuation message is a reply to the card
            st.fake.push_message("is the long one risky?", topic_of(st), reply_to=ids[1])
            await st.pump()
            assert st.relay.got[-1][1].startswith("[Context: the user replies about question Q1 (")
            # the whole card follows the state: staging rewrites it, the keyboard stays on the last message only
            press(st, aqid, "B")
            await st.pump()
            ids2 = card_ids(st, aqid)
            assert ids2[:len(ids)] == ids  # the same messages were edited (a longer card only adds one)
            assert "Выбрано: Use the long approach" in text_of(st, aqid)
            assert [m for m in ids2 if st.fake.buttons(m)] == [ids2[-1]]
            assert [b["text"] for b in st.fake.buttons(ids2[-1])] == ["✅ Отправить в Codex", "↩️ Отмена"]
            assert all(len(st.fake.messages[m]["text"]) <= 4096 for m in ids2)
            press(st, aqid, "Отправить в Codex")
            await st.pump()
            assert st.agent_client.sent[0][1] == reply_text("call_1", 0, long_title, LONG_OPTION + " и не трьінди After delegation, wait for the agent’s result using notifications or a wait tool.")  # the exact answer
            # answered: the keyboard is gone everywhere and the header says so on the first message
            st.agent_client.items["r1"].append(user_item("um1", reply_text("call_1", 0, long_title, LONG_OPTION)))
            await st.agent.scan_thread("r1")
            await st.settle()
            assert not [m for m in card_ids(st, aqid) if st.fake.buttons(m)]
            assert st.fake.messages[card_of(st, aqid)]["text"].splitlines()[0].endswith("· ✅ отвечено")
    run(main())


def test_closing_keeps_the_whole_question_text_and_a_short_card_is_one_message(conn):
    async def main():
        async with make_stack(conn, thread("r1")) as st:
            [aqid] = await posted(st, [{"title": "T?", "options": ["a", LONG_OPTION]}])
            ids = card_ids(st, aqid)
            assert len(ids) >= 2
            press(st, aqid, "A")  # staged "a": the card keeps both options
            await st.pump()
            st.agent_client.items["r1"].append(user_item("um1"))
            await st.agent.scan_thread("r1")
            await st.settle()
            assert card_ids(st, aqid) == ids  # closing never removes the question text
            [short] = await posted(st, [{"title": "Short?", "options": ["x", "y"]}], item_id="call_2")
            assert len(card_ids(st, short)) == 1
    run(main())


def test_answered_elsewhere_before_confirmation(conn):
    async def main():
        async with make_stack(conn, thread("r1")) as st:
            [aqid] = await posted(st)
            card = card_of(st, aqid)
            press(st, aqid, "A")
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
            [aqid] = await posted(st)
            press(st, aqid, "A")
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
            [aqid] = await posted(st)
            await st.agent.handle(ThreadArchiveChanged("r1", True))
            await st.settle()
            assert "⌛" in text_of(st, aqid) and st.fake.buttons(card_of(st, aqid)) == []
            assert text_of(st, aqid).splitlines()[0].endswith("· устарел") and st.agent_dao.get(aqid).state is S.STALE
    run(main())


def test_send_failures_keep_the_question_open_and_say_so(conn):
    async def main():
        async with make_stack(conn, thread("r1")) as st:
            [aqid] = await posted(st)
            press(st, aqid, "A")
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
            ids = await posted(st, Q3)
            press(st, ids[0], "A")
            await st.pump()
            assert q_of(st, ids[0]).staged == "Yes" and st.fake.button(card_of(st, ids[0]), "Отправить")
            press(st, ids[0], "Отмена")
            await st.pump()
            assert q_of(st, ids[0]).staged is None and st.fake.button(card_of(st, ids[0]), "A")
    run(main())


def test_no_consultant_means_marker_only_then_card_when_the_consultant_opens(conn):
    async def main():
        async with make_stack(conn, thread("r1", name="Fox")) as st:
            before = len(st.fake.methods("sendMessage"))
            [aqid] = await posted(st, consultant=False)
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
            [aqid] = await posted(st, [{"title": "T" * 25000, "options": ["a"]}])  # past the stored bounds
            assert not q_of(st, aqid).answerable and "ответьте в Codex" in text_of(st, aqid)
            assert not [m for m in card_ids(st, aqid) if st.fake.buttons(m)]
            topic, card = topic_of(st), card_of(st, aqid)
            st.fake.push_message("x", topic, reply_to=card)
            await st.pump()
            assert st.agent_client.sent == [] and q_of(st, aqid).staged is None
    run(main())


def test_card_survives_a_deleted_message_and_a_stale_key_heals(conn):
    async def main():
        async with make_stack(conn, thread("r1")) as st:
            [aqid] = await posted(st)
            old = card_of(st, aqid)
            st.fake.push_callback("a:unknownkey000", old)  # an expired key on the card: re-render with fresh keys
            await st.pump()
            await st.settle()
            assert "Кнопка устарела" in toasts(st)[-1] and st.fake.button(old, "A")
    run(main())


def test_card_spacing_agent_card_has_an_empty_line_after_the_header_and_before_the_options(conn):
    async def main():
        async with make_stack(conn, thread("r1", name="Fox")) as st:
            [aqid] = await posted(st, Q1)
            text = text_of(st, aqid)
            assert text.startswith("❓ Q1 · Fox\n\nWhere to store the settings?\n\nA. Existing table\nB. Separate table\n\n")
    run(main())
