"""SQLite storage. One file, WAL mode, one connection per thread."""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS contacts (
  id          INTEGER PRIMARY KEY,
  name        TEXT NOT NULL DEFAULT '',
  name_key    TEXT NOT NULL DEFAULT '',
  company     TEXT NOT NULL DEFAULT '',
  title       TEXT NOT NULL DEFAULT '',
  avatar_url  TEXT NOT NULL DEFAULT '',
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS contacts_name_key ON contacts(name_key);

-- One row per (channel, account) a contact reaches us from.
CREATE TABLE IF NOT EXISTS identities (
  id          INTEGER PRIMARY KEY,
  contact_id  INTEGER NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
  channel     TEXT NOT NULL,
  key         TEXT NOT NULL,
  handle      TEXT NOT NULL DEFAULT '',
  name        TEXT NOT NULL DEFAULT '',
  url         TEXT NOT NULL DEFAULT '',
  meta        TEXT NOT NULL DEFAULT '{}',
  UNIQUE(channel, key)
);
CREATE INDEX IF NOT EXISTS identities_contact ON identities(contact_id);

CREATE TABLE IF NOT EXISTS threads (
  id                INTEGER PRIMARY KEY,
  channel           TEXT NOT NULL,
  external_id       TEXT NOT NULL,
  contact_id        INTEGER REFERENCES contacts(id) ON DELETE SET NULL,
  subject           TEXT NOT NULL DEFAULT '',
  url               TEXT NOT NULL DEFAULT '',
  snippet           TEXT NOT NULL DEFAULT '',
  message_count     INTEGER NOT NULL DEFAULT 0,
  inbound_count     INTEGER NOT NULL DEFAULT 0,
  first_message_at  TEXT,
  last_message_at   TEXT,
  last_inbound_at   TEXT,
  last_outbound_at  TEXT,
  last_direction    TEXT NOT NULL DEFAULT '',
  unread            INTEGER NOT NULL DEFAULT 0,
  stage             TEXT NOT NULL DEFAULT 'new',
  starred           INTEGER NOT NULL DEFAULT 0,
  snoozed_until     TEXT,
  content_hash      TEXT NOT NULL DEFAULT '',
  meta              TEXT NOT NULL DEFAULT '{}',
  is_demo           INTEGER NOT NULL DEFAULT 0,
  created_at        TEXT NOT NULL,
  updated_at        TEXT NOT NULL,
  -- Which of the owner's own accounts received this conversation (an
  -- accounts.key). Empty where a channel has only ever had one account.
  account           TEXT NOT NULL DEFAULT '',
  UNIQUE(channel, external_id)
);

-- The owner's own accounts on a channel, remembered by their stable id so a
-- conversation keeps its label even after the account is disconnected.
CREATE TABLE IF NOT EXISTS accounts (
  channel     TEXT NOT NULL,
  key         TEXT NOT NULL,
  username    TEXT NOT NULL DEFAULT '',
  label       TEXT NOT NULL DEFAULT '',
  updated_at  TEXT NOT NULL,
  PRIMARY KEY (channel, key)
);
CREATE INDEX IF NOT EXISTS threads_last ON threads(last_message_at);
CREATE INDEX IF NOT EXISTS threads_contact ON threads(contact_id);

CREATE TABLE IF NOT EXISTS messages (
  id            INTEGER PRIMARY KEY,
  thread_id     INTEGER NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
  external_id   TEXT NOT NULL,
  direction     TEXT NOT NULL CHECK (direction IN ('in', 'out')),
  sender_name   TEXT NOT NULL DEFAULT '',
  sender_handle TEXT NOT NULL DEFAULT '',
  sent_at       TEXT NOT NULL,
  text          TEXT NOT NULL DEFAULT '',
  attachments   TEXT NOT NULL DEFAULT '[]',
  meta          TEXT NOT NULL DEFAULT '{}',
  UNIQUE(thread_id, external_id)
);
CREATE INDEX IF NOT EXISTS messages_thread ON messages(thread_id, sent_at);

-- Latest triage verdict per thread. `signals` are normalised 0..1 numbers,
-- `answers` is the raw model response kept for auditing.
CREATE TABLE IF NOT EXISTS triage (
  thread_id            INTEGER PRIMARY KEY REFERENCES threads(id) ON DELETE CASCADE,
  content_hash         TEXT NOT NULL DEFAULT '',
  category             TEXT NOT NULL DEFAULT '',
  category_confidence  REAL,
  priority             TEXT NOT NULL DEFAULT '',
  score                REAL NOT NULL DEFAULT 0,
  needs_review         INTEGER NOT NULL DEFAULT 0,
  review_reason        TEXT NOT NULL DEFAULT '',
  signals              TEXT NOT NULL DEFAULT '{}',
  answers              TEXT NOT NULL DEFAULT '{}',
  summary              TEXT NOT NULL DEFAULT '',
  draft_reply          TEXT NOT NULL DEFAULT '',
  extracted            TEXT NOT NULL DEFAULT '{}',
  source               TEXT NOT NULL DEFAULT '',
  model                TEXT NOT NULL DEFAULT '',
  cost                 REAL NOT NULL DEFAULT 0,
  category_override    TEXT,
  priority_override    TEXT,
  triaged_at           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notes (
  id          INTEGER PRIMARY KEY,
  thread_id   INTEGER NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
  body        TEXT NOT NULL,
  created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
  id          INTEGER PRIMARY KEY,
  thread_id   INTEGER REFERENCES threads(id) ON DELETE CASCADE,
  kind        TEXT NOT NULL,
  detail      TEXT NOT NULL DEFAULT '{}',
  created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sync_state (
  connector TEXT NOT NULL,
  key       TEXT NOT NULL,
  value     TEXT NOT NULL,
  PRIMARY KEY (connector, key)
);

CREATE TABLE IF NOT EXISTS sync_runs (
  id           INTEGER PRIMARY KEY,
  connector    TEXT NOT NULL,
  started_at   TEXT NOT NULL,
  finished_at  TEXT,
  status       TEXT NOT NULL DEFAULT 'running',
  stats        TEXT NOT NULL DEFAULT '{}',
  error        TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS sync_runs_connector ON sync_runs(connector, id);

CREATE TABLE IF NOT EXISTS spend (
  id          INTEGER PRIMARY KEY,
  kind        TEXT NOT NULL,
  model       TEXT NOT NULL,
  cost        REAL NOT NULL DEFAULT 0,
  input_tokens INTEGER NOT NULL DEFAULT 0,
  created_at  TEXT NOT NULL
);
"""


class DB:
    def __init__(self, path: Path | str):
        self.path = str(path)
        self._local = threading.local()
        self._write_lock = threading.RLock()
        self._migrate()

    def _migrate(self) -> None:
        """Create a fresh schema, or bring an older database up to it. Safe to run every start."""
        conn = self.conn
        existing = {row["name"] for row in conn.execute("SELECT name FROM pragma_table_info('threads')")}
        needs_account = bool(existing) and "account" not in existing
        with self.tx():
            if needs_account:
                # Added when several Instagram accounts became possible. Done before the
                # schema script, which indexes this column.
                conn.execute("ALTER TABLE threads ADD COLUMN account TEXT NOT NULL DEFAULT ''")
        conn.executescript(SCHEMA)
        conn.execute("CREATE INDEX IF NOT EXISTS threads_account ON threads(channel, account)")
        if needs_account:
            self._adopt_single_instagram_account()

    def _adopt_single_instagram_account(self) -> None:
        """One-time: label conversations synced back when only one Instagram account could exist.

        That account's id is known when it was reached through graph.facebook.com
        (Unified or Facebook Login). For an Instagram Login setup only its username
        was stored, so the connector finishes the job on its next sync, and only
        if the token still belongs to that same username.
        """
        account_id = self.get_state("instagram", "fb_ig_id") or ""
        username = self.get_state("instagram", "username") or ""
        with self.tx():
            if account_id:
                self.run(
                    "UPDATE threads SET account=? WHERE channel='instagram' AND account='' AND is_demo=0",
                    (account_id,),
                )
                self.run(
                    "INSERT OR IGNORE INTO accounts(channel, key, username, label, updated_at) "
                    "VALUES('instagram', ?, ?, '', strftime('%Y-%m-%dT%H:%M:%SZ','now'))",
                    (account_id, username),
                )
                self.set_state("instagram", "account_id", account_id)
            elif username:
                self.set_state("instagram", "adopt_orphans_for", username)

    @property
    def conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = conn
        return conn

    def all(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, tuple(params)).fetchall()

    def one(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, tuple(params)).fetchone()

    def scalar(self, sql: str, params: Iterable[Any] = (), default: Any = None) -> Any:
        row = self.one(sql, params)
        return default if row is None or row[0] is None else row[0]

    def run(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, tuple(params))

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """Serialised write transaction. Re-entrant within a thread."""
        with self._write_lock:
            depth = getattr(self._local, "depth", 0)
            if depth == 0:
                self.conn.execute("BEGIN IMMEDIATE")
            self._local.depth = depth + 1
            try:
                yield self.conn
            except BaseException:
                self._local.depth = depth
                if depth == 0:
                    self.conn.execute("ROLLBACK")
                raise
            else:
                self._local.depth = depth
                if depth == 0:
                    self.conn.execute("COMMIT")

    # -- sync cursors -----------------------------------------------------

    def get_state(self, connector: str, key: str, default: str | None = None) -> str | None:
        return self.scalar(
            "SELECT value FROM sync_state WHERE connector=? AND key=?", (connector, key), default
        )

    def set_state(self, connector: str, key: str, value: Any) -> None:
        self.run(
            "INSERT INTO sync_state(connector, key, value) VALUES(?,?,?) "
            "ON CONFLICT(connector, key) DO UPDATE SET value=excluded.value",
            (connector, key, str(value)),
        )

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None


def row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return None if row is None else {key: row[key] for key in row.keys()}
