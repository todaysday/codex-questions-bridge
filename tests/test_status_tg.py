"""/status in consultant topics: one static snapshot message per request, posted once when everything is ready.
The control topic never gets a status; a second request while one runs is answered, not started."""

import asyncio

import pytest

from cqb.domain.enums import ArchiveState, RunState as R
from cqb.domain.status import GoalSummary, StatusSummary
from cqb.telegram.fmt import TEXT_LIMIT
from cqb.telegram.status_post import BUSY, UNAVAILABLE
from test_tg_control import STRANGER
from tg_stack import NOW, make_stack, snapshot, thread


def run(coro):
    return asyncio.run(coro)


def cards(st, topic):
    return [(i, m) for i, m in st.fake.messages.items() if m["thread_id"] == topic and not m.get("deleted")
            and ("Субагенты" in m["text"] or "📈 лимит —" in m["text"])]


def typing(st, topic):
    return [p for p in st.fake.methods("sendChatAction") if p.get("message_thread_id") == topic]


async def consultant_topic(st, root="r1"):
    await st.router._create(root)
    return st.consultant_dao.get("s1", root).tg_topic_id


class Says:
    """A narrator that answers at once."""

    def __init__(self, text=StatusSummary("Корень правит тесты.", "прогнать e2e")):
        self.text, self.calls = text, []

    async def summarize(self, source_id, root_id, timeout):
        self.calls.append((source_id, root_id, timeout))
        return self.text


class Gate:
    """A narrator that waits until the test lets it answer (or raises / hangs)."""

    def __init__(self, text=StatusSummary("Готово."), fail=False):
        self.open, self.text, self.fail, self.started = asyncio.Event(), text, fail, 0

    async def summarize(self, source_id, root_id, timeout):
        self.started += 1
        await self.open.wait()
        if self.fail:
            raise RuntimeError("boom")
        return self.text


class Hangs:
    async def summarize(self, source_id, root_id, timeout):
        await asyncio.sleep(60)


def test_status_posts_one_static_message_with_numbers_now_lines_and_summary(conn):
    async def main():
        narrator = Says()
        async with make_stack(conn, thread("r1", name="Fox"), narrator=narrator) as st:
            st.status_source.threads["r1"] = snapshot("r1", R.WORKING, updated=NOW - 5, name="Fox", model="gpt-6.1")
            topic = await consultant_topic(st)
            sends = len(st.fake.methods("sendMessage"))
            st.fake.push_message("/status", topic)
            await st.pump()
            await st.status_done()
            [(mid, card)] = cards(st, topic)
            assert "🟢Fox\n🧠gpt-6.1 low<b> · </b>" in card["text"] and "💭" not in card["text"]
            assert "🔛<b>:</b> Корень правит тесты.\n\n🔜<b>:</b> прогнать e2e" in card["text"]
            assert len(card["text"]) <= TEXT_LIMIT and card["parse_mode"] == "HTML"
            assert narrator.calls == [("s1", "r1", 300)]
            assert len(st.fake.methods("sendMessage")) == sends + 1  # exactly one message, no placeholder
            assert st.relay.got == []  # never relayed to the consultant
            # a second request is a NEW message; the first is history and is never edited
            st.clock.now += 120
            st.status_source.threads["r1"] = snapshot("r1", R.IDLE, updated=NOW - 5, name="Fox")
            st.fake.push_message("/status@cqb_test_bot", topic)
            await st.pump()
            await st.status_done()
            both = cards(st, topic)
            assert len(both) == 2 and both[0][0] == mid and "edits" not in both[0][1] and "edits" not in both[1][1]
            assert both[0][1]["text"].startswith("🟢Fox\n") and both[1][1]["text"].startswith("⚪Fox\n")
            assert not st.fake.methods("editMessageText") or all(
                p["message_id"] not in (i for i, _ in both) for p in st.fake.methods("editMessageText"))
    run(main())


def test_nothing_is_posted_until_everything_is_ready_and_typing_is_refreshed(conn):
    async def main():
        gate = Gate()
        async with make_stack(conn, thread("r1", name="Fox"), narrator=gate) as st:
            st.status_source.threads["r1"] = snapshot("r1", R.WORKING, updated=NOW - 5)
            topic = await consultant_topic(st)
            before = len(st.fake.live_messages(topic))
            st.fake.push_message("/status", topic)
            await st.pump()  # returns at once: the collect runs in its own task and does not block polling
            for _ in range(200):
                if len(typing(st, topic)) >= 3:
                    break
                await asyncio.sleep(0.01)
            assert len(typing(st, topic)) >= 3  # refreshed, not sent once
            assert len(st.fake.live_messages(topic)) == before and "r1" in st.status_cards._running  # no placeholder
            gate.open.set()
            await st.status_done()
            assert len(cards(st, topic)) == 1
            sent = len(typing(st, topic))
            await asyncio.sleep(0.05)
            assert len(typing(st, topic)) == sent  # the indicator stops with the snapshot
    run(main())


def test_second_status_while_one_collects_is_answered_not_started(conn):
    async def main():
        gate = Gate()
        async with make_stack(conn, thread("r1", name="Fox"), thread("r2", name="Owl"), narrator=gate) as st:
            st.status_source.threads["r1"] = snapshot("r1", R.WORKING, updated=NOW - 5)
            st.status_source.threads["r2"] = snapshot("r2", R.WORKING, updated=NOW - 5)
            topic = await consultant_topic(st)
            other = await consultant_topic(st, "r2")
            st.fake.push_message("/status", topic)
            await st.pump()
            await asyncio.sleep(0.02)
            st.fake.push_message("/status", topic)
            await st.pump()
            assert [m["text"] for m in st.fake.live_messages(topic)].count(BUSY) == 1
            assert gate.started == 1  # no second collect for the same root
            st.fake.push_message("/status", other)  # another root is independent
            await st.pump()
            await asyncio.sleep(0.02)
            assert gate.started == 2 and BUSY not in [m["text"] for m in st.fake.live_messages(other)]
            gate.open.set()
            await st.status_done()
            assert len(cards(st, topic)) == 1 and len(cards(st, other)) == 1
            st.fake.push_message("/status", topic)  # finished: a new request works again
            await st.pump()
            await st.status_done()
            assert len(cards(st, topic)) == 2
    run(main())


def test_summary_that_does_not_arrive_in_time_gives_the_card_without_it(conn, monkeypatch):
    async def main():
        import cqb.services.status as status_mod
        monkeypatch.setattr(status_mod, "SUMMARY_GRACE", 0.05)
        async with make_stack(conn, thread("r1", name="Fox"), narrator=Hangs(), summary_timeout=0.1) as st:
            st.status_source.threads["r1"] = snapshot("r1", R.IDLE, updated=NOW - 5, name="Fox")
            topic = await consultant_topic(st)
            st.fake.push_message("/status", topic)
            await st.pump()
            await asyncio.wait_for(st.status_done(), 5)
            [(_, card)] = cards(st, topic)
            assert "📝ошибка резюме: агент статуса не ответил вовремя" in card["text"] and "В процессе" not in card["text"]
            assert " <b>ctx</b> —" in card["text"] and "📈" not in card["text"]  # numbers are all there
    run(main())


@pytest.mark.parametrize("failing", [True, False])
def test_failing_or_missing_narrator_gives_the_card_with_the_unavailable_line(conn, failing):
    async def main():
        narrator = Gate(fail=True) if failing else None  # None: no [pico], the no-op narrator
        async with make_stack(conn, thread("r1", name="Fox"), narrator=narrator) as st:
            st.status_source.threads["r1"] = snapshot("r1", R.IDLE, updated=NOW - 5)
            if narrator is not None:
                narrator.open.set()
            topic = await consultant_topic(st)
            st.fake.push_message("/status", topic)
            await st.pump()
            await st.status_done()
            [(_, card)] = cards(st, topic)
            assert card["text"].count("ошибка резюме") == 1 and "boom" not in card["text"]
    run(main())


def test_registry_has_no_status_button_and_status_in_control_topic_posts_nothing(conn):
    async def main():
        async with make_stack(conn, thread("r1", name="Fox", updated=NOW - 10)) as st:
            st.status_source.threads["r1"] = snapshot("r1", R.WAITING_INPUT)
            assert not [b for b in st.fake.buttons(st.registry_message) if "Подробнее" in b["text"] or "ℹ️" in b["text"]]
            st.fake.push_message("/status", st.control_topic)
            await st.pump()
            assert st.status_source.calls == [] and len(st.control_messages()) == 1
    run(main())


def test_unavailable_data_is_reported_not_crashed(conn):
    async def main():
        async with make_stack(conn, thread("r1", name="Fox"), narrator=Says()) as st:
            topic = await consultant_topic(st)
            st.status_source.fail = True
            st.fake.push_message("/status", topic)
            await st.pump()
            await st.status_done()
            assert any(UNAVAILABLE in m["text"] for m in st.fake.live_messages(topic))
            assert cards(st, topic) == []
    run(main())


def test_status_from_strangers_is_ignored(conn):
    async def main():
        async with make_stack(conn, thread("r1", name="Fox")) as st:
            st.status_source.threads["r1"] = snapshot("r1")
            topic = await consultant_topic(st)
            before = len(st.fake.methods("sendMessage"))
            st.fake.push_message("/status", topic, user_id=STRANGER)
            await st.pump()
            assert len(st.fake.methods("sendMessage")) == before and st.status_source.calls == []
    run(main())


def test_status_in_other_topics_and_closed_consultants_is_ignored(conn):
    async def main():
        async with make_stack(conn, thread("r1", name="Fox")) as st:
            st.status_source.threads["r1"] = snapshot("r1")
            topic = await consultant_topic(st)
            st.fake.push_message("/status", st.control_topic)
            st.fake.push_message("/status", 9999)
            st.threads.set_archive_state("s1", "r1", ArchiveState.ARCHIVED, 1)
            await st.consultants.reconcile()
            st.fake.push_message("/status", topic)
            await st.pump()
            assert st.status_source.calls == [] and st.relay.got == []
    run(main())


def test_opening_message_mentions_status(conn):
    async def main():
        async with make_stack(conn, thread("r1", name="Fox")) as st:
            topic = await consultant_topic(st)
            assert any("/status" in m["text"] for m in st.fake.live_messages(topic))
    run(main())


def test_card_is_sent_as_html_with_escaped_names_and_notices_stay_plain(conn):
    async def main():
        async with make_stack(conn, thread("r1", name="Fox"), narrator=Says()) as st:
            st.status_source.threads["r1"] = snapshot("r1", R.WORKING, updated=NOW - 5, name='<Fox> & "Co"')
            topic = await consultant_topic(st)
            st.fake.push_message("/status", topic)
            await st.pump()
            await st.status_done()
            [(_, card)] = cards(st, topic)  # the fake validates the HTML like Telegram: it was accepted as it is
            assert card["parse_mode"] == "HTML" and card["text"].startswith("🟢&lt;Fox&gt; &amp; &quot;Co&quot;\n")
            st.status_source.fail = True
            st.fake.push_message("/status", topic)
            await st.pump()
            await st.status_done()
            notice = next(m for m in st.fake.live_messages(topic) if UNAVAILABLE in m["text"])
            assert notice["parse_mode"] is None
    run(main())


def test_a_card_telegram_cannot_parse_is_resent_as_plain_text_never_lost(conn, monkeypatch):
    async def main():
        import cqb.telegram.status_post as post
        monkeypatch.setattr(post, "render_status", lambda r, link="": "🟢Fox &amp; Co\n🔛<b>:</b> шаг <i>1\n📈 лимит —")  # broken
        async with make_stack(conn, thread("r1", name="Fox"), narrator=Says()) as st:
            st.status_source.threads["r1"] = snapshot("r1", R.WORKING, updated=NOW - 5)
            topic = await consultant_topic(st)
            st.fake.push_message("/status", topic)
            await st.pump()
            await st.status_done()
            [(_, card)] = cards(st, topic)
            assert card["text"] == "🟢Fox & Co\n🔛: шаг 1\n📈 лимит —" and card["parse_mode"] is None
            sends = [p for p in st.fake.methods("sendMessage") if "📈" in p["text"]]
            assert [p.get("parse_mode") for p in sends] == ["HTML", None]  # tried as HTML first, then the same text plain
    run(main())


def test_a_server_side_parse_error_also_falls_back_to_plain_text(conn):
    async def main():
        async with make_stack(conn, thread("r1", name="Fox"), narrator=Says()) as st:
            st.status_source.threads["r1"] = snapshot("r1", R.WORKING, updated=NOW - 5, name="Fox")
            topic = await consultant_topic(st)
            st.fake.fail("sendMessage", 400, "Bad Request: can't parse entities: Unsupported start tag", times=1)
            st.fake.push_message("/status", topic)
            await st.pump()
            await st.status_done()
            [(_, card)] = cards(st, topic)
            assert card["parse_mode"] is None and "<b>" not in card["text"] and card["text"].startswith("🟢Fox\n")
            assert " ctx —\n" in card["text"] and "🔛: Корень правит тесты." in card["text"]
    run(main())


def test_open_link_line_is_the_last_line_of_the_posted_card_and_telegram_accepts_the_html(conn):
    async def main():
        link = "https://chatgpt.com/codex/tasks/{thread_id}?a=1&b=2"
        async with make_stack(conn, thread("r1", name="Fox"), narrator=Says(), open_link=link) as st:
            st.status_source.threads["r1"] = snapshot("r1", R.WORKING, updated=NOW - 5, name="Fox")
            topic = await consultant_topic(st)
            st.fake.push_message("/status", topic)
            await st.pump()
            await st.status_done()
            [(mid, card)] = cards(st, topic)
            lines = card["text"].splitlines()
            assert card["parse_mode"] == "HTML"  # the fake rejects bad HTML like Telegram: it was accepted as it is
            assert lines[-1] == '🔗<a href="https://chatgpt.com/codex/tasks/r1?a=1&amp;b=2">Открыть в Codex</a>'
            assert "📈" not in card["text"] and card["text"].count("🔗") == 1
            # not in the registry message and not in the opening message of the topic
            assert not any("🔗" in m["text"] or "chatgpt.com" in m["text"] for i, m in st.fake.messages.items() if i != mid)
    run(main())


def test_no_link_line_by_default(conn):
    async def main():
        async with make_stack(conn, thread("r1", name="Fox"), narrator=Says()) as st:
            st.status_source.threads["r1"] = snapshot("r1", R.WORKING, updated=NOW - 5, name="Fox")
            topic = await consultant_topic(st)
            st.fake.push_message("/status", topic)
            await st.pump()
            await st.status_done()
            [(mid, card)] = cards(st, topic)
            assert "🔗" not in card["text"] and "<a " not in card["text"] and "📈" not in card["text"]
    run(main())


@pytest.mark.parametrize("rich", [False, True])
def test_status_agent_button_opens_agent_thread(conn, rich):
    async def main():
        async with make_stack(conn, thread("r1", name="Fox"), narrator=Says(), rich=rich) as st:
            st.status_source.threads["r1"] = snapshot("r1", name="Fox")
            st.status_source.children["r1"] = (snapshot("child1", R.WORKING, parent="r1", nickname="Inventory"),)
            st.status_source.goals["r1"] = GoalSummary("Проверить задачу", "active", 0, None)
            st.agent_client.items["child1"] = [{"type": "agentMessage", "id": "m1", "text": "Agent work"}]
            topic = await consultant_topic(st)
            st.fake.push_message("/status", topic)
            await st.pump()
            await st.status_done()
            [(mid, card)] = cards(st, topic)
            if rich:
                assert '<tg-button type="callback_data"' in card["rich"]
                assert '<details><summary>🤖<b>Субагенты' in card["rich"]
                assert '<details><summary>🎯 Проверить задачу</summary><p>Проверить задачу</p></details>' in card["rich"]
                assert "Показать агентов" not in card["rich"]
                assert "</summary><p><br>" not in card["rich"]
                assert "<br></p></details>" not in card["rich"]
                assert "</details><p><br>" not in card["rich"]
                label = "👁️‍🗨️"
            else:
                assert '<blockquote expandable>' in card["text"]
                label = "Inventory"
            st.fake.press(mid, label)
            await st.pump()
            message = st.fake.live_messages(topic)[-1]
            text = message["text"]
            assert text.startswith("<blockquote>\u00a0</blockquote>\n👁️‍🗨️ <b>Inventory</b>\n<blockquote>\u00a0</blockquote>")
            assert "Agent work" in text
    run(main())


def test_collapsed_status_previews_goal_and_agents(conn):
    async def main():
        async with make_stack(conn, thread("r1", name="Fox"), narrator=Says(), rich=True) as st:
            st.status_source.threads["r1"] = snapshot("r1", name="Fox")
            goal = "a" * 210 + " & конец"
            st.status_source.goals["r1"] = GoalSummary(goal, "active", 0, None)
            st.status_source.children["r1"] = tuple(
                snapshot(f"c{i}", R.WORKING, parent="r1", nickname=f"Agent {i}") for i in range(6)
            )
            topic = await consultant_topic(st)
            st.fake.push_message("/status", topic)
            await st.pump()
            await st.status_done()
            [(_, card)] = cards(st, topic)
            rich = card["rich"]
            assert "<summary>🎯 " + "a" * 200 + "…</summary>" in rich
            assert "<p>" + "a" * 210 + " &amp; конец</p></details>" in rich
            assert "<summary>🤖<b>Субагенты" in rich
            assert "Agent 0" in rich.split("</summary>")[-2]
            assert rich.count('<tg-button type="callback_data"') == 6
    run(main())


def test_goal_preview_without_agents(conn):
    async def main():
        async with make_stack(conn, thread("r1", name="Fox"), narrator=Says(), rich=True) as st:
            st.status_source.threads["r1"] = snapshot("r1", name="Fox")
            st.status_source.goals["r1"] = GoalSummary("Goal & test", "active", 0, None)
            topic = await consultant_topic(st)
            st.fake.push_message("/status", topic)
            await st.pump()
            await st.status_done()
            [(_, card)] = cards(st, topic)
            assert "<summary>🎯 Goal &amp; test</summary>" in card["rich"]
    run(main())
