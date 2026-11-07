import json
import sqlite3

from cqb.dao.cards import CARD_MATCH, extra_ids_from_text, extra_ids_to_text
from cqb.dao.db import transaction
from cqb.dao.numbering import next_number
from cqb.domain.enums import AgentQuestionState
from cqb.domain.models import AgentQuestion

_PENDING = AgentQuestionState.PENDING.value


class AgentQuestionDao:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def insert_if_new(self, q: AgentQuestion) -> bool:
        """Stores a new question and gives it the next `Q` number of its root; False for a replay."""
        with transaction(self._conn):
            cur = self._insert(q)
            if cur.rowcount > 0:
                self._conn.execute(
                    "UPDATE agent_questions SET display_no=? WHERE agent_question_id=?",
                    (next_number(self._conn, q.source_id, q.root_thread_id), q.agent_question_id),
                )
        return cur.rowcount > 0

    def _insert(self, q: AgentQuestion) -> sqlite3.Cursor:
        return self._conn.execute(
            "INSERT INTO agent_questions(agent_question_id, source_id, thread_id, root_thread_id, turn_id, "
            "item_id, item_index, title, options_json, answerable, state, staged, sent_at, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
            (
                q.agent_question_id, q.source_id, q.thread_id, q.root_thread_id, q.turn_id, q.item_id, q.index,
                q.title, json.dumps(list(q.options), ensure_ascii=False), int(q.answerable), q.state.value,
                None, None, q.created_at, q.updated_at,
            ),
        )

    def list_for_root(
        self, source_id: str, root_thread_id: str, limit: int, number: int | None = None
    ) -> list[AgentQuestion]:
        """Open and closed questions of a root, newest number first; optionally only one number."""
        sql = "SELECT * FROM agent_questions WHERE source_id=? AND root_thread_id=?"
        args: list[object] = [source_id, root_thread_id]
        if number is not None:
            sql += " AND display_no=?"
            args.append(number)
        return [_to_question(r) for r in self._conn.execute(sql + " ORDER BY display_no DESC LIMIT ?", (*args, limit))]

    def get(self, agent_question_id: str) -> AgentQuestion | None:
        row = self._conn.execute(
            "SELECT * FROM agent_questions WHERE agent_question_id=?", (agent_question_id,)
        ).fetchone()
        return _to_question(row) if row else None

    def find_by_card_message(self, message_id: int) -> AgentQuestion | None:
        row = self._conn.execute(
            f"SELECT * FROM agent_questions WHERE {CARD_MATCH}", (message_id, message_id)
        ).fetchone()
        return _to_question(row) if row else None

    def list_open(self, source_id: str, root_thread_id: str | None = None, limit: int = 50) -> list[AgentQuestion]:
        sql = "SELECT * FROM agent_questions WHERE source_id=? AND state=?"
        args: list[object] = [source_id, _PENDING]
        if root_thread_id is not None:
            sql += " AND root_thread_id=?"
            args.append(root_thread_id)
        sql += " ORDER BY created_at DESC, display_no DESC LIMIT ?"
        args.append(limit)
        return [_to_question(r) for r in self._conn.execute(sql, args)]

    def list_open_for_thread(self, source_id: str, thread_id: str) -> list[AgentQuestion]:
        rows = self._conn.execute(
            "SELECT * FROM agent_questions WHERE source_id=? AND thread_id=? AND state=? ORDER BY display_no",
            (source_id, thread_id, _PENDING),
        )
        return [_to_question(r) for r in rows]

    def list_open_in_archived(self, source_id: str) -> list[AgentQuestion]:
        rows = self._conn.execute(
            "SELECT q.* FROM agent_questions q JOIN codex_threads t ON t.source_id=q.source_id "
            "AND t.thread_id=q.thread_id AND t.archive_state='archived' WHERE q.source_id=? AND q.state=?",
            (source_id, _PENDING),
        )
        return [_to_question(r) for r in rows]

    def list_postable(self, source_id: str, limit: int = 50) -> list[AgentQuestion]:
        """Open questions without a card whose root has an open consultant topic to post it in."""
        rows = self._conn.execute(
            "SELECT q.* FROM agent_questions q JOIN consultants c ON c.source_id=q.source_id "
            "AND c.root_thread_id=q.root_thread_id AND c.state='open' AND c.tg_topic_id IS NOT NULL "
            "WHERE q.source_id=? AND q.card_message_id IS NULL AND q.state=? "
            "ORDER BY q.created_at, q.display_no LIMIT ?",
            (source_id, _PENDING, limit),
        )
        return [_to_question(r) for r in rows]

    def open_counts_by_root(self, source_id: str) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT root_thread_id, COUNT(*) FROM agent_questions WHERE source_id=? AND state=? "
            "GROUP BY root_thread_id",
            (source_id, _PENDING),
        )
        return {r[0]: r[1] for r in rows}

    def scan_roots(self, source_id: str, since: int) -> list[str]:
        """Active roots worth reading: updated since `since`, working, or holding a pending question."""
        rows = self._conn.execute(
            "SELECT thread_id FROM codex_threads WHERE source_id=? AND parent_thread_id IS NULL "
            "AND archive_state='active' AND (updated_at>=? OR run_state='working' OR thread_id IN "
            "(SELECT thread_id FROM agent_questions WHERE source_id=? AND state=?)) ORDER BY updated_at DESC",
            (source_id, since, source_id, _PENDING),
        )
        return [r[0] for r in rows]

    def set_state(self, agent_question_id: str, state: AgentQuestionState, now: int) -> None:
        self._conn.execute(
            "UPDATE agent_questions SET state=?, updated_at=? WHERE agent_question_id=?",
            (state.value, now, agent_question_id),
        )

    def set_staged(self, agent_question_id: str, staged: str | None, now: int) -> None:
        self._conn.execute(
            "UPDATE agent_questions SET staged=?, updated_at=? WHERE agent_question_id=?",
            (staged, now, agent_question_id),
        )

    def mark_sent(self, agent_question_id: str, now: int) -> None:
        self._conn.execute(
            "UPDATE agent_questions SET sent_at=?, updated_at=? WHERE agent_question_id=?",
            (now, now, agent_question_id),
        )

    def set_card_messages(self, agent_question_id: str, ids: list[int]) -> None:
        """The card's message ids in order; none clears the card."""
        self._conn.execute(
            "UPDATE agent_questions SET card_message_id=?, card_extra_ids=? WHERE agent_question_id=?",
            (ids[0] if ids else None, extra_ids_to_text(ids[1:]), agent_question_id),
        )

    def prune_closed(self, source_id: str, before: int) -> int:
        return self._conn.execute(
            "DELETE FROM agent_questions WHERE source_id=? AND state<>? AND updated_at<?",
            (source_id, _PENDING, before),
        ).rowcount


def _to_question(r: sqlite3.Row) -> AgentQuestion:
    return AgentQuestion(
        agent_question_id=r["agent_question_id"],
        source_id=r["source_id"],
        thread_id=r["thread_id"],
        root_thread_id=r["root_thread_id"],
        turn_id=r["turn_id"],
        item_id=r["item_id"],
        index=r["item_index"],
        title=r["title"],
        options=tuple(json.loads(r["options_json"])),
        answerable=bool(r["answerable"]),
        state=AgentQuestionState(r["state"]),
        staged=r["staged"],
        sent_at=r["sent_at"],
        created_at=r["created_at"],
        updated_at=r["updated_at"],
        card_message_id=r["card_message_id"],
        display_no=r["display_no"],
        card_extra_ids=extra_ids_from_text(r["card_extra_ids"]),
    )
