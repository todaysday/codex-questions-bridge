import asyncio

import pytest

from cqb import main as cqb_main
from cqb.config import CodexConfig, Config, DbConfig, TelegramConfig
from cqb.dao import db
from fake_appserver import FakeAppServer, wait_until
from fake_telegram import CHAT_ID, OWNER, TOKEN, FakeTelegram

QUESTION = [{"id": "pick", "header": "Storage", "question": "Where store settings?", "isOther": False,
             "isSecret": False, "options": [{"label": "A", "description": ""}, {"label": "B", "description": ""}]}]


def make_config(tmp_path, socket):
    return Config(
        CodexConfig(socket, "local", 30, 36500), DbConfig(str(tmp_path / "c.db")),
        TelegramConfig(CHAT_ID, "📋 Треды Codex", "📥 Inbox", (OWNER,), 5),
    )


def test_run_end_to_end_with_graceful_stop(tmp_path):
    async def main():
        app, tg = FakeAppServer(), FakeTelegram()
        await app.start()
        await tg.start()
        app.add_thread("t1", 2_000_000_000, name="FoxProfile")
        stop = asyncio.Event()
        task = asyncio.create_task(cqb_main.run(
            make_config(tmp_path, app.path), stop, token=TOKEN, telegram_base_url=tg.base_url, rss_interval=60))
        check = db.connect(str(tmp_path / "c.db"))
        control = lambda: tg.topic_named("📋 Треды Codex")[0]  # noqa: E731
        await wait_until(lambda: tg.topic_named("📋 Треды Codex") and tg.topic_named("📥 Inbox"))
        await wait_until(lambda: "thread/list" in app.methods())
        await wait_until(lambda: check.execute("SELECT archive_state FROM codex_threads").fetchone())
        tg.push_message("/threads", control())  # the periodic refresh is slow; ask for it
        registry = lambda: next(m for m in tg.live_messages(control()))  # noqa: E731
        await wait_until(lambda: "FoxProfile" in registry()["text"] or False)
        assert tg.pinned

        # a live question becomes an Inbox card; the owner answers from Telegram
        await app.ask("t1", "u1", "i1", QUESTION)
        inbox = tg.topic_named("📥 Inbox")[0]
        await wait_until(lambda: any("Where store settings?" in m["text"] for m in tg.live_messages(inbox)))
        card_id = next(i for i, m in tg.messages.items() if m["thread_id"] == inbox)
        tg.push_callback(tg.button(card_id, "B.")["callback_data"], card_id)
        await wait_until(lambda: "Выбрано: B" in tg.messages[card_id]["text"])
        tg.push_callback(tg.button(card_id, "Отправить")["callback_data"], card_id)
        await wait_until(lambda: app.answers == [{"answers": {"pick": {"answers": ["B"]}}}])
        await wait_until(lambda: "✅ Доставлено" in tg.messages[card_id]["text"])

        # manual consultant creation from the registry
        reg_id = next(i for i, m in tg.messages.items() if m["thread_id"] == control())
        await wait_until(lambda: any("Создать" in b["text"] for b in tg.buttons(reg_id)))
        tg.push_callback(tg.button(reg_id, "Создать")["callback_data"], reg_id)
        await wait_until(lambda: tg.topic_named("FoxProfile"))
        assert check.execute("SELECT state FROM consultants").fetchone()[0] == "open"
        assert len(tg.topic_named("FoxProfile")) == 1

        stop.set()
        await asyncio.wait_for(task, 3)
        await app.stop()
        await tg.stop()
        check.close()
    asyncio.run(main())


def test_bad_token_fails_fast(tmp_path):
    async def main():
        app, tg = FakeAppServer(), FakeTelegram()
        await app.start()
        await tg.start()
        with pytest.raises(Exception, match="401"):
            await asyncio.wait_for(cqb_main.run(
                make_config(tmp_path, app.path), asyncio.Event(), token="1:wrong", telegram_base_url=tg.base_url), 3)
        await app.stop()
        await tg.stop()
    asyncio.run(main())


def test_rss_reads_proc():
    assert cqb_main.rss_mib() > 1
