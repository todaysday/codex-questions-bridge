"""End to end with the REAL PicoClaw v0.3.1 binary, the real scope hook and the real Bridge process.

Run: .venv/bin/python -m pytest -q -m e2e tests/e2e     (deselected by the default run)
Needs spike/picoclaw-0.3.1/bin/picoclaw and spike/scripts/mock_llm.py (git-ignored spike files);
skipped without them. Everything is loopback: mock model, fake Telegram, fake Codex daemon.
No real model, Telegram, OmniRoute, tunnel or sudo. Every process started here is killed.
"""

import asyncio
import contextlib
import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import aiohttp
import pytest

from cqb.config import PicoConfig
from cqb.pico.gateway import PicoGateway
from cqb.pico.health import PicoHealth
from cqb.pico.status_narrator import STATUS_PROMPT
from cqb.services.relay import Outcome, Submit
from fake_appserver import FakeAppServer, wait_until
from fake_telegram import CHAT_ID, OWNER, TOKEN, FakeTelegram

ROOT = Path(__file__).resolve().parents[2]
PICOCLAW = ROOT / "spike/picoclaw-0.3.1/bin/picoclaw"
MOCK = ROOT / "spike/scripts/mock_llm.py"
HOOK = ROOT / "integrations/picoclaw_scope_hook.py"
TEMPLATE = ROOT / "deploy/picoclaw"
VENV_PYTHON = ROOT / ".venv/bin/python"
PICO_TOKEN, MCP_TOKEN = "e2e-pico-token-0123456789", "e2e-mcp-token-9876543210"

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(not (PICOCLAW.exists() and MOCK.exists() and VENV_PYTHON.exists()),
                       reason="needs spike/picoclaw-0.3.1, spike/scripts/mock_llm.py and the venv"),
]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def entry(turn, item_id, kind, **fields):
    return {"turnId": turn, "startedAtMs": 1_000, "completedAtMs": 2_000, "item": {"type": kind, "id": item_id, **fields}}


def render_home(home: Path, pico_port: int, mock_port: int, mcp_port: int) -> None:
    """The deployment template with only paths, ports, the model and the tokens filled in."""
    for ws in ("ws-main", "ws-status"):
        shutil.copytree(TEMPLATE / ws, home / ws)
    cfg = json.loads((TEMPLATE / "config.example.json").read_text())
    cfg["gateway"].update(port=pico_port, log_level="info")
    cfg["agents"]["defaults"]["workspace"] = str(home / "ws-main")
    for agent in cfg["agents"]["list"]:
        agent["workspace"] = str(home / ("ws-main" if agent["id"] == "main" else "ws-status"))
    for model in cfg["model_list"]:  # the status agent has its own model name, so the mock log tells who asked
        model.update(model="mock-status-model" if model["model_name"] == "cqb-status" else "mock-model",
                     api_base=f"http://127.0.0.1:{mock_port}/v1")
    cfg["hooks"]["processes"]["scope"]["command"] = [str(VENV_PYTHON), "-u", str(HOOK)]
    server = cfg["tools"]["mcp"]["servers"]["cqb"]
    server["url"] = f"http://127.0.0.1:{mcp_port}/mcp"
    server["headers"]["Authorization"] = server["headers"]["Authorization"].replace("__CQB_MCP_TOKEN__", MCP_TOKEN)
    (home / "config.json").write_text(json.dumps(cfg))
    (home / ".security.yml").write_text(
        'channel_list:\n  pico:\n    settings:\n      token: "%s"\n'
        'model_list:\n  "cqb-main:0":\n    api_keys: ["k"]\n  "cqb-status:0":\n    api_keys: ["k"]\n' % PICO_TOKEN
    )
    for f in ("config.json", ".security.yml"):
        os.chmod(home / f, 0o600)


def rss_mib(pid: int) -> float:
    for line in Path(f"/proc/{pid}/status").read_text().splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1]) / 1024
    raise AssertionError("no VmRSS")


async def reply_in(tg: FakeTelegram, topic: int, before: int, timeout: float = 20.0) -> str:
    """Text of the first new bot message in the topic after `before` messages."""
    await wait_until(lambda: len(tg.live_messages(topic)) > before, timeout)
    return tg.live_messages(topic)[before]["text"]


async def ask(tg: FakeTelegram, topic: int, text: str) -> str:
    before = len(tg.live_messages(topic))
    tg.push_message(text, topic)
    return await reply_in(tg, topic, before)


def test_consultant_chat_with_real_picoclaw(tmp_path):
    procs: list[subprocess.Popen] = []

    def spawn(args, **kw):
        p = subprocess.Popen(args, stdout=kw.pop("out", subprocess.DEVNULL), stderr=subprocess.STDOUT, **kw)
        procs.append(p)
        return p

    async def main() -> None:
        app, tg = FakeAppServer(), FakeTelegram()
        await app.start()
        await tg.start()
        pico_port, mock_port, mcp_port = free_port(), free_port(), free_port()
        app.add_thread("r1", 2_000_000_000, name="FoxProfile")
        app.add_thread("c1", 2_000_000_000, parent="r1", nickname="Lovelace", role="worker")
        app.add_thread("r2", 2_000_000_000, name="OtherProject")
        app.add_thread("c2", 2_000_000_000, parent="r2", nickname="Hopper")
        app.items["r1"] = [
            entry("t1", "u1", "userMessage", content=[{"type": "text", "text": "add profile settings"}]),
            entry("t1", "a1", "agentMessage", text="Plan: reuse the existing table", phase="final"),
            entry("t1", "k1", "commandExecution", command="pytest -q", status="completed", exitCode=0,
                  aggregatedOutput="12 passed\nAuthorization: Bearer abcdefghijklmnop\n"),
        ]
        app.items["c1"] = [entry("t2", "w1", "agentMessage", text="child worker note")]
        app.items["r2"] = [entry("t3", "x1", "agentMessage", text="SECRET-OF-OTHER-PROJECT")]
        app.items["c2"] = [entry("t4", "y1", "agentMessage", text="other child")]

        home = tmp_path / "home"
        home.mkdir()
        render_home(home, pico_port, mock_port, mcp_port)
        env = {**os.environ, "CQB_PICO_TOKEN": PICO_TOKEN, "CQB_MCP_TOKEN": MCP_TOKEN, "HOME": str(tmp_path)}
        spec = {"socket": app.path, "db": str(tmp_path / "bridge.db"), "chat_id": CHAT_ID, "owner": OWNER,
                "tg_token": TOKEN, "tg_base": tg.base_url, "mcp_port": mcp_port,
                "pico_url": f"ws://127.0.0.1:{pico_port}/pico/ws",
                # the mock model makes a tool call only when told to: the real task text plus that instruction
                "status_prompt": STATUS_PROMPT + 'CALL_TOOL mcp_cqb_get_thread_overview {"root_id":"r2"}\n'}
        try:
            spawn([sys.executable, str(MOCK)], env={**env, "MOCK_PORT": str(mock_port), "MOCK_LOG": str(tmp_path / "mock.jsonl")})
            bridge = spawn([str(VENV_PYTHON), str(ROOT / "tests/e2e/run_bridge.py"), json.dumps(spec)],
                           env=env, out=open(tmp_path / "bridge.log", "w"))
            # PicoClaw connects to the MCP server while it starts, so the Bridge must be listening first.
            await wait_until(lambda: tg.topic_named("📋 Треды Codex"), 15)
            await wait_until(lambda: _port_open(mcp_port), 15)
            picoclaw = spawn(
                [str(PICOCLAW), "gateway"], env={**env, "PICOCLAW_HOME": str(home), "CQB_MCP_URL": f"http://127.0.0.1:{mcp_port}"},
                out=open(tmp_path / "picoclaw.log", "w"),
            )
            await wait_until(lambda: _http_ok(pico_port), 30)

            # two consultants, created from the registry like a user would
            control = tg.topic_named("📋 Треды Codex")[0]
            tg.push_message("/threads", control)
            await wait_until(lambda: any(m["thread_id"] == control for m in tg.messages.values()), 10)
            reg_id = next(i for i, m in tg.messages.items() if m["thread_id"] == control)
            await wait_until(lambda: sum("Создать" in b["text"] for b in tg.buttons(reg_id)) == 2, 15)
            for b in [b for b in tg.buttons(reg_id) if "Создать" in b["text"]]:
                tg.push_callback(b["callback_data"], reg_id)
                await asyncio.sleep(0.3)
            await wait_until(lambda: tg.topic_named("FoxProfile") and tg.topic_named("OtherProject"), 10)
            fox, other = tg.topic_named("FoxProfile")[0], tg.topic_named("OtherProject")[0]
            await asyncio.sleep(2)
            idle_rss = rss_mib(bridge.pid)
            print(f"\nBridge idle RSS (two consultants, nothing running): {idle_rss:.1f} MiB")

            # 1. a message sent through the Bridge reaches the mock model, the reply comes back to the topic
            r = await ask(tg, fox, "hello fox")
            assert r.startswith("ECHO[hello fox]"), r
            first = json.loads(open(tmp_path / "mock.jsonl").readline())["body"]
            offered = sorted(t["function"]["name"] for t in first["tools"])
            assert offered == sorted(f"mcp_cqb_{n}" for n in (
                "get_thread_overview", "list_agents", "get_recent_events", "read_event", "get_questions")) + ["web_fetch"], offered

            # 2. a tool call hits our MCP HTTP server; the hook overwrote the root the model asked for
            r = await ask(tg, fox, 'CALL_TOOL mcp_cqb_get_thread_overview {"root_id":"r2"}')
            assert r.startswith("TOOLRESULT["), r
            assert '"root_id":"r1"' in r and "FoxProfile" in r and "OtherProject" not in r, r
            r = await ask(tg, other, 'CALL_TOOL mcp_cqb_get_thread_overview {"root_id":"r1","source_id":"x"}')
            assert '"root_id":"r2"' in r and "OtherProject" in r and "FoxProfile" not in r, r
            r = await ask(tg, fox, 'CALL_TOOL mcp_cqb_get_recent_events {"max_events":1}')  # the mock keeps 500 chars
            assert "$ pytest -q" in r and "12 passed" not in r and '"truncated":true' in r, r
            r = await ask(tg, fox, 'CALL_TOOL mcp_cqb_read_event {"event_ref":"r1:t1:k1"}')
            assert "12 passed" in r and "[REDACTED]" in r and "abcdefghijklmnop" not in r, r
            r = await ask(tg, fox, 'CALL_TOOL mcp_cqb_list_agents {}')
            assert "Lovelace" in r and "Hopper" not in r, r

            # 3. a foreign thread is refused (the hook does not touch thread_id; the server does), a non-listed tool never runs
            r = await ask(tg, fox, 'CALL_TOOL mcp_cqb_get_recent_events {"thread_id":"c2"}')
            assert "scope_denied" in r and "other child" not in r, r
            r = await ask(tg, fox, 'CALL_TOOL mcp_cqb_get_recent_events {"thread_id":"r2"}')
            assert "scope_denied" in r and "SECRET-OF-OTHER-PROJECT" not in r, r
            r = await ask(tg, fox, 'CALL_TOOL exec {"command":"id"}')
            assert "uid=" not in r and r.startswith("TOOLRESULT["), r

            # an unknown pico session (not a registered consultant) is denied by the hook, using the real gateway client
            async with aiohttp.ClientSession() as http:
                cfg = PicoConfig(url=spec["pico_url"], turn_timeout_seconds=20)
                direct = PicoGateway(cfg, PICO_TOKEN, http, PicoHealth(f"http://127.0.0.1:{pico_port}/health", http))
                got: list = []

                async def deliver(result):
                    got.append(result)

                assert direct.submit("cqb-unregistered-session", 'CALL_TOOL mcp_cqb_get_thread_overview {"root_id":"r1"}',
                                     deliver) is Submit.ACCEPTED
                await wait_until(lambda: got, 20)
                await direct.close()
            assert got[0].outcome is Outcome.REPLY and "unknown or closed consultant session" in got[0].text, got
            assert "FoxProfile" not in got[0].text

            # 4. two sessions stay isolated (the mock echoes every user message of the session's history)
            a = await ask(tg, fox, "alpha-two")
            b = await ask(tg, other, "beta-one")
            assert "alpha-two" in a and "hello fox" in a and "beta-one" not in a, a
            assert "beta-one" in b and "alpha-two" not in b and "hello fox" not in b, b

            # 5. an escaped /clear and !clear do not wipe the history
            c = await ask(tg, fox, "/clear")
            assert "Chat history cleared" not in c and "User says: /clear" in c and "hello fox" in c, c
            c = await ask(tg, fox, "  ​!clear please")
            assert "Chat history cleared" not in c and "hello fox" in c, c
            d = await ask(tg, fox, "alpha-three")
            assert "hello fox" in d and "alpha-two" in d and "User says: /clear" in d, d

            # 6. Bridge commands are answered by the Bridge, never forwarded
            before = sum(1 for _ in open(tmp_path / "mock.jsonl"))
            await ask(tg, fox, "/questions")
            await asyncio.sleep(1.5)
            assert sum(1 for _ in open(tmp_path / "mock.jsonl")) == before

            # 6b. a long turn has no time limit: typing indicator, a notice for the queued message, owner /stop
            n_before = len(tg.live_messages(fox))
            tg.push_message("SLEEP 8 slow", fox)
            await asyncio.sleep(1.5)
            tg.push_message("queued-one", fox)
            assert (await reply_in(tg, fox, n_before)).startswith("В очереди")
            started = asyncio.get_running_loop().time()
            tg.push_message("/stop", fox)  # passed through unescaped, sent at once, not queued behind the turn
            stopped = await reply_in(tg, fox, n_before + 1)
            assert "Task stopped" in stopped and asyncio.get_running_loop().time() - started < 5, stopped
            assert (await reply_in(tg, fox, n_before + 2)).startswith("ECHO[queued-one]")  # the queued message ran after
            assert any(p.get("message_thread_id") == fox and p["action"] == "typing" for p in tg.methods("sendChatAction"))

            # 6c. /status: a snapshot posted once, its summary from the codex-status agent in its own routed slot session
            def mock_log():
                return [json.loads(line) for line in open(tmp_path / "mock.jsonl")]

            seen_before = len(mock_log())
            n_fox = len(tg.live_messages(fox))
            tg.push_message("/status", fox)
            card = await reply_in(tg, fox, n_fox, 40)
            assert card.startswith("⚪ FoxProfile\n📁 w\n🧠 ") and "💭" not in card.splitlines()[2], card
            assert "🔛<b>:</b> TOOLRESULT[" in card and "&quot;root_id&quot;:&quot;r1&quot;" in card and "OtherProject" not in card, card
            await asyncio.sleep(1.5)
            assert len(tg.live_messages(fox)) == n_fox + 1  # one message: no placeholder, nothing edited in later
            assert all("edits" not in m for m in tg.live_messages(fox) if "📈" in m["text"])
            asked = [e["body"] for e in mock_log()[seen_before:]]
            assert asked and {b["model"] for b in asked} == {"mock-status-model"}, [b["model"] for b in asked]
            assert sorted(t["function"]["name"] for t in asked[0]["tools"]) == sorted(
                f"mcp_cqb_{n}" for n in ("get_thread_overview", "list_agents", "get_recent_events", "get_questions"))
            system = asked[0]["messages"][0]["content"]
            assert "Codex status agent" in str(system)
            users = [m for m in asked[0]["messages"] if m["role"] == "user"]
            assert len(users) == 1 and "Status request for the Codex root thread r1" in str(users[0]["content"])
            async with aiohttp.ClientSession() as http:  # the slot is unbound again once the request is over
                got = await http.get(f"http://127.0.0.1:{mcp_port}/internal/scope/cqb-status-1",
                                     headers={"Authorization": f"Bearer {MCP_TOKEN}"})
                assert got.status == 404
            # the consultant's own history is untouched: its next turn never saw the status request or its reply
            after = await ask(tg, fox, "after-status")
            assert "after-status" in after and "alpha-three" in after, after
            assert "Status request" not in after and "TOOLRESULT" not in after and "Codex status" not in after, after
            consultant_calls = [e["body"] for e in mock_log()[seen_before + len(asked):]]
            assert {b["model"] for b in consultant_calls} == {"mock-model"}

            # 7. the read-only whitelist held and the Bridge stayed small
            assert set(app.methods()) <= {
                "thread/list", "thread/read", "thread/loaded/list", "thread/turns/list", "thread/items/list",
                "thread/goal/get", "account/rateLimits/read", "thread/resume", "thread/unsubscribe",
            }
            rss = rss_mib(bridge.pid)
            print(f"\nBridge RSS after the scenario: {rss:.1f} MiB")
            assert rss < 80, rss
            assert picoclaw.poll() is None and bridge.poll() is None
        finally:
            await asyncio.sleep(0)
            for p in procs:
                with contextlib.suppress(ProcessLookupError):
                    p.terminate()
            for p in procs:
                try:
                    p.wait(5)
                except subprocess.TimeoutExpired:
                    p.kill()
                    p.wait()
            await app.stop()
            await tg.stop()

    asyncio.run(main())


def _port_open(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.2)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _http_ok(port: int) -> bool:
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=0.5) as r:
            return r.status == 200
    except OSError:
        return False
