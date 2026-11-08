"""Consultant replies as rich Markdown: the sanitizer (untrusted model output), splitting, rendering through the fake
Telegram's Markdown validation, and the fallbacks (rich mode off, method unsupported, Markdown refused)."""

import asyncio
import logging
import re

import pytest

from cqb.services.relay import ConsultantRelay, DialogResult, Outcome
from cqb.telegram.markdown import CUT, LIMIT, MAX_PARTS, clean_markdown, markdown_parts
from fake_telegram import parse_markdown
from test_relay import Dialog, consultant
from fake_appserver import wait_until
from tg_stack import make_stack, thread


def run(coro):
    return asyncio.run(coro)


def hostile_features(text):
    """What the fake Telegram would build from `text`: nothing may be a tag, a button, an image or a tg:// link."""
    if not text.strip():
        return [], [], [], [], []
    f = parse_markdown(text).features
    return f["tags"], f["buttons"], f["images"], f["tg_links"], [u for u in f["links"] if not re.match(r"(?i)https?://", u)]


# --- what is kept ----------------------------------------------------------------------


def test_formatting_a_model_writes_is_kept():
    text = ("# Title\n\n**bold** and *italic* and `code`\n\n- one\n- two\n\n1. first\n\n> quote\n\n"
            "| a | b |\n|---|---|\n| 1 | 2 |\n\n```python\nprint('x')\n```\n\nsee [docs](https://example.org/a?b=1&c=2)")
    assert clean_markdown(text) == text
    assert markdown_parts(text) == [text]


def test_plain_http_and_https_links_are_kept_and_the_title_is_dropped():
    assert clean_markdown('[a](https://x.org "title") [b](HTTP://Y.org/p) [c](<https://z.org/q>)') == \
        "[a](https://x.org) [b](HTTP://Y.org/p) [c](https://z.org/q)"
    assert clean_markdown("a [b](https://x.org/(c)) d") == "a [b](https://x.org/(c)) d"
    assert clean_markdown("https://bare.example/x?a=1") == "https://bare.example/x?a=1"


# --- what is removed -------------------------------------------------------------------


@pytest.mark.parametrize("raw,expected", [
    ("![alt text](https://x.org/a.png)", "[🖼 alt text](https://x.org/a.png)"),
    ("![](https://x.org/a.png)", "[🖼](https://x.org/a.png)"),
    ("![a  b](HTTP://X.org/p?a=1&b=2 \"title\")", "[🖼 a b](HTTP://X.org/p?a=1&b=2)"),
    ("before ![a](http://x.org/i.jpg) after", "before [🖼 a](http://x.org/i.jpg) after"),
    ("![a](tg://photo?id=1)", "a"), ("![a](javascript:alert(1))", "a"), ("![a](data:image/png;base64,AAAA)", "a"),
    ("![a](file:///etc/passwd)", "a"), ("![a](//x.org/i.png)", "a"), ("![a](/rel.png)", "a"), ("![a]()", "a"),
    ("![ ](tg://emoji?id=5368324170671202286)", ""),
    ("![22:45 tomorrow](tg://time?unix=1647531900&format=wDT)", "22:45 tomorrow"),
    ("![ref image][r]\n\n[r]: https://x.org/a.png", "[🖼 ref image][r]\n\n[r]: https://x.org/a.png"),
    ("!![x](y)", "!x"), ("![x](https://x.org/" + "a" * 1980 + ")", "[🖼 x](https://x.org/" + "a" * 1980 + ")"),
    ("![x](https://x.org/" + "a" * 2100 + ")", "x"),  # a very long address is capped: the label stays
    # an image inside a link: one link, to the page when that is http(s), else to the image
    ("[![alt](https://i.org/x.png)](https://page.org/p)", "[🖼 alt](https://page.org/p)"),
    ("[![alt](https://i.org/x.png)](javascript:x)", "[🖼 alt](https://i.org/x.png)"),
    ("[![alt](tg://photo?id=1)](tg://user?id=1)", "alt"),
    # raw HTML media and links with an http(s) address become links, anything else is just its label
    ('<img src="https://x.org/a.png?a=1&amp;b=2" alt="Cat [1]">', "[🖼 Cat (1)](https://x.org/a.png?a=1&b=2)"),
    ("<img src='https://x.org/a.png'/>", "[🖼](https://x.org/a.png)"), ("<IMG SRC=https://x.org/a.png>", "[🖼](https://x.org/a.png)"),
    ('<img src="data:image/png;base64,AA" alt="x">', "x"), ('<img src="tg://photo?id=1">', ""), ("<img>", ""),
    ('<a href="https://x.org">click</a>', "[click](https://x.org)"), ('<a href="javascript:x">click</a>', "click"),
    ('<a href="tg://user?id=1">me</a>', "me"), ('<a href="https://x.org"></a>', "[https://x.org](https://x.org)"),
    ('<video src="https://v.org/a.mp4"></video>', "[🔗 video](https://v.org/a.mp4)"),
    ('<tg-document src="https://d.org/a.zip"></tg-document>', "[🔗 tg-document](https://d.org/a.zip)"),
    ('<audio src="javascript:x"></audio>', ""),
])
def test_images_and_media_become_links_when_http_else_their_label(raw, expected):
    clean = clean_markdown(raw)
    assert clean == expected
    assert hostile_features(clean) == ([], [], [], [], [])  # and none of it is an image, tag or non-http link


def test_no_image_syntax_survives_anywhere():
    out = clean_markdown("![a](https://x.org/1.png) ![b](tg://photo?id=1) [![c](https://x.org/2.png)](https://p.org) <img src=\"https://x.org/3.png\">")
    assert "![" not in out and "<img" not in out and out.count("[🖼") == 3


@pytest.mark.parametrize("raw", [
    "<b>bold</b>", "<tg-button type=\"callback_data\" data=\"x\">Go</tg-button>", "<TG-BUTTON data=x>Go</TG-BUTTON>",
    "<tg-button-row><tg-button type=\"url\" url=\"https://x.org\">a</tg-button></tg-button-row>",
    "<script>alert(1)</script>", "<img src=\"https://x.org/a.png\"/>", "<!-- c -->", "<?php x ?>", "<a href=\"tg://user?id=1\">x</a>",
    "<details open><summary>s</summary>c</details>", "<tg-emoji emoji-id=\"1\"> </tg-emoji>", "<video src=\"https://x\"></video>",
    "<https://auto.link/x>", "<tg://user?id=1>", "<​tg-button data=x>Go</tg-button>", "a < b and c <d",
])
def test_raw_html_never_survives(raw):
    clean = clean_markdown(raw)
    assert "<" not in clean
    assert hostile_features(clean) == ([], [], [], [], [])  # nothing for the fake to parse as a tag or a button


def test_html_inside_code_is_neutralized_too_as_a_lookalike():
    out = clean_markdown("`<tg-button data=x>` and\n```html\n<tg-button data=y>Go</tg-button>\n```")
    assert "<" not in out and "＜tg-button data=x>" in out and "＜tg-button data=y>Go＜/tg-button>" in out
    assert hostile_features(out) == ([], [], [], [], [])
    assert clean_markdown("if a<b") == "if a&lt;b" and clean_markdown("`a<b`") == "`a＜b`"


def test_entity_encoded_tags_are_just_text_and_the_fullwidth_lookalike_is_left_alone():
    assert hostile_features(clean_markdown("&#60;tg-button data=x&#62;Go&lt;/tg-button&gt;")) == ([], [], [], [], [])
    assert clean_markdown("＜tg-button＞") == "＜tg-button＞"  # never normalized into a real `<`


@pytest.mark.parametrize("raw,expected", [
    ("[me](tg://user?id=123456789)", "me"),
    ("[me](TG://user?id=1)", "me"),
    ("[x](javascript:alert(1))", "x"), ("[x](  JavaScript:alert(1))", "x"), ("[x](java\tscript:alert(1))", "x"),
    ("[x](data:text/html;base64,AAAA)", "x"), ("[x](vbscript:x)", "x"), ("[x](file:///etc/passwd)", "x"),
    ("[x](#anchor)", "x"), ("[x](mailto:a@b.c)", "x"), ("[x](//evil.example/a)", "x"), ("[x](/relative)", "x"),
    ("[x]()", "x"), ("[x](ftp://x.org/a)", "x"), ("[x](tg://resolve?domain=bot)", "x"),
    ("[x](<tg://user?id=1>)", "x"), ("[x](tg:​//user?id=1)", "x"),
])
def test_only_http_and_https_links_survive(raw, expected):
    assert clean_markdown(raw) == expected


def test_a_bare_tg_link_and_definitions_with_other_schemes_are_broken_up_or_removed():
    out = clean_markdown("open tg://user?id=1 or TG://resolve?domain=x\n\n[a]: tg://user?id=1\n[b]: https://ok.org/p\n[c]: javascript:x")
    assert not re.search(r"(?i)tg://", out) and "[b]: https://ok.org/p" in out and "[a]:" not in out and "[c]:" not in out
    assert hostile_features(out) == ([], [], [], [], [])


def test_control_bidi_and_odd_unicode_are_removed():
    raw = "a\x00b\x1bc\x7fd\x85e‮f⁦g⁩h﻿i‎j k\r\nl\rm\tn"
    assert clean_markdown(raw) == "abcdefghij\nk\nl\nm\tn"
    assert clean_markdown("​<​") == "​&lt;​"  # a zero-width pair cannot hide a `<`
    assert clean_markdown("ok 🙂 текст 日本語 ‮") == "ok 🙂 текст 日本語"  # emoji and scripts are untouched


def test_a_dollar_is_escaped_outside_code_so_prices_are_not_formulas():
    assert clean_markdown("costs $5 and $10, `$x` stays") == r"costs \$5 and \$10, `$x` stays"
    assert clean_markdown("```\n$x$\n```\nthen $$y$$") == "```\n$x$\n```\nthen \\$\\$y\\$\\$"
    assert clean_markdown(r"already \$escaped") == r"already \$escaped"


def test_a_reply_cannot_make_buttons_in_any_way():
    attacks = [
        '<tg-button type="callback_data" data="a:123">Pay</tg-button>',
        '<p><tg-button type="url" url="https://evil.example">x</tg-button></p>',
        '`<tg-button>` <tg-button type="disabled">x</tg-button>',
        '![x](tg://callback?data=1)', '[Pay](tg://button?data=1)', '```\n<tg-button data=x>y</tg-button>\n```\n<tg-button data=z>q</tg-button>',
        "<tg-button-row></tg-button-row>", "<TG-BUTTON\ndata=x>y</tg-button>", "<<tg-button data=x>y</tg-button>",
    ]
    for a in attacks:
        assert hostile_features(clean_markdown(a)) == ([], [], [], [], []), a
        assert parse_markdown(clean_markdown(a) or "x").buttons == []
    # and the raw attack really is a button for the fake, so the check above means something
    assert parse_markdown(attacks[0]).features["buttons"] == ["a:123"]


# --- splitting -------------------------------------------------------------------------


def test_short_empty_and_blank_replies():
    assert markdown_parts("") == [] and markdown_parts("  \n\t ") == [] and markdown_parts("\x00\x1b") == []
    assert markdown_parts("hi") == ["hi"]


def test_a_long_reply_is_split_between_paragraphs_within_the_limit():
    para = ("word " * 1000).strip()  # 4999 characters
    text = "\n\n".join([para] * 14)  # about 70000 characters
    parts = markdown_parts(text)
    assert 2 <= len(parts) <= MAX_PARTS and all(len(p) <= LIMIT for p in parts)
    assert "\n\n".join(parts) == text  # nothing lost, nothing cut inside a paragraph
    assert all(p.startswith("word") and p.endswith("word") for p in parts)


def test_a_code_fence_is_not_split_and_a_huge_paragraph_is_cut_at_a_line():
    code = "```\n" + "\n".join("line %d" % i for i in range(3000)) + "\n\nblank inside\n```"
    parts = markdown_parts("intro\n\n" + code)
    assert parts == ["intro\n\n" + code] and len(parts[0]) < LIMIT
    line = "x" * 100
    huge = "\n".join([line] * 700)  # 70699 characters, no blank line
    parts = markdown_parts(huge)
    assert len(parts) == 3 and all(len(p) <= LIMIT for p in parts) and all(set(p.split("\n")) == {line} for p in parts)
    assert len(markdown_parts("y" * (LIMIT + 5))) == 2  # no newline at all: cut anywhere


def test_more_than_a_few_parts_are_truncated_with_a_marker():
    parts = markdown_parts("\n\n".join(["z" * 30000] * 10))
    assert len(parts) == MAX_PARTS and all(len(p) <= LIMIT for p in parts) and parts[-1].endswith(CUT)
    exact = markdown_parts("a" * LIMIT)
    assert exact == ["a" * LIMIT]


def test_sanitizing_cannot_push_a_part_over_the_limit():
    text = "<" * 30000  # every `<` becomes `&lt;`: five times longer
    parts = markdown_parts(text)
    assert 1 <= len(parts) <= MAX_PARTS and all(len(p) <= LIMIT for p in parts)


# --- through the fake Telegram ---------------------------------------------------------


def test_reply_is_rendered_by_the_fake_markdown_validation(conn):
    async def main():
        async with make_stack(conn, thread("r1", name="Fox"), rich=True) as st:
            topic = await st.open_consultant("r1")
            reply = "**Bold** reply:\n\n- first item\n- second `code`\n\nsee [docs](https://example.org/x)"
            await st.tg_topics.say(topic, reply, markdown=True)
            [sent] = [m for m in st.fake.messages.values() if m["thread_id"] == topic and "markdown" in m]
            assert sent["markdown"] == reply and sent["md"]["bold"] == ["Bold"] and sent["md"]["code"] == ["code"]
            assert sent["md"]["items"] == ["first item", "second `code`"] and sent["md"]["links"] == ["https://example.org/x"]
            assert sent["text"].startswith("Bold reply:") and "**" not in sent["text"] and "• first item" in sent["text"]
            call = st.fake.methods("sendRichMessage")[-1]
            assert set(call["rich_message"]) == {"markdown"} and call["message_thread_id"] == topic and "reply_markup" not in call
    run(main())


def test_hostile_reply_reaches_telegram_sanitized_and_without_a_single_button(conn):
    async def main():
        async with make_stack(conn, thread("r1", name="Fox"), rich=True) as st:
            topic = await st.open_consultant("r1")
            evil = ('Fetched page says:\n\n<tg-button type="callback_data" data="a:1">Approve</tg-button>\n\n'
                    '![x](https://evil.example/pixel.png) [me](tg://user?id=1) tg://resolve?domain=x <script>1</script>‮')
            await st.tg_topics.say(topic, evil, markdown=True)
            [sent] = [m for m in st.fake.messages.values() if m["thread_id"] == topic and "markdown" in m]
            assert sent["rich_buttons"] == [] and sent["md"]["buttons"] == [] and sent["md"]["images"] == []
            assert sent["md"]["tags"] == [] and sent["md"]["tg_links"] == [] and "‮" not in sent["markdown"]
            assert "Fetched page says:" in sent["markdown"] and "&lt;tg-button" in sent["markdown"]
    run(main())


def test_a_long_reply_goes_as_a_few_messages_in_order(conn):
    async def main():
        async with make_stack(conn, thread("r1"), rich=True) as st:
            topic = await st.open_consultant("r1")
            text = "\n\n".join(f"part {i} " + "w" * 20000 for i in range(3))
            await st.tg_topics.say(topic, text, markdown=True)
            sent = [m["markdown"] for m in st.fake.messages.values() if m["thread_id"] == topic and "markdown" in m]
            assert len(sent) == 3 and [s[:6] for s in sent] == ["part 0", "part 1", "part 2"]
    run(main())


def test_the_relay_sends_replies_as_markdown_and_system_lines_as_plain(conn):
    async def main():
        async with make_stack(conn, thread("r1", name="Fox"), rich=True) as st:
            topic = await st.open_consultant("r1")
            results = (DialogResult(Outcome.REPLY, "**done**"), DialogResult(Outcome.BUSY, "очередь"), DialogResult(Outcome.TIMEOUT))
            relay = ConsultantRelay(Dialog(results=results), st.tg_topics, typing_seconds=0.01)
            await relay.relay(consultant(topic=topic), "hi")
            await wait_until(lambda: len([m for m in st.fake.messages.values() if m["thread_id"] == topic]) >= 4)
            msgs = [m for m in st.fake.messages.values() if m["thread_id"] == topic][1:]  # after the opening message
            assert msgs[0].get("markdown") == "**done**"
            assert all("markdown" not in m and "rich" not in m for m in msgs[1:])  # failure lines stay plain text
            assert msgs[1]["text"].startswith("Консультант занят")
    run(main())


# --- fallbacks -------------------------------------------------------------------------


def test_rich_mode_off_keeps_the_plain_text_path(conn):
    async def main():
        async with make_stack(conn, thread("r1"), rich=False) as st:
            topic = await st.open_consultant("r1")
            await st.tg_topics.say(topic, "**bold** <b>x</b>", markdown=True)
            assert st.fake.methods("sendRichMessage") == []
            assert st.fake.live_messages(topic)[-1]["text"] == "**bold** <b>x</b>"  # as before: the raw text
    run(main())


def test_unsupported_method_falls_back_to_plain_once_with_one_warning(conn, caplog):
    async def main():
        async with make_stack(conn, thread("r1"), rich=True) as st:
            topic = await st.open_consultant("r1")
            st.fake.rich_supported = False
            with caplog.at_level(logging.WARNING, logger="cqb"):
                await st.tg_topics.say(topic, "**one**", markdown=True)
                await st.tg_topics.say(topic, "**two**", markdown=True)
            texts = [m["text"] for m in st.fake.live_messages(topic)][-2:]
            assert texts == ["**one**", "**two**"] and not st.present.rich
            asked = [c for c in st.fake.methods("sendRichMessage") if "markdown" in c["rich_message"]]
            assert len(asked) == 1  # asked once, never again
            assert sum("not available" in r.getMessage() for r in caplog.records) == 1
            assert "one" not in caplog.text
    run(main())


def test_refused_markdown_is_resent_as_plain_with_the_markers_left_and_rich_stays_on(conn, caplog):
    async def main():
        async with make_stack(conn, thread("r1"), rich=True) as st:
            topic = await st.open_consultant("r1")
            st.fake.fail("sendRichMessage", 400, "Bad Request: can't parse rich message: broken markdown")
            with caplog.at_level(logging.WARNING, logger="cqb"):
                await st.tg_topics.say(topic, "**bold** and `code`", markdown=True)
            sent = st.fake.live_messages(topic)[-1]
            assert sent["text"] == "**bold** and `code`" and "markdown" not in sent and st.present.rich
            assert "bold" not in caplog.text
            await st.tg_topics.say(topic, "**again**", markdown=True)  # the next reply is rich again
            assert st.fake.live_messages(topic)[-1].get("markdown") == "**again**"
    run(main())


def test_a_refusal_in_the_middle_resends_the_rest_as_plain(conn):
    async def main():
        async with make_stack(conn, thread("r1"), rich=True) as st:
            topic = await st.open_consultant("r1")
            text = "\n\n".join(f"part {i} " + "w" * 20000 for i in range(3))
            real = st.api.send_rich_markdown
            calls = []

            async def flaky(chat, markdown, *, thread_id=None):
                calls.append(markdown[:6])
                if len(calls) == 2:
                    st.fake.fail("sendRichMessage", 400, "Bad Request: can't parse rich message: x")
                return await real(chat, markdown, thread_id=thread_id)

            st.api.send_rich_markdown = flaky
            await st.tg_topics.say(topic, text, markdown=True)
            msgs = [m for m in st.fake.messages.values() if m["thread_id"] == topic][1:]
            assert msgs[0].get("markdown", "").startswith("part 0")
            assert "markdown" not in msgs[1] and msgs[1]["text"].startswith("part 1")  # part 1 and 2 go plain
            assert any("part 2" in m["text"] for m in msgs[1:])
    run(main())


@pytest.mark.parametrize("evil", ["[a](" * 40000, "(" * 200000, "![" * 100000, "[" * 100000 + "](" * 100000, "`" * 150000,
                                  "<" * 150000, "\n" * 150000, "[x]: " * 40000, "```\n" * 50000, "$" * 150000])
def test_hostile_long_input_is_cleaned_quickly(evil):
    import time
    start = time.perf_counter()
    parts = markdown_parts(evil)
    assert time.perf_counter() - start < 2.0
    assert len(parts) <= MAX_PARTS and all(len(p) <= LIMIT for p in parts)
