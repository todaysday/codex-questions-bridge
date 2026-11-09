"""The whole process with the consultant chat wired: fake daemon, fake Telegram, fake PicoClaw."""

import asyncio
import socket

import aiohttp
import pytest

from cqb import main as cqb_main
from cqb.dao import db
from cqb.dao.threads import SourceDao, ThreadDao
from cqb.domain.enums import ArchiveState
from cqb.domain.models import Source, Thread
from cqb.config import CodexConfig, Config, ConfigError, ConsultantTokens, DbConfig, McpConfig, PicoConfig, TelegramConfig
from cqb.services.relay import NOT_CONFIGURED
from fake_appserver import FakeAppServer, wait_until
from fake_pico import FakePico, create
from fake_pico import TOKEN as PICO_TOKEN
from fake_telegram import CHAT_ID, OWNER, TOKEN, FakeTelegram

MCP_TOKEN = "mcp-main-test-token"


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def make_config(tmp_path, socket_path, pico=None, mcp_port=0):
    return Config(
        CodexConfig(socket_path, "local", 30, 36500), DbConfig(str(tmp_path / "c.db")),
        TelegramConfig(CHAT_ID, "📋 Треды Codex", (OWNER,), 5), pico=pico, mcp=McpConfig(mcp_port),
    )


def seed(db_path, thread_id="t1", name="FoxProfile"):
    """The thread is known before startup, so the very first registry render already offers the button."""
    conn = db.connect(db_path)
    db.migrate(conn)
    SourceDao(conn).upsert(Source("local", label="local"))
    ThreadDao(conn).upsert_many([Thread("local", thread_id, thread_id, None, "cli", name, "/w", 2_000_000_000,
                                        ArchiveState.ACTIVE, 1, 1)])
    conn.close()


async def create_consultant(tg, name):
    await wait_until(lambda: tg.topic_named("📋 Треды Codex"))
    control = tg.topic_named("📋 Треды Codex")[0]
    reg = None
    tg.push_message("/threads", control)
    await wait_until(lambda: any(m["thread_id"] == control for m in tg.messages.values()))
    reg = next(i for i, m in tg.messages.items() if m["thread_id"] == control)
    await wait_until(lambda: any("Создать" in b["text"] for b in tg.buttons(reg)))
    tg.push_callback(tg.button(reg, "Создать")["callback_data"], reg)
    await wait_until(lambda: tg.topic_named(name))
    return tg.topic_named(name)[0]


def test_run_with_consultant_chat_replies_in_topic_and_serves_mcp(tmp_path, monkeypatch):
    async def main():
        async def answer(sid, text):
            return [create(f"echo:{text}", context_usage={"used_tokens": 123300, "total_tokens": 475000})]

        async def compactions(sid):
            return 2

        monkeypatch.setattr(cqb_main, "observed_compactions", compactions)

        app, tg, pico = FakeAppServer(), FakeTelegram(), FakePico(answer)
        for s in (app, tg, pico):
            await s.start()
        app.add_thread("t1", 2_000_000_000, name="FoxProfile")
        seed(str(tmp_path / "c.db"))
        port = free_port()
        stop = asyncio.Event()
        cfg = make_config(tmp_path, app.path, PicoConfig(url=pico.url, idle_seconds=60), port)
        task = asyncio.create_task(cqb_main.run(
            cfg, stop, token=TOKEN, telegram_base_url=tg.base_url, rss_interval=60,
            consultant_tokens=ConsultantTokens(PICO_TOKEN, MCP_TOKEN)))
        try:
            topic = await create_consultant(tg, "FoxProfile")
            control = tg.topic_named("📋 Треды Codex")[0]
            tg.push_message("/threads", control)
            await wait_until(lambda: any("📈 осталось 70% нед." in m["text"] for m in tg.live_messages(control)))
            assert "account/rateLimits/read" in app.methods()
            tg.push_message("why option A?", topic)
            await wait_until(lambda: any(m["text"] == "echo:why option A?" for m in tg.live_messages(topic)))
            await wait_until(lambda: any("ctx 123.3k/475k (26%) · 🗜️2" in m["text"] for m in tg.live_messages(topic)))
            opening = next(i for i, m in tg.messages.items() if m.get("thread_id") == topic and "ctx 123.3k" in m["text"])
            assert [b["text"] for b in tg.buttons(opening)] == ["❓ Вопросы", "📊 Статус", "📜 Показать тред"]
            tg.push_message("/clear", topic)  # escaped on the way to PicoClaw
            await wait_until(lambda: any(m["text"] == "echo:User says: /clear" for m in tg.live_messages(topic)))
            key = next(iter(pico.sockets))
            assert key.startswith("cqb-") and pico.merged == 0 and pico.auth_failures == 0
            # the MCP endpoint of the same process, with its own token
            async with aiohttp.ClientSession() as http:
                url = f"http://127.0.0.1:{port}"
                auth = {"Authorization": f"Bearer {MCP_TOKEN}"}
                async with http.get(f"{url}/internal/scope/{key}", headers=auth) as r:
                    assert await r.json() == {"source_id": "local", "root_id": "t1"}
                async with http.post(f"{url}/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers=auth) as r:
                    assert len((await r.json())["result"]["tools"]) == 5
                async with http.post(f"{url}/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                                     headers={"Authorization": f"Bearer {PICO_TOKEN}"}) as r:
                    assert r.status == 401  # the pico token is not the MCP token
                async with http.post(f"{url}/mcp", headers=auth, json={"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                     "params": {"name": "get_thread_overview", "arguments": {"root_id": "t1"}}}) as r:
                    assert '"name":"FoxProfile"' in (await r.json())["result"]["content"][0]["text"]
            assert "thread/read" in app.methods()  # read through the daemon client, whitelist intact
            stop.set()
            await asyncio.wait_for(task, 5)
            assert pico.open_count == 0  # shutdown closed the sockets
        finally:
            stop.set()
            await asyncio.gather(task, return_exceptions=True)
            for s in (pico, app, tg):
                await s.stop()
    asyncio.run(main())


def test_status_snapshot_gets_its_summary_from_a_slot_session_and_never_touches_the_consultant(tmp_path):
    async def main():
        seen = {}
        port = free_port()
        auth = {"Authorization": f"Bearer {MCP_TOKEN}"}

        async def agent(sid, text):
            async with aiohttp.ClientSession() as http:  # the scope hook's question, asked while the request runs
                async with http.get(f"http://127.0.0.1:{port}/internal/scope/{sid}", headers=auth) as r:
                    seen[sid] = (r.status, await r.json() if r.status == 200 else None)
            await asyncio.sleep(0.3)  # the agent "works": the card must not be posted yet
            return [create("В процессе: Корень правит тесты.\nДальше: прогнать e2e", "m1")]

        app, tg, pico = FakeAppServer(), FakeTelegram(), FakePico(agent)
        for srv in (app, tg, pico):
            await srv.start()
        app.add_thread("t1", 2_000_000_000, name="FoxProfile")
        seed(str(tmp_path / "c.db"))
        stop = asyncio.Event()
        cfg = make_config(tmp_path, app.path, PicoConfig(url=pico.url, idle_seconds=60), port)
        task = asyncio.create_task(cqb_main.run(
            cfg, stop, token=TOKEN, telegram_base_url=tg.base_url, rss_interval=60,
            consultant_tokens=ConsultantTokens(PICO_TOKEN, MCP_TOKEN)))
        try:
            topic = await create_consultant(tg, "FoxProfile")
            tg.push_message("/status", topic)
            await asyncio.sleep(0.15)
            assert not any("Субагенты</b>" in m["text"] for m in tg.live_messages(topic))  # nothing yet, no placeholder
            await wait_until(lambda: any(p.get("message_thread_id") == topic for p in tg.methods("sendChatAction")))
            await wait_until(lambda: any("Субагенты</b>" in m["text"] for m in tg.live_messages(topic)), 5)
            card = next(m["text"] for m in tg.live_messages(topic) if "Субагенты</b>" in m["text"])
            assert "🔛<b>:</b> Корень правит тесты.\n\n🔜<b>:</b> прогнать e2e" in card and "резюме недоступно" not in card
            assert list(pico.sockets) == ["cqb-status-1"] and pico.texts("cqb-status-1")[0].startswith("Status request")
            assert seen == {"cqb-status-1": (200, {"source_id": "local", "root_id": "t1"})}  # bound during the call
            async with aiohttp.ClientSession() as http:  # and unbound afterwards
                async with http.get(f"http://127.0.0.1:{port}/internal/scope/cqb-status-1", headers=auth) as r:
                    assert r.status == 404
            assert [sid for sid, _ in pico.sent] == ["cqb-status-1"]  # no message ever reached the consultant's session
        finally:
            stop.set()
            await asyncio.gather(task, return_exceptions=True)
            for srv in (pico, app, tg):
                await srv.stop()
    asyncio.run(main())


def test_run_without_pico_section_says_the_chat_is_not_configured(tmp_path):
    async def main():
        app, tg = FakeAppServer(), FakeTelegram()
        await app.start()
        await tg.start()
        app.add_thread("t1", 2_000_000_000, name="FoxProfile")
        seed(str(tmp_path / "c.db"))
        stop = asyncio.Event()
        task = asyncio.create_task(cqb_main.run(make_config(tmp_path, app.path), stop, token=TOKEN,
                                                telegram_base_url=tg.base_url, rss_interval=60))
        try:
            topic = await create_consultant(tg, "FoxProfile")
            tg.push_message("hello?", topic)
            await wait_until(lambda: any(m["text"] == NOT_CONFIGURED for m in tg.live_messages(topic)))
        finally:
            stop.set()
            await asyncio.wait_for(task, 5)
            await app.stop()
            await tg.stop()
    asyncio.run(main())


def test_pico_section_without_tokens_fails_before_anything_starts(tmp_path):
    async def main():
        with pytest.raises(ConfigError, match="CQB_PICO_TOKEN"):
            await cqb_main.run(make_config(tmp_path, "/nonexistent", PicoConfig()), asyncio.Event(), token=TOKEN)
    asyncio.run(main())


def test_main_fails_fast_on_missing_consultant_tokens(tmp_path, monkeypatch, capsys):
    cfg = tmp_path / "c.toml"
    cfg.write_text('[codex]\nsocket="s"\n[db]\npath="x"\n[telegram]\nchat_id=-100\nallowed_user_ids=[1]\n[pico]\n')
    monkeypatch.setenv("CQB_TELEGRAM_TOKEN", "1:x")
    monkeypatch.delenv("CQB_PICO_TOKEN", raising=False)
    monkeypatch.delenv("CQB_MCP_TOKEN", raising=False)
    assert cqb_main.main(["--config", str(cfg)]) == 2
    assert "CQB_PICO_TOKEN" in capsys.readouterr().err
    # and a config without [pico] needs neither token
    cfg.write_text('[codex]\nsocket="s"\n[db]\npath="x"\n[telegram]\nchat_id=-100\nallowed_user_ids=[1]\n')
    monkeypatch.setattr(cqb_main.asyncio, "run", lambda coro: coro.close())
    assert cqb_main.main(["--config", str(cfg)]) == 0


def test_restart_adds_thread_button_to_existing_opening(tmp_path):
    async def main():
        app, tg = FakeAppServer(), FakeTelegram()
        await app.start()
        await tg.start()
        app.add_thread("t1", 2_000_000_000, name="FoxProfile")
        seed(str(tmp_path / "c.db"))
        cfg = make_config(tmp_path, app.path)
        stop = asyncio.Event()
        task = asyncio.create_task(cqb_main.run(cfg, stop, token=TOKEN, telegram_base_url=tg.base_url))
        try:
            topic = await create_consultant(tg, "FoxProfile")
            await wait_until(lambda: any("ctx" in m["text"] for m in tg.live_messages(topic)))
            opening = next(i for i, m in tg.messages.items() if m["thread_id"] == topic and "ctx" in m["text"])
            stop.set()
            await asyncio.wait_for(task, 5)
            original = tg.messages[opening]["text"]
            tg.messages[opening]["markup"]["inline_keyboard"][0].pop()
            stop = asyncio.Event()
            task = asyncio.create_task(cqb_main.run(cfg, stop, token=TOKEN, telegram_base_url=tg.base_url))
            await wait_until(lambda: any("Показать тред" in b["text"] for b in tg.buttons(opening)))
            assert tg.messages[opening]["text"] == original
        finally:
            stop.set()
            await asyncio.gather(task, return_exceptions=True)
            await tg.stop()
            await app.stop()
    asyncio.run(main())
