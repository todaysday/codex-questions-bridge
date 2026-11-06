"""Test harness: the real Telegram layer + services + DAOs on a fake Telegram server."""

import asyncio
import contextlib

from cqb.dao.actions import ActionDao
from cqb.dao.bot_state import BotStateDao
from cqb.dao.consultants import ConsultantDao
from cqb.dao.questions import QuestionDao
from cqb.dao.threads import ThreadDao
from cqb.codex.events import QuestionRequested, RequestResolved
from cqb.domain.enums import ArchiveState
from cqb.domain.enums import RunState
from cqb.domain.models import QuestionItem, QuestionOption, Thread
from cqb.domain.status import Descendants, GoalSummary, RateLimits, ThreadSnapshot
from cqb.services.consultants import ConsultantService
from cqb.services.questions import QuestionService
from cqb.services.live_state import LiveState
from cqb.services.registry import RegistryQuery
from cqb.services.status import StatusService
from cqb.telegram.api import BotApi
from cqb.telegram.control import ControlTopics, RegistryPublisher
from cqb.telegram.inbox import InboxCards
from cqb.telegram.keys import ActionKeys
from cqb.telegram.poller import Poller
from cqb.telegram.router import Router, RouterConfig
from cqb.telegram.status_card import StatusCards
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


class StatusSourceStub:
    """In-memory StatusSource: no daemon, no aiohttp. `fail` makes every read return None."""

    def __init__(self):
        self.threads: dict[str, ThreadSnapshot] = {}
        self.children: dict[str, tuple[ThreadSnapshot, ...]] = {}
        self.more = False
        self.turn_start: int | None = None
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

    async def running_turn_start(self, thread_id):
        self.calls.append("turn")
        return self.turn_start

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

    async def relay(self, consultant, text):
        self.got.append((consultant.root_thread_id, text))


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
    def __init__(self, conn, fake: FakeTelegram, token=TOKEN):
        self.conn, self.fake = conn, fake
        self.clock = Clock()
        self.state = BotStateDao(conn)
        self.threads = ThreadDao(conn)
        self.api = BotApi(token, base_url=fake.base_url, sleep=self._nosleep, rng=lambda: 0)
        self.client = StubClient()
        self.relay = Relay()
        self.status_source = StatusSourceStub()
        self.live = LiveState(clock=self.clock)
        self.status_service = StatusService(self.status_source, self.live, lambda path: None, "s1", clock=self.clock)
        self.status_cards = StatusCards(self.api, self.state, self.status_service, CHAT_ID, "s1")
        self.inventory = Recorder()
        self.consultant_dao = ConsultantDao(conn)
        self.question_dao = QuestionDao(conn)
        self.questions = QuestionService(self.client, self.question_dao, self.threads, "s1")
        self.keys = ActionKeys(ActionDao(conn), "s1", self.clock)
        self.registry = RegistryQuery(self.threads, self.consultant_dao, self.question_dao, "s1", 30, self.clock)
        self.topics = ControlTopics(self.api, self.state, CHAT_ID, "📋 Треды Codex", "📥 Inbox")
        self.tg_topics = TelegramTopics(self.api, CHAT_ID)
        self.consultants = ConsultantService(
            self.consultant_dao, self.threads, self.tg_topics, "s1", CHAT_ID, self.clock, persist_delay=0
        )
        self.publisher = RegistryPublisher(
            self.api, self.topics, self.state, self.keys, self.registry, CHAT_ID,
            interval=0.05, min_edit_interval=0, clock=self.clock,
        )
        self.inbox = InboxCards(
            self.api, self.topics, self.keys, self.questions, self.registry, CHAT_ID, self.publisher.request_refresh
        )
        self.router = Router(
            RouterConfig(CHAT_ID, frozenset({OWNER}), "cqb_test_bot"), self.api, self.keys, self.topics,
            self.publisher, self.inbox, self.consultants, self.tg_topics, self.registry, self.questions,
            self.relay, self.status_cards, "s1", self.inventory.poke, self.clock,
        )
        self.poller = Poller(self.api, self.state, self.router.handle, poll_timeout=0)
        self.questions.subscribe(on_question=self.inbox.on_question, on_resolved=self.inbox.on_resolved)

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

    async def settle(self):
        """Apply coalesced registry refreshes and dirty cards, like the background loops would."""
        for qid in list(self.inbox._dirty):
            del self.inbox._dirty[qid]
            await self.inbox.refresh_card(qid)
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
        return self.topics.topic_id("control")

    @property
    def inbox_topic(self):
        return self.topics.topic_id("inbox")

    @property
    def registry_message(self):
        return self.state.get_int("registry.message_id")

    def card_of(self, qid):
        return self.question_dao.get(qid).inbox_message_id


@contextlib.asynccontextmanager
async def make_stack(conn, *threads):
    fake = FakeTelegram()
    await fake.start()
    stack = Stack(conn, fake)
    try:
        await stack.start(*threads)
        yield stack
    finally:
        await stack.api.close()
        await fake.stop()
