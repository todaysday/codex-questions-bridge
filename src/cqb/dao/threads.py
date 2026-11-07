import sqlite3
from collections.abc import Iterable

from cqb.dao.db import transaction
from cqb.domain.enums import ArchiveState, RunState
from cqb.domain.models import Source, Thread


class SourceDao:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def upsert(self, source: Source) -> None:
        self._conn.execute(
            "INSERT INTO codex_sources(source_id, label, backend, enabled) VALUES (?,?,?,?) "
            "ON CONFLICT(source_id) DO UPDATE SET label=excluded.label, "
            "backend=excluded.backend, enabled=excluded.enabled",
            (source.source_id, source.label, source.backend, int(source.enabled)),
        )


class ThreadDao:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def get(self, source_id: str, thread_id: str) -> Thread | None:
        row = self._conn.execute(
            "SELECT * FROM codex_threads WHERE source_id=? AND thread_id=?",
            (source_id, thread_id),
        ).fetchone()
        return _to_thread(row) if row else None

    def upsert_many(self, threads: Iterable[Thread]) -> None:
        """Insert or refresh metadata. An `unknown` archive state never overwrites a verified one."""
        with transaction(self._conn):
            for t in threads:
                self._conn.execute(
                    "INSERT INTO codex_threads(source_id, thread_id, root_thread_id, "
                    "parent_thread_id, source_kind, display_name, cwd, updated_at, "
                    "archive_state, archive_verified_at, last_seen_at, run_state) VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(source_id, thread_id) DO UPDATE SET "
                    "root_thread_id=excluded.root_thread_id, parent_thread_id=excluded.parent_thread_id, "
                    "source_kind=excluded.source_kind, display_name=excluded.display_name, "
                    "cwd=excluded.cwd, updated_at=excluded.updated_at, last_seen_at=excluded.last_seen_at, "
                    "archive_state=CASE WHEN excluded.archive_state='unknown' THEN archive_state "
                    "ELSE excluded.archive_state END, "
                    "archive_verified_at=CASE WHEN excluded.archive_state='unknown' THEN archive_verified_at "
                    "ELSE excluded.archive_verified_at END, "
                    "run_state=COALESCE(excluded.run_state, run_state)",
                    (
                        t.source_id, t.thread_id, t.root_thread_id, t.parent_thread_id,
                        t.source_kind, t.display_name, t.cwd, t.updated_at,
                        t.archive_state.value, t.archive_verified_at, t.last_seen_at,
                        t.run_state.value if t.run_state else None,
                    ),
                )

    def set_archive_state(
        self, source_id: str, thread_id: str, state: ArchiveState, verified_at: int
    ) -> bool:
        cur = self._conn.execute(
            "UPDATE codex_threads SET archive_state=?, archive_verified_at=? "
            "WHERE source_id=? AND thread_id=?",
            (state.value, verified_at, source_id, thread_id),
        )
        return cur.rowcount > 0

    def list_roots(
        self, source_id: str, state: ArchiveState | None, limit: int, offset: int = 0,
        since: int | None = None,
    ) -> list[Thread]:
        sql = "SELECT * FROM codex_threads WHERE source_id=? AND parent_thread_id IS NULL"
        args: list[object] = [source_id]
        if state is not None:
            sql += " AND archive_state=?"
            args.append(state.value)
        if since is not None:
            sql += " AND updated_at>=?"
            args.append(since)
        sql += " ORDER BY updated_at DESC, thread_id LIMIT ? OFFSET ?"
        args += [limit, offset]
        return [_to_thread(r) for r in self._conn.execute(sql, args)]

    def list_descendants(self, source_id: str, root_thread_id: str, limit: int) -> list[Thread]:
        rows = self._conn.execute(
            "SELECT * FROM codex_threads WHERE source_id=? AND root_thread_id=? "
            "AND thread_id<>? ORDER BY updated_at DESC LIMIT ?",
            (source_id, root_thread_id, root_thread_id, limit),
        )
        return [_to_thread(r) for r in rows]


def _to_thread(r: sqlite3.Row) -> Thread:
    return Thread(
        source_id=r["source_id"],
        thread_id=r["thread_id"],
        root_thread_id=r["root_thread_id"],
        parent_thread_id=r["parent_thread_id"],
        source_kind=r["source_kind"],
        display_name=r["display_name"],
        cwd=r["cwd"],
        updated_at=r["updated_at"],
        archive_state=ArchiveState(r["archive_state"]),
        archive_verified_at=r["archive_verified_at"],
        last_seen_at=r["last_seen_at"],
        run_state=RunState(r["run_state"]) if r["run_state"] else None,
    )
