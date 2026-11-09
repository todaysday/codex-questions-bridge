"""A new consultant topic starts with its opening message, pinned; cards of pending questions come only after it."""

import asyncio

from cqb.domain.enums import ArchiveState, RunState
from tg_stack import NOW, make_stack, snapshot, thread


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
            assert st.consultant_dao.get("s1", "r1").opening_message_id == opening
            assert card == st.card_of(st.qid_of("i1"))
            assert topic_calls(st, topic) == [("sendMessage", opening), ("pinChatMessage", opening), ("sendMessage", card)]
            assert st.fake.pinned[1:] == [opening] and card not in st.fake.pinned  # [0] is the registry message
            first = st.fake.messages[opening]["text"]
            assert first.startswith("ctx — · 🗜️—\n🤖 Fox") and "/status" in first
            assert [b["text"] for b in st.fake.buttons(opening)] == ["❓ Вопросы", "📊 Статус", "📜 Показать тред"]
    run(main())


def test_opening_buttons_route_to_the_bound_topic_and_keep_the_pin(conn):
    async def main():
        async with make_stack(conn, thread("r1", name="Fox"), thread("r2", name="Other")) as st:
            topic = await st.open_consultant("r1")
            await st.settle()
            st.status_source.threads["r1"] = snapshot("r1", RunState.WORKING, updated=NOW - 5, name="Fox")
            opening = st.consultant_dao.get("s1", "r1").opening_message_id
            st.clock.now += 3 * 86400  # these buttons remain usable without an expiring action key
            st.fake.press(opening, "Статус")
            await st.pump()
            await st.status_done()
            assert any("Субагенты</b>" in m["text"] for m in st.fake.live_messages(topic))
            st.fake.press(opening, "Вопросы")
            await st.pump()
            assert any(m["text"] == "Нет неотвеченных вопросов" for m in st.fake.live_messages(topic))
            assert not st.fake.messages[opening].get("deleted") and st.fake.buttons(opening)
            assert st.relay.got == []
    run(main())


def test_opening_questions_button_reposts_pending_cards_not_the_pin(conn):
    async def main():
        async with make_stack(conn, thread("r1", name="Fox")) as st:
            topic = await st.open_consultant("r1")
            await st.ask("r1", "i1")
            await st.settle()
            opening = st.consultant_dao.get("s1", "r1").opening_message_id
            qid = st.qid_of("i1")
            old = st.card_of(qid)
            st.fake.press(opening, "Вопросы")
            await st.pump()
            assert st.fake.messages[old].get("deleted")
            assert st.card_of(qid) != old and not st.fake.messages[opening].get("deleted")
            assert st.fake.messages[st.card_of(qid)]["thread_id"] == topic
    run(main())


def test_opening_buttons_reject_unknown_messages_and_other_users(conn):
    async def main():
        async with make_stack(conn, thread("r1", name="Fox")) as st:
            topic = await st.open_consultant("r1")
            await st.settle()
            opening = st.consultant_dao.get("s1", "r1").opening_message_id
            data = st.fake.button(opening, "Статус")["callback_data"]
            other = st.fake.push_message("other", topic)
            st.fake.push_callback(data, other)
            st.fake.push_callback(data, opening, user_id=999999)
            await st.pump()
            assert st.status_source.calls == []
            assert not st.fake.messages[opening].get("deleted")
    run(main())


def test_status_button_reports_an_inflight_status(conn, monkeypatch):
    async def main():
        async with make_stack(conn, thread("r1", name="Fox")) as st:
            await st.open_consultant("r1")
            opening = st.consultant_dao.get("s1", "r1").opening_message_id
            monkeypatch.setattr(st.status_cards, "request", lambda root, topic: False)
            st.fake.press(opening, "Статус")
            await st.pump()
            assert st.fake.callback_answers[-1]["text"] == "статус уже собирается"
            assert not st.fake.messages[opening].get("deleted")
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
            assert st.consultant_dao.get("s1", "r1").opening_message_id == opening
            assert len(cards) == 2 and texts(st, topic)[0].startswith("ctx — · 🗜️—\n🤖 Fox")
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
            assert st.consultant_dao.get("s1", "r1").opening_message_id is None
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
            assert texts(st, topic)[0].startswith("ctx — · 🗜️—\n🤖 Fox")
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


def test_opening_reply_goes_to_root_and_not_consultant(conn):
    async def main():
        async with make_stack(conn, thread("r1")) as st:
            topic = await st.open_consultant("r1")
            opening = st.consultant_dao.get("s1", "r1").opening_message_id
            st.fake.push_message("hello", topic, reply_to=opening)
            await st.pump()
            assert st.agent_client.sent == [("r1", "hello")]
            assert st.relay.got == []
    run(main())


def test_show_thread_includes_calls_without_outputs(conn):
    async def main():
        async with make_stack(conn, thread("r1")) as st:
            topic = await st.open_consultant("r1")
            opening = st.consultant_dao.get("s1", "r1").opening_message_id
            st.agent_client.items["r1"] = [
                {"type": "userMessage", "id": "u", "content": [{"type": "text", "text": "Hello"}]},
                {"type": "mcpToolCall", "id": "t", "server": "srv", "tool": "read", "arguments": {"path": "x" * 300}, "result": "SECRET_OUTPUT"},
                {"type": "agentMessage", "id": "a", "text": "Done"},
            ]
            st.fake.press(opening, "Показать тред")
            await st.pump()
            text = st.fake.live_messages(topic)[-1]["text"]
            assert "Hello" in text and "Done" in text and "srv/read" in text
            assert "x" * 300 in text and "<blockquote expandable>" in text
            assert "SECRET_OUTPUT" not in text
    run(main())


def test_show_thread_groups_reasoning_and_shows_collaboration_input(conn, tmp_path):
    import json

    async def main():
        sessions = tmp_path / "sessions"
        sessions.mkdir()
        rollout = sessions / "rollout.jsonl"
        rollout.write_text(json.dumps({"type": "response_item", "payload": {
            "type": "function_call", "namespace": "collaboration", "name": "send_message",
            "call_id": "call1", "arguments": json.dumps({"target": "/root/ind008", "message": "Check the tests"}),
        }}) + "\n" + json.dumps({"type": "response_item", "payload": {
            "type": "function_call_output", "call_id": "call1", "output": "MUST_NOT_APPEAR",
        }}) + "\n")
        async with make_stack(conn, thread("r1")) as st:
            topic = await st.open_consultant("r1")
            opening = st.consultant_dao.get("s1", "r1").opening_message_id
            st.router._codex_home = str(tmp_path)
            original_call = st.agent_client.call

            async def call(method, params=None):
                if method == "thread/read":
                    if params["threadId"] == "r1":
                        return {"thread": {"path": str(rollout)}}
                    return {"thread": {"source": {"subAgent": {"thread_spawn": {"agent_path": "/root/ind008"}}}}}
                return await original_call(method, params)

            st.agent_client.call = call
            st.agent_client.items["r1"] = [
                {"type": "reasoning", "id": "a", "summary": ["**First**"]},
                {"type": "reasoning", "id": "b", "summary": ["**Second**"]},
                {"type": "subAgentActivity", "id": "call1", "kind": "interacted", "agentThreadId": "uuid-agent"},
            ]
            st.fake.press(opening, "Показать тред")
            await st.pump()
            text = st.fake.live_messages(topic)[-1]["text"]
            assert text.count("🔧 reasoning") == 1
            assert "First" in text and "Second" in text and "**" not in text
            assert "ind008" in text and "uuid-agent" not in text
            assert "<blockquote expandable>" in text and "Check the tests" in text
            assert "MUST_NOT_APPEAR" not in text
    run(main())
