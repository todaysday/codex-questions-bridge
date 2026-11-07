import asyncio
import logging

import pytest

from cqb.telegram.api import BadRequest, BotApi, NetworkError, RateLimited, Unauthorized
from cqb.telegram.models import ActionSpec, Button
from fake_telegram import CHAT_ID, TOKEN, FakeTelegram


class Sleeps:
    def __init__(self):
        self.calls = []

    async def __call__(self, delay):
        self.calls.append(delay)


def with_api(coro_fn, token=TOKEN):
    async def main():
        fake = FakeTelegram()
        await fake.start()
        sleeps = Sleeps()
        api = BotApi(token, base_url=fake.base_url, sleep=sleeps, rng=lambda: 0.25)
        await api.start()
        try:
            return await coro_fn(api, fake, sleeps)
        finally:
            await api.close()
            await fake.stop()
    return asyncio.run(main())


def test_dtos_decoded_from_responses():
    async def body(api, fake, sleeps):
        me = await api.get_me()
        assert (me.username, me.can_read_all_group_messages) == ("cqb_test_bot", False)
        chat = await api.get_chat(CHAT_ID)
        assert chat.is_forum and chat.type == "supergroup"
        member = await api.get_chat_member(CHAT_ID, me.user_id)
        assert member.can_manage_topics and member.status == "administrator"
        topic = await api.create_forum_topic(CHAT_ID, "T")
        sent = await api.send_message(
            CHAT_ID, "hi", thread_id=topic.thread_id,
            keyboard=((Button("a", ActionSpec("x"), callback_data="a:1"), Button("b", url="https://t.me/x")),),
        )
        assert fake.messages[sent.message_id]["markup"] == {"inline_keyboard": [[
            {"text": "a", "callback_data": "a:1"}, {"text": "b", "url": "https://t.me/x"}]]}
    with_api(body)


def test_429_retry_after_is_honored_with_jitter():
    async def body(api, fake, sleeps):
        fake.fail("sendMessage", 429, "Too Many Requests: retry after 7", retry_after=7, times=2)
        sent = await api.send_message(CHAT_ID, "x")
        assert sent.message_id
        assert sleeps.calls == [7.25, 7.25]
        assert fake.methods().count("sendMessage") == 3
    with_api(body)


def test_429_backoff_is_bounded():
    async def body(api, fake, sleeps):
        fake.fail("sendMessage", 429, "slow down", retry_after=100000, times=10)
        with pytest.raises(RateLimited):
            await api.send_message(CHAT_ID, "x")
        assert max(sleeps.calls) <= 60.5 and len(sleeps.calls) == 4
    with_api(body)


def test_transient_error_retried_for_idempotent_calls():
    async def body(api, fake, sleeps):
        fake.drop_next["getMe"] = 2
        assert (await api.get_me()).username == "cqb_test_bot"
        assert len(sleeps.calls) == 2 and sleeps.calls[1] > sleeps.calls[0]
        fake.fail("getChat", 502, "Bad Gateway")
        assert (await api.get_chat(CHAT_ID)).is_forum
    with_api(body)


def test_non_idempotent_call_is_not_repeated_after_lost_response():
    async def body(api, fake, sleeps):
        fake.drop_next["createForumTopic"] = 1
        with pytest.raises(NetworkError):
            await api.create_forum_topic(CHAT_ID, "T")
        assert fake.methods().count("createForumTopic") == 1
    with_api(body)


def test_typed_errors_and_gone_detection():
    async def body(api, fake, sleeps):
        with pytest.raises(BadRequest) as e:
            await api.edit_message_text(CHAT_ID, 5, "x")
        assert e.value.gone and not e.value.not_modified
        with pytest.raises(BadRequest) as e:
            await api.send_message(CHAT_ID, "x", thread_id=999)
        assert e.value.gone
    with_api(body)


def test_bad_token_is_unauthorized_and_never_logged(caplog):
    caplog.set_level(logging.DEBUG)
    async def body(api, fake, sleeps):
        with pytest.raises(Unauthorized) as e:
            await api.get_me()
        assert "BAD-TOKEN" not in str(e.value)
    with_api(body, token="999:BAD-TOKEN")
    assert "BAD-TOKEN" not in caplog.text


def test_network_error_text_has_no_url_or_token():
    async def main():
        api = BotApi(TOKEN, base_url="http://127.0.0.1:9", sleep=Sleeps(), rng=lambda: 0)
        await api.start()
        try:
            with pytest.raises(NetworkError) as e:
                await api.get_me()
            assert TOKEN not in str(e.value) and "127.0.0.1" not in str(e.value)
            assert e.value.__cause__ is None and e.value.__suppress_context__
        finally:
            await api.close()
    asyncio.run(main())


def test_get_updates_decodes_message_and_callback():
    async def body(api, fake, sleeps):
        fake.push_message("hello", thread_id=100, reply_to=5)
        fake.push_message("fwd", thread_id=100, forwarded=True)
        fake.push_callback("a:1", message_id=7)
        ups = await api.get_updates(0, timeout=0)
        m, f, c = ups[0].message, ups[1].message, ups[2].callback
        assert (m.text, m.thread_id, m.reply_to_message_id, m.is_forwarded) == ("hello", 100, 5, False)
        assert f.is_forwarded
        assert (c.data, c.message_id, c.chat_id) == ("a:1", 7, CHAT_ID)
        assert fake.methods("getUpdates")[0]["allowed_updates"] == ["message", "callback_query"]
        assert await api.get_updates(ups[-1].update_id + 1, timeout=0) == []
    with_api(body)


def test_parse_mode_is_sent_only_when_asked():
    async def body(api, fake, sleeps):
        plain = await api.send_message(CHAT_ID, "a <b>b</b>")
        html = await api.send_message(CHAT_ID, "a <b>b</b>", parse_mode="HTML")
        assert fake.messages[plain.message_id]["parse_mode"] is None
        assert fake.messages[html.message_id]["parse_mode"] == "HTML" and fake.messages[html.message_id]["text"] == "a <b>b</b>"
        assert [p.get("parse_mode") for p in fake.methods("sendMessage")] == [None, "HTML"]
    with_api(body)


@pytest.mark.parametrize("text", ["📊 <b>x", "a < b", "x & y", "<unknown>t</unknown>", "<b>x</i>"])
def test_html_the_fake_rejects_matches_what_telegram_rejects_and_valid_html_passes(text):
    async def body(api, fake, sleeps):
        for good in ("<b>x</b> &lt;a&gt; &amp; &quot;q&quot; &#x27;", "no markup at all"):
            assert (await api.send_message(CHAT_ID, good, parse_mode="HTML")).message_id
        sent = await api.send_message(CHAT_ID, text, parse_mode="HTML")  # rejected, then resent as plain text
        assert fake.messages[sent.message_id]["parse_mode"] is None
    with_api(body)


def test_unparsable_html_is_resent_as_the_same_text_without_tags_so_nothing_is_lost(caplog):
    async def body(api, fake, sleeps):
        text = "📊 <b>Fox &amp; Co</b>\n🟢 <b>работает</b> &lt;3 &quot;q&quot; &#x27;s&#x27;\n<b>unclosed"
        with caplog.at_level(logging.WARNING):
            sent = await api.send_message(CHAT_ID, text, thread_id=None, parse_mode="HTML")
        stored = fake.messages[sent.message_id]
        assert stored["text"] == "📊 Fox & Co\n🟢 работает <3 \"q\" 's'\nunclosed" and stored["parse_mode"] is None
        calls = fake.methods("sendMessage")
        assert [p.get("parse_mode") for p in calls] == ["HTML", None]
        assert "Fox" not in caplog.text  # the warning never carries message text
    with_api(body)


def test_only_a_parse_error_triggers_the_plain_fallback():
    async def body(api, fake, sleeps):
        with pytest.raises(BadRequest):
            await api.send_message(CHAT_ID, "<b>x</b>", thread_id=999, parse_mode="HTML")  # another 400: raised, not retried
        assert fake.methods().count("sendMessage") == 1
        fake.fail("sendMessage", 400, "Bad Request: can't parse entities: Can't find end tag", times=1)
        sent = await api.send_message(CHAT_ID, "<b>ok</b>", parse_mode="HTML")  # a simulated server-side parse error
        assert fake.messages[sent.message_id]["text"] == "ok"
        fake.fail("sendMessage", 400, "Bad Request: can't parse entities", times=1)
        with pytest.raises(BadRequest):
            await api.send_message(CHAT_ID, "plain", parse_mode=None)  # no HTML asked: nothing to fall back from
    with_api(body)
