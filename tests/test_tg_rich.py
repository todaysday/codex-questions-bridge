"""Rich messages (Bot API 10.3 in-text buttons): the renderer, the API calls, question cards and the registry end to end
against the fake Telegram, the fallbacks (method unsupported, markup refused, flag off) and the fake's own validator."""

import asyncio
import logging
import re

import pytest

from cqb.codex.events import ThreadStatusChanged
from cqb.config import ConfigError, parse_config
from cqb.domain.enums import QuestionLifecycle as L, RunState as R
from cqb.domain.models import QuestionItem, QuestionOption
from cqb.telegram.api import BadRequest, NetworkError, RichRejected, _to_update
from cqb.telegram.card_post import card_parts
from cqb.telegram.models import ActionSpec, Bold, Button, Doc, Para, Row
from cqb.telegram.registry_view import PAGE_SIZE, RICH_PAGE_SIZE
from cqb.telegram.render import fits_rich, to_html, to_view
from cqb.telegram.router import STALE
from fake_telegram import CHAT_ID, TOKEN, FakeTelegram, _Fail, parse_rich
from tg_stack import NOW, agent_item, make_stack, thread
from test_tg_api import with_api

OTHER_ITEM = (QuestionItem("name", "", "Pick a name", True, ()),)
ITEM = (QuestionItem("pick", "Storage", "Where <store> profile & settings?", True,
                     (QuestionOption("Existing table", "no migration"), QuestionOption("Separate <table>"))),)
AGENT_Q = [{"title": "Where to store it?", "options": ["Existing table", "Separate table"]}]


def run(coro):
    return asyncio.run(coro)


def stack(conn, *threads, **kw):
    return make_stack(conn, *threads, rich=True, **kw)


def toasts(st):
    return [a.get("text") for a in st.fake.callback_answers]


async def card(st, root="r1", item_id="i1", **kw):
    """A question card of a root with an open consultant; returns (question id, card message id)."""
    if st.consultant_dao.get("s1", root) is None:
        await st.open_consultant(root)
    await st.ask(root, item_id, **kw)
    await st.settle()
    qid = st.qid_of(item_id)
    return qid, st.card_of(qid)


async def press(st, message, part):
    qid = st.fake.press(message, part)
    await st.pump()
    await st.settle()
    return qid


# --- the renderer ----------------------------------------------------------------------


def bound(text, data):
    return Button(text, callback_data=data)


def test_html_escapes_everything_dynamic_and_buttons_are_in_the_line():
    doc = Doc((
        Para((Bold("Q<1>"), " · a&b \"q\" 'x'"), volatile=True),
        Para(("A. <b>not bold</b>\nsecond line", Button("A", callback_data='a:"1"&', style="primary"))),
        Para(("see", Button("💬 Открыть", url="https://t.me/c/1/2?x=1&y=2"))),
    ))
    out = to_html(doc)
    assert out.startswith("<p><b>Q&lt;1&gt;</b> · a&amp;b &quot;q&quot; &#x27;x&#x27;</p>")
    assert 'A. &lt;b&gt;not bold&lt;/b&gt;<br>second line <tg-button type="callback_data" style="primary" data="a:&quot;1&quot;&amp;">A</tg-button>' in out
    assert '<tg-button type="url" url="https://t.me/c/1/2?x=1&amp;y=2">💬 Открыть</tg-button></p>' in out
    parse_rich({"html": out})  # the fake's validator (the documented grammar) accepts it


def test_a_row_holds_at_most_eight_buttons_and_a_button_label_is_clipped():
    row = Row(tuple(bound(f"b{i}", f"k:{i}") for i in range(19)))
    out = to_html(Doc((Para(("x",)), row)))
    assert out.count("<tg-button-row") == 3
    assert [r.count("<tg-button ") for r in out.split("</tg-button-row>")[:3]] == [8, 8, 3]
    assert "…" in to_html(Doc((Row((bound("я" * 200, "k:1"),)),)))
    parse_rich({"html": out})
    with pytest.raises(ValueError):
        to_html(Doc((Row((Button("x", ActionSpec("create")),)),)))  # not bound: no callback id


def test_classic_view_of_the_same_doc_and_the_rich_limits():
    doc = Doc((
        Para(("head",), volatile=True), Para(("title",)),
        Para(("A. one", bound("A", "k:a")), glue="\n"), Para(("B. two", bound("B", "k:b")), glue="\n"),
        Para(("note",)), Row((bound("go", "k:g"),)),
    ))
    view = to_view(doc)
    assert (view.header, view.body) == ("head", "title\nA. one\nB. two\n\nnote")
    assert [[b.text for b in r] for r in view.keyboard] == [["A", "B"], ["go"]]
    assert fits_rich(Doc((Para(("x" * 32768,)),))) and not fits_rich(Doc((Para(("x" * 32769,)),)))
    assert fits_rich(Doc(tuple(Para(("p",)) for _ in range(500)))) and not fits_rich(Doc(tuple(Para(("p",)) for _ in range(501))))


# --- the API ---------------------------------------------------------------------------


def test_send_and_edit_rich_message_params_and_errors():
    async def body(api, fake, sleeps):
        topic = await api.create_forum_topic(CHAT_ID, "T")
        html = '<p>hi <tg-button type="callback_data" data="k:1">go</tg-button></p>'
        sent = await api.send_rich_message(CHAT_ID, html, thread_id=topic.thread_id)
        assert fake.methods("sendRichMessage")[0] == {
            "chat_id": CHAT_ID, "rich_message": {"html": html}, "message_thread_id": topic.thread_id}
        assert fake.messages[sent.message_id]["text"] == "hi [go]" and fake.buttons(sent.message_id)[0]["callback_data"] == "k:1"
        with pytest.raises(BadRequest) as same:
            await api.edit_rich_message(CHAT_ID, sent.message_id, html)
        assert same.value.not_modified and not isinstance(same.value, RichRejected)
        await api.edit_rich_message(CHAT_ID, sent.message_id, "<p>new</p>")
        assert fake.methods("editMessageText")[-1]["rich_message"] == {"html": "<p>new</p>"}
        assert "text" not in fake.methods("editMessageText")[-1]
        fake.delete_message(sent.message_id)
        with pytest.raises(BadRequest) as gone:
            await api.edit_rich_message(CHAT_ID, sent.message_id, "<p>x</p>")
        assert gone.value.gone and not isinstance(gone.value, RichRejected)
        with pytest.raises(RichRejected) as bad:
            await api.send_rich_message(CHAT_ID, "<p>x <blink>y</blink></p>")
        assert not bad.value.unsupported and bad.value.code == 400
        fake.rich_supported = False
        with pytest.raises(RichRejected) as nope:
            await api.send_rich_message(CHAT_ID, "<p>x</p>")
        assert nope.value.unsupported and nope.value.code == 404
        with pytest.raises(RichRejected) as nope2:
            await api.edit_rich_message(CHAT_ID, 1, "<p>x</p>")
        assert nope2.value.unsupported
    with_api(body)


def test_a_lost_response_is_never_retried_for_send_but_is_for_edit():
    async def body(api, fake, sleeps):
        fake.drop_next["sendRichMessage"] = 1
        with pytest.raises(NetworkError):
            await api.send_rich_message(CHAT_ID, "<p>x</p>")
        assert fake.methods().count("sendRichMessage") == 1  # it may have been delivered: no duplicate
        sent = await api.send_rich_message(CHAT_ID, "<p>x</p>")
        fake.drop_next["editMessageText"] = 1
        await api.edit_rich_message(CHAT_ID, sent.message_id, "<p>y</p>")
        assert fake.methods().count("editMessageText") == 2
        fake.fail("sendRichMessage", 429, "Too Many Requests: retry after 3", retry_after=3)
        await api.send_rich_message(CHAT_ID, "<p>z</p>")  # a 429 is always safe to retry
        assert sleeps.calls[-1] == 3.25
    with_api(body)


# --- the fake validates like the documented grammar ------------------------------------


@pytest.mark.parametrize("rich,why", [
    ({"html": "<p>x</p>", "markdown": "x"}, "exactly one"),
    ({}, "exactly one"),
    ({"html": "<p>x"}, "end tag"),
    ({"html": "<p>x</div>"}, "unmatched"),
    ({"html": "<p><blink>x</blink></p>"}, "unsupported start tag"),
    ({"html": "<p>a < b</p>"}, "bare"),
    ({"html": "<p>a & b</p>"}, "bare"),
    ({"html": "<p>&copy;</p>"}, "entity"),
    ({"html": '<p><tg-button type="callback_data" data="">x</tg-button></p>'}, "1-64"),
    ({"html": '<p><tg-button type="callback_data" data="' + "я" * 33 + '">x</tg-button></p>'}, "1-64"),
    ({"html": '<p><tg-button type="url" style="link" url="https://t.me">x</tg-button></p>'}, "style"),
    ({"html": '<p><tg-button type="callback_data" style="red" data="a">x</tg-button></p>'}, "style"),
    ({"html": '<p><tg-button type="callback_data" data="a"><b>x</b></tg-button></p>'}, "plain text"),
    ({"html": '<tg-button type="callback_data" data="a">x</tg-button>'}, "inside a paragraph"),
    ({"html": '<tg-button-row>' + '<tg-button type="callback_data" data="a">x</tg-button>' * 9 + '</tg-button-row>'}, "1-8"),
    ({"html": "<tg-button-row></tg-button-row>"}, "1-8"),
    ({"html": "<p>" + "x" * 32769 + "</p>"}, "too long"),
    ({"html": "<p>x</p>" * 501}, "too many blocks"),
])
def test_the_fake_refuses_what_telegram_would(rich, why):
    with pytest.raises(_Fail) as exc:
        parse_rich(rich)
    assert exc.value.code == 400 and why in exc.value.description.lower()


def test_the_fake_accepts_the_documented_examples():
    ok = parse_rich({"html": (
        '<p>Pick: <tg-button type="callback_data" style="primary" data="size:S">Small</tg-button> or '
        '<tg-button type="url" url="https://t.me">x</tg-button> &amp; &#x27;q&#x27; &nbsp;<br>next</p>'
        '<tg-button-row align="center"><tg-button type="copy_text" text="t">Copy</tg-button>'
        '<tg-button type="disabled">Off</tg-button></tg-button-row>')})
    assert [b["text"] for b in ok.buttons] == ["Small", "x", "Copy", "Off"] and ok.rows == [ok.rows[0]] and len(ok.rows[0]) == 2
    assert ok.plain.startswith("Pick: [Small] or [x]")


# --- callbacks from messages without text ----------------------------------------------


def test_callback_from_a_rich_message_without_text_is_decoded():
    cb = _to_update({"update_id": 5, "callback_query": {
        "id": "q1", "from": {"id": 42}, "data": "a:1",
        "message": {"message_id": 77, "chat": {"id": CHAT_ID}, "date": 1, "rich_message": {"blocks": []}}}}).callback
    assert (cb.query_id, cb.from_id, cb.chat_id, cb.message_id, cb.data) == ("q1", 42, CHAT_ID, 77, "a:1")
    gone = _to_update({"update_id": 6, "callback_query": {
        "id": "q2", "from": {"id": 42}, "data": "a:1", "message": {"message_id": 77, "chat": {"id": CHAT_ID}, "date": 0}}})
    assert gone.callback.message_id == 77
    reply = _to_update({"update_id": 7, "message": {
        "message_id": 9, "chat": {"id": CHAT_ID}, "from": {"id": 42}, "is_topic_message": True, "message_thread_id": 3,
        "reply_to_message": {"message_id": 77, "chat": {"id": CHAT_ID}, "rich_message": {"blocks": []}}, "text": "hi"}}).message
    assert reply.reply_to_message_id == 77 and reply.text == "hi"


# --- question cards --------------------------------------------------------------------


def test_rich_question_card_layout_and_flow_to_a_delivered_answer(conn):
    async def main():
        async with stack(conn, thread("r1", name="Fox <b>&</b>")) as st:
            qid, msg = await card(st, items=ITEM)
            fake = st.fake
            assert not fake.methods("sendMessage") or all("Q1" not in p["text"] for p in fake.methods("sendMessage"))
            html = fake.messages[msg]["rich"]
            assert re.match(r"<p>❓ <b>Q1</b> · Fox &lt;b&gt;&amp;&lt;/b&gt; · \d\d:\d\d</p>", html)
            assert "<p>Storage: Where &lt;store&gt; profile &amp; settings?</p>" in html
            assert re.search(r'<p>A\. Existing table — no migration <tg-button type="callback_data" style="primary" data="a:\w+">A</tg-button></p>', html)
            assert re.search(r'<p>B\. Separate &lt;table&gt; <tg-button type="callback_data" style="primary" data="a:\w+">B</tg-button></p>', html)
            assert html.endswith("</tg-button></tg-button-row>") and fake.rows(msg) == [["✍️ Ответить текстом"]]
            assert fake.messages[msg]["markup"] is None  # no inline keyboard: every button is in the text

            await press(st, msg, "B")  # the update carries a message with rich_message and no text
            assert st.question_dao.get(qid).lifecycle is L.DECISION_STAGED and st.client.responses == []
            staged = fake.messages[msg]["rich"]
            assert "Выбрано: Separate &lt;table&gt;<br>Отправить этот ответ в Codex?" in staged
            assert fake.rows(msg) == [["✅ Отправить в Codex", "↩️ Отмена"]] and 'style="success"' in staged and 'style="danger"' in staged
            assert [b["text"] for b in fake.buttons(msg)] == ["✅ Отправить в Codex", "↩️ Отмена"]  # options lost their buttons
            assert sum("Q1" in m["text"] for m in fake.messages.values() if m["thread_id"] == st.consultant_dao.get("s1", "r1").tg_topic_id) == 1

            await press(st, msg, "Отправить")
            assert st.client.responses[0][1] == {"answers": {"pick": {"answers": ["Separate <table>"]}}}
            assert "ждём подтверждения" in fake.messages[msg]["text"] and fake.buttons(msg) == []

            await st.resolve("r1")
            await st.settle()
            assert "✅ Доставлено" in fake.messages[msg]["text"] and fake.buttons(msg) == []
            assert "<tg-button" not in fake.messages[msg]["rich"] and "отвечено" in fake.messages[msg]["text"].splitlines()[0]
            assert fake.messages[msg]["edits"] == 3  # staged, sent, delivered: always the same message
    run(main())


def test_cancel_returns_to_the_options_and_secret_cards_have_no_buttons(conn):
    async def main():
        async with stack(conn, thread("r1")) as st:
            qid, msg = await card(st)
            await press(st, msg, "A")
            await press(st, msg, "Отмена")
            assert st.question_dao.get(qid).lifecycle is L.PENDING_CONFIRMED and st.client.responses == []
            assert [b["text"] for b in st.fake.buttons(msg)][:2] == ["A", "B"]
            sid, smsg = await card(st, item_id="i2", secret=True)
            assert st.fake.buttons(smsg) == [] and "🔒" in st.fake.messages[smsg]["text"]
            assert "<tg-button" not in st.fake.messages[smsg]["rich"]
    run(main())


@pytest.mark.parametrize("kind", ["plain", "agent"])
def test_typed_answer_confirmation_is_a_rich_message_with_a_button_row(conn, kind):
    async def main():
        async with stack(conn, thread("r1")) as st:
            topic = await st.open_consultant("r1")
            if kind == "plain":
                await st.ask("r1", "i1", items=OTHER_ITEM)
                await st.settle()
                msg = st.card_of(st.qid_of("i1"))
            else:
                st.agent_client.items.setdefault("r1", []).append(agent_item("call_1", AGENT_Q))
                await st.agent.scan_thread("r1")
                await st.settle()
                [q] = st.agent.open_questions("r1")
                msg = st.agent_dao.get(q.agent_question_id).card_message_id
            fake = st.fake
            await press(st, msg, "Ответить текстом")
            assert "<p>✍️ следующий ответ на это сообщение уйдёт в Codex</p>" in fake.messages[msg]["rich"]
            assert fake.rows(msg) == [["↩️ Отмена"]]
            fake.push_message("my <own> & answer", topic, reply_to=msg)
            await st.pump()
            conf = max(i for i, m in fake.messages.items() if m["thread_id"] == topic)
            assert fake.messages[conf]["rich"].startswith("<p>Подтвердите отправку:</p><p>my &lt;own&gt; &amp; answer</p>")
            assert fake.rows(conf) == [["✅ Отправить в Codex", "↩️ Отмена"]] and fake.messages[conf]["markup"] is None
            assert [b["callback_data"] for b in fake.buttons(conf)] == [b["callback_data"] for b in fake.buttons(msg)]
            await press(st, conf, "Отправить")
            assert fake.messages[conf].get("deleted")  # retired like the classic confirmation
            assert fake.buttons(msg) == []
            sent = st.client.responses if kind == "plain" else st.agent_client.sent
            assert len(sent) == 1
    run(main())


def test_retiring_a_confirmation_the_bot_cannot_delete_edits_it_as_a_rich_note(conn):
    async def main():
        async with stack(conn, thread("r1")) as st:
            topic = await st.open_consultant("r1")
            await st.ask("r1", "i1", items=OTHER_ITEM)
            await st.settle()
            msg = st.card_of(st.qid_of("i1"))
            await press(st, msg, "Ответить текстом")
            st.fake.push_message("typed", topic, reply_to=msg)
            await st.pump()
            conf = max(i for i, m in st.fake.messages.items() if m["thread_id"] == topic)
            st.fake.fail("deleteMessage", 400, "Bad Request: message can't be deleted")
            await press(st, conf, "Отмена")
            assert st.fake.messages[conf]["rich"] == "<p>↩️ отменено</p>" and st.fake.buttons(conf) == []
    run(main())


def test_agent_card_rich_flow_and_a_stale_key_on_a_rich_card_heals_it(conn):
    async def main():
        async with stack(conn, thread("r1")) as st:
            await st.open_consultant("r1")
            st.agent_client.items.setdefault("r1", []).append(agent_item("call_1", AGENT_Q))
            await st.agent.scan_thread("r1")
            await st.settle()
            [q] = st.agent.open_questions("r1")
            msg = st.agent_dao.get(q.agent_question_id).card_message_id
            html = st.fake.messages[msg]["rich"]
            assert re.search(r'<p>A\. Existing table <tg-button type="callback_data" style="primary" data="a:\w+">A</tg-button></p>', html)
            assert "<p>Where to store it?</p>" in html and st.fake.rows(msg) == [["✍️ Ответить текстом"]]
            stale = st.fake.button(msg, "B")["callback_data"]
            st.keys._actions.consume(stale, 0)  # used up
            st.fake.push_callback(stale, msg)
            await st.pump()
            await st.settle()
            assert toasts(st)[-1] == STALE and st.fake.button(msg, "B")["callback_data"]  # re-rendered with live keys
            await press(st, msg, "A")
            await press(st, msg, "Отправить")
            assert st.agent_client.sent and "Existing table" in st.agent_client.sent[0][1]
    run(main())


def test_a_long_card_is_one_rich_message_but_the_classic_fallback_still_splits_it(conn):
    long_option = "Use the long approach: " + "very detailed words, " * 400  # about 8400 characters
    items = (QuestionItem("a", "", "Which?", False, (QuestionOption(long_option), QuestionOption(long_option))),)

    async def main():
        async with stack(conn, thread("r1")) as st:
            qid, msg = await card(st, items=items)
            q = st.question_dao.get(qid)
            assert not q.card_extra_ids
            topic = st.consultant_dao.get("s1", "r1").tg_topic_id
            assert len([m for m in st.fake.messages.values() if m["thread_id"] == topic and "Which?" in m["text"]]) == 1
            assert st.fake.messages[msg]["text"].count("very detailed words,") == 800 and "↪" not in st.fake.messages[msg]["text"]
            assert [b["text"] for b in st.fake.buttons(msg)] == ["A", "B"]
    run(main())


def test_the_classic_path_keeps_its_continuation_messages(conn):
    long_option = "Use the long approach: " + "very detailed words, " * 400
    items = (QuestionItem("a", "", "Which?", False, (QuestionOption(long_option), QuestionOption(long_option))),)

    async def main():
        async with make_stack(conn, thread("r2"), rich=False) as classic:
            qid, msg = await card(classic, root="r2", item_id="i9", items=items)
            assert len(classic.question_dao.get(qid).card_extra_ids) >= 2 and classic.fake.methods("sendRichMessage") == []
            assert len(card_parts("x " * 3000, "Q1")) == 2
    run(main())


def test_a_card_over_the_rich_limit_goes_out_classic_without_asking_telegram(conn):
    huge = (QuestionItem("a", "", "Which?", False, (QuestionOption("w " * 17000), QuestionOption("B"))),)  # > 32768 characters

    async def main():
        async with stack(conn, thread("r1")) as st:
            qid, msg = await card(st, items=huge)
            assert not [p for p in st.fake.methods("sendRichMessage") if "w w w" in p["rich_message"]["html"]]  # refused locally; rich stays on
            assert st.present.rich and len(st.question_dao.get(qid).card_extra_ids) >= 8
            assert st.fake.buttons(st.question_dao.get(qid).card_extra_ids[-1])
            qid2, msg2 = await card(st, item_id="i2")
            assert "rich" in st.fake.messages[msg2]
    run(main())


# --- the registry ----------------------------------------------------------------------


def test_rich_registry_paragraphs_buttons_in_the_line_and_one_navigation_row(conn):
    async def main():
        async with stack(conn, thread("r1", name="Fox", updated=NOW - 10), thread("r2", name="Old <x>", updated=NOW - 900),
                         thread("r3", name="Done", updated=NOW - 500)) as st:
            await st.open_consultant("r1")
            await st.ask("r2", "i1")
            await st.live.handle(ThreadStatusChanged("r3", False, False, R.WORKING))
            await st.settle()
            reg = st.fake.messages[st.registry_message]
            assert "sendMessage" not in st.fake.methods() or all("Треды" not in p["text"] for p in st.fake.methods("sendMessage"))
            paras = re.findall(r"<p>(.*?)</p>", reg["rich"])
            assert paras[0].startswith("<b>📋 Активные треды Codex</b> · обновлено ")
            assert re.fullmatch(r"❓ Old &lt;x&gt; · 📁 w · \d\d:\d\d · консультант: — <tg-button [^>]+>➕ Создать</tg-button>", paras[1])
            assert re.fullmatch(r"⚙️ Done · 📁 w · \d\d:\d\d · консультант: — <tg-button [^>]+>➕ Создать</tg-button>", paras[2])
            assert re.fullmatch(r'Fox · 📁 w · \d\d:\d\d · консультант: ✅ <tg-button type="url" url="https://t.me/c/1234567890/\d+">💬 Открыть</tg-button>', paras[3])
            assert st.fake.rows(st.registry_message) == [["📦 Архивные"]] and reg["markup"] is None
            await press(st, st.registry_message, "📦 Архивные")
            assert "<b>📦 Архивные треды Codex</b>" in st.fake.messages[st.registry_message]["rich"]
            assert st.fake.rows(st.registry_message) == [["📋 Активные"]]
    run(main())


def test_rich_registry_pages_25_threads_and_pages_through(conn):
    async def main():
        threads = [thread(f"t{i:02d}", name=f"Thread {i:02d}", updated=NOW - 100 - i) for i in range(30)]
        async with stack(conn, *threads) as st:
            msg = st.registry_message
            assert RICH_PAGE_SIZE == 25 and PAGE_SIZE == 5
            assert st.fake.messages[msg]["rich"].count("<p>") == 26 and st.fake.rows(msg) == [["▶️", "📦 Архивные"]]
            assert st.fake.messages[msg]["rich"].count("➕ Создать") == 25
            await press(st, msg, "▶️")
            assert st.fake.messages[msg]["rich"].count("<p>") == 6 and st.fake.rows(msg) == [["◀️", "📦 Архивные"]]
            assert "Thread 29" in st.fake.messages[msg]["text"] and "Thread 00" not in st.fake.messages[msg]["text"]
            await press(st, msg, "◀️")
            assert "Thread 00" in st.fake.messages[msg]["text"] and len(st.control_messages()) == 1
    run(main())


def test_rich_registry_edits_only_when_changed_and_coalesces(conn):
    async def main():
        async with stack(conn, thread("r1", name="Fox"), thread("r2", name="Bee")) as st:
            msg = st.registry_message
            assert st.fake.messages[msg].get("edits", 0) == 0
            st.clock.now += 120  # only the time header differs
            assert await st.publisher.refresh() is False
            assert await st.publisher.refresh() is False and st.fake.methods().count("editMessageText") == 0
            for _ in range(5):
                st.publisher.request_refresh()
            await st.settle()
            assert st.fake.methods().count("editMessageText") == 0  # marks without a change write nothing
            await st.open_consultant("r1")
            st.publisher.request_refresh()
            await st.settle()
            await st.settle()
            assert st.fake.messages[msg]["edits"] == 1 and "💬 Открыть" in st.fake.messages[msg]["text"]
            assert len(st.control_messages()) == 1 and st.fake.pinned[0] == msg
    run(main())


def test_registry_message_deleted_by_hand_is_posted_again_rich(conn):
    async def main():
        async with stack(conn, thread("r1")) as st:
            old = st.registry_message
            st.fake.delete_message(old)
            await st.publisher.refresh(force=True)
            assert st.registry_message != old and "rich" in st.fake.messages[st.registry_message]
            assert len(st.control_messages()) == 1
    run(main())


# --- fallbacks -------------------------------------------------------------------------


def test_method_unsupported_logs_one_warning_and_everything_is_classic_from_then_on(conn, caplog):
    async def main():
        fake = FakeTelegram()
        fake.rich_supported = False
        await fake.start()
        from tg_stack import Stack
        st = Stack(conn, fake, rich=True)
        try:
            with caplog.at_level(logging.WARNING, logger="cqb"):
                await st.start(thread("r1", name="Fox"))
                reg = fake.messages[st.registry_message]
                assert "rich" not in reg and reg["text"].startswith("📋 Активные треды Codex") and reg["markup"]
                assert not st.present.rich
                topic = await st.open_consultant("r1")
                await st.ask("r1", "i1", items=ITEM)
                await st.settle()
                card_msg = st.card_of(st.qid_of("i1"))
                assert fake.messages[card_msg]["text"].startswith("❓ Q1") and fake.button(card_msg, "A")
                await press(st, card_msg, "B")
                assert "Выбрано: Separate <table>" in fake.messages[card_msg]["text"]
                await press(st, card_msg, "Отправить")
                assert st.client.responses
                st.publisher.request_refresh(force=True)
                await st.settle()
                assert len(fake.methods("sendRichMessage")) == 1  # asked once, never again
                warnings = [r for r in caplog.records if "not available" in r.getMessage()]
                assert len(warnings) == 1 and warnings[0].levelno == logging.WARNING
                assert "Fox" not in caplog.text and "Where" not in caplog.text and TOKEN not in caplog.text  # no message text
                assert [m for m in fake.live_messages(st.control_topic)].__len__() == 1
        finally:
            await st.status_cards.close()
            await st.api.close()
            await fake.stop()
    run(main())


def test_rich_unsupported_on_edit_only_falls_back_for_a_card_and_resizes_the_registry(conn):
    async def main():
        threads = [thread(f"t{i:02d}", name="N" * 60, updated=NOW - 100 - i) for i in range(25)]
        async with stack(conn, *threads) as st:
            assert "rich" in st.fake.messages[st.registry_message]
            st.fake.rich_supported = False  # e.g. the bot lost the right after the message was posted
            st.publisher.request_refresh(force=True)
            await st.settle()
            await st.settle()
            reg = st.fake.messages[st.registry_message]
            assert not st.present.rich and "rich" not in reg and len(reg["text"]) <= 4096
            assert reg["text"].count("➕") == 0 and len(st.fake.buttons(st.registry_message)) == 5 + 2  # 5 rows per page, then ▶️ and the archive
    run(main())


def test_a_refused_markup_falls_back_for_that_message_only(conn, caplog):
    async def main():
        async with stack(conn, thread("r1")) as st:
            with caplog.at_level(logging.WARNING, logger="cqb"):
                st.fake.fail("sendRichMessage", 400, "Bad Request: can't parse rich message: Unsupported start tag \"x\"")
                qid, msg = await card(st, items=ITEM)  # refused once: this card goes out classic
                assert "rich" not in st.fake.messages[msg] and st.fake.button(msg, "A") and st.present.rich
                qid2, msg2 = await card(st, item_id="i2")  # the next card is rich again
                assert "rich" in st.fake.messages[msg2]
                assert sum("refused the rich message" in r.getMessage() for r in caplog.records) == 1
                assert "Where" not in caplog.text and "Existing" not in caplog.text
                await press(st, msg, "B")  # the classic card works
                assert st.question_dao.get(qid).lifecycle is L.DECISION_STAGED
    run(main())


def test_a_refused_edit_falls_back_to_a_classic_edit_and_is_remembered_for_that_card(conn):
    async def main():
        async with stack(conn, thread("r1")) as st:
            qid, msg = await card(st, items=ITEM)
            st.fake.fail("editMessageText", 400, "Bad Request: can't parse rich message: broken")
            await press(st, msg, "B")
            assert st.present.rich and "Выбрано: Separate <table>" in st.fake.messages[msg]["text"]
            assert "rich" not in st.fake.messages[msg] and st.fake.button(msg, "Отправить")
            rich_edits = lambda: sum("rich_message" in p for p in st.fake.methods("editMessageText"))  # noqa: E731
            asked = rich_edits()
            await press(st, msg, "Отмена")  # remembered: classic again, rich is not asked for again
            await press(st, msg, "B")
            assert rich_edits() == asked and "rich" not in st.fake.messages[msg] and st.fake.button(msg, "Отправить")
            qid2, msg2 = await card(st, item_id="i2")  # a new card is rich
            assert "rich" in st.fake.messages[msg2]
    run(main())


def test_flag_off_never_calls_the_rich_methods(conn):
    async def main():
        async with make_stack(conn, thread("r1"), rich=False) as st:
            qid, msg = await card(st, items=ITEM)
            await press(st, msg, "B")
            await press(st, msg, "Отправить")
            await st.resolve("r1")
            await st.settle()
            assert st.fake.methods("sendRichMessage") == [] and "rich_message" not in "".join(map(str, st.fake.methods("editMessageText")))
            assert "rich" not in st.fake.messages[st.registry_message] and "rich" not in st.fake.messages[msg]
    run(main())


def test_rich_cards_survive_a_restart_of_the_flag_both_ways(conn):
    async def main():
        async with stack(conn, thread("r1")) as st:
            qid, msg = await card(st, items=ITEM)
            st.present._rich = False  # flag turned off: the card is edited classic (same message id)
            await press(st, msg, "B")
            assert st.question_dao.get(qid).card_message_id == msg and "rich" not in st.fake.messages[msg]
            assert st.fake.button(msg, "Отправить")
    run(main())


# --- config ----------------------------------------------------------------------------


def test_config_flag_defaults_on_and_must_be_a_bool():
    base = {"codex": {"socket": "/s"}, "db": {"path": "/x"}, "telegram": {"chat_id": -100123, "allowed_user_ids": [1]}}
    assert parse_config(base).telegram.rich_messages is True
    off = parse_config({**base, "telegram": {**base["telegram"], "rich_messages": False}})
    assert off.telegram.rich_messages is False
    with pytest.raises(ConfigError, match="rich_messages"):
        parse_config({**base, "telegram": {**base["telegram"], "rich_messages": "yes"}})


# --- switching on rich mode over messages that already exist as classic -------------------------


async def classic_then_rich(conn_stack):
    """A stack whose messages were posted classic, then rich mode is switched on (the first deploy)."""
    conn_stack.present._rich = True


def test_registry_that_cannot_be_edited_into_rich_is_replaced_by_a_fresh_pinned_one(conn):
    async def main():
        async with make_stack(conn, thread("r1", name="Fox"), rich=False) as st:
            old = st.registry_message
            assert "rich" not in st.fake.messages[old] and st.fake.pinned == [old]
            await classic_then_rich(st)
            st.fake.fail("editMessageText", 400, "Bad Request: message can't be edited")
            st.publisher.request_refresh(force=True)
            await st.settle()
            new = st.registry_message
            assert new != old and "rich" in st.fake.messages[new] and st.fake.pinned == [old, new]
            assert st.fake.messages[old].get("deleted") and len(st.control_messages()) == 1
            assert st.state.get("registry.hash").endswith("+rich") and st.state.get("registry.message_id") == str(new)
            await st.open_consultant("r1")
            st.publisher.request_refresh()
            await st.settle()
            assert st.fake.messages[new]["edits"] == 1 and st.registry_message == new  # later changes edit the rich one
            await press(st, new, "📦 Архивные")  # and its in-text buttons work
            assert "Архивные треды" in st.fake.messages[new]["text"]
    run(main())


def test_registry_stays_classic_when_the_fresh_rich_send_is_refused_too_and_is_not_retried(conn):
    async def main():
        threads = [thread(f"t{i:02d}", name="N" * 60, updated=NOW - 100 - i) for i in range(12)]
        async with make_stack(conn, *threads, rich=False) as st:
            old = st.registry_message
            await classic_then_rich(st)
            st.fake.fail("editMessageText", 400, "Bad Request: message can't be edited")
            st.fake.fail("sendRichMessage", 400, "Bad Request: can't parse rich message: no")
            st.publisher.request_refresh(force=True)
            await st.settle()
            m = st.fake.messages[old]
            assert st.registry_message == old and not m.get("deleted") and "rich" not in m and st.present.rich
            assert len(m["text"]) <= 4096 and len(st.fake.buttons(old)) == 5 + 2  # drawn classic: 5 rows, not 25
            assert not st.state.get("registry.hash").endswith("+rich") and len(st.control_messages()) == 1
            asked = len(st.fake.methods("sendRichMessage")), sum("rich_message" in p for p in st.fake.methods("editMessageText"))
            for _ in range(3):  # every refresh from now on stays classic without asking for rich
                st.clock.now += 10
                st.publisher.request_refresh(force=True)
                await st.settle()
            assert (len(st.fake.methods("sendRichMessage")), sum("rich_message" in p for p in st.fake.methods("editMessageText"))) == asked
            assert st.registry_message == old and len(st.control_messages()) == 1
            await press(st, old, "▶️")  # the classic keyboard still works
            assert st.publisher.view() == (1, False)
    run(main())


def test_registry_gone_while_switching_is_posted_fresh_rich_and_unsupported_edit_goes_classic(conn):
    async def main():
        async with make_stack(conn, thread("r1"), rich=False) as st:
            old = st.registry_message
            await classic_then_rich(st)
            st.fake.delete_message(old)
            st.publisher.request_refresh(force=True)
            await st.settle()
            assert st.registry_message != old and "rich" in st.fake.messages[st.registry_message]
    run(main())


def test_existing_classic_cards_keep_working_when_rich_is_switched_on(conn):
    async def main():
        async with make_stack(conn, thread("r1"), rich=False) as st:
            qid, msg = await card(st, items=ITEM)  # posted classic
            assert "rich" not in st.fake.messages[msg]
            await classic_then_rich(st)
            st.fake.fail("editMessageText", 400, "Bad Request: message can't be edited")
            await press(st, msg, "B")  # stage: the rich edit is refused, the classic edit follows
            assert st.question_dao.get(qid).lifecycle is L.DECISION_STAGED and "rich" not in st.fake.messages[msg]
            assert st.fake.button(msg, "Отправить") and st.question_dao.get(qid).card_message_id == msg
            asked = sum("rich_message" in p for p in st.fake.methods("editMessageText"))
            await press(st, msg, "Отмена")
            await press(st, msg, "A")
            await press(st, msg, "Отправить")
            assert sum("rich_message" in p for p in st.fake.methods("editMessageText")) == asked  # remembered
            assert st.client.responses and "ждём подтверждения" in st.fake.messages[msg]["text"]
            qid2, msg2 = await card(st, item_id="i2")  # new cards are rich
            assert "rich" in st.fake.messages[msg2]
    run(main())
