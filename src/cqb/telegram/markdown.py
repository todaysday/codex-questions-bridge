"""Consultant replies as rich Markdown (`rich_message={"markdown": ...}`), sanitized first.

A reply is untrusted: the model may repeat fetched web content. Rich Markdown accepts raw HTML (including
`<tg-button>`, which would put buttons into a message), images and other media, `tg://` links (mentions, uploaded
files, emoji) and math. `clean_markdown` keeps the formatting a model writes (bold, italic, lists, code, headings,
tables, plain http(s) links, quotes) and removes the rest:

- control and bidirectional-override characters are dropped;
- every `<` becomes `&lt;` (a literal `＜` inside code, where entities are not decoded): there is no raw HTML at all;
- an image (`![alt](url)`, `<img src=...>`) becomes a plain link `[🖼 alt](url)` when its address is http(s), else its alt
  text; `<a href=...>` and other media tags with an http(s) src/href become `[label](url)` too (the owner can open
  what the model wanted to show, nothing is embedded);
- a link keeps only an http(s) address of a sane length (an allow-list: `tg://`, `javascript:`, `data:`, `#anchor`, ... drop the link and
  keep its text), reference definitions with another scheme are removed, a bare `tg://` is broken up;
- a `$` outside code is escaped so prices are not read as formulas.
"""

import html as _html
import re
import unicodedata

LIMIT = 32768  # characters in one rich message
MAX_PARTS = 4
CUT = "\n\n…[ответ обрезан]"

_DROPPED = {0x2028: "\n", 0x2029: "\n"}
_FORMAT_DROP = {*range(0x202A, 0x202F), *range(0x2066, 0x206A), 0x200E, 0x200F, 0x061C, 0xFEFF}
# Every repetition is bounded, so hostile input (`![![![...`) cannot make the patterns quadratic.
MAX_URL = 2000  # a longer address is dropped, the label stays
_DEST = r"\(((?:[^()\n]|\([^()\n]{0,300}\)){0,2400})\)"  # a link destination, one level of nested parentheses
_INLINE_IMAGE = re.compile(r"!\[([^\]\n]{0,300})\]" + _DEST)
_LINKED_IMAGE = re.compile(r"\[!\[([^\]\n]{0,300})\]" + _DEST + r"\]" + _DEST)
_REF_IMAGE = re.compile(r"!\[([^\]\n]{0,300})\]\[([^\]\n]{0,100})\]")
_ATTR = r"""\b{}\s*=\s*(?:"([^"]{{0,2500}})"|'([^']{{0,2500}})'|([^\s"'<>=`]{{1,2500}}))"""
_SRC, _HREF, _ALT = (re.compile(_ATTR.format(n), re.I) for n in ("src", "href", "alt"))
_HTML_IMG = re.compile(r"<img\b([^<>]{0,5000})>", re.I)
_HTML_MEDIA = re.compile(r"<(video|audio|source|iframe|embed|tg-document)\b([^<>]{0,5000})>", re.I)
_HTML_MEDIA_END = re.compile(r"</(?:video|audio|iframe|embed|tg-document)\s*>", re.I)
_HTML_LINK = re.compile(r"<a\b([^<>]{0,5000})>([^<]{0,300})</a\s*>", re.I)
_BANG = re.compile(r"!(?=\[)")
_LINK = re.compile(r"\[([^\]\n]{0,300})\]" + _DEST)
_DEFINITION = re.compile(r"^ {0,3}\[[^\]\n]+\]:[ \t]*(\S*).*$", re.MULTILINE)
_TG = re.compile(r"(?i)tg://")
_HTTP = re.compile(r"(?i)https?://[^\s<>\"`]+")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_INLINE_CODE = re.compile(r"(`+[^`\n]*?`+)")
_DOLLAR = re.compile(r"(?<!\\)\$")


def clean_markdown(text: str) -> str:
    text = _controls(text[: LIMIT * MAX_PARTS])  # more than that is cut later anyway; this bounds the work
    text = _HTML_IMG.sub(_html_image, text)
    text = _HTML_MEDIA_END.sub("", _HTML_MEDIA.sub(_html_media, text))
    text = _HTML_LINK.sub(_html_link, text)
    text = _LINKED_IMAGE.sub(_linked_image, text)
    text = _INLINE_IMAGE.sub(lambda m: _image(m.group(1), m.group(2)), text)
    text = _REF_IMAGE.sub(lambda m: f"[{_label('🖼', m.group(1))}][{m.group(2)}]", text)
    text = _BANG.sub("", text)
    text = _LINK.sub(_link, text)
    text = _DEFINITION.sub(lambda m: m.group(0) if _http(m.group(1)) else "", text)
    text = _TG.sub("tg:​//", text)
    return "".join(_literal(seg) if code else _prose(seg) for seg, code in _segments(text)).strip()


def markdown_parts(text: str) -> list[str]:
    """The cleaned reply as one or a few messages (at most MAX_PARTS), split between blocks, not inside a fence."""
    clean = clean_markdown(text)
    if not clean:
        return []
    parts: list[str] = []
    current = ""
    for block in _blocks(clean):
        for piece in _fit(block):
            if current and len(current) + 2 + len(piece) > LIMIT:
                parts.append(current)
                current = piece
            else:
                current = f"{current}\n\n{piece}" if current else piece
    parts.append(current)
    if len(parts) > MAX_PARTS:
        parts = parts[:MAX_PARTS]
        parts[-1] = parts[-1][: LIMIT - len(CUT)] + CUT
    return parts


# --- steps ---------------------------------------------------------------------------


def _controls(text: str) -> str:
    out = []
    for ch in text.replace("\r\n", "\n").replace("\r", "\n"):
        code = ord(ch)
        if code in _DROPPED:
            out.append(_DROPPED[code])
        elif code in _FORMAT_DROP or (unicodedata.category(ch) == "Cc" and ch not in "\n\t"):
            continue
        else:
            out.append(ch)
    return "".join(out)


def _http(dest: str) -> str | None:
    token = dest.strip().split(None, 1)[0] if dest.strip() else ""
    token = token.lstrip("<").rstrip(">")
    return token if len(token) <= MAX_URL and _HTTP.fullmatch(token) else None


def _label(icon: str, alt: str) -> str:
    alt = " ".join(alt.replace("[", "(").replace("]", ")").split())
    return f"{icon} {alt}" if alt else icon


def _image(alt: str, dest: str) -> str:
    """`[🖼 alt](url)` for an http(s) address, else just the alt text."""
    url = _http(dest)
    return f"[{_label('🖼', alt)}]({url})" if url else alt


def _linked_image(m: re.Match[str]) -> str:
    """`[![alt](img)](page)`: one link to the page when it is http(s), else the image's own link."""
    return _image(m.group(1), m.group(3) if _http(m.group(3)) else m.group(2))


def _attr(pattern: re.Pattern[str], attrs: str) -> str:
    m = pattern.search(attrs)
    return _html.unescape(next((g for g in m.groups() if g is not None), "")) if m else ""


def _html_image(m: re.Match[str]) -> str:
    return _image(_attr(_ALT, m.group(1)), _attr(_SRC, m.group(1)))


def _html_media(m: re.Match[str]) -> str:
    url = _http(_attr(_SRC, m.group(2)))
    return f"[{_label('🔗', m.group(1).lower())}]({url})" if url else ""


def _html_link(m: re.Match[str]) -> str:
    url = _http(_attr(_HREF, m.group(1)))
    label = " ".join(m.group(2).replace("[", "(").replace("]", ")").split())
    return f"[{label or url}]({url})" if url else label


def _link(m: re.Match[str]) -> str:
    url = _http(m.group(2))
    return f"[{m.group(1)}]({url})" if url else m.group(1)


def _segments(text: str) -> list[tuple[str, bool]]:
    """(text, is_code): fenced blocks and inline code spans are code, the rest is prose."""
    out: list[tuple[str, bool]] = []
    fence: str | None = None
    for line in text.split("\n"):
        m = _FENCE.match(line)
        if fence is not None or m:
            out.append((line + "\n", True))
            if fence is None and m:
                fence = m.group(1)[0]
            elif m and m.group(1)[0] == fence and line.strip().strip(fence) == "":
                fence = None
            continue
        for n, part in enumerate(_INLINE_CODE.split(line)):
            out.append((part, n % 2 == 1))
        out.append(("\n", False))
    if out:
        last, code = out[-1]
        out[-1] = (last.removesuffix("\n"), code)
    return out


def _prose(text: str) -> str:
    return _DOLLAR.sub(r"\\$", text.replace("<", "&lt;"))


def _literal(text: str) -> str:
    return text.replace("<", "＜")


def _blocks(text: str) -> list[str]:
    """Blocks separated by blank lines; a blank line inside a code fence does not separate."""
    blocks: list[str] = []
    current: list[str] = []
    fence: str | None = None
    for line in text.split("\n"):
        m = _FENCE.match(line)
        if m and fence is None:
            fence = m.group(1)[0]
        elif m and fence is not None and m.group(1)[0] == fence and line.strip().strip(fence) == "":
            fence = None
        if not line.strip() and fence is None:
            if current:
                blocks.append("\n".join(current))
                current = []
        else:
            current.append(line)
    if current:
        blocks.append("\n".join(current))
    return blocks


def _fit(block: str) -> list[str]:
    """A block longer than one message is cut at a line (else anywhere)."""
    pieces: list[str] = []
    while len(block) > LIMIT:
        cut = block.rfind("\n", 0, LIMIT)
        cut = cut if cut > LIMIT // 2 else LIMIT
        pieces.append(block[:cut].rstrip())
        block = block[cut:].lstrip("\n")
    pieces.append(block)
    return pieces
