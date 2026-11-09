"""Read collaboration recipients from a thread rollout, never messages or tool outputs."""

import json
import os
import stat
from datetime import datetime

from cqb.codex.rollout_tail import safe_path

TAIL_BYTES = 8 * 1024 * 1024


def read_interactions(path: str, codex_home: str, ids: set[str]) -> tuple[dict[str, tuple[str, str | None]], list[tuple[int, str, str]]]:
    real = safe_path(path, codex_home)
    if not real:
        return {}, []
    try:
        fd = os.open(real, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            st = os.fstat(stream.fileno())
            if not stat.S_ISREG(st.st_mode):
                return {}, []
            start = max(0, st.st_size - TAIL_BYTES)
            stream.seek(start)
            data = stream.read(TAIL_BYTES)
            lines = data.splitlines()
            if start:
                lines = lines[1:]
            found = {}
            incoming = []
            for line in lines:
                try:
                    row = json.loads(line)
                    payload = row.get("payload", {})
                    if row.get("type") == "response_item" and payload.get("type") == "agent_message":
                        author, recipient = payload.get("author"), payload.get("recipient")
                        if isinstance(author, str) and isinstance(recipient, str):
                            at = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00"))
                            incoming.append((int(at.timestamp() * 1000), author, recipient))
                        continue
                    if (row.get("type") != "response_item" or payload.get("type") != "function_call"
                            or payload.get("namespace") != "collaboration" or payload.get("call_id") not in ids):
                        continue
                    args = json.loads(payload.get("arguments") or "{}")
                    if isinstance(args, dict) and isinstance(payload.get("name"), str):
                        target = args.get("target")
                        found[payload["call_id"]] = (payload["name"], target if isinstance(target, str) else None)
                except (ValueError, TypeError, AttributeError, KeyError, OverflowError):
                    continue
            return found, incoming[-100:]
    except OSError:
        return {}, []
