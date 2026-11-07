"""Read-only project files for the consultant (design sections 4.2 and 10), behind `[mcp] project_files`.

Every call: flag, argument names, scope (root must be an open consultant), argument values, then the
working directory of the ROOT thread from the daemon (short TTL cache, bounded), then the confined
read in a worker thread (cqb.projectfs). Refusals are ToolErrors with stable codes and generic
messages (`denied` never says whether the path exists). Logs carry counts and codes only: never a path
and never file contents.
"""

import asyncio
import logging
import os
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from typing import Any, Protocol

from cqb import projectfs
from cqb.config import McpConfig
from cqb.domain.context import DirListing, FileChunk
from cqb.projectfs import DenyList, FsError
from cqb.services.context import ToolError, cap_int

log = logging.getLogger(__name__)

READ_ARGS = frozenset({"root_id", "source_id", "path", "offset_lines", "max_lines"})
LIST_ARGS = frozenset({"root_id", "source_id", "path"})
DEFAULT_MAX_LINES = 300
MAX_LINES = 1000
CWD_TTL = 30.0
CWD_CACHE_MAX = 64
MESSAGES = {
    "disabled": "project file access is switched off",
    "denied": "access to this path is denied",
    "invalid_path": "path must be relative, inside the project, without .., backslashes or control characters",
    "not_found": "no such file or directory",
    "symlink_refused": "symbolic links are never followed",
    "not_a_file": "not a regular file (use list_project_dir for directories)",
    "not_a_directory": "not a directory",
    "binary_or_too_large": "binary file, or larger than 2 MiB",
    "cwd_unavailable": "the thread has no usable working directory",
    "cwd_too_broad": "the thread's working directory is too broad to expose",
    "cwd_denied": "the thread's working directory is not allowed",
    "unavailable": "project files are not readable right now",
}


class CwdSource(Protocol):
    async def cwd(self, thread_id: str) -> str | None: ...


class ProjectFiles:
    def __init__(
        self, cfg: McpConfig, verify_scope: Callable[[Any, Any], None], source: CwdSource,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._cfg = cfg
        self._verify = verify_scope
        self._source = source
        self._clock = clock
        self._deny = DenyList(cfg.project_file_deny_extra)
        self._cwds: OrderedDict[str, tuple[float, str]] = OrderedDict()

    @property
    def enabled(self) -> bool:
        return self._cfg.project_files

    async def read(self, args: Mapping[str, Any]) -> FileChunk:
        root_id = await self._gate(args, READ_ARGS)
        if args.get("path") is None:
            raise ToolError("invalid_arguments", "path is required")
        offset = args.get("offset_lines")
        offset = 0 if offset is None else offset
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ToolError("invalid_arguments", "offset_lines must be an integer >= 0")
        max_lines = cap_int("max_lines", args.get("max_lines"), DEFAULT_MAX_LINES, 1, MAX_LINES)
        budget = self._cfg.project_file_max_bytes

        def work(root_fd: int) -> FileChunk:
            parts = projectfs.split_relpath(args["path"])
            return projectfs.read_file(
                root_fd, parts, self._deny, root_id=root_id, offset=offset, max_lines=max_lines, budget=budget)

        chunk = await self._run("read", root_id, work)
        log.info("project file read: lines=%d bytes=%d truncated=%s", chunk.returned_lines, len(chunk.text),
                 chunk.truncated)
        return chunk

    async def listing(self, args: Mapping[str, Any]) -> DirListing:
        root_id = await self._gate(args, LIST_ARGS)
        path = args.get("path")
        path = "." if path is None else path
        budget, entries = self._cfg.project_file_max_bytes, self._cfg.project_dir_max_entries

        def work(root_fd: int) -> DirListing:
            parts = projectfs.split_relpath(path)
            return projectfs.list_dir(
                root_fd, parts, self._deny, root_id=root_id, max_entries=entries, budget=budget)

        found = await self._run("list", root_id, work)
        log.info("project dir listed: entries=%d hidden=%d truncated=%s", len(found.entries), found.hidden,
                 found.truncated)
        return found

    # --- internals -------------------------------------------------------------------

    async def _gate(self, args: Mapping[str, Any], allowed: frozenset[str]) -> str:
        if not self.enabled:
            raise ToolError("disabled", MESSAGES["disabled"])
        if set(args) - allowed:
            raise ToolError("invalid_arguments", "unknown argument name")
        self._verify(args.get("root_id"), args.get("source_id"))  # ToolError invalid_arguments / scope_denied
        return args["root_id"]

    async def _run(self, op: str, root_id: str, work: Callable[[int], Any]) -> Any:
        cwd = await self._cwd(root_id)

        def blocking() -> Any:
            root_fd = projectfs.open_root(cwd)
            try:
                return work(root_fd)
            finally:
                os.close(root_fd)

        try:
            return await asyncio.to_thread(blocking)
        except FsError as err:
            log.info("project file %s refused: code=%s", op, err.code)
            raise ToolError(err.code, MESSAGES.get(err.code, MESSAGES["unavailable"])) from None
        except OSError as err:
            log.info("project file %s failed: errno=%s", op, err.errno)
            raise ToolError("unavailable", MESSAGES["unavailable"]) from None

    async def _cwd(self, root_id: str) -> str:
        now = self._clock()
        hit = self._cwds.get(root_id)
        if hit is not None and hit[0] > now:
            return hit[1]
        cwd = await self._source.cwd(root_id)
        if cwd is None:
            raise ToolError("cwd_unavailable", MESSAGES["cwd_unavailable"])
        self._cwds[root_id] = (now + CWD_TTL, cwd)
        self._cwds.move_to_end(root_id)
        while len(self._cwds) > CWD_CACHE_MAX:
            self._cwds.popitem(last=False)
        return cwd
