"""Read-only access to Codex history for the consultant context API (design section 4.2).

`thread/items/list` entries are normalized into bounded HistoryEvent DTOs. A list view never
carries tool output; `detail=True` (read_event only) adds a capped tail of command output or
tool result. Nothing here resumes or subscribes to a thread.
"""

import json
from typing import Any, Protocol

from cqb.domain.context import HistoryEvent

SUMMARY_CHARS = 600  # one event inside a list
ARGS_CHARS = 200
FIND_PAGE = 100
FIND_PAGES = 5


class HistoryUnavailable(Exception):
    """The daemon cannot answer right now (disconnected, timeout, unknown thread)."""


class Caller(Protocol):
    async def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]: ...


def make_ref(thread_id: str, turn_id: str, item_id: str) -> str:
    return f"{thread_id}:{turn_id}:{item_id}"


def split_ref(ref: str) -> tuple[str, str, str] | None:
    """Thread and turn ids contain no colon; the item id may."""
    parts = ref.split(":", 2)
    return (parts[0], parts[1], parts[2]) if len(parts) == 3 and all(parts) else None


class HistoryReader:
    def __init__(self, client: Caller) -> None:
        self._client = client

    async def recent(self, thread_id: str, limit: int) -> tuple[list[dict[str, Any]], bool]:
        """Newest `limit` raw entries in chronological order, and whether older ones exist."""
        result = await self._call(
            "thread/items/list", {"threadId": thread_id, "limit": limit, "sortDirection": "desc"}
        )
        entries = [e for e in result.get("data") or [] if isinstance(e, dict)]
        entries.reverse()
        return entries, bool(result.get("nextCursor"))

    async def page(self, thread_id: str, limit: int, cursor: str | None = None) -> tuple[list[dict[str, Any]], str | None]:
        params = {"threadId": thread_id, "limit": limit, "sortDirection": "desc"}
        if cursor:
            params["cursor"] = cursor
        result = await self._call("thread/items/list", params)
        entries = [e for e in result.get("data") or [] if isinstance(e, dict)]
        entries.reverse()
        return entries, result.get("nextCursor") or None

    async def find(self, thread_id: str, turn_id: str, item_id: str) -> dict[str, Any] | None:
        cursor: str | None = None
        for _ in range(FIND_PAGES):
            params: dict[str, Any] = {"threadId": thread_id, "turnId": turn_id, "limit": FIND_PAGE}
            if cursor:
                params["cursor"] = cursor
            result = await self._call("thread/items/list", params)
            for entry in result.get("data") or []:
                item = entry.get("item") if isinstance(entry, dict) else None
                if isinstance(item, dict) and item.get("id") == item_id:
                    return entry
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return None

    async def _call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            return await self._client.call(method, params)
        except Exception as exc:
            raise HistoryUnavailable(type(exc).__name__) from None


def event_from_entry(
    thread_id: str, entry: dict[str, Any], *, max_chars: int = SUMMARY_CHARS, detail: bool = False
) -> HistoryEvent | None:
    """None for entries that carry nothing useful or are malformed."""
    try:
        item = entry["item"]
        kind = str(item["type"])
        turn_id = str(entry["turnId"])
        item_id = str(item["id"])
        text, status = _describe(kind, item, max_chars, detail)
    except (KeyError, TypeError, AttributeError, ValueError):
        return None
    if text is None:
        return None
    text, cut = _cut(text, max_chars)
    at = entry.get("completedAtMs") or entry.get("startedAtMs")
    return HistoryEvent(
        ref=make_ref(thread_id, turn_id, item_id), thread_id=thread_id, turn_id=turn_id, kind=kind,
        at_ms=at if isinstance(at, int) and not isinstance(at, bool) else None,
        status=status, text=text, text_truncated=cut,
    )


def _describe(kind: str, item: dict[str, Any], max_chars: int, detail: bool) -> tuple[str | None, str | None]:
    status = item.get("status") if isinstance(item.get("status"), str) else None
    match kind:
        case "userMessage":
            return _user_text(item.get("content")), None
        case "agentMessage":
            phase = item.get("phase")
            return str(item.get("text") or ""), phase if isinstance(phase, str) else None
        case "plan":
            return str(item.get("text") or ""), None
        case "reasoning":
            return " ".join(str(s) for s in item.get("summary") or []), None
        case "commandExecution":
            text = "$ " + _cut(str(item.get("command") or ""), ARGS_CHARS)[0]
            if item.get("exitCode") is not None:
                text += f"  (exit {item['exitCode']})"
            if detail and item.get("aggregatedOutput"):
                text += "\n" + _tail(str(item["aggregatedOutput"]), max_chars)
            return text, status
        case "fileChange":
            paths = [str(c.get("path")) for c in item.get("changes") or [] if isinstance(c, dict)]
            return "files: " + ", ".join(paths[:20]) + (f" (+{len(paths) - 20} more)" if len(paths) > 20 else ""), status
        case "mcpToolCall":
            text = f"{item.get('server')}/{item.get('tool')} {_args(item.get('arguments'))}"
            if detail:
                text += "\n" + _json_tail(item.get("error") or item.get("result"), max_chars)
            return text, status
        case "dynamicToolCall":
            return f"{item.get('tool')} {_args(item.get('arguments'))}", status
        case "webSearch":
            return f"search: {item.get('query')}", None
        case "collabAgentToolCall":
            return f"{item.get('tool')} -> {', '.join(map(str, item.get('receiverThreadIds') or []))}", status
        case "subAgentActivity":
            return f"{item.get('kind')} {item.get('agentThreadId')}", None
        case "functionCallOutput":
            text = str(item.get("name") or "")
            if detail:
                text += "\n" + _json_tail(item.get("output"), max_chars)
            return text, None
        case "hookPrompt":
            return None, None
        case "contextCompaction":
            return "context compacted", None
    return kind, status


def _user_text(content: Any) -> str:
    parts = []
    for part in content or []:
        if isinstance(part, dict):
            parts.append(str(part["text"]) if part.get("type") == "text" else f"[{part.get('type', 'input')}]")
    return "\n".join(parts)


def _args(arguments: Any) -> str:
    if arguments is None:
        return ""
    return _cut(arguments if isinstance(arguments, str) else json.dumps(arguments, ensure_ascii=False), ARGS_CHARS)[0]


def _json_tail(value: Any, n: int) -> str:
    if value is None:
        return ""
    return _tail(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False), n)


def _cut(text: str, n: int) -> tuple[str, bool]:
    return (text[:n] + "…", True) if len(text) > n else (text, False)


def _tail(text: str, n: int) -> str:
    return "…" + text[-n:] if len(text) > n else text
