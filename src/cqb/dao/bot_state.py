import sqlite3


class BotStateDao:
    """Small key-value store: poll offset, service topic ids, registry message id and hash."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def get(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM bot_state WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def get_int(self, key: str) -> int | None:
        value = self.get(key)
        return int(value) if value is not None else None

    def set(self, key: str, value: str | int) -> None:
        self._conn.execute(
            "INSERT INTO bot_state(key, value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )

    def delete(self, key: str) -> None:
        self._conn.execute("DELETE FROM bot_state WHERE key=?", (key,))
