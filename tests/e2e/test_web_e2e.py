"""Web tools through the REAL PicoClaw v0.3.1, the real scope hook and the real Bridge process.

Run: .venv/bin/python -m pytest -q -m e2e tests/e2e/test_web_e2e.py   (deselected by the default run)
Same loopback-only setup as test_pico_e2e.py (mock model, fake Telegram, fake Codex daemon) with `[mcp] web_search`
on and a fake search endpoint on loopback. The one real internet access is a fetch of https://example.org (the test
is skipped without it); every private target must be refused by the hook before PicoClaw sees it.
"""

import asyncio
import contextlib
import json
import os
import socket
import subprocess
import sys

import pytest
from aiohttp import web

from e2e.test_pico_e2e import (
    MCP_TOKEN, MOCK, PICO_TOKEN, PICOCLAW, ROOT, VENV_PYTHON, _http_ok, _port_open, ask, free_port, render_home,
)
from fake_appserver import FakeAppServer, wait_until
from fake_telegram import CHAT_ID, OWNER, TOKEN, FakeTelegram

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(not (PICOCLAW.exists() and MOCK.exists() and VENV_PYTHON.exists()),
                       reason="needs spike/picoclaw-0.3.1, spike/scripts/mock_llm.py and the venv"),
]
KEY = "e2e-search-key-0123456789"


def _internet() -> bool:
    try:
        socket.create_connection(("example.org", 443), timeout=4).close()
        return True
    except OSError:
        return False


@pytest.mark.skipif(not _internet(), reason="needs access to example.org")
def test_web_tools_with_real_picoclaw(tmp_path):
    procs: list[subprocess.Popen] = []
    seen: list[tuple[str, dict]] = []  # what the fake search endpoint received: (authorization, body)

    def spawn(args, **kw):
        p = subprocess.Popen(args, stdout=kw.pop("out", subprocess.DEVNULL), stderr=subprocess.STDOUT, **kw)
        procs.append(p)
        return p

    async def search(request):
        seen.append((request.headers.get("Authorization", ""), await request.json()))
        return web.json_response({"results": [
            {"title": "PicoClaw docs", "url": "https://docs.example.org/picoclaw", "snippet": "Ultra-efficient assistant"},
            {"title": "bad scheme", "url": "javascript:alert(1)", "snippet": "x"},
        ]})

    async def main() -> None:
        app, tg = FakeAppServer(), FakeTelegram()
        await app.start()
        await tg.start()
        fake = web.Application()
        fake.router.add_post("/v1/search", search)
        runner = web.AppRunner(fake, access_log=None)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        search_port = runner.addresses[0][1]
        pico_port, mock_port, mcp_port = free_port(), free_port(), free_port()
        app.add_thread("r1", 2_000_000_000, name="FoxProfile")
        home = tmp_path / "home"
        home.mkdir()
        render_home(home, pico_port, mock_port, mcp_port)
        env = {**os.environ, "CQB_PICO_TOKEN": PICO_TOKEN, "CQB_MCP_TOKEN": MCP_TOKEN, "HOME": str(tmp_path), "E2E_SEARCH_KEY": KEY}
        spec = {"socket": app.path, "db": str(tmp_path / "bridge.db"), "chat_id": CHAT_ID, "owner": OWNER,
                "tg_token": TOKEN, "tg_base": tg.base_url, "mcp_port": mcp_port,
                "web_search_url": f"http://127.0.0.1:{search_port}/v1/search",
                "pico_url": f"ws://127.0.0.1:{pico_port}/pico/ws"}
        try:
            spawn([sys.executable, str(MOCK)], env={**env, "MOCK_PORT": str(mock_port), "MOCK_LOG": str(tmp_path / "mock.jsonl")})
            spawn([str(VENV_PYTHON), str(ROOT / "tests/e2e/run_bridge.py"), json.dumps(spec)],
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
            await wait_until(lambda: sum("Создать" in b["text"] for b in tg.buttons(reg_id)) == 1, 15)
            for b in [b for b in tg.buttons(reg_id) if "Создать" in b["text"]]:
                tg.push_callback(b["callback_data"], reg_id)
            await wait_until(lambda: tg.topic_named("FoxProfile"), 10)
            fox = tg.topic_named("FoxProfile")[0]
            await asyncio.sleep(2)  # the health check wants a hook heartbeat first

            # 1. the consultant is offered both web tools (PicoClaw's own search stays off: the Bridge's tool is the search)
            r = await ask(tg, fox, "hello fox")
            assert r.startswith("ECHO[hello fox]"), r
            first = json.loads(open(tmp_path / "mock.jsonl").readline())["body"]
            offered = {t["function"]["name"] for t in first["tools"]}
            assert {"web_fetch", "mcp_cqb_web_search"} <= offered and "web_search" not in offered, offered

            # 2. search: the key goes to the endpoint, never to the model; only title/url/snippet come back, bad links dropped
            r = await ask(tg, fox, 'CALL_TOOL mcp_cqb_web_search {"query":"PicoClaw github","max_results":2}')
            assert "PicoClaw docs" in r and "https://docs.example.org/picoclaw" in r, r
            assert "javascript:" not in r and KEY not in r, r
            assert seen == [(f"Bearer {KEY}", {"query": "PicoClaw github", "max_results": 2})], seen
            r = await ask(tg, fox, 'CALL_TOOL mcp_cqb_web_search {"query":"' + "0123456789abcdef" * 3 + '"}')
            assert "secret" in r and len(seen) == 1, r  # the hook refused a key-like query before the Bridge saw it

            # 3. fetch: a harmless public page works through the real gateway
            r = await ask(tg, fox, 'CALL_TOOL web_fetch {"url":"https://example.org/"}')
            assert '"status": 200' in r and "documentation examples" in r, r

            # 4. private and odd targets never leave the machine: the hook denies them with a reason
            for url in (f"http://127.0.0.1:{mcp_port}/mcp", "http://127.0.0.1:18790/health", "http://169.254.169.254/latest/",
                        "http://100.64.0.1/", "http://2130706433/", "http://[::1]/", "http://localhost/", "ftp://example.org/",
                        "https://user:pw@example.org/", "https://example.org/?token=abc"):
                r = await ask(tg, fox, 'CALL_TOOL web_fetch {"url":"%s"}' % url)
                print(url, "->", r[:140].replace("\n", " "))
                assert "allowed" in r and '"status"' not in r and "documentation examples" not in r, (url, r)

            # 5. the hook rejects argument names it does not know and the root_id trick does not reach web_fetch
            r = await ask(tg, fox, 'CALL_TOOL web_fetch {"url":"https://example.org/","headers":{"X":"1"}}')
            assert "unknown argument" in r and '"status"' not in r, r
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
            await runner.cleanup()
            await app.stop()
            await tg.stop()

    asyncio.run(main())
