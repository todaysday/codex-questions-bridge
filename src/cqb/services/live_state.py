"""Bounded in-memory view of daemon broadcasts: status, last-seen time, latest usage, goal flag.

Holds ints and enums only (no text). The daemon broadcasts every thread's status to every
connection and every subagent's token usage, which is all the status card needs from live data.
"""

import dataclasses
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass

from cqb.codex.events import Connected, Disconnected, Event, GoalChanged, ThreadStatusChanged, TokenUsageUpdated
from cqb.domain.enums import RunState
from cqb.domain.status import UsageSnapshot

MAX_ENTRIES = 4096


@dataclass(frozen=True, slots=True)
class LiveThread:
    state: RunState = RunState.UNKNOWN
    seen_at: int = 0  # last broadcast of any kind for this thread
    usage: UsageSnapshot | None = None
    has_goal: bool | None = None  # None: not known


class LiveState:
    def __init__(
        self,
        max_entries: int = MAX_ENTRIES,
        clock: Callable[[], float] = time.time,
        on_working_flip: Callable[[str], None] | None = None,
    ) -> None:
        """on_working_flip(thread_id) fires only when a thread starts or stops working, not on every event."""
        self._max = max_entries
        self._clock = clock
        self._on_flip = on_working_flip
        self._items: OrderedDict[str, LiveThread] = OrderedDict()

    def __len__(self) -> int:
        return len(self._items)

    def get(self, thread_id: str) -> LiveThread | None:
        return self._items.get(thread_id)

    async def handle(self, event: Event) -> None:
        match event:
            case ThreadStatusChanged():
                before = self._items.get(event.thread_id)
                self._update(event.thread_id, state=event.state)
                if self._on_flip and _flipped(before, event.state):
                    self._on_flip(event.thread_id)
            case TokenUsageUpdated():
                self._update(event.thread_id, usage=event.usage)
            case GoalChanged():
                self._update(event.thread_id, has_goal=event.present)
            case Connected() | Disconnected():
                self._items.clear()  # broadcasts were missed or are about to be: forget stale facts

    def _update(self, thread_id: str, **fields: object) -> None:
        current = self._items.get(thread_id) or LiveThread()
        self._items[thread_id] = dataclasses.replace(current, seen_at=int(self._clock()), **fields)
        self._items.move_to_end(thread_id)
        while len(self._items) > self._max:
            self._items.popitem(last=False)


def _flipped(before: LiveThread | None, now: RunState) -> bool:
    """Working vs not working. The first broadcast after a (re)connect counts: the stored status may differ."""
    if before is None or before.state is RunState.UNKNOWN:
        return True
    return (before.state is RunState.WORKING) != (now is RunState.WORKING)
