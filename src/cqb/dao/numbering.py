"""Display numbers of questions: one increasing counter per (source, root thread), shared by both kinds."""

import sqlite3


def next_number(conn: sqlite3.Connection, source_id: str, root_thread_id: str) -> int:
    """Take the next number of the root. Call inside the transaction that inserts the question row."""
    return conn.execute(
        "INSERT INTO question_counters(source_id, root_thread_id, last_no) VALUES (?,?,1) "
        "ON CONFLICT(source_id, root_thread_id) DO UPDATE SET last_no=last_no+1 RETURNING last_no",
        (source_id, root_thread_id),
    ).fetchone()[0]
