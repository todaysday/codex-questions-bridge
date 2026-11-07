import json
import sqlite3

from cqb.domain.enums import AgentQuestionState
from cqb.domain.models import AgentQuestion, AgentQuestionItem

_PENDING = AgentQuestionState.PENDING.value


class AgentQuestionDao:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def insert_if_new(self, q: AgentQuestion) -> bool:
        cur = self._conn.execute(
            "INSERT INTO agent_questions(agent_question_id, source_id, thread_id, root_thread_id, turn_id, "
            "item_id, items_json, answerable, state, staged_json, sent_at, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
            (
                q.agent_question_id, q.source_id, q.thread_id, q.root_thread_id, q.turn_id, q.item_id,
                json.dumps([{"title": i.title, "options": list(i.options)} for i in q.items], ensure_ascii=False),
                int(q.answerable), q.state.value, None, None, q.created_at, q.updated_at,
            ),
        )
        return cur.rowcount > 0

    def get(self, agent_question_id: str) -> AgentQuestion | None:
        row = self._conn.execute(
            "SELECT * FROM agent_questions WHERE agent_question_id=?", (agent_question_id,)
        ).fetchone()
        return _to_question(row) if row else None

    def find_by_card_message(self, message_id: int) -> AgentQuestion | None:
        row = self._conn.execute(
            "SELECT * FROM agent_questions WHERE card_message_id=?", (message_id,)
        ).fetchone()
        return _to_question(row) if row else None

    def list_open(self, source_id: str, root_thread_id: str | None = None, limit: int = 50) -> list[AgentQuestion]:
        sql = "SELECT * FROM agent_questions WHERE source_id=? AND state=?"
        args: list[object] = [source_id, _PENDING]
        if root_thread_id is not None:
            sql += " AND root_thread_id=?"
            args.append(root_thread_id)
        sql += " ORDER BY created_at DESC LIMIT ?"
        args.append(limit)
        return [_to_question(r) for r in self._conn.execute(sql, args)]

    def list_open_for_thread(self, source_id: str, thread_id: str) -> list[AgentQuestion]:
        rows = self._conn.execute(
            "SELECT * FROM agent_questions WHERE source_id=? AND thread_id=? AND state=?",
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
            "WHERE q.source_id=? AND q.card_message_id IS NULL AND q.state=? ORDER BY q.created_at LIMIT ?",
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

    def set_staged(self, agent_question_id: str, staged: dict[int, str] | None, now: int) -> None:
        self._conn.execute(
            "UPDATE agent_questions SET staged_json=?, updated_at=? WHERE agent_question_id=?",
            (json.dumps(staged, ensure_ascii=False) if staged else None, now, agent_question_id),
        )

    def mark_sent(self, agent_question_id: str, now: int) -> None:
        self._conn.execute(
            "UPDATE agent_questions SET sent_at=?, updated_at=? WHERE agent_question_id=?",
            (now, now, agent_question_id),
        )

    def set_card_message(self, agent_question_id: str, message_id: int | None) -> None:
        self._conn.execute(
            "UPDATE agent_questions SET card_message_id=? WHERE agent_question_id=?", (message_id, agent_question_id)
        )

    def prune_closed(self, source_id: str, before: int) -> int:
        return self._conn.execute(
            "DELETE FROM agent_questions WHERE source_id=? AND state<>? AND updated_at<?",
            (source_id, _PENDING, before),
        ).rowcount


def _to_question(r: sqlite3.Row) -> AgentQuestion:
    staged = json.loads(r["staged_json"]) if r["staged_json"] else {}
    return AgentQuestion(
        agent_question_id=r["agent_question_id"],
        source_id=r["source_id"],
        thread_id=r["thread_id"],
        root_thread_id=r["root_thread_id"],
        turn_id=r["turn_id"],
        item_id=r["item_id"],
        items=tuple(AgentQuestionItem(i["title"], tuple(i["options"])) for i in json.loads(r["items_json"])),
        answerable=bool(r["answerable"]),
        state=AgentQuestionState(r["state"]),
        staged={int(k): v for k, v in staged.items()},
        sent_at=r["sent_at"],
        created_at=r["created_at"],
        updated_at=r["updated_at"],
        card_message_id=r["card_message_id"],
    )
