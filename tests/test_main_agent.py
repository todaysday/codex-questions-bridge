"""Whole process: an agent-message question found by the watcher, answered from Telegram, delivered when seen."""

import asyncio
import logging

from cqb import main as cqb_main
from cqb.config import QuestionsConfig
from fake_appserver import FakeAppServer, agent_message, wait_until
from fake_telegram import TOKEN, FakeTelegram
from test_main import make_config

QUESTIONS = [{"title": "Where store settings?", "options": ["A", "B"]}]


def test_agent_question_end_to_end(tmp_path, caplog):
    async def main():
        caplog.set_level(logging.INFO)
        app, tg = FakeAppServer(), FakeTelegram()
        await app.start()
        await tg.start()
        app.add_thread("t1", 2_000_000_000, name="FoxProfile")
        app.items["t1"] = [{"turnId": "u1", "item": agent_message("call_1", QUESTIONS, text="PRIVATE-TEXT")}]
        stop = asyncio.Event()
        config = make_config(tmp_path, app.path)
        config = type(config)(config.codex, config.db, config.telegram, questions=QuestionsConfig(5))
        task = asyncio.create_task(cqb_main.run(config, stop, token=TOKEN, telegram_base_url=tg.base_url, rss_interval=0.2))
        control = lambda: tg.topic_named("📋 Треды Codex")[0]  # noqa: E731
        registry = lambda: next(m for m in tg.live_messages(control()))  # noqa: E731
        await wait_until(lambda: tg.topic_named("📋 Треды Codex"))
        await wait_until(lambda: "FoxProfile" in registry()["text"], 5)

        # a status broadcast of the root makes the watcher read its items: the registry row gets its marker
        await app.set_status("t1", ["waitingOnApproval"])
        await app.set_status("t1", None)
        await wait_until(lambda: "❓ FoxProfile" in registry()["text"], 5)
        assert not [m for m in tg.messages.values() if "Where store settings?" in m["text"]]  # no consultant: no card

        reg_id = next(i for i, m in tg.messages.items() if m["thread_id"] == control())
        tg.push_callback(tg.button(reg_id, "Создать")["callback_data"], reg_id)
        await wait_until(lambda: tg.topic_named("FoxProfile"))
        fox = tg.topic_named("FoxProfile")[0]
        await wait_until(lambda: any("Where store settings?" in m["text"] for m in tg.live_messages(fox)), 5)
        card = next(i for i, m in tg.messages.items() if m["thread_id"] == fox and "Where store" in m["text"])
        tg.push_callback(tg.button(card, "B")["callback_data"], card)
        await wait_until(lambda: "Выбрано: B" in tg.messages[card]["text"])
        tg.push_callback(tg.button(card, "Отправить")["callback_data"], card)
        await wait_until(lambda: "turn/start" in app.methods())
        [start] = [p for m, p in app.calls if m == "turn/start"]
        assert set(start) == {"threadId", "input"} and start["threadId"] == "t1"
        text = start["input"][0]["text"]
        assert text.startswith("<send_user_message_question_reply>\n[{") and '"answer":"B"' in text
        await wait_until(lambda: "✅ Доставлено" in tg.messages[card]["text"], 8)  # seen as a userMessage item
        await wait_until(lambda: "❓" not in registry()["text"])
        assert "thread/resume" in app.methods() and app.conns[0].subscribed == set()  # unsubscribed again

        stop.set()
        await asyncio.wait_for(task, 3)
        await app.stop()
        await tg.stop()
    asyncio.run(main())
    log = caplog.text
    assert "agent_questions_pending=1" in log and "agent_questions_pending=0" in log
    assert "PRIVATE-TEXT" not in log and "Where store" not in log
