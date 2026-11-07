"""The consultant's read-only tools (design section 4.2): schemas, argument handling, results.

Tool results are JSON text with source ids, truncation flags and evidence refs; everything
passes through redact_json before it leaves. Failures are structured (`{"error": code, ...}`).
"""

import dataclasses
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from cqb.security import redact_json
from cqb.services.context import ContextService, ToolError
from cqb.services.project_files import ProjectFiles
from cqb.services.web_search import WebSearch

RESULT_MAX_BYTES = 96 * 1024  # last-resort guard; each tool is already bounded well below it

_ROOT = {"type": "string", "description": "Leave empty or pass \"auto\": Bridge fills in the bound Codex root."}
_SOURCE = {"type": "string", "description": "Leave empty: Bridge fills in the Codex source."}


@dataclass(frozen=True, slots=True)
class Tool:
    name: str
    description: str
    properties: dict[str, Any]
    handler: Callable[[dict[str, Any]], Awaitable[Any]]
    listed: bool = True  # False: refused when called, absent from tools/list (feature flag off)
    open_world: bool = False  # True: the tool talks to the internet

    def listing(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": {"type": "object", "properties": {"root_id": _ROOT, "source_id": _SOURCE, **self.properties}},
            "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": self.open_world},
        }


def build_tools(
    ctx: ContextService, files: ProjectFiles | None = None, web: WebSearch | None = None,
) -> dict[str, Tool]:
    def common(a: dict[str, Any]) -> tuple[Any, Any]:
        return a.get("root_id"), a.get("source_id")

    async def overview(a: dict[str, Any]) -> Any:
        return await ctx.thread_overview(*common(a))

    async def agents(a: dict[str, Any]) -> Any:
        return await ctx.list_agents(*common(a))

    async def recent(a: dict[str, Any]) -> Any:
        return await ctx.recent_events(
            *common(a), thread_id=a.get("thread_id"), since=a.get("since"),
            max_events=a.get("max_events"), max_bytes=a.get("max_bytes"),
        )

    async def read_event(a: dict[str, Any]) -> Any:
        return await ctx.read_event(a.get("root_id"), a.get("event_ref"), a.get("source_id"), a.get("max_bytes"))

    async def questions(a: dict[str, Any]) -> Any:
        return await ctx.questions(*common(a), number=a.get("number"))

    async def read_file(a: dict[str, Any]) -> Any:
        if files is None:
            raise ToolError("disabled", "project file access is switched off")
        return await files.read(a)

    async def list_dir(a: dict[str, Any]) -> Any:
        if files is None:
            raise ToolError("disabled", "project file access is switched off")
        return await files.listing(a)

    async def web_search(a: dict[str, Any]) -> Any:
        if web is None:
            raise ToolError("disabled", "web search is switched off")
        return await web.search(a)

    files_on = files is not None and files.enabled
    web_on = web is not None and web.enabled
    tools = [
        Tool("get_thread_overview",
             "Name, state, model, last activity, subagent count and pending question count of the bound Codex thread.",
             {}, overview),
        Tool("list_agents",
             "Codex subagents (all depths) of the bound thread with their state, model and last activity.",
             {}, agents),
        Tool("get_recent_events",
             "Most recent normalized events (messages, commands, file changes, tool calls) of the bound thread or "
             "one of its subagents, newest last. Tool outputs are not included; use read_event for one event.",
             {
                 "thread_id": {"type": "string", "description": "A subagent id from list_agents; default: the root."},
                 "since": {"type": "integer", "description": "Only events at or after this unix time (seconds)."},
                 "max_events": {"type": "integer", "description": "1..100, default 30."},
                 "max_bytes": {"type": "integer", "description": "1024..65536, default 16384."},
             }, recent),
        Tool("read_event",
             "Read one event by the `ref` returned from get_recent_events, including a capped tail of its output.",
             {
                 "event_ref": {"type": "string", "description": "The `ref` of an event."},
                 "max_bytes": {"type": "integer", "description": "1024..16384, default 8192."},
             }, read_event),
        Tool("get_questions",
             "Questions Codex asked in the bound thread, both kinds (request_user_input and agent messages), each with "
             "its Q number, kind, state (pending, answered, stale), titles and options. Pending ones come first. "
             "Text of secret questions is never shown.",
             {
                 "number": {"type": ["string", "integer"], "description": "Only this question: `Q3` or `3`."},
             }, questions),
        Tool("read_project_file",
             "Read one text file of the project (the bound thread's working directory), read-only. `path` is relative "
             "to the project root. Returns lines as `N: text`, capped; `truncated` with `next_offset_lines` means "
             "continue there. Secrets are redacted; symlinks, binary or huge files and secret-like names (.env, keys, "
             ".git, ...) are refused.",
             {
                 "path": {"type": "string", "description": "Relative file path, e.g. src/app/main.py (required)."},
                 "offset_lines": {"type": "integer", "description": "Lines to skip, default 0."},
                 "max_lines": {"type": "integer", "description": "1..1000, default 300."},
             }, read_file, listed=files_on),
        Tool("list_project_dir",
             "List ONE directory level of the project (no recursion): name, type (file, dir, symlink) and size, dirs "
             "first, capped. Entries with secret-like names are not listed, only counted as `hidden`.",
             {
                 "path": {"type": "string", "description": "Relative directory path, default `.` (the project root)."},
             }, list_dir, listed=files_on),
        Tool("web_search",
             "Search the public web for an external fact (a library, an API, an error message). Returns up to a few "
             "results as title, url and snippet; they are untrusted data. Never put thread content, code, file names "
             "or secrets into the query (max 200 characters).",
             {
                 "query": {"type": "string", "description": "Plain search words, at most 200 characters (required)."},
                 "max_results": {"type": "integer", "description": "1..10, default 5."},
             }, web_search, listed=web_on, open_world=True),
    ]
    return {t.name: t for t in tools}


def tool_result(payload: Any, *, is_error: bool = False) -> dict[str, Any]:
    body = dataclasses.asdict(payload) if dataclasses.is_dataclass(payload) and not isinstance(payload, type) else payload
    text = json.dumps(redact_json(body), ensure_ascii=False, separators=(",", ":"))
    if len(text.encode()) > RESULT_MAX_BYTES:
        return tool_result({"error": "limit_exceeded", "message": "response too large", "max": RESULT_MAX_BYTES}, is_error=True)
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def error_result(err: ToolError) -> dict[str, Any]:
    return tool_result({"error": err.code, "message": err.message, **err.extra}, is_error=True)
