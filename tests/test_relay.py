import asyncio

from cqb.domain.enums import ArchiveState, ConsultantState
from cqb.domain.models import Consultant
from cqb.services.relay import (
    BUSY, FAILED, NOT_CONFIGURED, QUEUE_FULL, QUEUED, REPLY_MAX_CHARS, TIMED_OUT, TRUNCATED, UNAVAILABLE,
    ConsultantRelay, DialogResult, Outcome, Submit, present,
)
from fake_appserver import wait_until
from tg_stack import make_stack, thread

STRANGER = 99


def run(coro):
    return asyncio.run(coro)


class Dialog:
    """Records submissions; `outcome` decides the submit result, `results` what deliver gets."""

    def __init__(self, outcome=Submit.ACCEPTED, results=(DialogResult(Outcome.REPLY, "pong"),)):
        self.outcome, self.results, self.calls = outcome, results, []

    def submit(self, session_id, text, deliver):
        self.calls.append((session_id, text))
        if self.outcome in (Submit.ACCEPTED, Submit.QUEUED):
            async def go():
                for r in self.results:
                    await deliver(r)
            asyncio.get_running_loop().create_task(go())
        return self.outcome


class Sink:
    def __init__(self):
        self.said, self.typed = [], []

    async def say(self, topic, text):
        self.said.append((topic, text))

    async def typing(self, topic):
        self.typed.append(topic)


def consultant(state=ConsultantState.OPEN, topic=7):
    return Consultant(1, "s1", "r1", -100, topic, "cqb-" + "x" * 26, state, 1, 1)


def test_relay_forwards_only_open_bound_consultants_and_real_text():
    async def main():
        dialog, sink = Dialog(), Sink()
        relay = ConsultantRelay(dialog, sink, typing_seconds=0.01)
        for c, text in [(consultant(ConsultantState.ARCHIVED), "hi"), (consultant(ConsultantState.ERROR), "hi"),
                        (consultant(ConsultantState.CREATING), "hi"), (consultant(topic=None), "hi"),
                        (consultant(), "   \n"), (consultant(), "")]:
            await relay.relay(c, text)
        assert dialog.calls == [] and sink.said == []
        await relay.relay(consultant(), "why?")
        await wait_until(lambda: sink.said)
        assert dialog.calls == [("cqb-" + "x" * 26, "why?")] and sink.said == [(7, "pong")]
    run(main())


def test_not_configured_replies_without_any_dialog():
    async def main():
        sink = Sink()
        await ConsultantRelay(None, sink).relay(consultant(), "hi")
        assert sink.said == [(7, NOT_CONFIGURED)]
    run(main())


def test_status_lines_for_every_submit_and_dialog_outcome():
    async def main():
        for outcome, line in [(Submit.QUEUED, QUEUED), (Submit.QUEUE_FULL, QUEUE_FULL), (Submit.UNAVAILABLE, UNAVAILABLE)]:
            sink = Sink()
            await ConsultantRelay(Dialog(outcome, ()), sink).relay(consultant(), "hi")
            assert sink.said == [(7, line)]
        sink = Sink()
        await ConsultantRelay(Dialog(Submit.STOPPED, ()), sink).relay(consultant(), "/stop")
        assert sink.said == [] and sink.typed == []  # /stop is silent: the running turn answers
        await asyncio.sleep(0.02)
        assert sink.typed == []
    run(main())
    assert present(DialogResult(Outcome.UNAVAILABLE)) == UNAVAILABLE and present(DialogResult(Outcome.BUSY)) == BUSY
    assert present(DialogResult(Outcome.TIMEOUT)) == TIMED_OUT and present(DialogResult(Outcome.ERROR)) == FAILED
    assert present(DialogResult(Outcome.REPLY, "  ")) == FAILED  # an empty reply is shown as a failure, not silence
    for line in (UNAVAILABLE, FAILED, BUSY, TIMED_OUT):
        assert "Traceback" not in line and "\n" not in line


def test_reply_size_is_bounded_with_a_visible_notice():
    text = present(DialogResult(Outcome.REPLY, "x" * (REPLY_MAX_CHARS * 3)))
    assert len(text) == REPLY_MAX_CHARS and text.endswith(TRUNCATED)
    assert present(DialogResult(Outcome.REPLY, "ok")) == "ok"


def test_typing_indicator_lives_exactly_while_turns_are_pending():
    async def main():
        gate = asyncio.Event()
        sink = Sink()
        pending = []

        class Slow:
            def submit(self, sid, text, deliver):
                async def go():
                    await gate.wait()
                    await deliver(DialogResult(Outcome.REPLY, "done"))
                pending.append(asyncio.get_running_loop().create_task(go()))
                return Submit.ACCEPTED if len(pending) == 1 else Submit.QUEUED

        relay = ConsultantRelay(Slow(), sink, typing_seconds=0.02)
        await relay.relay(consultant(), "one")
        await relay.relay(consultant(), "two")  # queued: still one typing task for the topic
        await asyncio.sleep(0.1)
        assert len(sink.typed) >= 3 and set(sink.typed) == {7}
        assert [t for _, t in sink.said] == [QUEUED]  # no extra messages while waiting
        gate.set()
        await wait_until(lambda: len(sink.said) == 3)
        await asyncio.sleep(0.05)
        n = len(sink.typed)
        await asyncio.sleep(0.1)
        assert len(sink.typed) == n and relay._pending == {}  # it stopped with the last reply
    run(main())


def test_a_failing_typing_action_or_reply_post_does_not_break_the_relay():
    async def main():
        class Broken(Sink):
            async def typing(self, topic):
                raise RuntimeError("429")

            async def say(self, topic, text):
                raise RuntimeError("429")

        relay = ConsultantRelay(Dialog(), Broken(), typing_seconds=0.01)
        await relay.relay(consultant(), "hi")
        await asyncio.sleep(0.05)
        assert relay._pending == {}
    run(main())


# --- through the router and the real Telegram adapter ---------------------------------

def test_router_forwards_only_registered_open_topics_to_the_dialog_and_replies_in_topic(conn):
    async def main():
        async with make_stack(conn, thread("r1", name="Fox"), thread("r2", name="Plain")) as st:
            dialog = Dialog(results=(DialogResult(Outcome.REPLY, "a" * 5000 + "\n" + "b" * 5000),))
            st.router._relay = ConsultantRelay(dialog, st.tg_topics, typing_seconds=0.01)
            await st.router._create("r1")
            topic = st.consultant_dao.get("s1", "r1").tg_topic_id
            fake = st.fake
            # never forwarded: service topics, unknown/general topics, other commands handled by the Bridge,
            # strangers, bots, forwarded messages
            fake.push_message("free text", st.control_topic)
            fake.push_message("free text", 9999)
            fake.push_message("free text", None)
            fake.push_message("hi", topic, user_id=STRANGER)
            fake.push_message("hi", topic, forwarded=True)
            fake.push_message("hi", topic, is_bot=True)
            fake.push_message("/thread", topic)
            fake.push_message("/questions", topic)
            fake.push_message("/status", topic)
            await st.pump()
            assert dialog.calls == []
            # a registered open topic is forwarded, with the pico session key of its consultant
            key = st.consultant_dao.get("s1", "r1").pico_session_key
            fake.push_message("why option A?", topic)
            fake.push_message("/clear", topic)  # escaping is the gateway's job; the relay passes text through
            await st.pump()
            await wait_until(lambda: len(fake.live_messages(topic)) >= 6)
            assert dialog.calls == [(key, "why option A?"), (key, "/clear")]
            replies = [m["text"] for m in fake.live_messages(topic) if m["text"][:1] in "ab"]
            assert replies and all(len(t) <= 4096 for t in replies)  # chunked below the Telegram limit
            sent = fake.methods("sendMessage")
            assert all("parse_mode" not in p for p in sent)  # plain text: entities can never fail a reply
            assert any(p["message_thread_id"] == topic and p["action"] == "typing" for p in fake.methods("sendChatAction"))
            # archived consultant: closed topic, nothing forwarded
            st.threads.set_archive_state("s1", "r1", ArchiveState.ARCHIVED, 1)
            await st.consultants.reconcile()
            calls = len(dialog.calls)
            fake.push_message("anyone?", topic)
            await st.pump()
            assert len(dialog.calls) == calls
    run(main())


def test_router_replies_not_configured_when_there_is_no_dialog(conn):
    async def main():
        async with make_stack(conn, thread("r1")) as st:
            st.router._relay = ConsultantRelay(None, st.tg_topics)
            await st.router._create("r1")
            topic = st.consultant_dao.get("s1", "r1").tg_topic_id
            st.fake.push_message("hi", topic)
            await st.pump()
            assert any(m["text"] == NOT_CONFIGURED for m in st.fake.live_messages(topic))
    run(main())
