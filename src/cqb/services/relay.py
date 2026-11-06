"""Port to the consultant dialog. The PicoClaw gateway replaces the counter in a later increment."""

import logging
from typing import Protocol

from cqb.domain.models import Consultant

log = logging.getLogger(__name__)


class ConsultantRelay(Protocol):
    async def relay(self, consultant: Consultant, text: str) -> None: ...


class CountingRelay:
    """Drops the message; logs only a counter (message text never reaches the log)."""

    def __init__(self) -> None:
        self.count = 0

    async def relay(self, consultant: Consultant, text: str) -> None:
        self.count += 1
        log.info("consultant message dropped (no relay yet) consultant=%d total=%d",
                 consultant.consultant_id, self.count)
