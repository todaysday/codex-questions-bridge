import argparse
import asyncio
import gc
import logging
import os
import signal
import sys
from collections.abc import Callable

from cqb.codex.appserver import AppServerClient
from cqb.codex.stream import dispatch
from cqb.config import Config, ConfigError, load_config, read_token
from cqb.dao import db
from cqb.dao.actions import ActionDao
from cqb.dao.bot_state import BotStateDao
from cqb.dao.consultants import ConsultantDao
from cqb.dao.questions import QuestionDao
from cqb.dao.threads import SourceDao, ThreadDao
from cqb.domain.models import Question, Source
from cqb.services.consultants import ConsultantService
from cqb.services.inventory import InventoryService
from cqb.services.questions import QuestionService
from cqb.services.registry import RegistryQuery
from cqb.services.relay import CountingRelay
from cqb.telegram.api import API_BASE, BotApi
from cqb.telegram.control import ControlTopics, RegistryPublisher
from cqb.telegram.inbox import InboxCards
from cqb.telegram.keys import ActionKeys
from cqb.telegram.poller import Poller
from cqb.telegram.router import Router, RouterConfig
from cqb.telegram.topics import TelegramTopics

log = logging.getLogger("cqb")
RSS_LOG_SECONDS = 300


def rss_mib() -> float:
    with open("/proc/self/statm") as f:
        pages = int(f.read().split()[1])
    return pages * os.sysconf("SC_PAGE_SIZE") / 1048576


async def _log_rss(interval: float, counts: Callable[[], tuple[int, int]]) -> None:
    while True:
        consultants, pending = counts()
        log.info("rss=%.1f MiB consultants_open=%d questions_pending=%d", rss_mib(), consultants, pending)
        await asyncio.sleep(interval)


async def _log_question(q: Question) -> None:
    # Ids and counts only: question text may be sensitive and never goes to logs.
    log.info(
        "question %s thread=%s root=%s items=%d secret=%s",
        q.question_id, q.origin_thread_id, q.root_thread_id, len(q.items), q.is_secret,
    )


async def _log_resolved(q: Question) -> None:
    log.info("question %s -> %s", q.question_id, q.lifecycle.value)


async def run(
    config: Config,
    stop: asyncio.Event,
    *,
    token: str,
    telegram_base_url: str = API_BASE,
    rss_interval: float = RSS_LOG_SECONDS,
) -> None:
    conn = db.connect(config.db.path)
    db.migrate(conn)
    source_id = config.codex.source_id
    tg = config.telegram
    SourceDao(conn).upsert(Source(source_id, label=source_id))
    threads = ThreadDao(conn)
    consultant_dao = ConsultantDao(conn)
    question_dao = QuestionDao(conn)
    state = BotStateDao(conn)

    client = AppServerClient(config.codex.socket)
    questions = QuestionService(client, question_dao, threads, source_id)
    inventory = InventoryService(
        client, threads, source_id,
        poll_interval=config.codex.poll_interval_seconds,
        recency_days=config.codex.recency_days,
        on_waiting=questions.watch_thread,
    )
    api = BotApi(token, base_url=telegram_base_url)
    await api.start()
    try:
        me = await api.get_me()  # fails fast on a bad token
        keys = ActionKeys(ActionDao(conn), source_id)
        registry = RegistryQuery(threads, consultant_dao, question_dao, source_id, config.codex.recency_days)
        topics = ControlTopics(api, state, tg.chat_id, tg.control_topic_name, tg.inbox_topic_name)
        tg_topics = TelegramTopics(api, tg.chat_id)
        consultants = ConsultantService(consultant_dao, threads, tg_topics, source_id, tg.chat_id)
        publisher = RegistryPublisher(
            api, topics, state, keys, registry, tg.chat_id, interval=tg.registry_refresh_seconds
        )
        inbox = InboxCards(api, topics, keys, questions, registry, tg.chat_id, publisher.request_refresh)
        router = Router(
            RouterConfig(tg.chat_id, frozenset(tg.allowed_user_ids), me.username),
            api, keys, topics, publisher, inbox, consultants, tg_topics, registry, questions,
            CountingRelay(), source_id, inventory.poke,
        )
        questions.subscribe(on_question=_log_question, on_resolved=_log_resolved)
        questions.subscribe(on_question=inbox.on_question, on_resolved=inbox.on_resolved)

        await topics.ensure()
        consultants.recover()
        keys.purge_expired()
        await publisher.refresh(force=True)
        gc.collect()
        gc.freeze()

        poller = Poller(api, state, router.handle)
        tasks = [
            asyncio.create_task(client.run(), name="appserver"),
            asyncio.create_task(dispatch(client, [inventory.handle, questions.handle]), name="dispatch"),
            asyncio.create_task(inventory.run(), name="inventory"),
            asyncio.create_task(poller.run(), name="telegram-poll"),
            asyncio.create_task(publisher.run(before=consultants.reconcile), name="registry"),
            asyncio.create_task(inbox.run(), name="inbox"),
            asyncio.create_task(
                _log_rss(rss_interval, lambda: (consultants.open_count(), questions.open_count())), name="rss"
            ),
        ]
        watch_stop = asyncio.create_task(stop.wait())
        try:
            done, _ = await asyncio.wait([*tasks, watch_stop], return_when=asyncio.FIRST_COMPLETED)
            for t in done - {watch_stop}:
                t.result()  # a crashed task must not be silent
        finally:
            for t in (*tasks, watch_stop):
                t.cancel()
            await asyncio.gather(*tasks, watch_stop, return_exceptions=True)
    finally:
        await api.close()
        conn.close()


async def _amain(config: Config, token: str) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    await run(config, stop, token=token)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cqb")
    parser.add_argument("--config", default=os.environ.get("CQB_CONFIG", "config.toml"))
    args = parser.parse_args(argv)
    logging.basicConfig(
        stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    try:
        config = load_config(args.config)
        token = read_token(os.environ)
    except ConfigError as exc:
        print(f"cqb: config error: {exc}", file=sys.stderr)
        return 2
    asyncio.run(_amain(config, token))
    log.info("stopped")
    return 0
