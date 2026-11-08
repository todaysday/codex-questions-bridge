"""The two renderers of a Doc: rich HTML with in-text buttons (Bot API 10.3) and the classic text + inline keyboard.

Rich HTML uses only the tags the Bot API lists for `rich_message.html` (`<p>`, `<b>`, `<br>`, `<tg-button>`,
`<tg-button-row>`) and escapes every dynamic string; button labels are plain text."""

import dataclasses
import html

from cqb.telegram.fmt import BUTTON_TEXT_LIMIT, clip
from cqb.telegram.models import Bold, Button, Divider, Doc, Para, Row, View

RICH_TEXT_LIMIT = 32768  # characters of text in one rich message
RICH_BLOCK_LIMIT = 500
ROW_BUTTONS = 8  # buttons in one <tg-button-row>


def rich_size(doc: Doc) -> tuple[int, int]:
    """(characters of text, blocks) as Telegram counts them against the rich message limits."""
    chars = blocks = 0
    for b in doc.blocks:
        blocks += 1
        if isinstance(b, Divider):
            continue
        for p in b.buttons if isinstance(b, Row) else b.parts:
            chars += len(p.text) if isinstance(p, (Bold, Button)) else len(p)
    return chars, blocks


def fits_rich(doc: Doc) -> bool:
    chars, blocks = rich_size(doc)
    return chars <= RICH_TEXT_LIMIT and blocks <= RICH_BLOCK_LIMIT


# --- rich ----------------------------------------------------------------------------


def to_html(doc: Doc) -> str:
    out: list[str] = []
    for b in doc.blocks:
        if isinstance(b, Divider):
            out.append("<hr/>")
        elif isinstance(b, Row):
            for i in range(0, len(b.buttons), ROW_BUTTONS):
                row = "".join(_button(x) for x in b.buttons[i:i + ROW_BUTTONS])
                out.append(f'<tg-button-row align="left">{row}</tg-button-row>')
        else:
            inline = _inline(b)
            if inline:
                out.append(f"<p>{inline}</p>")
    return "".join(out)


def _inline(para: Para) -> str:
    out: list[str] = []
    for p in para.parts:
        if isinstance(p, Button):
            out.append((" " if out and not out[-1].endswith(" ") else "") + _button(p))
        elif isinstance(p, Bold):
            out.append(f"<b>{_esc(p.text)}</b>")
        elif p:
            out.append(_esc(p))
    return "".join(out).strip()


def _button(b: Button) -> str:
    style = f' style="{b.style}"' if b.style else ""
    label = _esc(clip(b.text, BUTTON_TEXT_LIMIT))
    if b.disabled:
        return f'<tg-button type="disabled"{style}>{label}</tg-button>'
    if b.url:
        return f'<tg-button type="url"{style} url="{_esc(b.url)}">{label}</tg-button>'
    if b.callback_data is None:
        raise ValueError("button without a callback id: bind the document first")
    return f'<tg-button type="callback_data"{style} data="{_esc(b.callback_data)}">{label}</tg-button>'


def _esc(text: str) -> str:
    return html.escape(text, quote=True).replace("\n", "<br>")


# --- classic -------------------------------------------------------------------------


def to_view(doc: Doc) -> View:
    """Plain text (the volatile header apart) and the inline keyboard: in-line buttons grouped `per_row` to a row,
    flushed by every Row, which is a keyboard row of its own."""
    blocks = list(doc.blocks)
    header = _plain(blocks.pop(0)) if blocks and isinstance(blocks[0], Para) and blocks[0].volatile else ""
    body = ""
    keyboard: list[tuple[Button, ...]] = []
    pending: list[Button] = []

    def flush() -> None:
        keyboard.extend(tuple(pending[i:i + doc.per_row]) for i in range(0, len(pending), doc.per_row))
        pending.clear()

    for b in blocks:
        if isinstance(b, Divider):
            continue
        if isinstance(b, Row):
            flush()
            if row := tuple(_classic(x) for x in b.buttons if not x.disabled):
                keyboard.append(row)
            continue
        pending.extend(_classic(p) for p in b.parts if isinstance(p, Button) and not p.disabled)
        if text := (b.classic if b.classic is not None else _plain(b)):
            body = body + b.glue + text if body else text
    flush()
    if not header:
        return View(body, "", tuple(keyboard))
    return View(header, body, tuple(keyboard))


def _plain(para: Para) -> str:
    return "".join(p.text if isinstance(p, Bold) else p for p in para.parts if not isinstance(p, Button))


def _classic(b: Button) -> Button:
    return dataclasses.replace(b, text=b.classic_text) if b.classic_text else b

