import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

# Forward-only: append new scripts, never edit old ones. user_version == len(applied).
MIGRATIONS: tuple[str, ...] = (
    """
    CREATE TABLE codex_sources (
      source_id TEXT PRIMARY KEY,
      label TEXT NOT NULL,
      backend TEXT NOT NULL,
      enabled INTEGER NOT NULL DEFAULT 1
    );
    CREATE TABLE codex_threads (
      source_id TEXT NOT NULL REFERENCES codex_sources(source_id),
      thread_id TEXT NOT NULL,
      root_thread_id TEXT NOT NULL,
      parent_thread_id TEXT,
      source_kind TEXT,
      display_name TEXT,
      cwd TEXT,
      updated_at INTEGER NOT NULL,
      archive_state TEXT NOT NULL DEFAULT 'unknown',
      archive_verified_at INTEGER,
      last_seen_at INTEGER NOT NULL,
      PRIMARY KEY(source_id, thread_id)
    );
    CREATE INDEX idx_threads_root ON codex_threads(source_id, root_thread_id);
    CREATE INDEX idx_threads_recency ON codex_threads(source_id, updated_at);
    CREATE TABLE questions (
      question_id TEXT PRIMARY KEY,
      source_id TEXT NOT NULL,
      root_thread_id TEXT NOT NULL,
      origin_thread_id TEXT NOT NULL,
      turn_id TEXT NOT NULL,
      tool_use_id TEXT NOT NULL,
      request_id TEXT,
      questions_json TEXT NOT NULL,
      is_secret INTEGER NOT NULL DEFAULT 0,
      is_blocking INTEGER NOT NULL DEFAULT 1,
      lifecycle TEXT NOT NULL,
      verification_source TEXT NOT NULL,
      staged_json TEXT,
      delivered_to_codex INTEGER NOT NULL DEFAULT 0,
      created_at INTEGER NOT NULL,
      updated_at INTEGER NOT NULL,
      UNIQUE(source_id, origin_thread_id, turn_id, tool_use_id)
    );
    CREATE INDEX idx_question_root_state ON questions(source_id, root_thread_id, lifecycle);
    CREATE INDEX idx_question_origin_state ON questions(source_id, origin_thread_id, lifecycle);
    CREATE TABLE telegram_actions (
      action_key TEXT PRIMARY KEY,
      action TEXT NOT NULL,
      source_id TEXT,
      root_thread_id TEXT,
      payload_json TEXT,
      expires_at INTEGER NOT NULL
    );
    """,
    """
    CREATE TABLE consultants (
      consultant_id INTEGER PRIMARY KEY,
      source_id TEXT NOT NULL,
      root_thread_id TEXT NOT NULL,
      tg_chat_id INTEGER NOT NULL,
      tg_topic_id INTEGER,
      pico_session_key TEXT NOT NULL,
      state TEXT NOT NULL,
      created_at INTEGER NOT NULL,
      updated_at INTEGER NOT NULL,
      UNIQUE(source_id, root_thread_id),
      UNIQUE(tg_chat_id, tg_topic_id),
      FOREIGN KEY(source_id, root_thread_id) REFERENCES codex_threads(source_id, thread_id)
    );
    CREATE TABLE bot_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    ALTER TABLE questions ADD COLUMN inbox_message_id INTEGER;
    CREATE INDEX idx_question_inbox ON questions(inbox_message_id);
    """,
    """
    ALTER TABLE questions RENAME COLUMN inbox_message_id TO card_message_id;
    ALTER TABLE codex_threads ADD COLUMN run_state TEXT;
    """,
)


def connect(path: str) -> sqlite3.Connection:
    # Autocommit; multi-statement changes use transaction(). Never await inside one.
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA cache_size=-512")
    conn.execute("PRAGMA mmap_size=0")
    return conn


def migrate(conn: sqlite3.Connection) -> int:
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    if current > len(MIGRATIONS):
        raise RuntimeError(
            f"database schema v{current} is newer than this build (v{len(MIGRATIONS)})"
        )
    for version in range(current, len(MIGRATIONS)):
        conn.executescript(
            f"BEGIN;\n{MIGRATIONS[version]}\nPRAGMA user_version = {version + 1};\nCOMMIT;"
        )
    return len(MIGRATIONS)


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[None]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")
