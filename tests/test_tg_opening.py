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
            assert text.count("🧠 <b>reasoning</b>") == 1
            assert "🧠 <b>reasoning</b>\n▪️ First\n▪️ Second" in text and "**" not in text
            assert "ind008" in text and "uuid-agent" not in text
            assert "🤖 <b>send_message</b> · Codex → ind008" in text
            assert "🔧 subAgentActivity" not in text
            assert "Check the tests" not in text and "MUST_NOT_APPEAR" not in text
    run(main())


def test_long_tool_calls_remain_separate_expandable_blocks(conn):
    async def main():
        async with make_stack(conn, thread("r1")) as st:
            topic = await st.open_consultant("r1")
            opening = st.consultant_dao.get("s1", "r1").opening_message_id
            st.agent_client.items["r1"] = [
                {"type": "commandExecution", "id": "a", "command": "a" * 220},
                {"type": "commandExecution", "id": "b", "command": "b" * 220},
                {"type": "reasoning", "id": "c", "summary": ["first"]},
                {"type": "reasoning", "id": "d", "summary": ["second"]},
            ]
            st.fake.press(opening, "Показать тред")
            await st.pump()
            text = st.fake.live_messages(topic)[-1]["text"]
            assert text.count("<blockquote expandable>") == 2
            assert text.count("🔧 commandExecution") == 1
            assert text.count("🧠 <b>reasoning</b>") == 1
    run(main())


def test_incoming_agent_message_is_placed_in_order_without_content(conn, tmp_path):
    import json

    async def main():
        sessions = tmp_path / "sessions"
        sessions.mkdir()
        rollout = sessions / "rollout.jsonl"
        rollout.write_text(json.dumps({"timestamp": "2026-11-09T16:43:25.896Z", "type": "response_item", "payload": {
            "type": "agent_message", "author": "/root/ind008", "recipient": "/root",
            "content": [{"type": "input_text", "text": "MUST_NOT_APPEAR"}],
        }}) + "\n")
        async with make_stack(conn, thread("r1")) as st:
            topic = await st.open_consultant("r1")
            opening = st.consultant_dao.get("s1", "r1").opening_message_id
            st.router._codex_home = str(tmp_path)

            async def call(method, params=None):
                if method == "thread/read":
                    return {"thread": {"path": str(rollout), "source": "cli"}}
                return {"data": [
                    {"turnId": "t", "startedAtMs": 1794242606000, "item": {"type": "agentMessage", "id": "b", "text": "after"}},
                    {"turnId": "t", "startedAtMs": 1794242605000, "item": {"type": "agentMessage", "id": "a", "text": "before"}},
                ]}

            st.agent_client.call = call
            st.fake.press(opening, "Показать тред")
            await st.pump()
            text = st.fake.live_messages(topic)[-1]["text"]
            assert "🤖 <b>message</b> · ind008 → Codex" in text
            assert "🔧 incomingAgentMessage" not in text
            assert text.index("before") < text.index("ind008") < text.index("after")
            assert "MUST_NOT_APPEAR" not in text
    run(main())


def test_single_reasoning_is_shown_on_one_line(conn):
    async def main():
        async with make_stack(conn, thread("r1")) as st:
            topic = await st.open_consultant("r1")
            opening = st.consultant_dao.get("s1", "r1").opening_message_id
            st.agent_client.items["r1"] = [{"type": "reasoning", "id": "a", "summary": ["Aligning preflight"]}]
            st.fake.press(opening, "Показать тред")
            await st.pump()
            text = st.fake.live_messages(topic)[-1]["text"]
            assert "🧠 <b>reasoning</b> Aligning preflight" in text
    run(main())


def test_completed_agent_is_shown_as_finished_not_as_message(conn):
    async def main():
        async with make_stack(conn, thread("r1")) as st:
            topic = await st.open_consultant("r1")
            opening = st.consultant_dao.get("s1", "r1").opening_message_id
            st.agent_client.items["r1"] = [{"type": "subAgentActivity", "id": "done", "kind": "completed",
                                             "agentThreadId": "agent-id", "agentPath": "/root/tl_241_oracle_setup_preflight"}]
            st.fake.press(opening, "Показать тред")
            await st.pump()
            text = st.fake.live_messages(topic)[-1]["text"]
            assert "🤖 <b>completed</b> · tl_241_oracle_setup_preflight" in text
            assert "→" not in text
    run(main())


def test_truncated_reasoning_group_with_one_visible_entry_uses_inline_format(conn):
    async def main():
        async with make_stack(conn, thread("r1")) as st:
            topic = await st.open_consultant("r1")
            opening = st.consultant_dao.get("s1", "r1").opening_message_id
            st.agent_client.items["r1"] = [
                {"type": "reasoning", "id": "a", "summary": ["first " * 10]},
                {"type": "reasoning", "id": "b", "summary": ["last " * 12]},
                {"type": "userMessage", "id": "c", "content": [{"type": "text", "text": "x" * 3540}]},
            ]
            st.fake.press(opening, "Показать тред")
            await st.pump()
            text = st.fake.live_messages(topic)[-1]["text"]
            assert "🧠 <b>reasoning</b> …" in text
            assert "▪️" not in text
    run(main())


def test_started_agent_is_shown_without_sender(conn):
    async def main():
        async with make_stack(conn, thread("r1")) as st:
            topic = await st.open_consultant("r1")
            opening = st.consultant_dao.get("s1", "r1").opening_message_id
            st.agent_client.items["r1"] = [{"type": "subAgentActivity", "id": "start", "kind": "started",
                                             "agentThreadId": "agent-id", "agentPath": "/root/ind008"}]
            st.fake.press(opening, "Показать тред")
            await st.pump()
            text = st.fake.live_messages(topic)[-1]["text"]
            assert "🤖 <b>started</b> · ind008" in text and "→" not in text
    run(main())


def test_short_commands_are_separate_expandable_blocks(conn):
    async def main():
        async with make_stack(conn, thread("r1")) as st:
            topic = await st.open_consultant("r1")
            opening = st.consultant_dao.get("s1", "r1").opening_message_id
            st.agent_client.items["r1"] = [
                {"type": "commandExecution", "id": "a", "command": "pwd"},
                {"type": "commandExecution", "id": "b", "command": "git status"},
                {"type": "mcpToolCall", "id": "c", "server": "test", "tool": "read", "arguments": {"x": 1}},
            ]
            st.fake.press(opening, "Показать тред")
            await st.pump()
            text = st.fake.live_messages(topic)[-1]["text"]
            assert "<b>🔧 commandExecution</b>\n<blockquote expandable>$ pwd</blockquote>" in text
            assert "<blockquote expandable>$ git status</blockquote>" in text
            assert text.count("🔧 commandExecution") == 1
            assert text.count("<blockquote expandable>") == 2
            assert "<b>🔧 mcpToolCall</b> test/read" in text
    run(main())


def test_adjacent_short_mcp_calls_share_a_heading_but_long_calls_remain_separate(conn):
    async def main():
        async with make_stack(conn, thread("r1")) as st:
            topic = await st.open_consultant("r1")
            opening = st.consultant_dao.get("s1", "r1").opening_message_id
            st.agent_client.items["r1"] = [
                {"type": "mcpToolCall", "id": "a", "server": "server", "tool": "read_a", "arguments": {"path": "a" * 100}},
                {"type": "mcpToolCall", "id": "b", "server": "server", "tool": "read_b", "arguments": {"path": "b" * 100}},
                {"type": "mcpToolCall", "id": "c", "server": "server", "tool": "read_long", "arguments": {"path": "c" * 220}},
                {"type": "mcpToolCall", "id": "d", "server": "server", "tool": "read_d", "arguments": {"path": "d" * 100}},
            ]
            st.fake.press(opening, "Показать тред")
            await st.pump()
            text = st.fake.live_messages(topic)[-1]["text"]
            assert text.count("🔧 mcpToolCall") == 3
            assert "▪️ server/read_a" in text and "▪️ server/read_b" in text
            assert "<blockquote expandable>server/read_long" in text
            assert "<b>🔧 mcpToolCall</b> server/read_d" in text
    run(main())


def test_adjacent_commands_keep_internal_blank_lines_in_their_own_blocks(conn):
    async def main():
        async with make_stack(conn, thread("r1")) as st:
            topic = await st.open_consultant("r1")
            opening = st.consultant_dao.get("s1", "r1").opening_message_id
            st.agent_client.items["r1"] = [
                {"type": "commandExecution", "id": "a", "command": "echo one\n\necho two"},
                {"type": "commandExecution", "id": "b", "command": "pwd"},
            ]
            st.fake.press(opening, "Показать тред")
            await st.pump()
            text = st.fake.live_messages(topic)[-1]["text"]
            assert text.count("🔧 commandExecution") == 1
            assert text.count("<blockquote expandable>") == 2
            assert "<blockquote expandable>$ echo one\n\necho two</blockquote>" in text
            assert "<blockquote expandable>$ pwd</blockquote>" in text
    run(main())


def test_expandable_blocks_keep_ten_lines_and_report_omitted_lines(conn):
    async def main():
        async with make_stack(conn, thread("r1")) as st:
            topic = await st.open_consultant("r1")
            opening = st.consultant_dao.get("s1", "r1").opening_message_id
            command = "\n".join(f"command-line-{i}" for i in range(12))
            tool = "\n".join(f"tool-line-{i}-" + "x" * 30 for i in range(13))
            st.agent_client.items["r1"] = [
                {"type": "commandExecution", "id": "a", "command": command},
                {"type": "mcpToolCall", "id": "b", "server": "server", "tool": "call", "arguments": tool},
            ]
            st.fake.press(opening, "Показать тред")
            await st.pump()
            text = st.fake.live_messages(topic)[-1]["text"]
            assert "command-line-9\n... 2 lines more</blockquote>" in text
            assert "command-line-10" not in text and "command-line-11" not in text
            assert "tool-line-9-" in text and "... 3 lines more</blockquote>" in text
            assert "tool-line-10" not in text and "tool-line-12" not in text
    run(main())


def test_expandable_line_counter_survives_the_telegram_length_limit(conn):
    async def main():
        async with make_stack(conn, thread("r1")) as st:
            topic = await st.open_consultant("r1")
            opening = st.consultant_dao.get("s1", "r1").opening_message_id
            command = "\n".join(f"line-{i}-" + "x" * 700 for i in range(15))
            st.agent_client.items["r1"] = [{"type": "commandExecution", "id": "a", "command": command}]
            st.fake.press(opening, "Показать тред")
            await st.pump()
            text = st.fake.live_messages(topic)[-1]["text"]
            assert "... 10 lines more</blockquote>" in text
            assert "line-5-" not in text and "line-14-" not in text
    run(main())


def test_shortened_expandable_call_stays_separate_from_short_calls(conn):
    async def main():
        async with make_stack(conn, thread("r1")) as st:
            topic = await st.open_consultant("r1")
            opening = st.consultant_dao.get("s1", "r1").opening_message_id
            long_call = "\n".join("x" * 10 for _ in range(20))
            st.agent_client.items["r1"] = [
                {"type": "mcpToolCall", "id": "a", "server": "s", "tool": "one", "arguments": {"x": 1}},
                {"type": "mcpToolCall", "id": "b", "server": "s", "tool": "long", "arguments": long_call},
                {"type": "mcpToolCall", "id": "c", "server": "s", "tool": "two", "arguments": {"x": 2}},
            ]
            st.fake.press(opening, "Показать тред")
            await st.pump()
            text = st.fake.live_messages(topic)[-1]["text"]
            assert text.count("🔧 mcpToolCall") == 3
            assert "<blockquote expandable>s/long " in text
            assert "... 10 lines more</blockquote>" in text
    run(main())


def test_question_reply_shows_answers_without_question_or_wire_format(conn):
    from cqb.codex.agent_reply import build_reply

    async def main():
        async with make_stack(conn, thread("r1")) as st:
            topic = await st.open_consultant("r1")
            opening = st.consultant_dao.get("s1", "r1").opening_message_id
            reply = build_reply("call_1", 0, "A private question?", "Да, <GCP> & API")
            st.agent_client.items["r1"] = [
                {"type": "userMessage", "id": "a", "content": [{"type": "text", "text": reply}]},
                {"type": "userMessage", "id": "b", "content": [{"type": "text", "text": "A normal message"}]},
            ]
            st.fake.press(opening, "Показать тред")
            await st.pump()
            text = st.fake.live_messages(topic)[-1]["text"]
            assert "<b>👤 Вы</b> answer\nДа, &lt;GCP&gt; &amp; API" in text
            assert "<b>👤 Вы</b>\nA normal message" in text
            assert "A private question?" not in text and "questionItemId" not in text
            assert "send_user_message_question_reply" not in text
    run(main())


def test_thread_view_has_no_heading_even_when_history_is_truncated(conn):
    async def main():
        async with make_stack(conn, thread("r1")) as st:
            topic = await st.open_consultant("r1")
            opening = st.consultant_dao.get("s1", "r1").opening_message_id
            st.agent_client.items["r1"] = [
                {"type": "agentMessage", "id": str(i), "text": f"entry {i}"} for i in range(101)
            ]
            st.fake.press(opening, "Показать тред")
            await st.pump()
            text = st.fake.live_messages(topic)[-1]["text"]
            assert text.startswith("👁️‍🗨️ <b>Главный тред</b>\n\n<b>🤖 Codex</b>")
            assert "Показана последняя часть треда" not in text and "Тред Codex" not in text
    run(main())


def test_reasoning_excludes_markdown_and_survives_truncation(conn):
    async def main():
        async with make_stack(conn, thread("r1")) as st:
            topic = await st.open_consultant("r1")
            opening = st.consultant_dao.get("s1", "r1").opening_message_id
            summary = "**" + "я" * 1500 + "**"
            st.agent_client.items["r1"] = [{"type": "reasoning", "id": "a", "summary": [summary]}]
            st.fake.press(opening, "Показать тред")
            await st.pump()
            text = st.fake.live_messages(topic)[-1]["text"]
            assert "я" * 1500 + "</blockquote>" in text and "**" not in text

            # A huge summary is truncated to the message limit.
            st.agent_client.items["r1"] = [{"type": "reasoning", "id": "b", "summary": ["x" * 5000]}]
            st.fake.press(opening, "Показать тред")
            await st.pump()
            text = st.fake.live_messages(topic)[-1]["text"]
            assert "…</blockquote>" in text
    run(main())


def test_reasoning_truncates_after_ten_lines(conn):
    async def main():
        async with make_stack(conn, thread("r1")) as st:
            topic = await st.open_consultant("r1")
            opening = st.consultant_dao.get("s1", "r1").opening_message_id
            summary = "\n".join("x" * 200 for _ in range(15))
            st.agent_client.items["r1"] = [{"type": "reasoning", "id": "a", "summary": [summary]}]
            st.fake.press(opening, "Показать тред")
            await st.pump()
            text = st.fake.live_messages(topic)[-1]["text"]
            assert "\n... 5 lines more</blockquote>" in text
    run(main())


def test_reasoning_omitted_lines_survive_message_limit(conn):
    async def main():
        async with make_stack(conn, thread("r1")) as st:
            topic = await st.open_consultant("r1")
            opening = st.consultant_dao.get("s1", "r1").opening_message_id
            summary = "\n".join("x" * 700 for _ in range(15))
            st.agent_client.items["r1"] = [{"type": "reasoning", "id": "a", "summary": [summary]}]
            st.fake.press(opening, "Показать тред")
            await st.pump()
            text = st.fake.live_messages(topic)[-1]["text"]
            assert "\n... 9 lines more</blockquote>" in text
    run(main())


def test_repeated_empty_wait_calls_are_one_line_without_collab_header(conn):
    async def main():
        async with make_stack(conn, thread("r1")) as st:
            topic = await st.open_consultant("r1")
            opening = st.consultant_dao.get("s1", "r1").opening_message_id
            st.agent_client.items["r1"] = [
                *[{"type": "collabAgentToolCall", "id": f"w{i}", "tool": "wait", "receiverThreadIds": []} for i in range(20)],
                {"type": "reasoning", "id": "r", "summary": ["Doing work"]},
                *[{"type": "collabAgentToolCall", "id": f"x{i}", "tool": "wait", "receiverThreadIds": []} for i in range(4)],
                {"type": "collabAgentToolCall", "id": "target", "tool": "wait", "receiverThreadIds": ["agent-id"]},
            ]
            st.fake.press(opening, "Показать тред")
            await st.pump()
            text = st.fake.live_messages(topic)[-1]["text"]
            assert text.count("💤 <b>wait</b> -&gt;") == 2
            assert "▪️ wait -&gt;" not in text
            assert "<b>🔧 collabAgentToolCall</b> wait -&gt; agent-id" in text
    run(main())
