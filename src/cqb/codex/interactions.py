"""Read recent collaboration call inputs from a thread's rollout, never tool outputs."""

import json
import os
import stat

from cqb.codex.rollout_tail import safe_path

TAIL_BYTES = 8 * 1024 * 1024


def read_interactions(path: str, codex_home: str, ids: set[str]) -> dict[str, dict]:
    real = safe_path(path, codex_home)
    if not real or not ids:
        return {}
    try:
        fd = os.open(real, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            st = os.fstat(stream.fileno())
            if not stat.S_ISREG(st.st_mode):
                return {}
            start = max(0, st.st_size - TAIL_BYTES)
            stream.seek(start)
            data = stream.read(TAIL_BYTES)
            lines = data.splitlines()
            if start:
                lines = lines[1:]
            found = {}
            for line in lines:
                try:
                    row = json.loads(line)
                    payload = row.get("payload", {})
                    if (row.get("type") != "response_item" or payload.get("type") != "function_call"
                            or payload.get("namespace") != "collaboration" or payload.get("call_id") not in ids):
                        continue
                    args = json.loads(payload.get("arguments") or "{}")
                    if isinstance(args, dict):
                        found[payload["call_id"]] = args
                except (ValueError, TypeError, AttributeError):
                    continue
            return found
    except OSError:
        return {}
