"""Fake Telegram Bot API server: aiohttp on 127.0.0.1, implementing only the methods cqb calls."""

import asyncio
import html
import itertools
import re
from html.parser import HTMLParser
from typing import Any

from aiohttp import web

TOKEN = "123456:TEST-TOKEN"
CHAT_ID = -1001234567890
BOT_ID = 777
OWNER = 42


class FakeTelegram:
    def __init__(self, chat_id: int = CHAT_ID) -> None:
        self.chat_id = chat_id
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.topics: dict[int, dict[str, Any]] = {}
        self.messages: dict[int, dict[str, Any]] = {}
        self.callback_answers: list[dict[str, Any]] = []
        self.pinned: list[int] = []
        self.deleted_user_messages: list[int] = []  # ids of pushed (user) messages the bot deleted
        self.errors: dict[str, list[tuple[int, dict[str, Any]]]] = {}  # method -> queued failures
        self.drop_next: dict[str, int] = {}  # method -> connections to drop before answering
        self.rich_supported = True  # False: sendRichMessage answers 404 and editMessageText refuses `rich_message`
        self._updates: list[dict[str, Any]] = []
        self._update_ids = itertools.count(1)
        self._msg_ids = itertools.count(1000)
        self._topic_ids = itertools.count(100)
        self._wake = asyncio.Event()
        self._runner: web.AppRunner | None = None
        self.port = 0

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def start(self) -> None:
        app = web.Application()
        app.router.add_post("/bot{token}/{method}", self._handle)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        self.port = self._runner.addresses[0][1]

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()

    # --- test helpers ----------------------------------------------------------------

    def methods(self, name: str | None = None) -> list[str] | list[dict[str, Any]]:
        if name is None:
            return [m for m, _ in self.calls]
        return [p for m, p in self.calls if m == name]

    def fail(self, method: str, code: int, description: str, retry_after: int | None = None, times: int = 1) -> None:
        body: dict[str, Any] = {"ok": False, "error_code": code, "description": description}
        if retry_after is not None:
            body["parameters"] = {"retry_after": retry_after}
        self.errors.setdefault(method, []).extend([(code, body)] * times)

    def delete_topic(self, topic_id: int) -> None:
        self.topics.pop(topic_id, None)
        for m in self.messages.values():
            if m.get("thread_id") == topic_id:
                m["deleted"] = True

    def delete_message(self, message_id: int) -> None:
        self.messages[message_id]["deleted"] = True

    def topic_named(self, name: str) -> list[int]:
        return [i for i, t in self.topics.items() if t["name"] == name]

    def live_messages(self, thread_id: int) -> list[dict[str, Any]]:
        return [m for m in self.messages.values() if m["thread_id"] == thread_id and not m.get("deleted")]

    def push_message(
        self, text: str, thread_id: int | None, user_id: int = OWNER, chat_id: int | None = None,
        forwarded: bool = False, sender_chat: bool = False, reply_to: int | None = None, is_bot: bool = False,
    ) -> int:
        msg: dict[str, Any] = {
            "message_id": next(self._msg_ids), "chat": {"id": chat_id if chat_id is not None else self.chat_id},
            "from": {"id": user_id, "is_bot": is_bot}, "text": text,
        }
        if thread_id is not None:
            msg["message_thread_id"] = thread_id
            msg["is_topic_message"] = True
            msg["reply_to_message"] = {"message_id": reply_to or thread_id}
        if forwarded:
            msg["forward_origin"] = {"type": "user"}
        if sender_chat:
            msg["sender_chat"] = {"id": self.chat_id}
        self._push({"message": msg})
        return msg["message_id"]

    def push_callback(self, data: str, message_id: int, user_id: int = OWNER, chat_id: int | None = None) -> str:
        qid = f"cbq{next(self._update_ids)}"
        message: dict[str, Any] = {
            "message_id": message_id, "chat": {"id": chat_id if chat_id is not None else self.chat_id},
        }
        if {"rich", "markdown"} & self.messages.get(message_id, {}).keys():
            message["rich_message"] = {"blocks": []}  # a rich message carries `rich_message` and has no `text`
        self._push({"callback_query": {
            "id": qid, "from": {"id": user_id, "is_bot": False}, "data": data, "message": message,
        }})
        return qid

    def buttons(self, message_id: int) -> list[dict[str, Any]]:
        """Inline-keyboard buttons, then the in-text buttons of a rich message: {text, callback_data | url, ...}."""
        m = self.messages[message_id]
        markup = m.get("markup") or {}
        return [b for row in markup.get("inline_keyboard", []) for b in row] + list(m.get("rich_buttons", []))

    def rows(self, message_id: int) -> list[list[str]]:
        """The labels of the `<tg-button-row>`s of a rich message, row by row."""
        return [[b["text"] for b in row] for row in self.messages[message_id].get("rich_rows", [])]

    def press(self, message_id: int, text_part: str, user_id: int = OWNER) -> str:
        """The owner presses the (in-text or keyboard) button whose label contains `text_part`."""
        return self.push_callback(self.button(message_id, text_part)["callback_data"], message_id, user_id)

    def button(self, message_id: int, text_part: str) -> dict[str, Any]:
        found = [b for b in self.buttons(message_id) if text_part in b["text"]]
        assert found, f"no button containing {text_part!r} in {[b['text'] for b in self.buttons(message_id)]}"
        return found[0]

    def _push(self, body: dict[str, Any]) -> None:
        self._updates.append({"update_id": next(self._update_ids), **body})
        self._wake.set()

    # --- Bot API ---------------------------------------------------------------------

    async def _handle(self, request: web.Request) -> web.Response:
        method = request.match_info["method"]
        if request.match_info["token"] != TOKEN:
            return web.json_response({"ok": False, "error_code": 401, "description": "Unauthorized"}, status=401)
        params = await request.json()
        self.calls.append((method, params))
        if self.drop_next.get(method):
            self.drop_next[method] -= 1
            request.transport.close()
            return web.Response()
        queued = self.errors.get(method)
        if queued:
            code, body = queued.pop(0)
            return web.json_response(body, status=code)
        handler = getattr(self, f"m_{method}", None)
        if handler is None:
            return self._err(404, "Not Found")
        try:
            result = await handler(params)
        except _Fail as exc:
            return self._err(exc.code, exc.description)
        return web.json_response({"ok": True, "result": result})

    @staticmethod
    def _err(code: int, description: str) -> web.Response:
        return web.json_response({"ok": False, "error_code": code, "description": description}, status=code)

    async def m_getMe(self, p: dict[str, Any]) -> dict[str, Any]:
        return {"id": BOT_ID, "is_bot": True, "username": "cqb_test_bot", "can_read_all_group_messages": False}

    async def m_getChat(self, p: dict[str, Any]) -> dict[str, Any]:
        return {"id": p["chat_id"], "type": "supergroup", "is_forum": True}

    async def m_getChatMember(self, p: dict[str, Any]) -> dict[str, Any]:
        return {"status": "administrator", "can_manage_topics": True, "can_pin_messages": True}

    async def m_getUpdates(self, p: dict[str, Any]) -> list[dict[str, Any]]:
        offset = p.get("offset") or 0
        self._updates = [u for u in self._updates if u["update_id"] >= offset]
        if not self._updates:
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), min(p.get("timeout", 0), 0.2))
            except TimeoutError:
                pass
        return [u for u in self._updates if u["update_id"] >= offset][: p.get("limit", 100)]

    async def m_sendMessage(self, p: dict[str, Any]) -> dict[str, Any]:
        thread = p.get("message_thread_id")
        if thread is not None and thread not in self.topics:
            raise _Fail(400, "Bad Request: message thread not found")
        if p.get("parse_mode") == "HTML":
            check_html(p["text"])
        mid = next(self._msg_ids)
        self.messages[mid] = {
            "text": p["text"], "thread_id": thread, "markup": p.get("reply_markup"), "chat_id": p["chat_id"],
            "parse_mode": p.get("parse_mode"),
        }
        return {"message_id": mid}

    async def m_sendRichMessage(self, p: dict[str, Any]) -> dict[str, Any]:
        if not self.rich_supported:
            raise _Fail(404, "Not Found")
        thread = p.get("message_thread_id")
        if thread is not None and thread not in self.topics:
            raise _Fail(400, "Bad Request: message thread not found")
        rich = parse_rich(p.get("rich_message"))
        mid = next(self._msg_ids)
        self.messages[mid] = {
            "text": rich.plain, "rich_buttons": rich.buttons, "rich_rows": rich.rows,
            "thread_id": thread, "markup": p.get("reply_markup"), "chat_id": p["chat_id"], "parse_mode": None,
        }
        if rich.markdown is not None:
            self.messages[mid].update(markdown=rich.markdown, md=rich.features)
        else:
            self.messages[mid]["rich"] = rich.html
        return {"message_id": mid}

    async def m_editMessageText(self, p: dict[str, Any]) -> bool:
        if "rich_message" in p and not self.rich_supported:
            raise _Fail(400, "Bad Request: rich_message is not supported")
        m = self.messages.get(p["message_id"])
        if m is None or m.get("deleted"):
            raise _Fail(400, "Bad Request: message to edit not found")
        if "rich_message" in p:
            if "text" in p:
                raise _Fail(400, "Bad Request: text and rich_message are mutually exclusive")
            rich = parse_rich(p["rich_message"])
            if m.get("rich") == rich.html and m["markup"] == p.get("reply_markup"):
                raise _Fail(400, "Bad Request: message is not modified")
            m.update(text=rich.plain, rich=rich.html, rich_buttons=rich.buttons, rich_rows=rich.rows)
            m["markup"] = p.get("reply_markup")
        else:
            if not p.get("text"):
                raise _Fail(400, "Bad Request: text must be non-empty")
            if m["text"] == p["text"] and m["markup"] == p.get("reply_markup") and "rich" not in m:
                raise _Fail(400, "Bad Request: message is not modified")
            for key in ("rich", "rich_buttons", "rich_rows"):
                m.pop(key, None)
            m["text"], m["markup"] = p["text"], p.get("reply_markup")
        m["edits"] = m.get("edits", 0) + 1
        return True

    async def m_sendChatAction(self, p: dict[str, Any]) -> bool:
        return True

    async def m_pinChatMessage(self, p: dict[str, Any]) -> bool:
        self.pinned.append(p["message_id"])
        return True

    async def m_deleteMessage(self, p: dict[str, Any]) -> bool:
        if p["message_id"] in self.messages:
            self.delete_message(p["message_id"])
        else:
            self.deleted_user_messages.append(p["message_id"])
        return True

    async def m_createForumTopic(self, p: dict[str, Any]) -> dict[str, Any]:
        tid = next(self._topic_ids)
        self.topics[tid] = {"name": p["name"], "closed": False}
        return {"message_thread_id": tid, "name": p["name"], "icon_color": 0}

    async def m_closeForumTopic(self, p: dict[str, Any]) -> bool:
        return self._set_closed(p["message_thread_id"], True)

    async def m_reopenForumTopic(self, p: dict[str, Any]) -> bool:
        return self._set_closed(p["message_thread_id"], False)

    def _set_closed(self, topic_id: int, closed: bool) -> bool:
        topic = self.topics.get(topic_id)
        if topic is None:
            raise _Fail(400, "Bad Request: message thread not found")
        if topic["closed"] == closed:
            raise _Fail(400, "Bad Request: TOPIC_NOT_MODIFIED")
        topic["closed"] = closed
        return True

    async def m_answerCallbackQuery(self, p: dict[str, Any]) -> bool:
        self.callback_answers.append(p)
        return True


_HTML_TAG = re.compile(r"</?([A-Za-z][A-Za-z0-9-]*)[^<>]*>")
_HTML_TAGS = {"b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "code", "pre", "a", "tg-spoiler", "blockquote"}
_HTML_ENTITY = re.compile(r"&(#\d+|#x[0-9a-fA-F]+|lt|gt|amp|quot);")


def check_html(text: str) -> None:
    """What Telegram's HTML parse mode rejects with `can't parse entities`: unknown or unbalanced tags, a bare `<` or `&`."""
    stack: list[str] = []
    for m in _HTML_TAG.finditer(text):
        tag = m.group(1).lower()
        if tag not in _HTML_TAGS:
            raise _Fail(400, f"Bad Request: can't parse entities: Unsupported start tag \"{tag}\"")
        if m.group(0).startswith("</"):
            if not stack or stack.pop() != tag:
                raise _Fail(400, f"Bad Request: can't parse entities: Unmatched end tag \"{tag}\"")
        else:
            stack.append(tag)
    if stack:
        raise _Fail(400, "Bad Request: can't parse entities: Can't find end tag")
    bare = _HTML_TAG.sub("", text)
    if "<" in bare:
        raise _Fail(400, "Bad Request: can't parse entities: Unsupported start tag")
    if _HTML_ENTITY.sub("", bare).count("&"):
        raise _Fail(400, "Bad Request: can't parse entities: Unsupported entity")


RICH_TEXT_LIMIT = 32768
RICH_BLOCK_LIMIT = 500
RICH_ROW_LIMIT = 8
_RICH_NAMED = {"lt", "gt", "amp", "quot", "apos", "nbsp", "hellip", "mdash", "ndash", "lsquo", "rsquo", "ldquo", "rdquo"}
_RICH_TAGS = {
    "a", "b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "code", "mark", "sub", "sup", "tg-spoiler",
    "tg-reference", "tg-emoji", "img", "tg-time", "tg-math", "h1", "h2", "h3", "h4", "h5", "h6", "p", "pre", "footer",
    "hr", "ul", "ol", "li", "input", "blockquote", "cite", "aside", "video", "audio", "tg-document", "figure",
    "figcaption", "tg-map", "tg-collage", "tg-slideshow", "table", "tr", "th", "td", "caption", "details", "summary",
    "tg-math-block", "tg-button", "tg-button-row", "br",
}
_RICH_VOID = {"hr", "br", "img", "input", "tg-map"}
_RICH_BLOCKS = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "pre", "footer", "hr", "li", "blockquote", "aside", "tr",
                "details", "figure", "tg-button-row"}
_BUTTON_TYPES = {"callback_data", "url", "web_app", "login_url", "switch_inline_query", "switch_inline_query_current_chat",
                 "switch_inline_query_chosen_chat", "copy_text", "disabled"}
_BUTTON_STYLES = {"danger", "success", "primary", "link"}


class Rich:
    """A parsed `rich_message`: the HTML as sent, a plain rendering (buttons as `[label]`) and the buttons."""

    def __init__(self) -> None:
        self.html = ""
        self.plain = ""
        self.buttons: list[dict[str, Any]] = []
        self.rows: list[list[dict[str, Any]]] = []
        self.markdown: str | None = None  # set when the message was given as Markdown
        self.features: dict[str, Any] = {}


class _RichParser(HTMLParser):
    """Validates what Bot API 10.3 documents for `rich_message.html`; anything else is `can't parse rich message`."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.stack: list[str] = []
        self.rich = Rich()
        self.chars = 0
        self.blocks = 0
        self._label: list[str] = []
        self._button: dict[str, Any] | None = None
        self._row: list[dict[str, Any]] | None = None
        self._plain: list[str] = []

    def _bad(self, why: str) -> None:
        raise _Fail(400, f"Bad Request: can't parse rich message: {why}")

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in _RICH_TAGS:
            self._bad(f'unsupported start tag "{tag}"')
        a = {k: (v if v is not None else "") for k, v in attrs}
        if self._button is not None and tag not in ("tg-emoji", "tg-time"):
            self._bad(f'"{tag}" inside a button: a button label is plain text')
        if tag in _RICH_BLOCKS:
            self.blocks += 1
            self._plain.append("\n")
        if tag == "br":
            self._plain.append("\n")
        if tag == "tg-button-row":
            if self.stack:
                self._bad("tg-button-row must be a top-level block")
            if a.get("align", "left") not in ("left", "center", "right"):
                self._bad("bad align of tg-button-row")
            self._row = []
        elif tag == "tg-button":
            if not self.stack or self.stack[-1] not in ("p", "tg-button-row", "li"):
                self._bad("tg-button must be inside a paragraph, a list item or a tg-button-row")
            self._button = self._check_button(a)
        elif self._row is not None:
            self._bad(f'"{tag}" inside tg-button-row: only buttons')
        if tag not in _RICH_VOID:
            self.stack.append(tag)

    def _check_button(self, a: dict[str, str]) -> dict[str, Any]:
        kind = a.get("type")
        if kind not in _BUTTON_TYPES:
            self._bad("tg-button needs a valid type")
        style = a.get("style")
        if style is not None and (style not in _BUTTON_STYLES or (style == "link" and kind != "callback_data")):
            self._bad("bad button style")
        button: dict[str, Any] = {"style": style, "type": kind}
        if kind == "callback_data":
            data = a.get("data", "")
            if not 1 <= len(data.encode()) <= 64:
                self._bad("callback data must be 1-64 bytes")
            button["callback_data"] = data
        elif kind == "url":
            url = a.get("url", "")
            if not url.startswith(("http://", "https://", "tg://")):
                self._bad("button url must be http, https or tg")
            button["url"] = url
        elif kind == "copy_text" and not a.get("text"):
            self._bad("copy_text needs text")
        return button

    def handle_endtag(self, tag: str) -> None:
        if tag in _RICH_VOID:
            return
        if not self.stack or self.stack.pop() != tag:
            self._bad(f'unmatched end tag "{tag}"')
        if tag == "tg-button":
            assert self._button is not None
            label = "".join(self._label)
            self._label = []
            if not label.strip():
                self._bad("a button needs a label")
            button = {**self._button, "text": label}
            self._button = None
            self.rich.buttons.append(button)
            self._plain.append(f"[{label}]")
            if self._row is not None:
                self._row.append(button)
        elif tag == "tg-button-row":
            assert self._row is not None
            if not 1 <= len(self._row) <= RICH_ROW_LIMIT:
                self._bad(f"a tg-button-row holds 1-{RICH_ROW_LIMIT} buttons")
            self.rich.rows.append(self._row)
            self._row = None

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in _RICH_VOID:
            self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        if "<" in data or "&" in data:
            self._bad("bare < or &")
        self._text(data)

    def handle_entityref(self, name: str) -> None:
        if name not in _RICH_NAMED:
            self._bad(f"unsupported entity &{name};")
        self._text(html.unescape(f"&{name};"))

    def handle_charref(self, name: str) -> None:
        if not re.fullmatch(r"\d+|[xX][0-9a-fA-F]+", name):
            self._bad("bad numeric entity")
        self._text(html.unescape(f"&#{name};"))

    def _text(self, text: str) -> None:
        if self._row is not None and self._button is None and text.strip():
            self._bad("text inside tg-button-row")
        self.chars += len(text)
        (self._label if self._button is not None else self._plain).append(text)


def parse_rich(rich_message: Any) -> Rich:
    """Validate an InputRichMessage the way the Bot API documents it (html only: markdown/blocks are not used here)."""
    if not isinstance(rich_message, dict):
        raise _Fail(400, "Bad Request: rich_message must be an object")
    given = [k for k in ("html", "markdown", "blocks") if k in rich_message]
    if len(given) != 1:
        raise _Fail(400, "Bad Request: exactly one of html, markdown or blocks must be used in rich_message")
    if given[0] == "markdown":
        return parse_markdown(rich_message["markdown"])
    if given[0] != "html":
        raise _Fail(400, f"Bad Request: the fake supports only rich_message.html and markdown, not {given[0]}")
    source = rich_message["html"]
    if not isinstance(source, str) or not source.strip():
        raise _Fail(400, "Bad Request: rich message is empty")
    parser = _RichParser()
    parser.feed(source)
    parser.close()
    if parser.stack:
        raise _Fail(400, f"Bad Request: can't parse rich message: can't find end tag for \"{parser.stack[-1]}\"")
    if parser.chars > RICH_TEXT_LIMIT:
        raise _Fail(400, "Bad Request: rich message is too long")
    if parser.blocks > RICH_BLOCK_LIMIT:
        raise _Fail(400, "Bad Request: too many blocks in the rich message")
    parser.rich.html = source
    parser.rich.plain = re.sub(r"\n\s*\n+", "\n", "".join(parser._plain)).strip()
    return parser.rich


_MD_TAG = re.compile(r"</?([A-Za-z][A-Za-z0-9-]*)[^<>]*>")


def parse_markdown(source: Any) -> Rich:
    """`rich_message.markdown`: only what a test needs to see what Telegram would build from it. `features` lists the
    constructs found (bold, italic, code, items, links, images, tags: raw HTML, buttons: in-text buttons made by
    `<tg-button>` HTML, tg_links: tg:// links); raw HTML is parsed like the html field, so a forged button is a button."""
    if not isinstance(source, str) or not source.strip():
        raise _Fail(400, "Bad Request: rich message is empty")
    if len(source) > RICH_TEXT_LIMIT:
        raise _Fail(400, "Bad Request: rich message is too long")
    rich = Rich()
    rich.html = ""
    f: dict[str, Any] = {
        "bold": re.findall(r"\*\*(.+?)\*\*", source), "code": re.findall(r"`([^`\n]+)`", source),
        "items": re.findall(r"^\s*(?:[-*+]|\d+\.)\s+(.+)$", source, re.M), "headings": re.findall(r"^#{1,6}\s+(.+)$", source, re.M),
        "images": re.findall(r"!\[[^\]]*\]\([^)]*\)", source), "links": re.findall(r"(?<!!)\[[^\]]*\]\(([^)]*)\)", source),
        "tags": [m.group(1).lower() for m in _MD_TAG.finditer(source)], "tg_links": re.findall(r"(?i)tg://\S*", source),
        "entities": re.findall(r"&lt;|&amp;", source),
    }
    for tag in f["tags"]:
        if tag not in _RICH_TAGS:
            raise _Fail(400, f"Bad Request: can't parse rich message: unsupported start tag \"{tag}\"")
    parser = _RichParser()
    for m in re.finditer(r"<tg-button\b.*?</tg-button>", source, re.S):
        parser.feed(f"<p>{m.group(0)}</p>")
    rich.buttons = parser.rich.buttons
    f["buttons"] = [b.get("callback_data") or b.get("url") for b in rich.buttons]
    plain = re.sub(r"\*\*(.+?)\*\*", r"\1", source)
    plain = re.sub(r"`([^`\n]+)`", r"\1", plain)
    rich.plain = re.sub(r"^\s*(?:[-*+])\s+", "• ", plain, flags=re.M).strip()
    rich.markdown = source
    rich.features = f
    return rich


class _Fail(Exception):
    def __init__(self, code: int, description: str) -> None:
        self.code, self.description = code, description
