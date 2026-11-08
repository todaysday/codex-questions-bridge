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
    """
    CREATE TABLE agent_questions (
      agent_question_id TEXT PRIMARY KEY,
      source_id TEXT NOT NULL,
      thread_id TEXT NOT NULL,
      root_thread_id TEXT NOT NULL,
      turn_id TEXT NOT NULL,
      item_id TEXT NOT NULL,
      items_json TEXT NOT NULL,
      answerable INTEGER NOT NULL DEFAULT 1,
      state TEXT NOT NULL,
      staged_json TEXT,
      sent_at INTEGER,
      card_message_id INTEGER,
      created_at INTEGER NOT NULL,
      updated_at INTEGER NOT NULL,
      UNIQUE(source_id, thread_id, item_id)
    );
    CREATE INDEX idx_agent_question_root ON agent_questions(source_id, root_thread_id, state);
    CREATE INDEX idx_agent_question_thread ON agent_questions(source_id, thread_id, state);
    CREATE INDEX idx_agent_question_card ON agent_questions(card_message_id);
    """,
    """
    ALTER TABLE questions ADD COLUMN display_no INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE agent_questions ADD COLUMN display_no INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE consultants ADD COLUMN focus_no INTEGER;
    CREATE TABLE question_counters (
      source_id TEXT NOT NULL,
      root_thread_id TEXT NOT NULL,
      last_no INTEGER NOT NULL,
      PRIMARY KEY(source_id, root_thread_id)
    );
    CREATE TEMP TABLE _numbered AS
      SELECT k, id, ROW_NUMBER() OVER (PARTITION BY source_id, root_thread_id ORDER BY created_at, k, id) AS n
      FROM (SELECT 'q' AS k, question_id AS id, source_id, root_thread_id, created_at FROM questions
            UNION ALL
            SELECT 'a', agent_question_id, source_id, root_thread_id, created_at FROM agent_questions);
    UPDATE questions SET display_no=(SELECT n FROM _numbered WHERE k='q' AND id=question_id);
    UPDATE agent_questions SET display_no=(SELECT n FROM _numbered WHERE k='a' AND id=agent_question_id);
    INSERT INTO question_counters
      SELECT source_id, root_thread_id, MAX(display_no) FROM
        (SELECT source_id, root_thread_id, display_no FROM questions
         UNION ALL SELECT source_id, root_thread_id, display_no FROM agent_questions)
      GROUP BY source_id, root_thread_id;
    DROP TABLE _numbered;
    """,
    """
    -- One row per agent-message QUESTION (message item id + index). Rows of messages with several questions held
    -- them in one row; they are dropped here and the next scan finds them again as separate questions.
    DELETE FROM agent_questions WHERE json_array_length(items_json) <> 1;
    CREATE TABLE agent_questions_v2 (
      agent_question_id TEXT PRIMARY KEY,
      source_id TEXT NOT NULL,
      thread_id TEXT NOT NULL,
      root_thread_id TEXT NOT NULL,
      turn_id TEXT NOT NULL,
      item_id TEXT NOT NULL,
      item_index INTEGER NOT NULL DEFAULT 0,
      title TEXT NOT NULL,
      options_json TEXT NOT NULL,
      answerable INTEGER NOT NULL DEFAULT 1,
      state TEXT NOT NULL,
      staged TEXT,
      sent_at INTEGER,
      card_message_id INTEGER,
      card_extra_ids TEXT,
      display_no INTEGER NOT NULL DEFAULT 0,
      created_at INTEGER NOT NULL,
      updated_at INTEGER NOT NULL,
      UNIQUE(source_id, thread_id, item_id, item_index)
    );
    INSERT INTO agent_questions_v2(agent_question_id, source_id, thread_id, root_thread_id, turn_id, item_id,
        item_index, title, options_json, answerable, state, staged, sent_at, card_message_id, display_no,
        created_at, updated_at)
      SELECT agent_question_id, source_id, thread_id, root_thread_id, turn_id, item_id, 0,
        json_extract(items_json, '$[0].title'), json_extract(items_json, '$[0].options'), answerable, state,
        json_extract(staged_json, '$."0"'), sent_at, card_message_id, display_no, created_at, updated_at
      FROM agent_questions;
    DROP TABLE agent_questions;
    ALTER TABLE agent_questions_v2 RENAME TO agent_questions;
    CREATE INDEX idx_agent_question_root ON agent_questions(source_id, root_thread_id, state);
    CREATE INDEX idx_agent_question_thread ON agent_questions(source_id, thread_id, state);
    CREATE INDEX idx_agent_question_card ON agent_questions(card_message_id);
    ALTER TABLE questions ADD COLUMN card_extra_ids TEXT;
    """,
    """
    ALTER TABLE consultants ADD COLUMN opening_message_id INTEGER;
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
