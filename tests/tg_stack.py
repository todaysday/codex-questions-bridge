"""Test harness: the real Telegram layer + services + DAOs on a fake Telegram server."""

import asyncio
import contextlib

from cqb.dao.actions import ActionDao
from cqb.dao.agent_questions import AgentQuestionDao
from cqb.dao.bot_state import BotStateDao
from cqb.dao.consultants import ConsultantDao
from cqb.dao.questions import QuestionDao
from cqb.dao.threads import ThreadDao
from cqb.codex.events import QuestionRequested, RequestResolved
from cqb.domain.enums import ArchiveState
from cqb.domain.enums import RunState
from cqb.domain.models import QuestionItem, QuestionOption, Thread
from cqb.domain.status import Activity, Descendants, GoalSummary, RateLimits, ThreadSnapshot
from cqb.services.agent_questions import AgentQuestionService
from cqb.services.consultants import ConsultantService
from cqb.services.focus import QuestionFocus
from cqb.services.questions import QuestionService
from cqb.services.live_state import LiveState
from cqb.services.registry import RegistryQuery
from cqb.services.status import StatusService
from cqb.telegram.agent_cards import AgentQuestionCards
from cqb.telegram.api import BotApi
from cqb.telegram.consultant_view import render_opening
from cqb.telegram.control import ControlTopics, RegistryPublisher
from cqb.telegram.cards import QuestionCards
from cqb.telegram.keys import ActionKeys
from cqb.telegram.poller import Poller
from cqb.telegram.router import Router, RouterConfig
from cqb.telegram.status_post import StatusPoster
from cqb.telegram.topics import TelegramTopics
from fake_telegram import CHAT_ID, OWNER, TOKEN, FakeTelegram

NOW = 1_800_000_000


class StubClient:
    """Stands in for the app-server connection: records answers, accepts resume/unsubscribe."""

    def __init__(self):
        self.responses = []
        self.fail_respond = False

    async def call(self, method, params=None):
        return {"thread": {"status": {"activeFlags": ["waitingOnUserInput"]}}}

    async def respond(self, request_id, result):
        self.responses.append((request_id, result))


class AgentClientStub:
    """The app-server calls the agent-question service makes: items of each thread (oldest first) and sends."""

    def __init__(self):
        self.items: dict[str, list[dict]] = {}
        self.sent: list[tuple[str, str]] = []
        self.send_error: Exception | None = None
        self.echo = False  # a successful send appends the userMessage the daemon would store
        self.calls: list[str] = []

    async def call(self, method, params=None):
        self.calls.append(method)
        if method == "thread/read":
            return {"thread": {"id": params["threadId"]}}
        rows = [{"turnId": "u1", "item": i} for i in self.items.get(params["threadId"], [])][::-1]
        start = int(params.get("cursor") or 0)
        limit = params.get("limit") or 25
        nxt = str(start + limit) if start + limit < len(rows) else None
        return {"data": rows[start:start + limit], "nextCursor": nxt}

    async def send_user_message(self, thread_id, text):
        if self.send_error:
            raise self.send_error
        self.sent.append((thread_id, text))
        if self.echo:
            self.items.setdefault(thread_id, []).append(user_item(f"u{len(self.sent)}", text))
        return "steer"


def agent_item(item_id, questions):
    return {"type": "agentMessage", "id": item_id, "text": "t", "phase": "final_answer", "delivery": "async",
            "questions": questions}


def user_item(item_id, text="hi"):
    return {"type": "userMessage", "id": item_id, "content": [{"type": "text", "text": text, "text_elements": []}]}


class StatusSourceStub:
    """In-memory StatusSource: no daemon, no aiohttp. `fail` makes every read return None."""

    def __init__(self):
        self.threads: dict[str, ThreadSnapshot] = {}
        self.children: dict[str, tuple[ThreadSnapshot, ...]] = {}
        self.more = False
        self.turn_start: int | None = None
        self.activities: dict[str, Activity] = {}  # thread id -> what its latest turn says; absent = unreadable
        self.item_reads: list[str] = []
        self.goals: dict[str, GoalSummary] = {}
        self.limits: RateLimits | None = RateLimits(30, NOW + 3600, 10080)
        self.fail = False
        self.calls: list[str] = []

    async def thread(self, thread_id):
        self.calls.append("thread")
        return None if self.fail else self.threads.get(thread_id)

    async def descendants(self, root_id, limit):
        self.calls.append("descendants")
        return None if self.fail else Descendants(self.children.get(root_id, ()), self.more)

    async def activity(self, thread_id, with_item):
        self.calls.append("turn")
        if with_item:
            self.item_reads.append(thread_id)
        found = self.activities.get(thread_id)
        if found is None and self.turn_start is not None and thread_id == "r1":
            return Activity(True, self.turn_start, None, None)
        return found

    async def goal(self, thread_id):
        self.calls.append("goal")
        return self.goals.get(thread_id)

    async def rate_limits(self):
        self.calls.append("limits")
        return self.limits


def snapshot(tid, state=RunState.IDLE, updated=NOW - 60, parent=None, **kw):
    fields = dict(model="gpt-6", effort="low", nickname=None, role=None, name=f"Thread {tid}", path=None)
    fields.update(kw)
    return ThreadSnapshot(tid, parent, state, updated_at=updated, **fields)


class Relay:
    def __init__(self):
        self.got = []

    async def relay(self, consultant, text, lead=None):
        self.got.append((consultant.root_thread_id, (lead or "") + text))


class Recorder:
    def __init__(self):
        self.polls = 0

    def poke(self):
        self.polls += 1


class Clock:
    def __init__(self, now=NOW):
        self.now = now

    def __call__(self):
        return self.now


def thread(tid, updated=NOW - 60, name=None, state=ArchiveState.ACTIVE, parent=None):
    return Thread("s1", tid, parent or tid, parent, "cli", name or f"Thread {tid}", "/w", updated, state, 1, 1)


class Stack:
    def __init__(self, conn, fake: FakeTelegram, token=TOKEN, narrator=None, summary_timeout=300, open_link=""):
        self.conn, self.fake = conn, fake
        self.clock = Clock()
        self.state = BotStateDao(conn)
        self.threads = ThreadDao(conn)
        self.api = BotApi(token, base_url=fake.base_url, sleep=self._nosleep, rng=lambda: 0)
        self.client = StubClient()
        self.relay = Relay()
        self.status_source = StatusSourceStub()
        self.live = LiveState(
            clock=self.clock, on_working_flip=lambda t: self.registry.is_root(t) and self.publisher.request_refresh()
        )
        self.status_service = StatusService(
            self.status_source, self.live, lambda path: None, "s1", narrator, self.clock, summary_timeout=summary_timeout)
        self.status_cards = StatusPoster(self.api, self.status_service, CHAT_ID, "s1", typing_seconds=0.01, open_link=open_link)
        self.inventory = Recorder()
        self.consultant_dao = ConsultantDao(conn)
        self.question_dao = QuestionDao(conn)
        self.agent_dao = AgentQuestionDao(conn)
        self.agent_client = AgentClientStub()
        self.questions = QuestionService(self.client, self.question_dao, self.threads, "s1")
        self.agent = AgentQuestionService(self.agent_client, self.agent_dao, self.threads, "s1", clock=self.clock)
        self.keys = ActionKeys(ActionDao(conn), "s1", self.clock)
        self.registry = RegistryQuery(self.threads, self.consultant_dao, self.question_dao, "s1", 30, self.clock, live=self.live,
                                       agent_questions=self.agent_dao)
        self.topics = ControlTopics(self.api, self.state, CHAT_ID, "📋 Треды Codex")
        self.tg_topics = TelegramTopics(self.api, CHAT_ID)
        self.publisher = RegistryPublisher(
            self.api, self.topics, self.state, self.keys, self.registry, CHAT_ID,
            interval=0.05, clock=self.clock,
        )
        self.cards = QuestionCards(
            self.api, self.keys, self.questions, self.registry, CHAT_ID, self.publisher.request_refresh, self.clock
        )
        self.agent_cards = AgentQuestionCards(
            self.api, self.keys, self.agent, self.registry, CHAT_ID, self.publisher.request_refresh, self.clock
        )
        self.consultants = ConsultantService(
            self.consultant_dao, self.threads, self.tg_topics, "s1", CHAT_ID, self.clock, persist_delay=0,
            on_open=lambda root: (self.cards.mark_root(root), self.agent_cards.mark_root(root)),
            opening=lambda root: (row := self.registry.describe(root)) and render_opening(row, "s1"),
        )
        self.focus = QuestionFocus(self.consultant_dao, self.questions, self.agent, "s1")
        self.router = Router(
            RouterConfig(CHAT_ID, frozenset({OWNER}), "cqb_test_bot"), self.api, self.keys, self.topics,
            self.publisher, self.cards, self.agent_cards, self.consultants, self.registry,
            self.questions, self.agent, self.relay, self.focus, self.status_cards, "s1", self.inventory.poke, self.clock,
        )
        self.poller = Poller(self.api, self.state, self.router.handle, poll_timeout=0)
        self.questions.subscribe(on_question=self.cards.on_question, on_resolved=self.cards.on_resolved)
        self.agent.subscribe(on_question=self.agent_cards.on_question, on_closed=self.agent_cards.on_closed)

    @staticmethod
    async def _nosleep(delay):
        await asyncio.sleep(0)

    async def start(self, *threads):
        await self.api.start()
        self.threads.upsert_many(threads)
        await self.topics.ensure()
        await self.publisher.refresh(force=True)

    async def pump(self):
        """Fetch pending updates and handle them (one polling round)."""
        offset = self.state.get_int("poll_offset") or 0
        await self.poller.process(await self.api.get_updates(offset, timeout=0))

    async def status_done(self):
        """Wait until every running status collect has posted (or failed)."""
        while self.status_cards._running:
            await asyncio.sleep(0.01)

    async def settle(self):
        """Apply coalesced registry refreshes and dirty cards, like the background loops would."""
        await self.cards.process()
        await self.agent_cards.process()
        if self.publisher._wake.is_set():
            force, self.publisher._force = self.publisher._force, False
            self.publisher._wake.clear()
            await self.publisher.refresh(force)

    async def ask(self, thread_id, item_id, items=None, request_id=1, secret=False, turn="u1"):
        items = items if items is not None else (
            QuestionItem("pick", "Storage", "Where store profile settings?", False,
                         (QuestionOption("Existing table"), QuestionOption("Separate table"))),
        )
        await self.questions.handle(QuestionRequested(
            request_id, thread_id, turn, item_id, () if secret else items, secret, True))

    def open_ids(self):
        return [r[0] for r in self.conn.execute("SELECT question_id FROM questions ORDER BY rowid")]

    def qid_of(self, item_id):
        return self.conn.execute("SELECT question_id FROM questions WHERE tool_use_id=?", (item_id,)).fetchone()[0]

    async def resolve(self, thread_id, request_id=1):
        await self.questions.handle(RequestResolved(request_id, thread_id))

    @property
    def control_topic(self):
        return self.topics.topic_id()

    @property
    def registry_message(self):
        return self.state.get_int("registry.message_id")

    def control_messages(self):
        """Every live message the bot left in the control topic (must be exactly the registry)."""
        return self.fake.live_messages(self.control_topic)

    async def open_consultant(self, root):
        """Create the consultant of `root` and return its topic id."""
        await self.router._create(root)
        return self.consultant_dao.get("s1", root).tg_topic_id

    def card_of(self, qid):
        return self.question_dao.get(qid).card_message_id


@contextlib.asynccontextmanager
async def make_stack(conn, *threads, **kw):
    fake = FakeTelegram()
    await fake.start()
    stack = Stack(conn, fake, **kw)
    try:
        await stack.start(*threads)
        yield stack
    finally:
        await stack.status_cards.close()
        await stack.api.close()
        await fake.stop()
