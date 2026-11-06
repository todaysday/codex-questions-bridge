import sqlite3

from cqb.domain.models import TelegramAction


class ActionDao:
    """One-time callback actions (Telegram callback_data is too short for full ids)."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def put(self, a: TelegramAction) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO telegram_actions(action_key, action, source_id, "
            "root_thread_id, payload_json, expires_at) VALUES (?,?,?,?,?,?)",
            (a.action_key, a.action, a.source_id, a.root_thread_id, a.payload, a.expires_at),
        )

    def get(self, action_key: str, now: int) -> TelegramAction | None:
        """Read without using it up (navigation buttons may be pressed repeatedly)."""
        row = self._conn.execute(
            "SELECT * FROM telegram_actions WHERE action_key=?", (action_key,)
        ).fetchone()
        if row is None or row["expires_at"] <= now:
            return None
        return _to_action(row)

    def consume(self, action_key: str, now: int) -> TelegramAction | None:
        row = self._conn.execute(
            "DELETE FROM telegram_actions WHERE action_key=? RETURNING *", (action_key,)
        ).fetchone()
        if row is None or row["expires_at"] <= now:
            return None
        return _to_action(row)

    def delete_expired(self, now: int) -> int:
        return self._conn.execute(
            "DELETE FROM telegram_actions WHERE expires_at<=?", (now,)
        ).rowcount


def _to_action(row: sqlite3.Row) -> TelegramAction:
    return TelegramAction(
        row["action_key"], row["action"], row["source_id"], row["root_thread_id"],
        row["payload_json"], row["expires_at"],
    )
