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


def hhmm(ts: int) -> str:
    return time.strftime("%H:%M", time.localtime(ts))


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
