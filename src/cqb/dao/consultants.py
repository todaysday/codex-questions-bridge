import sqlite3
from collections.abc import Collection

from cqb.domain.enums import ConsultantState
from cqb.domain.models import Consultant


class ConsultantDao:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def get(self, source_id: str, root_thread_id: str) -> Consultant | None:
        row = self._conn.execute(
            "SELECT * FROM consultants WHERE source_id=? AND root_thread_id=?",
            (source_id, root_thread_id),
        ).fetchone()
        return _to_consultant(row) if row else None

    def get_by_topic(self, chat_id: int, topic_id: int) -> Consultant | None:
        row = self._conn.execute(
            "SELECT * FROM consultants WHERE tg_chat_id=? AND tg_topic_id=?", (chat_id, topic_id)
        ).fetchone()
        return _to_consultant(row) if row else None

    def get_by_session_key(self, session_key: str) -> Consultant | None:
        row = self._conn.execute(
            "SELECT * FROM consultants WHERE pico_session_key=?", (session_key,)
        ).fetchone()
        return _to_consultant(row) if row else None

    def get_many(self, source_id: str, root_ids: Collection[str]) -> dict[str, Consultant]:
        if not root_ids:
            return {}
        marks = ",".join("?" * len(root_ids))
        rows = self._conn.execute(
            f"SELECT * FROM consultants WHERE source_id=? AND root_thread_id IN ({marks})",
            (source_id, *root_ids),
        )
        return {r["root_thread_id"]: _to_consultant(r) for r in rows}

    def reserve(
        self, source_id: str, root_thread_id: str, chat_id: int, session_key: str, now: int
    ) -> Consultant:
        """Insert the CREATING reservation unless a binding already exists, and return the row."""
        self._conn.execute(
            "INSERT OR IGNORE INTO consultants(source_id, root_thread_id, tg_chat_id, "
            "pico_session_key, state, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
            (source_id, root_thread_id, chat_id, session_key, ConsultantState.CREATING.value, now, now),
        )
        found = self.get(source_id, root_thread_id)
        assert found is not None
        return found

    def open(self, consultant_id: int, topic_id: int, now: int) -> None:
        self._conn.execute(
            "UPDATE consultants SET tg_topic_id=?, state=?, updated_at=? WHERE consultant_id=?",
            (topic_id, ConsultantState.OPEN.value, now, consultant_id),
        )

    def set_state(self, consultant_id: int, state: ConsultantState, now: int) -> None:
        self._conn.execute(
            "UPDATE consultants SET state=?, updated_at=? WHERE consultant_id=?",
            (state.value, now, consultant_id),
        )

    def list_by_states(
        self, source_id: str, states: Collection[ConsultantState], limit: int = 500
    ) -> list[Consultant]:
        marks = ",".join("?" * len(states))
        rows = self._conn.execute(
            f"SELECT * FROM consultants WHERE source_id=? AND state IN ({marks}) "
            "ORDER BY consultant_id LIMIT ?",
            (source_id, *(s.value for s in states), limit),
        )
        return [_to_consultant(r) for r in rows]

    def fail_stale_creating(self, source_id: str, now: int) -> int:
        """A CREATING row without a topic id after a restart has an unknown outcome."""
        return self._conn.execute(
            "UPDATE consultants SET state=?, updated_at=? WHERE source_id=? AND state=? "
            "AND tg_topic_id IS NULL",
            (ConsultantState.ERROR.value, now, source_id, ConsultantState.CREATING.value),
        ).rowcount

    def count_open(self, source_id: str) -> int:
        return self._conn.execute(
            "SELECT COUNT(*) FROM consultants WHERE source_id=? AND state=?",
            (source_id, ConsultantState.OPEN.value),
        ).fetchone()[0]


def _to_consultant(r: sqlite3.Row) -> Consultant:
    return Consultant(
        consultant_id=r["consultant_id"],
        source_id=r["source_id"],
        root_thread_id=r["root_thread_id"],
        tg_chat_id=r["tg_chat_id"],
        tg_topic_id=r["tg_topic_id"],
        pico_session_key=r["pico_session_key"],
        state=ConsultantState(r["state"]),
        created_at=r["created_at"],
        updated_at=r["updated_at"],
    )
