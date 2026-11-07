"""Liveness of the PicoClaw gateway and of its scope hook process (design sections 10, 11).

Facts from the spike: a hook process that dies is never respawned until the gateway restarts,
after which approve_tool denies every call, and before_llm/after_llm hooks fail open. Bridge
therefore watches two signals and refuses consultant turns while either is bad:
  * the gateway answers GET /health;
  * the hook process posted its heartbeat (`beat`) recently. The hook sends one at start and
    then every few seconds from its own thread, so a dead or wedged process goes silent.
"""

import logging
import time
from collections.abc import Callable
from urllib.parse import urlsplit

import aiohttp

log = logging.getLogger(__name__)

HEALTH_TIMEOUT = 3.0
HEALTH_FAILING = "health-проверка PicoClaw не проходит"  # what the owner reads in the chat
HOOK_SILENT = "хук не отвечает"


def health_url(pico_url: str) -> str:
    parts = urlsplit(pico_url)
    host = f"[{parts.hostname}]" if ":" in (parts.hostname or "") else parts.hostname
    return f"http://{host}:{parts.port}/health"


class PicoHealth:
    def __init__(
        self, url: str, http: aiohttp.ClientSession, *, beat_max_age: float = 35.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._url = url
        self._http = http
        self._max_age = beat_max_age
        self._clock = clock
        self._last_beat: float | None = None
        self._started = clock()  # the hook gets one interval of grace after the Bridge starts
        self.ok = True
        self.reason = ""  # for the log
        self.user_reason = ""  # for the chat: short, Russian, no ids

    def beat(self) -> None:
        self._last_beat = self._clock()

    async def check(self) -> bool:
        reason, shown = await self._probe()
        if reason and self.ok:
            log.error("consultant chat unavailable: %s; messages are refused until it recovers", reason)
        elif not reason and not self.ok:
            log.warning("consultant chat recovered")
        self.ok, self.reason, self.user_reason = not reason, reason, shown
        return self.ok

    async def _probe(self) -> tuple[str, str]:
        """(log reason, chat reason); both empty when healthy."""
        try:
            async with self._http.get(self._url, timeout=aiohttp.ClientTimeout(total=HEALTH_TIMEOUT)) as r:
                if r.status != 200:
                    return f"PicoClaw /health answered {r.status}", f"{HEALTH_FAILING}, ответ {r.status}"
        except (aiohttp.ClientError, TimeoutError, OSError):
            return "PicoClaw gateway is not reachable", HEALTH_FAILING
        age = self._clock() - (self._last_beat if self._last_beat is not None else self._started)
        if age > self._max_age:
            return "the PicoClaw scope hook process is silent (dead hook: restart the PicoClaw gateway)", HOOK_SILENT
        return "", ""
