import logging
from collections.abc import Awaitable, Callable

from cqb.codex.appserver import AppServerClient
from cqb.codex.events import Event, normalize

log = logging.getLogger(__name__)


async def dispatch(client: AppServerClient, handlers: list[Callable[[Event], Awaitable[None]]]) -> None:
    """Single consumer of the client inbox; handlers run in order, one message at a time."""
    while True:
        event = normalize(await client.inbox.get())
        if event is None:
            continue
        for handler in handlers:
            try:
                await handler(event)
            except Exception:
                log.exception("handler %s failed on %s", getattr(handler, "__qualname__", handler), type(event).__name__)
