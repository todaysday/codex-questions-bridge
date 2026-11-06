"""Latest token usage of a root thread from the tail of its rollout file.

The daemon only streams a root thread's usage to subscribers and the Bridge never subscribes,
so the rollout (JSONL, written by the daemon) is the source. Read-only and bounded: at most the
last TAIL_STEPS[-1] bytes are read, the path must stay inside the Codex sessions directories, and any
problem yields None. File contents are parsed for numbers only and are never logged.
"""

import json
import os
import stat

from cqb.domain.status import UsageSnapshot

TAIL_STEPS = (64 * 1024, 256 * 1024)  # widen once before giving up: token_count lines are sparse
ROOT_DIRS = ("sessions", "archived_sessions")


def read_usage(path: str, codex_home: str) -> UsageSnapshot | None:
    real = _safe_path(path, codex_home)
    if real is None:
        return None
    try:
        fd = os.open(real, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)  # never block on a FIFO
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return None
        size = st.st_size
        for step in TAIL_STEPS:
            start = max(size - step, 0)
            usage = _last_usage(_read_tail(fd, start), skip_first=start > 0)
            if usage is not None or start == 0:
                return usage
    except OSError:
        return None
    finally:
        os.close(fd)
    return None


def _safe_path(path: str, codex_home: str) -> str | None:
    try:
        real = os.path.realpath(path)
        for name in ROOT_DIRS:
            base = os.path.realpath(os.path.join(codex_home, name))
            if os.path.commonpath((real, base)) == base and real != base:
                return real
    except (OSError, ValueError):
        pass
    return None


def _read_tail(fd: int, start: int) -> bytes:
    os.lseek(fd, start, os.SEEK_SET)
    chunks = []
    while chunk := os.read(fd, 65536):
        chunks.append(chunk)
    return b"".join(chunks)


def _last_usage(data: bytes, skip_first: bool) -> UsageSnapshot | None:
    lines = data.split(b"\n")
    if skip_first:
        lines = lines[1:]  # the cut may fall inside a line
    for line in reversed(lines):
        if b"token_count" not in line:
            continue
        try:
            msg = json.loads(line)
            if msg.get("type") != "event_msg" or msg["payload"].get("type") != "token_count":
                continue
            info = msg["payload"].get("info")
            if not info:
                continue  # rate-limit-only update without usage
            last = info["last_token_usage"]
            window = info.get("model_context_window")
            return UsageSnapshot(
                int(last["total_tokens"]), int(window) if window else None,
                int(last["input_tokens"]), int(last.get("cached_input_tokens") or 0),
            )
        except (ValueError, KeyError, TypeError, AttributeError):
            continue
    return None
