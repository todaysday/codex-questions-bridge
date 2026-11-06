import json
import sqlite3

from cqb.domain.enums import OPEN_LIFECYCLES, QuestionLifecycle, VerificationSource
from cqb.domain.models import Question, QuestionItem, QuestionOption

_OPEN = ",".join(f"'{s.value}'" for s in OPEN_LIFECYCLES)


class QuestionDao:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def insert_if_new(self, q: Question) -> bool:
        cur = self._conn.execute(
            "INSERT INTO questions(question_id, source_id, root_thread_id, origin_thread_id, "
            "turn_id, tool_use_id, request_id, questions_json, is_secret, is_blocking, "
            "lifecycle, verification_source, staged_json, delivered_to_codex, created_at, "
            "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT DO NOTHING",
            (
                q.question_id, q.source_id, q.root_thread_id, q.origin_thread_id, q.turn_id,
                q.tool_use_id, q.request_id, _items_to_json(q.items), int(q.is_secret),
                int(q.is_blocking), q.lifecycle.value, q.verification_source.value,
                _staged_to_json(q.staged), int(q.delivered_to_codex), q.created_at, q.updated_at,
            ),
        )
        return cur.rowcount > 0

    def get(self, question_id: str) -> Question | None:
        row = self._conn.execute(
            "SELECT * FROM questions WHERE question_id=?", (question_id,)
        ).fetchone()
        return _to_question(row) if row else None

    def list_open(self, source_id: str, root_thread_id: str | None = None, limit: int = 50) -> list[Question]:
        sql = f"SELECT * FROM questions WHERE source_id=? AND lifecycle IN ({_OPEN})"
        args: list[object] = [source_id]
        if root_thread_id is not None:
            sql += " AND root_thread_id=?"
            args.append(root_thread_id)
        sql += " ORDER BY created_at DESC LIMIT ?"
        args.append(limit)
        return [_to_question(r) for r in self._conn.execute(sql, args)]

    def open_thread_ids(self, source_id: str) -> list[str]:
        rows = self._conn.execute(
            f"SELECT DISTINCT origin_thread_id FROM questions WHERE source_id=? "
            f"AND lifecycle IN ({_OPEN})",
            (source_id,),
        )
        return [r[0] for r in rows]

    def find_open_by_request(self, source_id: str, thread_id: str, request_id: str) -> Question | None:
        row = self._conn.execute(
            f"SELECT * FROM questions WHERE source_id=? AND origin_thread_id=? AND request_id=? "
            f"AND lifecycle IN ({_OPEN})",
            (source_id, thread_id, request_id),
        ).fetchone()
        return _to_question(row) if row else None

    def list_open_for_thread(
        self, source_id: str, thread_id: str, turn_id: str | None = None
    ) -> list[Question]:
        sql = (
            f"SELECT * FROM questions WHERE source_id=? AND origin_thread_id=? "
            f"AND lifecycle IN ({_OPEN})"
        )
        args: list[object] = [source_id, thread_id]
        if turn_id is not None:
            sql += " AND turn_id=?"
            args.append(turn_id)
        return [_to_question(r) for r in self._conn.execute(sql, args)]

    def attach_request(self, question_id: str, request_id: str, lifecycle: QuestionLifecycle, now: int) -> None:
        self._conn.execute(
            "UPDATE questions SET request_id=?, lifecycle=?, updated_at=? WHERE question_id=?",
            (request_id, lifecycle.value, now, question_id),
        )

    def detach_requests(self, source_id: str, now: int) -> None:
        """Connection-scoped request ids die with the connection; demote unconfirmed state."""
        self._conn.execute(
            "UPDATE questions SET request_id=NULL, updated_at=?, lifecycle=CASE lifecycle "
            "WHEN 'pending_confirmed' THEN 'captured' ELSE lifecycle END "
            f"WHERE source_id=? AND lifecycle IN ({_OPEN})",
            (now, source_id),
        )

    def set_lifecycle(self, question_id: str, lifecycle: QuestionLifecycle, now: int) -> None:
        self._conn.execute(
            "UPDATE questions SET lifecycle=?, updated_at=? WHERE question_id=?",
            (lifecycle.value, now, question_id),
        )

    def stage(self, question_id: str, staged: dict[str, tuple[str, ...]], now: int) -> None:
        self._conn.execute(
            "UPDATE questions SET lifecycle=?, staged_json=?, updated_at=? WHERE question_id=?",
            (QuestionLifecycle.DECISION_STAGED.value, _staged_to_json(staged), now, question_id),
        )

    def unstage(self, question_id: str, lifecycle: QuestionLifecycle, now: int) -> None:
        self._conn.execute(
            "UPDATE questions SET lifecycle=?, staged_json=NULL, updated_at=? WHERE question_id=?",
            (lifecycle.value, now, question_id),
        )

    def set_inbox_message(self, question_id: str, message_id: int | None) -> None:
        self._conn.execute(
            "UPDATE questions SET inbox_message_id=? WHERE question_id=?", (message_id, question_id)
        )

    def find_by_inbox_message(self, message_id: int) -> Question | None:
        row = self._conn.execute(
            "SELECT * FROM questions WHERE inbox_message_id=?", (message_id,)
        ).fetchone()
        return _to_question(row) if row else None

    def list_open_without_card(self, source_id: str, limit: int = 50) -> list[Question]:
        rows = self._conn.execute(
            f"SELECT * FROM questions WHERE source_id=? AND inbox_message_id IS NULL "
            f"AND lifecycle IN ({_OPEN}) ORDER BY created_at LIMIT ?",
            (source_id, limit),
        )
        return [_to_question(r) for r in rows]

    def open_counts_by_root(self, source_id: str) -> dict[str, int]:
        rows = self._conn.execute(
            f"SELECT root_thread_id, COUNT(*) FROM questions WHERE source_id=? "
            f"AND lifecycle IN ({_OPEN}) GROUP BY root_thread_id",
            (source_id,),
        )
        return {r[0]: r[1] for r in rows}

    def mark_delivered(self, question_id: str, now: int) -> None:
        self._conn.execute(
            "UPDATE questions SET delivered_to_codex=1, updated_at=? WHERE question_id=?",
            (now, question_id),
        )


def _items_to_json(items: tuple[QuestionItem, ...]) -> str:
    return json.dumps(
        [
            {
                "id": i.id, "header": i.header, "text": i.text, "is_other": i.is_other,
                "options": [{"label": o.label, "description": o.description} for o in i.options],
            }
            for i in items
        ],
        ensure_ascii=False,
    )


def _staged_to_json(staged: dict[str, tuple[str, ...]] | None) -> str | None:
    return None if staged is None else json.dumps(staged, ensure_ascii=False)


def _to_question(r: sqlite3.Row) -> Question:
    items = tuple(
        QuestionItem(
            id=i["id"], header=i["header"], text=i["text"], is_other=i["is_other"],
            options=tuple(QuestionOption(o["label"], o["description"]) for o in i["options"]),
        )
        for i in json.loads(r["questions_json"])
    )
    staged = json.loads(r["staged_json"]) if r["staged_json"] else None
    return Question(
        question_id=r["question_id"],
        source_id=r["source_id"],
        root_thread_id=r["root_thread_id"],
        origin_thread_id=r["origin_thread_id"],
        turn_id=r["turn_id"],
        tool_use_id=r["tool_use_id"],
        request_id=r["request_id"],
        items=items,
        is_secret=bool(r["is_secret"]),
        is_blocking=bool(r["is_blocking"]),
        lifecycle=QuestionLifecycle(r["lifecycle"]),
        verification_source=VerificationSource(r["verification_source"]),
        staged={k: tuple(v) for k, v in staged.items()} if staged else None,
        delivered_to_codex=bool(r["delivered_to_codex"]),
        created_at=r["created_at"],
        updated_at=r["updated_at"],
        inbox_message_id=r["inbox_message_id"],
    )
