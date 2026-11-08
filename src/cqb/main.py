import argparse
import asyncio
import gc
import logging
import os
import signal
import sys
from collections.abc import Awaitable, Callable

import aiohttp

from cqb.codex.appserver import AppServerClient
from cqb.codex.history import HistoryReader
from cqb.codex.rollout_count import CompactionCounter
from cqb.codex.rollout_tail import read_usage
from cqb.codex.status_reader import DaemonStatusReader
from cqb.codex.stream import dispatch
from cqb.config import (
    Config, ConfigError, ConsultantTokens, WebSearchEndpoint, load_config, read_consultant_tokens, read_token,
    read_web_search_endpoint,
)
from cqb.dao import db
from cqb.dao.actions import ActionDao
from cqb.dao.agent_questions import AgentQuestionDao
from cqb.dao.bot_state import BotStateDao
from cqb.dao.consultants import ConsultantDao
from cqb.dao.questions import QuestionDao
from cqb.dao.threads import SourceDao, ThreadDao
from cqb.domain.models import AgentQuestion, Consultant, Question, Source
from cqb.mcp.server import McpServer
from cqb.mcp.tools import build_tools
from cqb.pico.compactions import observed_compactions
from cqb.pico.gateway import PicoGateway
from cqb.pico.health import PicoHealth, health_url
from cqb.pico.status_narrator import STATUS_PROMPT, PicoStatusNarrator
from cqb.services.agent_questions import AgentQuestionService
from cqb.services.consultants import ConsultantService
from cqb.services.context import ContextService
from cqb.services.project_files import ProjectFiles
from cqb.services.web_search import WebSearch
from cqb.services.inventory import InventoryService
from cqb.services.live_state import LiveState
from cqb.services.focus import QuestionFocus
from cqb.services.questions import QuestionService
from cqb.services.registry import RegistryQuery
from cqb.services.relay import ConsultantRelay
from cqb.services.scope import SessionScopes
from cqb.services.status import StatusNarrator, StatusService
from cqb.telegram.agent_cards import AgentQuestionCards
from cqb.telegram.api import API_BASE, BadRequest, BotApi
from cqb.telegram.present import Presenter
from cqb.telegram.cards import QuestionCards
from cqb.telegram.consultant_view import render_opening
from cqb.telegram.control import ControlTopics, RegistryPublisher
from cqb.telegram.keys import ActionKeys
from cqb.telegram.poller import Poller
from cqb.telegram.router import Router, RouterConfig
from cqb.telegram.status_post import StatusPoster
from cqb.telegram.topics import TelegramTopics

log = logging.getLogger("cqb")
RSS_LOG_SECONDS = 300


def rss_mib() -> float:
    with open("/proc/self/statm") as f:
        pages = int(f.read().split()[1])
    return pages * os.sysconf("SC_PAGE_SIZE") / 1048576


async def _log_rss(interval: float, counts: Callable[[], tuple[int, int, int]]) -> None:
    while True:
        consultants, pending, agent_pending = counts()
        log.info(
            "rss=%.1f MiB consultants_open=%d questions_pending=%d agent_questions_pending=%d",
            rss_mib(), consultants, pending, agent_pending,
        )
        await asyncio.sleep(interval)


async def _log_question(q: Question) -> None:
    # Ids and counts only: question text may be sensitive and never goes to logs.
    log.info(
        "question %s thread=%s root=%s items=%d secret=%s",
        q.question_id, q.origin_thread_id, q.root_thread_id, len(q.items), q.is_secret,
    )


async def _log_agent_question(q: AgentQuestion) -> None:
    # Ids and counts only: question text may be sensitive and never goes to logs.
    log.info("agent question %s thread=%s root=%s index=%d", q.agent_question_id, q.thread_id, q.root_thread_id, q.index)


async def _log_agent_closed(q: AgentQuestion) -> None:
    log.info("agent question %s -> %s", q.agent_question_id, q.state.value)


async def _log_resolved(q: Question) -> None:
    log.info("question %s -> %s", q.question_id, q.lifecycle.value)


async def run(
    config: Config,
    stop: asyncio.Event,
    *,
    token: str,
    telegram_base_url: str = API_BASE,
    rss_interval: float = RSS_LOG_SECONDS,
    consultant_tokens: ConsultantTokens | None = None,
    web_search: WebSearchEndpoint | None = None,
    status_prompt: str = STATUS_PROMPT,  # a test seam: the Status Agent's task text
) -> None:
    """Without a [pico] section (or its tokens) the bot runs and consultant topics say they are not configured."""
    if config.pico is not None and consultant_tokens is None:
        raise ConfigError("[pico] is configured but CQB_PICO_TOKEN / CQB_MCP_TOKEN were not provided")
    conn = db.connect(config.db.path)
    db.migrate(conn)
    source_id = config.codex.source_id
    tg = config.telegram
    SourceDao(conn).upsert(Source(source_id, label=source_id))
    threads = ThreadDao(conn)
    consultant_dao = ConsultantDao(conn)
    question_dao = QuestionDao(conn)
    agent_dao = AgentQuestionDao(conn)
    state = BotStateDao(conn)

    client = AppServerClient(config.codex.socket)
    questions = QuestionService(client, question_dao, threads, source_id)
    agent_questions = AgentQuestionService(
        client, agent_dao, threads, source_id, scan_seconds=config.questions.agent_scan_seconds
    )
    api = BotApi(token, base_url=telegram_base_url)
    closers: list[Callable[[], Awaitable[None]]] = [api.close]
    gateway: PicoGateway | None = None
    await api.start()
    try:
        me = await api.get_me()  # fails fast on a bad token
        keys = ActionKeys(ActionDao(conn), source_id)
        # registry and publisher are bound below; the callback only runs on later broadcasts
        live = LiveState(on_working_flip=lambda thread_id: registry.is_root(thread_id) and publisher.request_refresh())
        registry = RegistryQuery(
            threads, consultant_dao, question_dao, source_id, config.codex.recency_days, live=live,
            agent_questions=agent_dao,
        )
        present = Presenter(api, tg.rich_messages)
        topics = ControlTopics(present, state, tg.chat_id, tg.control_topic_name)
        tg_topics = TelegramTopics(present, tg.chat_id)
        publisher = RegistryPublisher(
            present, topics, state, keys, registry, tg.chat_id, interval=tg.registry_refresh_seconds,
            rows=tg.registry_rows_per_page,
        )
        cards = QuestionCards(present, keys, questions, registry, tg.chat_id, publisher.request_refresh)
        agent_cards = AgentQuestionCards(present, keys, agent_questions, registry, tg.chat_id, publisher.request_refresh)

        def cards_for_root(root: str) -> None:
            cards.mark_root(root)
            agent_cards.mark_root(root)

        def opening_text(root: str) -> str | None:
            row = registry.describe(root)
            return render_opening(row, source_id) if row else None

        async def update_opening_usage(consultant: Consultant, usage: tuple[int, int]) -> None:
            if consultant.opening_message_id is None:
                return
            row = registry.describe(consultant.root_thread_id)
            if row:
                count = await observed_compactions(consultant.pico_session_key)
                text = render_opening(row, source_id, usage, count)
                try:
                    await api.edit_message_text(tg.chat_id, consultant.opening_message_id, text)
                except BadRequest as exc:
                    if not exc.not_modified:
                        raise

        consultants = ConsultantService(
            consultant_dao, threads, tg_topics, source_id, tg.chat_id, on_open=cards_for_root, opening=opening_text
        )
        inventory = InventoryService(
            client, threads, source_id,
            poll_interval=config.codex.poll_interval_seconds,
            recency_days=config.codex.recency_days,
            on_waiting=questions.watch_thread,
            on_change=publisher.request_refresh,
        )
        home = config.codex.codex_home
        reader = DaemonStatusReader(client)
        mcp: McpServer | None = None
        narrator: StatusNarrator | None = None
        if config.pico is not None and consultant_tokens is not None:
            http = aiohttp.ClientSession()
            closers.append(http.close)
            health = PicoHealth(health_url(config.pico.url), http)
            gateway = PicoGateway(config.pico, consultant_tokens.pico, http, health)
            context = ContextService(
                consultant_dao, threads, question_dao, reader, HistoryReader(client), source_id, agent_dao
            )
            scopes = SessionScopes(consultants.scope_for_session)
            files = ProjectFiles(config.mcp, context.check_scope, reader)
            web = WebSearch(config.mcp, context.check_scope, web_search, http)
            mcp = McpServer(
                consultant_tokens.mcp, build_tools(context, files, web), scopes.lookup, health.beat, port=config.mcp.port,
            )
            narrator = PicoStatusNarrator(
                gateway, scopes, config.pico.session_prefix, config.status.slots, status_prompt
            )
        else:
            log.info("consultant chat is not configured: no [pico] section")
        status = StatusPoster(
            api, StatusService(
                reader, live, lambda path: read_usage(path, home), source_id, narrator,
                summary_timeout=config.status.timeout_seconds, compactions=CompactionCounter(home),
            ),
            tg.chat_id, source_id, open_link=config.status.open_link,
        )
        closers.append(status.close)
        focus = QuestionFocus(consultant_dao, questions, agent_questions, source_id)
        router = Router(
            RouterConfig(tg.chat_id, frozenset(tg.allowed_user_ids), me.username),
            present, keys, topics, publisher, cards, agent_cards, consultants, registry, questions,
            agent_questions,
            ConsultantRelay(gateway, tg_topics, context=focus, on_usage=update_opening_usage),
            focus, status, source_id, inventory.poke,
        )
        closers.append(router.close)
        questions.subscribe(on_question=_log_question, on_resolved=_log_resolved)
        questions.subscribe(on_question=cards.on_question, on_resolved=cards.on_resolved)
        agent_questions.subscribe(on_question=_log_agent_question, on_closed=_log_agent_closed)
        agent_questions.subscribe(on_question=agent_cards.on_question, on_closed=agent_cards.on_closed)

        if mcp is not None:
            await mcp.start()
            closers.append(mcp.stop)
        await topics.ensure()
        consultants.recover()
        keys.purge_expired()
        await publisher.refresh(force=True)
        gc.collect()
        gc.freeze()

        poller = Poller(api, state, router.handle)
        tasks = [
            asyncio.create_task(client.run(), name="appserver"),
            asyncio.create_task(dispatch(client, [inventory.handle, questions.handle, agent_questions.handle, live.handle]), name="dispatch"),
            asyncio.create_task(inventory.run(), name="inventory"),
            asyncio.create_task(poller.run(), name="telegram-poll"),
            asyncio.create_task(publisher.run(before=consultants.reconcile), name="registry"),
            asyncio.create_task(cards.run(), name="cards"),
            asyncio.create_task(agent_cards.run(), name="agent-cards"),
            asyncio.create_task(agent_questions.run(), name="agent-questions"),
            *([asyncio.create_task(gateway.run(), name="pico-gateway")] if gateway else []),
            asyncio.create_task(
                _log_rss(rss_interval, lambda: (consultants.open_count(), questions.open_count(), agent_questions.open_count())), name="rss"
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
        if gateway is not None:
            await gateway.close()
        for close in reversed(closers):
            await close()
        conn.close()


async def _amain(
    config: Config, token: str, consultant_tokens: ConsultantTokens | None, web_search: WebSearchEndpoint | None,
) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    await run(config, stop, token=token, consultant_tokens=consultant_tokens, web_search=web_search)


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
        consultant_tokens = read_consultant_tokens(os.environ) if config.pico is not None else None
        web_search = read_web_search_endpoint(os.environ) if config.pico is not None and config.mcp.web_search else None
    except ConfigError as exc:
        print(f"cqb: config error: {exc}", file=sys.stderr)
        return 2
    asyncio.run(_amain(config, token, consultant_tokens, web_search))
    log.info("stopped")
    return 0
