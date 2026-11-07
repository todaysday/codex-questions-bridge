"""The PicoClaw scope hook, as PicoClaw runs it: a separate stdlib-only process speaking JSON lines."""

import ast
import json
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[1] / "integrations" / "picoclaw_scope_hook.py"
TOKEN = "hook-test-token"
SID = "cqb-" + "a" * 26
OTHER_SID = "cqb-" + "b" * 26
SLOT = "cqb-status-1"  # a Status Agent slot session
TOOL = "mcp_cqb_get_recent_events"


class FakeBridge:
    def __init__(self, delay=0.0):
        self.scopes = {SID: ("s1", "r1"), OTHER_SID: ("s1", "r2"), SLOT: ("s1", "r7")}
        self.requests: list[tuple[str, str, str]] = []  # method, path, authorization
        self.mode = "ok"
        self.delay = delay
        bridge = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _serve(self, method):
                bridge.requests.append((method, self.path, self.headers.get("Authorization", "")))
                time.sleep(bridge.delay)
                if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                    return self._send(401, b"")
                if bridge.mode == "500":
                    return self._send(500, b"boom")
                if bridge.mode == "garbage":
                    return self._send(200, b"<html>")
                if bridge.mode == "shape":
                    return self._send(200, b'{"root_id": 5}')
                if self.path.startswith("/internal/scope/"):
                    found = bridge.scopes.get(self.path.rsplit("/", 1)[1])
                    if found is None:
                        return self._send(404, b"")
                    return self._send(200, json.dumps({"source_id": found[0], "root_id": found[1]}).encode())
                self._send(204, b"")

            def _send(self, code, body):
                self.send_response(code)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = lambda self: self._serve("GET")  # noqa: E731
            do_POST = lambda self: self._serve("POST")  # noqa: E731

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def bridge():
    b = FakeBridge()
    yield b
    b.close()


def spawn(bridge_url, token=TOKEN, **extra):
    env = {"CQB_MCP_URL": bridge_url, "CQB_HOOK_BEAT_SECONDS": "3600", "PATH": "/usr/bin:/bin", **extra}
    if token is not None:
        env["CQB_MCP_TOKEN"] = token
    # -I -S: isolated, no site-packages, so the hook can only use the standard library
    return subprocess.Popen([sys.executable, "-I", "-S", str(HOOK)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, env=env)


def converse(bridge_url, requests, token=TOKEN, raw=(), **extra):
    """Send requests (dicts, or raw lines), close stdin, return {id: response} and the stderr text."""
    p = spawn(bridge_url, token, **extra)
    lines = [json.dumps(r) if isinstance(r, dict) else r for r in requests] + list(raw)
    out, err = p.communicate("\n".join(lines) + "\n", timeout=15)
    responses = [json.loads(line) for line in out.splitlines() if line.strip()]
    return {r["id"]: r for r in responses}, err, p.returncode


def call(method, tool=TOOL, args=None, chat=f"pico:{SID}", agent="main", id=1, **extra):
    params = {"meta": {"AgentID": agent}, "context": {"inbound": {"chat_id": chat, "raw": {"session_id": chat[5:]}}},
              "tool": tool, "arguments": args if args is not None else {}, **extra}
    return {"jsonrpc": "2.0", "id": id, "method": method, "params": params}


def denied(resp, reason_part=""):
    r = resp["result"]
    assert r.get("approved") is False or r.get("action") == "deny_tool", r
    assert reason_part in r["reason"], r
    for secret in (TOKEN, SID, OTHER_SID):
        assert secret not in r["reason"]


def test_hello_and_protocol_hygiene(bridge):
    got, err, code = converse(
        bridge.url,
        [{"jsonrpc": "2.0", "id": 1, "method": "hook.hello", "params": {"name": "scope", "version": 1, "modes": ["tool"]}},
         {"jsonrpc": "2.0", "id": 2, "method": "hook.nonsense", "params": {}},
         {"jsonrpc": "2.0", "method": "hook.runtime_event", "params": {"kind": "x"}}],  # notification: no answer
        raw=["not json at all", "[1, 2]", ""],
    )
    assert got[1]["result"]["ok"] is True and got[2]["error"]["code"] == -32601 and set(got) == {1, 2} and code == 0


def test_before_tool_overwrites_root_and_keeps_everything_else(bridge):
    req = call("hook.before_tool", args={"root_id": "r2", "source_id": "evil", "max_events": 5, "thread_id": "c1"},
               extra_field={"keep": "me"})
    got, _, _ = converse(bridge.url, [req])
    r = got[1]["result"]
    assert r["action"] == "modify"
    assert r["call"]["arguments"] == {"root_id": "r1", "source_id": "s1", "max_events": 5, "thread_id": "c1"}
    assert r["call"]["tool"] == TOOL and r["call"]["extra_field"] == {"keep": "me"} and r["call"]["meta"]["AgentID"] == "main"
    assert ("GET", f"/internal/scope/{SID}", f"Bearer {TOKEN}") in bridge.requests


def test_before_tool_supplies_the_root_when_the_model_sent_no_arguments(bridge):
    req = call("hook.before_tool")
    req["params"]["arguments"] = None
    got, _, _ = converse(bridge.url, [req])
    assert got[1]["result"]["call"]["arguments"] == {"root_id": "r1", "source_id": "s1"}


def test_each_session_gets_its_own_root_and_ids_are_lowercased(bridge):
    got, _, _ = converse(bridge.url, [
        call("hook.before_tool", args={}, chat=f"pico:{SID}", id=1),
        call("hook.before_tool", args={}, chat=f"pico:{OTHER_SID}", id=2),
        call("hook.before_tool", args={}, chat=f"pico:{SID.upper()}", id=3),
    ])
    assert [got[i]["result"]["call"]["arguments"]["root_id"] for i in (1, 2, 3)] == ["r1", "r2", "r1"]


def test_approve_allows_own_root_and_denies_any_other_argument(bridge):
    ok = {"root_id": "r1", "source_id": "s1"}
    got, _, _ = converse(bridge.url, [
        call("hook.approve_tool", args=ok, id=1),
        call("hook.approve_tool", args={**ok, "root_id": "r2"}, id=2),
        call("hook.approve_tool", args={**ok, "source_id": "other"}, id=3),
        call("hook.approve_tool", args={}, id=4),
        call("hook.approve_tool", args={"root_id": "r1"}, id=5),
    ])
    assert got[1]["result"] == {"approved": True}
    for i in (2, 3, 4, 5):
        denied(got[i], "outside the consultant's scope")


def test_unknown_session_and_non_consultant_chats_are_denied(bridge):
    ok = {"root_id": "r1", "source_id": "s1"}
    unknown = "pico:cqb-" + "z" * 26
    got, _, _ = converse(bridge.url, [
        call("hook.approve_tool", args=ok, chat=unknown, id=1),
        call("hook.before_tool", args=ok, chat=unknown, id=2),
        call("hook.approve_tool", args=ok, chat="telegram:123", id=3),
        call("hook.before_tool", args=ok, chat="pico:../../etc", id=4),
        call("hook.approve_tool", args=ok, chat="pico:", id=5),
    ])
    denied(got[1], "unknown or closed consultant session")
    denied(got[2], "unknown or closed consultant session")
    denied(got[3], "not a consultant session")
    denied(got[4], "not a consultant session")
    denied(got[5], "not a consultant session")
    assert not any("etc" in path for _, path, _ in bridge.requests)  # a malformed id never reaches the URL


def test_only_the_consultant_tools_and_agents_are_allowed(bridge):
    ok = {"root_id": "r1", "source_id": "s1"}
    got, _, _ = converse(bridge.url, [
        call("hook.approve_tool", tool="exec", args={"command": "id"}, id=1),
        call("hook.approve_tool", tool="mcp_other_get_recent_events", args=ok, id=2),
        call("hook.before_tool", tool="write_file", args={}, id=3),
        call("hook.approve_tool", tool="delegate", args=ok, id=4),
        call("hook.approve_tool", tool=None, args=ok, id=5),
        call("hook.approve_tool", agent="evil-agent", args=ok, id=6),
        call("hook.approve_tool", agent="codex-status", args=ok, id=7),
        call("hook.approve_tool", tool="mcp_cqb_read_event", args=ok, id=8),
    ])
    for i in (1, 2, 3, 4, 5):
        denied(got[i], "not available to the consultant")
    denied(got[6], "agent may not use")
    denied(got[7], "agent may not use")  # the status agent has no business in a consultant session
    assert got[8]["result"] == {"approved": True}


def test_status_slot_sessions_map_through_the_same_endpoint_for_the_status_agent_only(bridge):
    chat = f"pico:{SLOT}"
    status_tools = ("get_thread_overview", "list_agents", "get_recent_events", "get_questions")
    reqs = [call("hook.before_tool", tool=f"mcp_cqb_{t}", args={"root_id": "r1", "source_id": "x"}, chat=chat,
                 agent="codex-status", id=i) for i, t in enumerate(status_tools, 1)]
    got, _, _ = converse(bridge.url, reqs)
    for i in range(1, 5):  # the model's own root is overwritten with the one bound to the slot
        assert got[i]["result"]["call"]["arguments"] == {"root_id": "r7", "source_id": "s1"}
    assert ("GET", f"/internal/scope/{SLOT}", f"Bearer {TOKEN}") in bridge.requests
    ok = {"root_id": "r7", "source_id": "s1"}
    got, _, _ = converse(bridge.url, [
        call("hook.approve_tool", args=ok, chat=chat, agent="codex-status", id=1),
        call("hook.approve_tool", args={**ok, "root_id": "r1"}, chat=chat, agent="codex-status", id=2),
        call("hook.approve_tool", args=ok, chat=chat, agent="main", id=3),  # routing fell through to the consultant agent
        call("hook.approve_tool", args=ok, chat=chat, agent=None, id=4),
        call("hook.approve_tool", args=ok, chat=chat, agent="evil", id=5),
        call("hook.approve_tool", tool="mcp_cqb_read_event", args=ok, chat=chat, agent="codex-status", id=6),
        call("hook.before_tool", tool="mcp_cqb_read_event", args=ok, chat=chat, agent="codex-status", id=7),
    ])
    assert got[1]["result"] == {"approved": True}
    denied(got[2], "outside the consultant's scope")
    for i in (3, 4, 5):
        denied(got[i], "may not use status sessions")
    for i in (6, 7):
        denied(got[i], "not available to the status agent")


def test_unbound_or_unknown_status_slots_are_denied(bridge):
    ok = {"root_id": "r7", "source_id": "s1"}
    del bridge.scopes[SLOT]  # the request ended: the slot is no longer bound to a root
    got, _, _ = converse(bridge.url, [
        call("hook.before_tool", args=ok, chat=f"pico:{SLOT}", agent="codex-status", id=1),
        call("hook.approve_tool", args=ok, chat="pico:cqb-status-9", agent="codex-status", id=2),
        call("hook.approve_tool", args=ok, chat="pico:CQB-STATUS-1", agent="codex-status", id=3),  # ids are lowercased
    ])
    for i in (1, 2, 3):
        denied(got[i], "unknown or closed consultant session")


@pytest.mark.parametrize("mode", ["500", "garbage", "shape"])
def test_fail_closed_when_bridge_answers_badly(bridge, mode):
    bridge.mode = mode
    got, _, _ = converse(bridge.url, [call("hook.approve_tool", args={"root_id": "r1", "source_id": "s1"}, id=1),
                                      call("hook.before_tool", id=2)])
    denied(got[1])
    denied(got[2])
    assert got[2]["result"]["action"] == "deny_tool"


def test_fail_closed_when_bridge_is_down_or_token_is_wrong_or_missing(bridge):
    dead = "http://127.0.0.1:9"
    ok = {"root_id": "r1", "source_id": "s1"}
    for url, token, reason in [(dead, TOKEN, "unavailable"), (bridge.url, "wrong", "refused"), (bridge.url, None, "not configured")]:
        got, _, _ = converse(url, [call("hook.approve_tool", args=ok, id=1), call("hook.before_tool", id=2)], token=token)
        denied(got[1], reason)
        denied(got[2], reason)


@pytest.mark.parametrize("params", ["garbage", None, [], 5, {}, {"tool": TOOL}, {"tool": TOOL, "context": "x"},
                                    {"tool": TOOL, "context": {"inbound": []}},
                                    {"tool": {"x": 1}, "context": {}}])
def test_malformed_params_are_denied_never_crash(bridge, params):
    msgs = [{"jsonrpc": "2.0", "id": 1, "method": "hook.approve_tool", "params": params},
            {"jsonrpc": "2.0", "id": 2, "method": "hook.before_tool", "params": params},
            call("hook.approve_tool", args={"root_id": "r1", "source_id": "s1"}, id=3)]  # and it still works afterwards
    got, _, code = converse(bridge.url, msgs)
    denied(got[1])
    denied(got[2])
    assert got[3]["result"] == {"approved": True} and code == 0


def test_requests_are_answered_concurrently():
    slow = FakeBridge(delay=0.4)
    try:
        started = time.time()
        got, _, _ = converse(slow.url, [call("hook.approve_tool", args={"root_id": "r1", "source_id": "s1"}, id=i)
                                        for i in range(1, 5)])
        assert all(got[i]["result"] == {"approved": True} for i in range(1, 5))
        assert time.time() - started < 1.2  # one by one it would take 1.6 s
    finally:
        slow.close()


def test_heartbeat_is_posted_while_the_hook_lives(bridge):
    p = spawn(bridge.url, CQB_HOOK_BEAT_SECONDS="0.1")
    try:
        deadline = time.time() + 5
        while sum(1 for m, path, _ in bridge.requests if (m, path) == ("POST", "/internal/hook/beat")) < 3:
            assert time.time() < deadline, bridge.requests
            time.sleep(0.05)
        assert ("POST", "/internal/hook/beat", f"Bearer {TOKEN}") in bridge.requests
    finally:
        p.stdin.close()
        p.wait(5)
    seen = len(bridge.requests)
    time.sleep(0.4)
    assert len(bridge.requests) == seen  # EOF on stdin (gateway gone): the process ended and the beats stopped


def test_hook_imports_only_the_standard_library():
    tree = ast.parse(HOOK.read_text())
    imported = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    imported |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert imported and imported <= set(sys.stdlib_module_names), imported - set(sys.stdlib_module_names)


# --- project-file tools: main consultant sessions only, known argument names only ---------

READ_FILE, LIST_DIR = "mcp_cqb_read_project_file", "mcp_cqb_list_project_dir"


@pytest.mark.parametrize("tool,args", [(READ_FILE, {"path": "src/a.py", "offset_lines": 10, "max_lines": 5}),
                                      (LIST_DIR, {"path": "src"}), (LIST_DIR, {})])
def test_file_tools_are_pinned_to_the_root_for_the_main_consultant(bridge, tool, args):
    got, _, _ = converse(bridge.url, [
        call("hook.before_tool", tool=tool, args={**args, "root_id": "r2", "source_id": "evil"}, id=1),
        call("hook.approve_tool", tool=tool, args={**args, "root_id": "r1", "source_id": "s1"}, id=2),
        call("hook.approve_tool", tool=tool, args={**args, "root_id": "r2", "source_id": "s1"}, id=3),
        call("hook.approve_tool", tool=tool, args=args, id=4),  # before_tool did not run: no root_id
    ])
    assert got[1]["result"]["action"] == "modify"
    assert got[1]["result"]["call"]["arguments"] == {**args, "root_id": "r1", "source_id": "s1"}
    assert got[2]["result"] == {"approved": True}
    denied(got[3], "outside the consultant's scope")
    denied(got[4], "outside the consultant's scope")


@pytest.mark.parametrize("tool,extra", [(READ_FILE, "max_bytes"), (READ_FILE, "cwd"), (READ_FILE, "Path"), (READ_FILE, "path "),
                                       (READ_FILE, "root"), (LIST_DIR, "offset_lines"), (LIST_DIR, "max_lines"),
                                       (LIST_DIR, "recursive"), (LIST_DIR, "")])
def test_file_tools_reject_argument_names_they_do_not_know(bridge, tool, extra):
    ok = {"root_id": "r1", "source_id": "s1", "path": "x"}
    got, _, _ = converse(bridge.url, [
        call("hook.before_tool", tool=tool, args={"path": "x", extra: 1}, id=1),
        call("hook.approve_tool", tool=tool, args={**ok, extra: 1}, id=2),
    ])
    denied(got[1], "unknown argument")
    denied(got[2], "unknown argument")


def test_file_tools_are_denied_to_everything_but_the_main_agent_in_a_consultant_session(bridge):
    ok = {"root_id": "r1", "source_id": "s1", "path": "x"}
    reqs, expect = [], {}
    i = 0
    for tool in (READ_FILE, LIST_DIR):
        for method in ("hook.before_tool", "hook.approve_tool"):
            for agent, reason in (("main", None), (None, "main consultant agent only"), ("evil", "may not use consultant tools"),
                                  ("codex-status", "may not use consultant tools"), ("", "may not use consultant tools")):
                i += 1
                reqs.append(call(method, tool=tool, args=ok, agent=agent, id=i))
                expect[i] = reason
    got, _, _ = converse(bridge.url, reqs)
    for n, reason in expect.items():
        if reason is None:
            assert got[n]["result"].get("approved") is True or got[n]["result"].get("action") == "modify", got[n]
        else:
            denied(got[n], reason)


def test_status_slots_keep_exactly_their_four_tools_and_never_get_file_tools(bridge):
    chat = f"pico:{SLOT}"
    ok = {"root_id": "r7", "source_id": "s1", "path": "x"}
    reqs, i = [], 0
    for tool in (READ_FILE, LIST_DIR):
        for method in ("hook.before_tool", "hook.approve_tool"):
            for agent in ("codex-status", "main", None, "evil"):
                i += 1
                reqs.append(call(method, tool=tool, args=ok, chat=chat, agent=agent, id=i))
    got, _, _ = converse(bridge.url, reqs)
    for n in range(1, i + 1):
        denied(got[n])
        assert "available to the status agent" in got[n]["result"]["reason"] or "may not use status sessions" in got[n]["result"]["reason"]
    for tool in ("get_thread_overview", "list_agents", "get_recent_events", "get_questions"):  # unchanged
        got, _, _ = converse(bridge.url, [call("hook.approve_tool", tool=f"mcp_cqb_{tool}", args={"root_id": "r7", "source_id": "s1"},
                                               chat=chat, agent="codex-status")])
        assert got[1]["result"] == {"approved": True}


def test_file_tools_fail_closed_for_unknown_sessions_and_a_dead_bridge(bridge):
    ok = {"root_id": "r1", "source_id": "s1"}
    got, _, _ = converse(bridge.url, [
        call("hook.before_tool", tool=READ_FILE, args={"path": "x"}, chat="pico:cqb-" + "z" * 26, id=1),
        call("hook.approve_tool", tool=LIST_DIR, args=ok, chat="telegram:1", id=2)])
    denied(got[1], "unknown or closed consultant session")
    denied(got[2], "not a consultant session")
    got, _, _ = converse("http://127.0.0.1:9", [call("hook.before_tool", tool=READ_FILE, args={"path": "x"}, id=1)])
    denied(got[1], "unavailable")


def test_file_tool_denial_reasons_carry_no_secrets_or_paths(bridge):
    got, _, _ = converse(bridge.url, [call("hook.before_tool", tool=READ_FILE, args={"path": "/etc/shadow", "x": 1}, id=1)])
    denied(got[1])
    assert "shadow" not in got[1]["result"]["reason"]
