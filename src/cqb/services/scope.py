"""Which Codex root a pico session may read: the consultant's own, or a Status Agent slot's for one call.

The scope hook asks the MCP server's internal endpoint, which asks this table. A slot is bound to a root
only while its status request runs, so a late tool call of an abandoned request finds no scope.
"""

from collections.abc import Callable

Scope = tuple[str, str]  # (source_id, root_id)


class SessionScopes:
    def __init__(self, consultants: Callable[[str], Scope | None]) -> None:
        self._consultants = consultants
        self._bound: dict[str, Scope] = {}  # at most one entry per status slot

    def bind(self, session_id: str, source_id: str, root_id: str) -> None:
        self._bound[session_id] = (source_id, root_id)

    def unbind(self, session_id: str) -> None:
        self._bound.pop(session_id, None)

    def lookup(self, session_id: str) -> Scope | None:
        return self._bound.get(session_id) or self._consultants(session_id)
