"""Formatting helpers shared by the views: limits, times, deep links, callback keys."""

import hashlib
import time

from cqb.telegram.models import ActionSpec

TEXT_LIMIT = 4096
CALLBACK_LIMIT = 64
BUTTON_TEXT_LIMIT = 64


def clip(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: max(limit - 1, 0)] + "…"


def chunks(text: str, size: int = 4000) -> list[str]:
    """Split on a newline (else a space, else hard) so each piece fits one Telegram message."""
    out: list[str] = []
    text = text.strip()
    while len(text) > size:
        cut = max(text.rfind("\n", 0, size), text.rfind(" ", 0, size))
        cut = cut if cut > size // 2 else size
        out.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    if text:
        out.append(text)
    return out


def hhmm(ts: int) -> str:
    return time.strftime("%H:%M", time.localtime(ts))


def age(ts: int, now: int) -> str:
    """How long ago, relative and coarse so the text changes rarely: `сейчас`, `5м` (steps of five minutes), `2ч`,
    `вчера`, `3д`."""
    delta = max(now - ts, 0)
    if delta < 300:
        return "сейчас"
    if delta < 3600:
        return f"{delta // 300 * 5}м"
    if delta < 86400:
        return f"{delta // 3600}ч"
    if delta < 172800:
        return "вчера"
    return f"{delta // 86400}д"


def when(ts: int, now: int) -> str:
    """HH:MM today, `вчера`, else DD.MM."""
    day = time.localtime(ts)[:3]
    today = time.localtime(now)[:3]
    if day == today:
        return hhmm(ts)
    if time.localtime(now - 86400)[:3] == day:
        return "вчера"
    return time.strftime("%d.%m", time.localtime(ts))


def topic_link(chat_id: int, topic_id: int) -> str | None:
    """Deep link to a forum topic: t.me/c/<chat id without the -100 prefix>/<topic id>."""
    raw = str(chat_id)
    if not raw.startswith("-100") or len(raw) <= 4:
        return None
    return f"https://t.me/c/{raw[4:]}/{topic_id}"


def callback_key(spec: ActionSpec, source_id: str) -> str:
    """Deterministic, so an unchanged keyboard hashes the same and survives re-renders."""
    raw = "\0".join((spec.action, source_id, spec.root or "", spec.payload or ""))
    return "a:" + hashlib.sha1(raw.encode()).hexdigest()[:12]
