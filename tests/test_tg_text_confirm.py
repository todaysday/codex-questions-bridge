"""Typed answer to an armed card (`✍️ Ответить текстом`): the confirmation message carries the card's own
send/cancel buttons, shows the typed text, and is removed once the answer is sent or cancelled.
Both card kinds (request_user_input and agent-message questions) run through the same scenarios."""

import asyncio

import pytest

from cqb.codex.appserver import RpcError
from cqb.domain.models import QuestionItem
from cqb.telegram.router import CANCELLED_NOTE, DONE_NOTE, STALE
from tg_stack import agent_item, make_stack, thread

OTHER_ITEM = (QuestionItem("name", "", "Pick a name", True, ()),)
AGENT_Q = [{"title": "Name it?", "options": ["A1"]}]
PROMPT = "Подтвердите отправку:\n"
SEND, CANCEL = "✅ Отправить в Codex", "↩️ Отмена"


def run(coro):
    return asyncio.run(coro)


class Flow:
    """One armed card of either kind, with what differs between the kinds."""

    def __init__(self, st, kind):
        self.st, self.kind = st, kind

    async def start(self):
        st = self.st
        self.topic = await st.open_consultant("r1")
        if self.kind == "plain":
            await st.ask("r1", "i1", items=OTHER_ITEM)
            await st.settle()
            self.qid = st.qid_of("i1")
            self.card = st.card_of(self.qid)
            self.actions = ("send", "cancel")
        else:
            st.agent_client.items.setdefault("r1", []).append(agent_item("call_1", AGENT_Q))
            await st.agent.scan_thread("r1")
            await st.settle()
            [q] = st.agent.open_questions("r1")
            self.qid, self.card = q.agent_question_id, st.agent_dao.get(q.agent_question_id).card_message_id
            self.actions = ("asend", "acancel")
        await self.press(self.card, "Ответить текстом")
        return self

    async def press(self, message, part):
        self.st.fake.push_callback(self.st.fake.button(message, part)["callback_data"], message)
        await self.st.pump()
        await self.st.settle()

    async def type(self, text):
        """Reply to the armed card; returns the confirmation message id (the bot's newest message in the topic)."""
        self.st.fake.push_message(text, self.topic, reply_to=self.card)
        await self.st.pump()
        self.confirm = max(i for i, m in self.st.fake.messages.items() if m["thread_id"] == self.topic)
        return self.confirm

    def delivered(self):
        return len(self.st.client.responses if self.kind == "plain" else self.st.agent_client.sent)

    def break_sending(self, broken: bool):
        if self.kind == "agent":
            self.st.agent_client.send_error = RpcError("turn/start", {"message": "boom"}) if broken else None
            return
        client = self.st.client
        if broken:
            self.real, client.respond = client.respond, self._raise
        else:
            client.respond = self.real

    @staticmethod
    async def _raise(*_):
        raise RuntimeError("boom")

    def question(self):
        st = self.st
        return st.question_dao.get(self.qid) if self.kind == "plain" else st.agent_dao.get(self.qid)

    def staged(self):
        return self.question().staged


kinds = pytest.mark.parametrize("kind", ["plain", "agent"])


def live(st, message):
    return not st.fake.messages[message].get("deleted")


def toasts(st):
    return [a.get("text") for a in st.fake.callback_answers]


@kinds
def test_confirmation_message_has_the_two_buttons_with_valid_one_time_keys_and_shows_the_text(conn, kind):
    async def main():
        async with make_stack(conn, thread("r1")) as st:
            f = await Flow(st, kind).start()
            conf = await f.type("my <own> answer")
            assert st.fake.messages[conf]["text"] == PROMPT + "my <own> answer"
            assert "parse_mode" not in st.fake.methods("sendMessage")[-1]  # plain text: the typed text needs no escaping
            buttons = st.fake.buttons(conf)
            assert [b["text"] for b in buttons] == [SEND, CANCEL]
            assert f.staged() is not None  # the card is in its confirm step too
            card_buttons = st.fake.buttons(f.card)
            assert [b["callback_data"] for b in buttons] == [b["callback_data"] for b in card_buttons]  # the card's own keys
            for b, action in zip(buttons, f.actions):
                key = b["callback_data"]
                assert key.startswith("a:") and len(key.encode()) <= 64
                stored = st.keys._actions.get(key, 0)
                assert stored.action == action and stored.payload == f.qid and stored.root_thread_id == "r1"
    run(main())


@kinds
def test_send_from_the_confirmation_delivers_once_deletes_it_and_the_key_is_used_up(conn, kind):
    async def main():
        async with make_stack(conn, thread("r1")) as st:
            f = await Flow(st, kind).start()
            conf = await f.type("typed answer")
            sent_keys = [b["callback_data"] for b in st.fake.buttons(conf)]
            before = len(st.fake.methods("sendMessage"))
            await f.press(conf, "Отправить")
            assert f.delivered() == 1 and toasts(st)[-1] == "Отправлено в Codex; ждём подтверждения"
            assert not live(st, conf) and st.fake.buttons(f.card) == []  # confirmation gone, card has no buttons left
            if kind == "plain":
                assert st.client.responses[0][1] == {"answers": {"name": {"answers": ["typed answer и не трьінди After delegation, wait for the agent’s result using notifications or a wait tool."]}}}
            else:
                assert "typed answer" in st.agent_client.sent[0][1]
            assert len(st.fake.methods("sendMessage")) == before  # nothing else was posted
            st.fake.push_callback(sent_keys[0], conf)  # the very same key again
            await st.pump()
            assert toasts(st)[-1] == STALE and f.delivered() == 1  # one-time: a second press delivers nothing
    run(main())


@kinds
def test_cancel_from_the_confirmation_deletes_it_returns_the_card_to_normal_and_disarms(conn, kind):
    async def main():
        async with make_stack(conn, thread("r1")) as st:
            f = await Flow(st, kind).start()
            conf = await f.type("typed answer")
            await f.press(conf, "Отмена")
            assert toasts(st)[-1] == "Отменено" and not live(st, conf)
            assert f.staged() is None and f.delivered() == 0
            texts = [b["text"] for b in st.fake.buttons(f.card)]
            assert "✍️ Ответить текстом" in texts and SEND not in texts  # the normal stage again
            assert "следующий ответ" not in st.fake.messages[f.card]["text"]  # disarmed
            st.fake.push_message("a plain remark", f.topic, reply_to=f.card)
            await st.pump()
            assert f.staged() is None and len(st.relay.got) == 1  # a discussion again, not an answer
    run(main())


@kinds
def test_a_failed_send_keeps_the_confirmation_and_its_buttons_usable(conn, kind):
    async def main():
        async with make_stack(conn, thread("r1")) as st:
            f = await Flow(st, kind).start()
            conf = await f.type("typed answer")
            f.break_sending(True)
            await f.press(conf, "Отправить")
            assert f.delivered() == 0 and live(st, conf)
            assert [b["text"] for b in st.fake.buttons(conf)] == [SEND, CANCEL]
            f.break_sending(False)
            await f.press(conf, "Отправить")  # the same message works after the failure
            assert f.delivered() == 1 and not live(st, conf)
    run(main())


@kinds
@pytest.mark.parametrize("press,note", [("Отправить", DONE_NOTE), ("Отмена", CANCELLED_NOTE)])
def test_when_the_bot_may_not_delete_the_confirmation_becomes_a_one_line_note_without_buttons(conn, kind, press, note):
    async def main():
        async with make_stack(conn, thread("r1")) as st:
            f = await Flow(st, kind).start()
            conf = await f.type("typed answer")
            st.fake.fail("deleteMessage", 400, "Bad Request: message can't be deleted for everyone")
            await f.press(conf, press)
            assert live(st, conf) and st.fake.messages[conf]["text"] == note and st.fake.buttons(conf) == []
            assert "\n" not in note
    run(main())


@kinds
def test_an_error_while_removing_the_confirmation_is_ignored(conn, kind):
    async def main():
        async with make_stack(conn, thread("r1")) as st:
            f = await Flow(st, kind).start()
            conf = await f.type("typed answer")
            st.fake.delete_message(conf)  # the owner already removed it: delete and edit both say "not found"
            st.fake.fail("deleteMessage", 400, "Bad Request: message to delete not found")
            st.fake.push_callback(st.fake.button(f.card, "Отправить")["callback_data"], conf)
            await st.pump()
            assert f.delivered() == 1  # the answer went out, the cleanup failure changed nothing
    run(main())


@kinds
def test_expired_keys_do_nothing_and_the_dead_confirmation_is_removed(conn, kind):
    async def main():
        async with make_stack(conn, thread("r1")) as st:
            f = await Flow(st, kind).start()
            conf = await f.type("typed answer")
            st.clock.now += 86401  # the day the keys live
            await f.press(conf, "Отправить")
            assert toasts(st)[-1] == STALE and f.delivered() == 0 and f.staged() is not None
            assert not live(st, conf)
    run(main())


@kinds
def test_pressing_a_card_button_instead_leaves_a_dead_confirmation_that_goes_on_its_next_press(conn, kind):
    async def main():
        async with make_stack(conn, thread("r1")) as st:
            f = await Flow(st, kind).start()
            conf = await f.type("typed answer")
            await f.press(f.card, "Отмена")  # the card's own cancel uses up the shared key
            assert f.staged() is None and live(st, conf)
            await f.press(conf, "Отмена")
            assert toasts(st)[-1] == STALE and not live(st, conf) and f.delivered() == 0
    run(main())


@kinds
def test_the_longest_allowed_answer_fits_one_confirmation_message_and_is_sent_whole(conn, kind):
    async def main():
        async with make_stack(conn, thread("r1")) as st:
            f = await Flow(st, kind).start()
            answer = "я" * 1000
            conf = await f.type(answer)
            assert st.fake.messages[conf]["text"] == PROMPT + answer and len(st.fake.messages[conf]["text"]) <= 4096
            await f.press(conf, "Отправить")
            sent = st.client.responses[0][1]["answers"]["name"]["answers"][0] if kind == "plain" else st.agent_client.sent[0][1]
            assert answer in sent and f.delivered() == 1
    run(main())
