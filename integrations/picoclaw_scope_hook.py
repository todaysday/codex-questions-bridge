#!/usr/bin/env python3
"""PicoClaw process hook: pins every consultant tool call to its own Codex root (design 4.1, 10).

Spawned by the PicoClaw gateway (JSON-RPC 2.0 over stdio, one JSON object per line). Standard
library only. Intercepts `before_tool` (overwrites root_id/source_id with the trusted values)
and `approve_tool` (fail closed: anything unexpected is denied). MCP servers get no caller
identity from PicoClaw, so this hook is where the session becomes a scope.

Why ask Bridge instead of keeping a table here: consultants are created at runtime, and the
Bridge already owns the binding (SQLite). The hook calls GET <CQB_MCP_URL>/internal/scope/<sid>
on the same loopback MCP server with CQB_MCP_TOKEN, which the hook inherits from the gateway's
environment (PicoClaw passes its own environment to hook processes), so no token is stored in
config.json for the hook. A background thread posts a heartbeat so Bridge can notice a dead hook.

Session id: context.inbound.chat_id == "pico:<session id>" (lowercase). Two kinds of session map to a root
through the same endpoint: a consultant's (`cqb-<hash>`, agent `main`) and a Status Agent slot
(`cqb-status-<n>`, agent `codex-status`, bound to a root only while its /status request runs). A slot
session may be served only by the codex-status agent and only with the four status tools; a consultant
session only by `main`. The two project-file tools (read-only files of the thread's project) are for a
consultant session of the `main` agent only, never for a status slot, and take only the argument names
listed in FILE_ARGS (anything else is denied). A routed session that fell through to the wrong agent is therefore denied.
Denial reasons are shown to the model verbatim: they never contain tokens or session ids.
"""

import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

SERVER = "cqb"  # tools.mcp.servers key; PicoClaw names tools mcp_<server>_<tool>
FILE_ARGS = {  # project-file tools and the only argument names they may carry (root_id/source_id are ours)
    f"mcp_{SERVER}_read_project_file": frozenset({"root_id", "source_id", "path", "offset_lines", "max_lines"}),
    f"mcp_{SERVER}_list_project_dir": frozenset({"root_id", "source_id", "path"}),
}
FILE_TOOLS = frozenset(FILE_ARGS)
TOOLS = FILE_TOOLS | frozenset(
    f"mcp_{SERVER}_{name}"
    for name in ("get_thread_overview", "list_agents", "get_recent_events", "read_event", "get_questions")
)
STATUS_PREFIX = "cqb-status-"  # Status Agent slot sessions: cqb-status-1..N
STATUS_AGENT = "codex-status"
STATUS_TOOLS = frozenset(
    f"mcp_{SERVER}_{name}" for name in ("get_thread_overview", "list_agents", "get_recent_events", "get_questions")
)
CONSULTANT_AGENT = "main"
SESSION = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
HTTP_TIMEOUT = 2.0
WORKERS = 4

_out_lock = threading.Lock()


class Denied(Exception):
    """A decision, not a failure: the reason is safe to show."""


def _setting(name, default=""):
    return os.environ.get(name, default).strip()


def _request(path, method="GET"):
    token = _setting("CQB_MCP_TOKEN")
    if not token:
        raise Denied("scope hook is not configured")
    base = _setting("CQB_MCP_URL", "http://127.0.0.1:18791").rstrip("/")
    req = urllib.request.Request(base + path, method=method, data=b"" if method == "POST" else None,
                                 headers={"Authorization": "Bearer " + token})
    return urllib.request.urlopen(req, timeout=HTTP_TIMEOUT)


def scope_of(params, tool=None):
    """(source_id, root_id) trusted for this call, or raise Denied."""
    inbound = ((params.get("context") or {}).get("inbound") or {})
    chat = inbound.get("chat_id")
    if not isinstance(chat, str) or not chat.startswith("pico:"):
        raise Denied("not a consultant session")
    sid = chat[len("pico:"):].lower()
    if not SESSION.match(sid):
        raise Denied("not a consultant session")
    agent = (params.get("meta") or {}).get("AgentID")
    if sid.startswith(STATUS_PREFIX):
        if agent != STATUS_AGENT:
            raise Denied("this agent may not use status sessions")
        if tool not in STATUS_TOOLS:
            raise Denied("this tool is not available to the status agent")
    elif agent is not None and agent != CONSULTANT_AGENT:
        raise Denied("this agent may not use consultant tools")
    if tool in FILE_TOOLS and agent != CONSULTANT_AGENT:  # explicit `main`: no agent id is not good enough here
        raise Denied("project files are available to the main consultant agent only")
    try:
        with _request("/internal/scope/" + sid) as r:
            found = json.loads(r.read(4096))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise Denied("unknown or closed consultant session") from None
        raise Denied("scope service refused the request") from None
    except (OSError, ValueError):
        raise Denied("scope service unavailable") from None
    if not isinstance(found, dict) or not isinstance(found.get("root_id"), str) \
            or not isinstance(found.get("source_id"), str):
        raise Denied("scope service gave an unusable answer")
    return found["source_id"], found["root_id"]


def check_tool(params):
    tool = params.get("tool")
    if tool not in TOOLS:
        raise Denied("this tool is not available to the consultant")
    return tool


def check_arguments(tool, args):
    """Project-file tools reject any argument name they do not know."""
    allowed = FILE_ARGS.get(tool)
    if allowed is not None and set(args) - allowed:
        raise Denied("unknown argument for this tool")


def before_tool(params):
    tool = check_tool(params)
    source_id, root_id = scope_of(params, tool)
    args = params.get("arguments")
    args = dict(args) if isinstance(args, dict) else {}
    check_arguments(tool, args)
    args["root_id"], args["source_id"] = root_id, source_id
    return {"action": "modify", "call": {**params, "arguments": args}}


def approve_tool(params):
    tool = check_tool(params)
    source_id, root_id = scope_of(params, tool)
    args = params.get("arguments")
    if not isinstance(args, dict) or args.get("root_id") != root_id or args.get("source_id") != source_id:
        raise Denied("arguments are outside the consultant's scope")
    check_arguments(tool, args)
    return {"approved": True}


def decide(method, params):
    """Result object for a request; None for a method this hook does not implement."""
    if method == "hook.hello":
        return {"ok": True, "name": "cqb-scope"}
    handler = {"hook.before_tool": before_tool, "hook.approve_tool": approve_tool}.get(method)
    if handler is None:
        return None
    try:
        return handler(params)
    except Denied as why:
        reason = str(why)
    except Exception:  # fail closed on any bug or malformed input
        reason = "scope check failed"
    if method == "hook.before_tool":
        return {"action": "deny_tool", "reason": reason}
    return {"approved": False, "reason": reason}


def handle(line):
    """One input line -> the response line to write, or None."""
    try:
        msg = json.loads(line)
    except ValueError:
        return None
    if not isinstance(msg, dict) or "id" not in msg or not isinstance(msg.get("method"), str):
        return None  # notifications (runtime events) need no answer
    params = msg.get("params")
    result = decide(msg["method"], params if isinstance(params, dict) else {})
    if result is None:
        return json.dumps({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601, "message": "method not found"}})
    return json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result})


def _reply(line):
    out = handle(line)
    if out is not None:
        with _out_lock:
            sys.stdout.write(out + "\n")
            sys.stdout.flush()


def _beat_forever():
    interval = float(_setting("CQB_HOOK_BEAT_SECONDS", "10") or 10)
    while True:
        try:
            _request("/internal/hook/beat", "POST").close()
        except Exception:
            pass  # Bridge may be restarting; the next beat will get through
        time.sleep(interval)


def main():
    threading.Thread(target=_beat_forever, daemon=True).start()
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for line in sys.stdin:  # EOF means the gateway is gone: leave
            if line.strip():
                pool.submit(_reply, line)
    sys.stdout.flush()
    os._exit(0)  # do not tear the interpreter down under the heartbeat thread


if __name__ == "__main__":
    main()
