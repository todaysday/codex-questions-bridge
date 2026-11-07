import asyncio
import contextlib
import json

import aiohttp
import pytest

from cqb.codex.history import HistoryUnavailable
from cqb.dao.agent_questions import AgentQuestionDao
from cqb.dao.consultants import ConsultantDao
from cqb.dao.questions import QuestionDao
from cqb.dao.threads import ThreadDao
from cqb.domain.enums import AgentQuestionState, ConsultantState, QuestionLifecycle, RunState
from cqb.domain.models import AgentQuestion
from cqb.mcp.server import PROTOCOLS, McpServer
from cqb.mcp.tools import build_tools
from cqb.services.context import MAX_AGENTS, ContextService
from test_db_dao import question
from tg_stack import NOW, StatusSourceStub, snapshot, thread

TOKEN = "mcp-test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def entry(turn, item_id, kind="agentMessage", **fields):
    return {"turnId": turn, "startedAtMs": 1_000, "completedAtMs": 2_000_000, "item": {"type": kind, "id": item_id, **fields}}


class HistoryStub:
    def __init__(self):
        self.items: dict[str, list] = {}
        self.fail = False
        self.calls = []

    async def recent(self, thread_id, limit):
        self.calls.append(("recent", thread_id, limit))
        if self.fail:
            raise HistoryUnavailable("NotConnected")
        rows = self.items.get(thread_id, [])
        return rows[-limit:], len(rows) > limit

    async def find(self, thread_id, turn_id, item_id):
        self.calls.append(("find", thread_id, turn_id, item_id))
        if self.fail:
            raise HistoryUnavailable("NotConnected")
        return next((e for e in self.items.get(thread_id, []) if e["turnId"] == turn_id and e["item"]["id"] == item_id), None)


class World:
    """r1 (open consultant) with child c1 and grandchild g1; r2 (open) with c2; r3 archived consultant."""

    def __init__(self, conn):
        self.conn = conn
        self.threads, self.consultants, self.questions = ThreadDao(conn), ConsultantDao(conn), QuestionDao(conn)
        self.threads.upsert_many([
            thread("r1", name="Fox"), thread("c1", parent="r1"), thread("r2", name="Other"), thread("c2", parent="r2"),
            thread("r3", name="Old"),
        ])
        for i, root in enumerate(("r1", "r2", "r3")):
            c = self.consultants.reserve("s1", root, -100, f"cqb-{root}", 1)
            self.consultants.open(c.consultant_id, 10 + i, 1)
        self.consultants.set_state(self.consultants.get("s1", "r3").consultant_id, ConsultantState.ARCHIVED, 2)
        self.source = StatusSourceStub()
        self.source.threads["r1"] = snapshot("r1", RunState.WORKING, name="Fox", model="gpt-6", effort="high")
        self.source.children["r1"] = (snapshot("c1", RunState.WORKING, parent="r1", nickname="Lovelace"),
                                      snapshot("g1", RunState.IDLE, parent="c1", nickname="Turing"))
        self.history = HistoryStub()
        self.history.items["r1"] = [entry("t1", f"i{n}", text=f"event {n}") for n in range(120)]
        self.history.items["c1"] = [entry("t2", "w1", text="child note")]
        self.history.items["g1"] = [entry("t3", "z1", text="grandchild note")]
        self.history.items["r2"] = [entry("t4", "x1", text="OTHER-ROOT-SECRET")]
        self.agent_questions = AgentQuestionDao(conn)
        self.ctx = ContextService(self.consultants, self.threads, self.questions, self.source, self.history, "s1",
                                  self.agent_questions)
        self.tools = build_tools(self.ctx)


@contextlib.asynccontextmanager
async def server(conn, token=TOKEN):
    w = World(conn)
    w.beats = 0
    w.scopes = {"cqb-r1": ("s1", "r1")}

    def beat():
        w.beats += 1
    srv = McpServer(token, w.tools, w.scopes.get, beat, port=0)
    await srv.start()
    async with aiohttp.ClientSession() as http:
        w.http, w.srv, w.url = http, srv, f"http://127.0.0.1:{srv.port}"
        try:
            yield w
        finally:
            await srv.stop()


async def rpc(w, method, params=None, id=1, headers=AUTH):
    body = {"jsonrpc": "2.0", "method": method, **({"id": id} if id is not None else {}), **({"params": params} if params is not None else {})}
    async with w.http.post(w.url + "/mcp", json=body, headers=headers) as r:
        return r.status, (await r.json() if r.content_type == "application/json" else None)


async def call(w, name, **args):
    status, body = await rpc(w, "tools/call", {"name": name, "arguments": args})
    assert status == 200, body
    result = body["result"]
    return json.loads(result["content"][0]["text"]), result["isError"]


def arun(coro):
    return asyncio.run(coro)


# --- protocol and auth ----------------------------------------------------------------

def test_initialize_negotiates_protocol_and_advertises_tools_only(conn):
    async def main():
        async with server(conn) as w:
            for asked in PROTOCOLS:
                _, b = await rpc(w, "initialize", {"protocolVersion": asked, "capabilities": {}, "clientInfo": {}})
                assert b["result"]["protocolVersion"] == asked
            _, b = await rpc(w, "initialize", {"protocolVersion": "1999-01-01"})
            r = b["result"]
            assert r["protocolVersion"] == PROTOCOLS[0] and list(r["capabilities"]) == ["tools"]
            assert r["serverInfo"]["name"] == "cqb-context" and b["id"] == 1
    arun(main())


def test_notifications_ping_and_unknown_methods(conn):
    async def main():
        async with server(conn) as w:
            assert (await rpc(w, "notifications/initialized", id=None))[0] == 202
            assert (await rpc(w, "notifications/cancelled", {"requestId": 3}, id=None))[0] == 202
            assert (await rpc(w, "ping", id=5))[1] == {"jsonrpc": "2.0", "id": 5, "result": {}}
            _, b = await rpc(w, "resources/list", id=6)
            assert b["error"]["code"] == -32601 and b["id"] == 6
            async with w.http.post(w.url + "/mcp", data=b"{nope", headers=AUTH) as r:
                assert (await r.json())["error"]["code"] == -32700
            async with w.http.post(w.url + "/mcp", json=[{"jsonrpc": "2.0", "id": 1, "method": "ping"}], headers=AUTH) as r:
                assert (await r.json())["error"]["code"] == -32600
            async with w.http.post(w.url + "/mcp", json={"id": 1, "method": "ping"}, headers=AUTH) as r:
                assert (await r.json())["error"]["code"] == -32600
            _, b = await rpc(w, "tools/call", {"name": "nope"})
            assert b["error"]["code"] == -32602
            _, b = await rpc(w, "tools/call", {"name": "list_agents", "arguments": [1]})
            assert b["error"]["code"] == -32602
    arun(main())


def test_tools_list_is_read_only_with_schemas(conn):
    async def main():
        async with server(conn) as w:
            _, b = await rpc(w, "tools/list")
            tools = {t["name"]: t for t in b["result"]["tools"]}
            assert set(tools) == {"get_thread_overview", "list_agents", "get_recent_events", "read_event", "get_questions"}
            assert all(t["annotations"]["readOnlyHint"] and t["inputSchema"]["type"] == "object" for t in tools.values())
            assert all("root_id" in t["inputSchema"]["properties"] for t in tools.values())
    arun(main())


def test_authentication_origin_and_stream_endpoints(conn):
    async def main():
        async with server(conn) as w:
            ping = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
            for headers in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": TOKEN}, {"Authorization": "Basic " + TOKEN}):
                async with w.http.post(w.url + "/mcp", json=ping, headers=headers) as r:
                    assert r.status == 401
            async with w.http.post(w.url + "/mcp", json=ping, headers={**AUTH, "Origin": "http://evil.example"}) as r:
                assert r.status == 403
            for method in ("get", "delete"):
                async with getattr(w.http, method)(w.url + "/mcp", headers=AUTH) as r:
                    assert r.status == 405 and "POST" in r.headers["Allow"]
            async with w.http.get(w.url + "/mcp") as r:
                assert r.status == 401  # unauthenticated callers learn nothing about the route
            async with w.http.post(w.url + "/mcp", data=b"x" * 70_000, headers=AUTH) as r:
                assert r.status == 413
    arun(main())


def test_listens_on_loopback_only(conn):
    async def main():
        with pytest.raises(ValueError, match="loopback"):
            McpServer("t", {}, lambda s: None, lambda: None, port=0, host="0.0.0.0")
        async with server(conn) as w:
            assert w.srv._runner.addresses[0][0] == "127.0.0.1"
    arun(main())


def test_internal_scope_and_heartbeat_endpoints(conn):
    async def main():
        async with server(conn) as w:
            async with w.http.get(w.url + "/internal/scope/cqb-r1", headers=AUTH) as r:
                assert r.status == 200 and await r.json() == {"source_id": "s1", "root_id": "r1"}
            async with w.http.get(w.url + "/internal/scope/cqb-nobody", headers=AUTH) as r:
                assert r.status == 404
            async with w.http.get(w.url + "/internal/scope/cqb-r1") as r:
                assert r.status == 401
            async with w.http.post(w.url + "/internal/hook/beat", headers=AUTH) as r:
                assert r.status == 204
            async with w.http.post(w.url + "/internal/hook/beat") as r:
                assert r.status == 401
            assert w.beats == 1
    arun(main())


# --- scope: the server checks what the hook already pinned ---------------------------

def test_root_must_be_an_open_consultant_of_the_configured_source(conn):
    async def main():
        async with server(conn) as w:
            for tool in (n for n, t in w.tools.items() if t.listed):  # project-file tools: test_project_files.py
                for args in ({"root_id": "r3"}, {"root_id": "c1"}, {"root_id": "zzz"}, {"root_id": "r1", "source_id": "other"}):
                    body, err = await call(w, tool, **({"event_ref": "r1:t1:i1"} if tool == "read_event" else {}), **args)
                    assert err and body["error"] == "scope_denied", (tool, args, body)
                body, err = await call(w, tool, **({"event_ref": "r1:t1:i1"} if tool == "read_event" else {}))
                assert err and body["error"] == "invalid_arguments"  # no root_id: the hook did not run
            assert w.history.calls == []  # nothing was read for a denied scope
    arun(main())


def test_threads_must_be_root_or_descendant_known_locally_or_by_the_daemon(conn):
    async def main():
        async with server(conn) as w:
            body, err = await call(w, "get_recent_events", root_id="r1", thread_id="c1")  # known in the registry
            assert not err and body["events"][0]["text"] == "child note"
            body, err = await call(w, "get_recent_events", root_id="r1", thread_id="g1")  # new: only the daemon knows it
            assert not err and body["events"][0]["text"] == "grandchild note"
            for foreign in ("c2", "r2", "unknown"):
                body, err = await call(w, "get_recent_events", root_id="r1", thread_id=foreign)
                assert err and body["error"] == "scope_denied", foreign
            body, err = await call(w, "get_recent_events", root_id="r1", thread_id="../etc")
            assert err and body["error"] == "invalid_arguments"
            w.source.fail = True  # daemon unreadable: an unknown thread is denied, never guessed
            body, err = await call(w, "get_recent_events", root_id="r1", thread_id="g1")
            assert err and body["error"] == "scope_denied"
            assert ("recent", "c2", 30) not in w.history.calls and ("recent", "r2", 30) not in w.history.calls
    arun(main())


# --- tools ----------------------------------------------------------------------------

def test_overview_and_agents(conn):
    async def main():
        async with server(conn) as w:
            w.questions.insert_if_new(question("q1", root_thread_id="r1", origin_thread_id="r1"))
            body, err = await call(w, "get_thread_overview", root_id="r1", source_id="s1")
            assert not err and body["name"] == "Fox" and body["state"] == "working" and body["model"] == "gpt-6"
            assert (body["source_id"], body["root_id"], body["agents_total"], body["pending_questions"], body["partial"]) \
                == ("s1", "r1", 2, 1, False)
            body, _ = await call(w, "list_agents", root_id="r1")
            assert [a["nickname"] for a in body["agents"]] == ["Lovelace", "Turing"] and body["truncated"] is False
            w.source.more = True
            assert (await call(w, "list_agents", root_id="r1"))[0]["truncated"] is True
            w.source.fail = True
            body, err = await call(w, "get_thread_overview", root_id="r1")  # degraded, not failed
            assert not err and body["partial"] is True and body["state"] == "unknown" and body["name"] == "Fox"
            body, err = await call(w, "list_agents", root_id="r1")
            assert err and body["error"] == "unavailable"
    arun(main())


def test_agent_list_is_capped(conn):
    async def main():
        async with server(conn) as w:
            w.source.children["r1"] = tuple(snapshot(f"k{i}", parent="r1", updated=NOW - i) for i in range(MAX_AGENTS + 20))
            body, _ = await call(w, "list_agents", root_id="r1")
            assert len(body["agents"]) == MAX_AGENTS and body["truncated"] is True
    arun(main())


def test_recent_events_caps_truncation_and_evidence_refs(conn):
    async def main():
        async with server(conn) as w:
            body, err = await call(w, "get_recent_events", root_id="r1")
            assert not err and len(body["events"]) == 30 and body["truncated"] is True  # default 30 of 120
            assert body["events"][-1]["text"] == "event 119" and body["events"][0]["text"] == "event 90"  # newest last
            assert body["events"][-1]["ref"] == "r1:t1:i119" and body["source_id"] == "s1"
            body, _ = await call(w, "get_recent_events", root_id="r1", max_events=100)
            assert len(body["events"]) == 100
            for args, name in (({"max_events": 101}, "max_events"), ({"max_bytes": 70_000}, "max_bytes")):
                body, err = await call(w, "get_recent_events", root_id="r1", **args)
                assert err and body["error"] == "limit_exceeded" and body["limit"] == name and body["max"] in (100, 65536)
            for bad in ({"max_events": 0}, {"max_events": "9"}, {"max_bytes": 10}, {"since": -1}, {"max_events": True}):
                body, err = await call(w, "get_recent_events", root_id="r1", **bad)
                assert err and body["error"] == "invalid_arguments", bad
            # the byte budget keeps the newest events and says that older ones were cut
            w.history.items["r1"] = [entry("t1", f"b{n}", text="word " * 118) for n in range(100)]
            body, _ = await call(w, "get_recent_events", root_id="r1", max_events=100, max_bytes=4096)
            assert 3 <= len(body["events"]) <= 6 and body["truncated"] is True
            assert body["events"][-1]["ref"] == "r1:t1:b99"
            assert len(json.dumps(body["events"])) <= 4096 + 600
    arun(main())


def test_recent_events_since_filter_and_daemon_failure(conn):
    async def main():
        async with server(conn) as w:
            w.history.items["r1"] = [
                {"turnId": "t", "completedAtMs": 1_000_000, "item": {"type": "agentMessage", "id": "old", "text": "old"}},
                {"turnId": "t", "completedAtMs": 5_000_000, "item": {"type": "agentMessage", "id": "new", "text": "new"}},
            ]
            body, _ = await call(w, "get_recent_events", root_id="r1", since=2_000)
            assert [e["text"] for e in body["events"]] == ["new"]
            w.history.fail = True
            body, err = await call(w, "get_recent_events", root_id="r1")
            assert err and body["error"] == "unavailable"
    arun(main())


def test_long_text_is_cut_per_event_and_flagged(conn):
    async def main():
        async with server(conn) as w:
            w.history.items["r1"] = [entry("t", "big", text="zz " * 1700)]
            body, _ = await call(w, "get_recent_events", root_id="r1")
            ev = body["events"][0]
            assert ev["text_truncated"] is True and len(ev["text"]) <= 601
    arun(main())


def test_everything_returned_is_redacted(conn):
    async def main():
        async with server(conn) as w:
            secret_key = "-----BEGIN RSA PRIVATE KEY-----\nMIIEabc\n-----END RSA PRIVATE KEY-----"
            w.history.items["r1"] = [
                entry("t", "a", text="curl -H 'Authorization: Bearer abcdefghijklmnop1234' https://x"),
                entry("t", "b", text=f"key: {secret_key}"),
                entry("t", "c", text="export API_KEY=sk-live-0123456789abcdefABCDEF and token=ghp_0123456789abcdefghijABCDEFGHIJ"),
                entry("t", "d", text="blob " + "QUJD" * 100),
            ]
            body, _ = await call(w, "get_recent_events", root_id="r1")
            blob = json.dumps(body)
            for leaked in ("abcdefghijklmnop1234", "MIIEabc", "sk-live-0123456789", "ghp_0123456789", "QUJDQUJD"):
                assert leaked not in blob, leaked
            assert all(e["redacted"] for e in body["events"]) and "[REDACTED" in blob and "BLOB" in blob
            # a thread name that holds a secret is redacted too (final pass over every string)
            w.source.threads["r1"] = snapshot("r1", name="deploy password=hunter22 now")
            body, _ = await call(w, "get_thread_overview", root_id="r1")
            assert "hunter22" not in json.dumps(body)
    arun(main())


def test_read_event_detail_caps_scope_and_redaction(conn):
    async def main():
        async with server(conn) as w:
            w.history.items["r1"] = [
                entry("t1", "k1", "commandExecution", command="make test", status="completed", exitCode=0,
                      aggregatedOutput="ok\n" + "line\n" * 4000 + "password=hunter2 end"),
                entry("t1", "m1", "mcpToolCall", server="s", tool="t", arguments={"a": 1}, status="completed", result={"x": "y"}),
            ]
            body, err = await call(w, "read_event", root_id="r1", event_ref="r1:t1:k1")
            assert not err and "hunter2" not in body["text"] and body["text"].startswith("$ make test")
            assert body["text_truncated"] is True and len(body["text"].encode()) <= 8192 + 8  # default read cap
            body, _ = await call(w, "read_event", root_id="r1", event_ref="r1:t1:k1", max_bytes=2048)
            assert len(body["text"].encode()) <= 2048 + 8
            body, err = await call(w, "read_event", root_id="r1", event_ref="r1:t1:k1", max_bytes=20_000)
            assert err and body["error"] == "limit_exceeded" and body["max"] == 16384
            body, _ = await call(w, "read_event", root_id="r1", event_ref="r1:t1:m1")
            assert '"x": "y"' in body["text"]
            # list view never carries output
            body, _ = await call(w, "get_recent_events", root_id="r1")
            assert "line" not in json.dumps(body)
            for ref, code in (("r1:t1:nope", "not_found"), ("garbage", "invalid_arguments"), ("r1::x", "invalid_arguments"),
                              ("r2:t4:x1", "scope_denied"), ("c2:t:x", "scope_denied")):
                body, err = await call(w, "read_event", root_id="r1", event_ref=ref)
                assert err and body["error"] == code, (ref, body)
            body, err = await call(w, "read_event", root_id="r1", event_ref="c1:t2:w1")  # own child: fine
            assert not err and body["text"] == "child note"
    arun(main())


def test_questions_never_return_secret_text(conn):
    async def main():
        async with server(conn) as w:
            w.questions.insert_if_new(question("pub", root_thread_id="r1", origin_thread_id="c1"))
            # even if a secret row somehow carried items, the service strips them
            w.questions.insert_if_new(question("sec", root_thread_id="r1", origin_thread_id="r1", is_secret=True))
            w.questions.insert_if_new(question("oth", root_thread_id="r2", origin_thread_id="r2"))
            body, err = await call(w, "get_questions", root_id="r1")
            assert not err
            by_id = {q["question_id"]: q for q in body["questions"]}
            assert set(by_id) == {"pub", "sec"}  # only this root's questions
            assert by_id["pub"]["items"][0]["title"] == "H: Pick?" and by_id["pub"]["thread_id"] == "c1"
            assert by_id["pub"]["items"][0]["options"] == ["A — da", "B"] and by_id["pub"]["kind"] == "request_user_input"
            assert by_id["sec"]["is_secret"] is True and by_id["sec"]["items"] == []
            assert "Pick?" not in json.dumps(by_id["sec"])
    arun(main())


def agent_question(aqid, root="r1", title="Which table?", options=("Existing", "Separate"), state=AgentQuestionState.PENDING, index=0):
    return AgentQuestion(
        agent_question_id=aqid, source_id="s1", thread_id=root, root_thread_id=root, turn_id="u1", item_id=aqid,
        index=index, title=title, options=tuple(options), answerable=True, state=state, staged=None,
        sent_at=None, created_at=1, updated_at=1,
    )


def test_get_questions_returns_both_kinds_with_numbers_states_and_the_number_filter(conn):
    async def main():
        async with server(conn) as w:
            w.questions.insert_if_new(question("old", root_thread_id="r1", origin_thread_id="r1"))  # Q1
            w.agent_questions.insert_if_new(agent_question("a_two"))  # Q2
            for n, (title, options) in enumerate((("First?", ("Yes", "No")), ("Second?", ()), ("Third?", ("Red",)))):
                w.agent_questions.insert_if_new(agent_question(f"a_m{n}", title=title, options=options, index=n))  # Q3..Q5
            w.questions.insert_if_new(question("sec", root_thread_id="r1", origin_thread_id="r1", is_secret=True))  # Q6
            w.agent_questions.insert_if_new(agent_question("a_other", root="r2"))  # r2's Q1, never visible from r1
            w.questions.set_lifecycle("old", QuestionLifecycle.RESOLVED_OBSERVED, 5)
            w.agent_questions.set_state("a_two", AgentQuestionState.STALE, 5)
            body, err = await call(w, "get_questions", root_id="r1")
            assert not err
            rows = {q["question_id"]: q for q in body["questions"]}
            assert set(rows) == {"old", "a_two", "a_m0", "a_m1", "a_m2", "sec"} and body["truncated"] is False
            assert [q["number"] for q in body["questions"]] == ["Q3", "Q4", "Q5", "Q6", "Q2", "Q1"]  # pending first, then newest closed
            assert (rows["old"]["kind"], rows["old"]["state"]) == ("request_user_input", "answered")
            assert (rows["a_two"]["kind"], rows["a_two"]["state"]) == ("agent_message", "stale")
            assert rows["a_m0"]["state"] == "pending" and rows["a_m0"]["is_secret"] is False
            assert [(i["title"], i["options"]) for q in ("a_m0", "a_m1", "a_m2") for i in rows[q]["items"]] == [
                ("First?", ["Yes", "No"]), ("Second?", []), ("Third?", ["Red"])]  # one question per number
            assert rows["a_two"]["items"][0]["options"] == ["Existing", "Separate"] and "label" not in rows["a_two"]["items"][0]
            assert rows["sec"]["is_secret"] is True and rows["sec"]["items"] == [] and "Pick?" not in json.dumps(rows["sec"])

            for wanted in ("Q4", "q4", "4", 4, "Q4.1"):
                body, err = await call(w, "get_questions", root_id="r1", number=wanted)
                assert not err and [q["question_id"] for q in body["questions"]] == ["a_m1"], wanted
            body, _ = await call(w, "get_questions", root_id="r1", number="Q9")
            assert body["questions"] == []
            for bad in ("Q", "abc", 0, -1, True, 2.5):
                body, err = await call(w, "get_questions", root_id="r1", number=bad)
                assert err and body["error"] == "invalid_arguments", bad
            assert (await call(w, "get_thread_overview", root_id="r1"))[0]["pending_questions"] == 4  # three agent + the secret
            listing = (await rpc(w, "tools/list"))[1]["result"]["tools"]
            schema = next(t for t in listing if t["name"] == "get_questions")["inputSchema"]["properties"]
            assert "number" in schema
    arun(main())


def test_get_questions_bounds_the_overview_gives_detail_by_number_and_redacts(conn):
    async def main():
        async with server(conn) as w:
            long_options = tuple(f"option {i} " + "word " * 80 for i in range(15))
            w.agent_questions.insert_if_new(agent_question("a_big", title="long title " * 100, options=long_options))  # Q1
            w.agent_questions.insert_if_new(agent_question(
                "a_leak", title="use token=abcd1234secretvalue here", options=("Bearer abcdefgh12345678",)))  # Q2
            body, err = await call(w, "get_questions", root_id="r1")
            assert not err
            big = next(q for q in body["questions"] if q["question_id"] == "a_big")["items"][0]
            assert len(big["title"]) <= 500 and len(big["options"]) == 10 and all(len(o) <= 200 for o in big["options"])
            assert big["text_truncated"] is True  # the consultant is told that something was cut
            text = json.dumps(body)
            assert "abcd1234secretvalue" not in text and "abcdefgh12345678" not in text and "[REDACTED]" in text
            body, _ = await call(w, "get_questions", root_id="r1", number="Q1")  # one question: the detail
            full = body["questions"][0]["items"][0]
            assert len(full["options"]) == 15 and all(len(o) > 300 for o in full["options"]) and len(full["title"]) > 1000
            assert full["text_truncated"] is False
    arun(main())


def test_tool_crash_and_timeout_are_structured_not_stack_traces(conn, monkeypatch):
    async def main():
        async with server(conn) as w:
            async def boom(*a, **k):
                raise RuntimeError("secret internals /srv/x")
            monkeypatch.setattr(w.ctx, "list_agents", boom)
            body, err = await call(w, "list_agents", root_id="r1")
            assert err and body == {"error": "internal", "message": "internal error"}
            monkeypatch.setattr("cqb.mcp.server.CALL_TIMEOUT", 0.05)

            async def slow(*a, **k):
                await asyncio.sleep(1)
            monkeypatch.setattr(w.ctx, "list_agents", slow)
            body, err = await call(w, "list_agents", root_id="r1")
            assert err and body["error"] == "unavailable"
    arun(main())


def test_oversized_results_become_limit_exceeded(conn, monkeypatch):
    from cqb.mcp import tools
    monkeypatch.setattr(tools, "RESULT_MAX_BYTES", 200)
    result = tools.tool_result({"x": "y " * 250})
    assert result["isError"] and json.loads(result["content"][0]["text"])["error"] == "limit_exceeded"
