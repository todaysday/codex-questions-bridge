"""Cards that wait for the owner's next reply as an ANSWER (design section 2.3).

Only the `✍️ Ответить текстом` button arms a card. The state is in memory, bounded and short-lived: an entry
expires after `ttl` seconds, after the answer, or on cancel; a restart forgets it, and a reply then simply goes to
the consultant as a discussion."""

import time
from collections import OrderedDict
from collections.abc import Callable

ARMED_TTL_SECONDS = 600
ARMED_MAX = 50


class ArmedAnswers:
    def __init__(
        self, clock: Callable[[], float] = time.time, ttl: float = ARMED_TTL_SECONDS, limit: int = ARMED_MAX
    ) -> None:
        self._clock = clock
        self._ttl = ttl
        self._limit = limit
        self._armed: OrderedDict[str, float] = OrderedDict()  # card key -> expires at

    def arm(self, key: str) -> list[str]:
        """Arms (or re-arms) a card; returns the keys pushed out by the bound, whose cards need a redraw."""
        self._armed.pop(key, None)
        self._armed[key] = self._clock() + self._ttl
        evicted = []
        while len(self._armed) > self._limit:
            evicted.append(self._armed.popitem(last=False)[0])
        return evicted

    def is_armed(self, key: str) -> bool:
        """Whether the card is armed (an expired entry is dropped here)."""
        until = self._armed.get(key)
        if until is None:
            return False
        if until <= self._clock():
            del self._armed[key]
            return False
        return True

    def disarm(self, key: str) -> bool:
        return self._armed.pop(key, None) is not None

    def drop_expired(self) -> list[str]:
        now = self._clock()
        gone = [k for k, until in self._armed.items() if until <= now]
        for k in gone:
            del self._armed[k]
        return gone
