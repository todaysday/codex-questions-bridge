"""Project files through the REAL PicoClaw v0.3.1, the real scope hook and the real Bridge process.

Run: .venv/bin/python -m pytest -q -m e2e tests/e2e/test_project_files_e2e.py   (deselected by the default run)
Same loopback-only setup as test_pico_e2e.py (mock model, fake Telegram, fake Codex daemon) with
`[mcp] project_files = true` and the root thread's cwd pointing at a temporary project.
"""

import asyncio
import contextlib
import json
import os
import subprocess
import sys

import pytest

from e2e.test_pico_e2e import (
    HOOK, MCP_TOKEN, MOCK, PICO_TOKEN, PICOCLAW, ROOT, VENV_PYTHON, _http_ok, _port_open, ask, free_port, render_home, rss_mib,
)
from fake_appserver import FakeAppServer, wait_until
from fake_telegram import CHAT_ID, OWNER, TOKEN, FakeTelegram

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(not (PICOCLAW.exists() and MOCK.exists() and VENV_PYTHON.exists()),
                       reason="needs spike/picoclaw-0.3.1, spike/scripts/mock_llm.py and the venv"),
]
assert HOOK.exists()


def test_project_file_tools_with_real_picoclaw(tmp_path):
    procs: list[subprocess.Popen] = []

    def spawn(args, **kw):
        p = subprocess.Popen(args, stdout=kw.pop("out", subprocess.DEVNULL), stderr=subprocess.STDOUT, **kw)
        procs.append(p)
        return p

    project, outside = tmp_path / "work" / "proj", tmp_path / "outside"
    (project / "src").mkdir(parents=True)
    outside.mkdir()
    (project / "README.md").write_text("hello project\nsecond line\n")
    (project / "src" / "app.py").write_text("print('app')\npassword = 'hunter2hunter2'\n")
    (project / ".env").write_text("DB_PASSWORD=PLANTED-ENV-SECRET\n")
    (outside / "o.txt").write_text("OUTSIDE-CONTENT")
    os.symlink(outside / "o.txt", project / "link.txt")

    async def main() -> None:
        app, tg = FakeAppServer(), FakeTelegram()
        await app.start()
        await tg.start()
        pico_port, mock_port, mcp_port = free_port(), free_port(), free_port()
        app.add_thread("r1", 2_000_000_000, name="FoxProfile", cwd=str(project))
        app.add_thread("r2", 2_000_000_000, name="OtherProject", cwd=str(outside))
        home = tmp_path / "home"
        home.mkdir()
        render_home(home, pico_port, mock_port, mcp_port)
        env = {**os.environ, "CQB_PICO_TOKEN": PICO_TOKEN, "CQB_MCP_TOKEN": MCP_TOKEN, "HOME": str(tmp_path)}
        spec = {"socket": app.path, "db": str(tmp_path / "bridge.db"), "chat_id": CHAT_ID, "owner": OWNER,
                "tg_token": TOKEN, "tg_base": tg.base_url, "mcp_port": mcp_port, "project_files": True,
                "pico_url": f"ws://127.0.0.1:{pico_port}/pico/ws"}
        try:
            spawn([sys.executable, str(MOCK)], env={**env, "MOCK_PORT": str(mock_port), "MOCK_LOG": str(tmp_path / "mock.jsonl")})
            bridge = spawn([str(VENV_PYTHON), str(ROOT / "tests/e2e/run_bridge.py"), json.dumps(spec)],
                           env=env, out=open(tmp_path / "bridge.log", "w"))
            await wait_until(lambda: tg.topic_named("📋 Треды Codex"), 15)
            await wait_until(lambda: _port_open(mcp_port), 15)
            spawn([str(PICOCLAW), "gateway"], env={**env, "PICOCLAW_HOME": str(home), "CQB_MCP_URL": f"http://127.0.0.1:{mcp_port}"},
                  out=open(tmp_path / "picoclaw.log", "w"))
            await wait_until(lambda: _http_ok(pico_port), 30)

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

            await asyncio.sleep(2)  # the health check wants a hook heartbeat first

            # 1. with the flag on the main agent is offered the two tools (the real AGENT.md allowlist let them through)
            r = await ask(tg, fox, "hello fox")
            assert r.startswith("ECHO[hello fox]"), r
            first = json.loads(open(tmp_path / "mock.jsonl").readline())["body"]
            offered = sorted(t["function"]["name"] for t in first["tools"])
            assert offered == sorted(f"mcp_cqb_{n}" for n in (
                "get_thread_overview", "list_agents", "get_recent_events", "read_event", "get_questions",
                "read_project_file", "list_project_dir")) + ["web_fetch"], offered

            # 2. real calls: the hook (agent id `main`) approves, the Bridge reads inside the root thread's cwd
            r = await ask(tg, fox, 'CALL_TOOL mcp_cqb_read_project_file {"path":"README.md"}')
            assert "1: hello project" in r and "2: second line" in r, r
            r = await ask(tg, fox, 'CALL_TOOL mcp_cqb_read_project_file {"path":"src/app.py"}')
            assert "1: print" in r and "hunter2hunter2" not in r and "REDACTED" in r, r
            r = await ask(tg, fox, 'CALL_TOOL mcp_cqb_list_project_dir {}')
            assert "README.md" in r and "src" in r and '"hidden":1' in r and ".env" not in r, r
            r = await ask(tg, fox, 'CALL_TOOL mcp_cqb_read_project_file {"path":".env"}')
            assert "denied" in r and "PLANTED-ENV-SECRET" not in r, r
            r = await ask(tg, fox, 'CALL_TOOL mcp_cqb_read_project_file {"path":"link.txt"}')
            assert "symlink_refused" in r and "OUTSIDE-CONTENT" not in r, r
            r = await ask(tg, fox, 'CALL_TOOL mcp_cqb_read_project_file {"path":"../outside/o.txt"}')
            assert "invalid_path" in r and "OUTSIDE-CONTENT" not in r, r
            r = await ask(tg, fox, 'CALL_TOOL mcp_cqb_read_project_file {"path":"/etc/passwd"}')
            assert "invalid_path" in r and "root:" not in r, r

            # 3. the hook overwrites root_id (the model asks for r2) and rejects argument names it does not know
            r = await ask(tg, fox, 'CALL_TOOL mcp_cqb_read_project_file {"root_id":"r2","path":"o.txt"}')
            assert "OUTSIDE-CONTENT" not in r and "not_found" in r, r
            r = await ask(tg, other, 'CALL_TOOL mcp_cqb_read_project_file {"path":"o.txt"}')  # r2's own cwd: allowed
            assert "OUTSIDE-CONTENT" in r, r
            r = await ask(tg, fox, 'CALL_TOOL mcp_cqb_read_project_file {"path":"README.md","max_bytes":99999}')
            assert "unknown argument" in r and "hello project" not in r, r

            rss = rss_mib(bridge.pid)
            print(f"\nBridge RSS after the project-file scenario: {rss:.1f} MiB")
            assert rss < 80, rss
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
