"""Count PicoClaw compaction events retained in the local gateway journal.

The journal can be truncated, so the result is a lower bound rather than a lifetime count.
"""

import asyncio
import contextlib
import re

_FIELDS = re.compile(r"(?:^|\s)(event_kind|agent_id|session_key|chat_id)=([^\s]+)")
_EVENTS = {"agent.context.compress", "agent.session.summarize"}


def count_observed(lines: str, session: str) -> int | None:
    records = [dict(_FIELDS.findall(line)) for line in lines.splitlines()]
    keys = {r["session_key"] for r in records
            if r.get("agent_id") == "main" and r.get("chat_id") == f"pico:{session}" and r.get("session_key")}
    if not keys:
        return None
    return sum(r.get("event_kind") in _EVENTS and r.get("session_key") in keys for r in records)


async def observed_compactions(session: str) -> int | None:
    try:
        process = await asyncio.create_subprocess_exec(
            "journalctl", "--unit=picoclaw-gateway.service", "--output=cat", "--no-pager", "--lines=5000",
            r"--grep=Runtime event: agent\.(turn\.start|context\.compress|session\.summarize)",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            output, _ = await asyncio.wait_for(process.communicate(), 3)
        except (TimeoutError, asyncio.CancelledError) as exc:
            if process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
            await process.communicate()
            if isinstance(exc, asyncio.CancelledError):
                raise
            return None
        if process.returncode:
            return None
        return count_observed(output.decode("utf-8", errors="replace"), session)
    except OSError:
        return None
