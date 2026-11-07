"""How many times the context of a thread was compacted: the number of `compacted` records in its rollout.

The rollout (JSONL, written by the daemon) is read-only and works for unsubscribed threads and after restarts.
The count is `rg -c` (ripgrep, preferred) or `grep -c` (the fallback when `rg` is not installed; resolved once)
in a short-lived child: no shell, argv list, path after `--`, `LC_ALL=C`, a hard timeout. One pattern serves both
tools (ERE and Rust regex) and both count matching LINES. rg prints nothing for zero matches, so the exit code
decides: 0 = matches, 1 = none, anything else = unknown.
A compaction is the top-level record `{"timestamp":"..","ordinal":N,"type":"compacted",..}`: the pattern is
anchored to the start of the line, so the same words quoted inside a message (nested deeper in the line) never
match. The daemon also writes an event_msg `item_completed` for each one; the `compacted` record is the marker.
The only state is a tiny bounded dict, (path, size, mtime_ns) -> count, so an unchanged file is not scanned again.
Path safety as for the tail reader: realpath inside the Codex sessions directories and a regular file, checked
before the spawn, and the checked real path is what the tool gets. Any problem (no tool, timeout, an exit code
other than 0 or 1, junk output) means unknown: None. File contents are never read here and never logged.
"""

import asyncio
import contextlib
import logging
import os
import re
import shutil
import stat

from cqb.codex.rollout_tail import safe_path

log = logging.getLogger(__name__)

# `-a`: a stray NUL byte must not hide matches. rg: no config file, no messages, no file name in the output.
TOOLS = {
    "rg": ["-c", "--no-config", "--no-messages", "--no-filename", "-a", "-e"],
    "grep": ["-c", "-a", "-E", "-e"],
}
PATTERN = r'^\{"timestamp":"[^"]*",("ordinal":[0-9]+,)?"type":"compacted"'
TIMEOUT_SECONDS = 5.0
CACHE_MAX = 256
OUTPUT_MAX = 32  # bytes of the tool's output read: a count is a few digits


class CompactionCounter:
    def __init__(
        self, codex_home: str, timeout: float = TIMEOUT_SECONDS, tools: tuple[str, ...] = ("rg", "grep"),
        argv: list[str] | None = None,
    ) -> None:
        """`tools`: executables in order of preference, the first one found is used. `argv` (tests): a complete
        command in place of the tool, pattern and `--`; only the path is appended."""
        self._home = codex_home
        self._tools = tools
        self._argv = argv
        self._resolved = argv is not None
        self._timeout = timeout
        self._cache: dict[tuple[str, int, int], int] = {}

    async def __call__(self, path: str) -> int | None:
        real = safe_path(path, self._home)
        if real is None:
            return None
        try:
            st = os.stat(real)
        except OSError:
            return None
        if not stat.S_ISREG(st.st_mode):
            return None
        key = (real, st.st_size, st.st_mtime_ns)
        if (known := self._cache.get(key)) is not None:
            return known
        count = await self._count(real)
        if count is not None:
            if len(self._cache) >= CACHE_MAX:
                del self._cache[next(iter(self._cache))]  # the oldest entry
            self._cache[key] = count
        return count

    def _command(self) -> list[str] | None:
        """The command without the path; resolved on first use and then kept (None: no tool installed)."""
        if not self._resolved:
            self._resolved = True
            for name in self._tools:
                if exe := shutil.which(name):
                    self._argv = [exe, *TOOLS.get(name, TOOLS["grep"]), PATTERN, "--"]
                    break
            else:
                log.warning("compaction count: neither rg nor grep found")
        return self._argv

    async def _count(self, real: str) -> int | None:
        argv = self._command()
        if argv is None:
            return None
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, real, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, env={"LC_ALL": "C", "PATH": os.environ.get("PATH", "/usr/bin:/bin")},
            )
        except OSError as exc:
            log.warning("compaction count: cannot start the counter: %s", type(exc).__name__)
            return None
        try:
            async with asyncio.timeout(self._timeout):
                out = await proc.stdout.read(OUTPUT_MAX)  # type: ignore[union-attr]
                code = await proc.wait()
        except TimeoutError:
            log.warning("compaction count: timed out")
            return None
        finally:
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                await asyncio.shield(proc.wait())
        if code == 1:
            return 0  # no matching line (rg prints nothing then)
        if code != 0:
            return None
        digits = out.strip()
        return int(digits) if re.fullmatch(rb"[0-9]{1,9}", digits) else None  # a count, nothing else
