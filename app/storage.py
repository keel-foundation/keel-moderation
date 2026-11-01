from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from threading import RLock

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cases (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    subject_type TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    data TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS actions (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    status TEXT NOT NULL,
    data TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS appeals (
    id TEXT PRIMARY KEY,
    action_id TEXT NOT NULL,
    status TEXT NOT NULL,
    submitted_at TEXT NOT NULL,
    data TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS appeals_action_id ON appeals (action_id);
CREATE INDEX IF NOT EXISTS appeals_status ON appeals (status);
"""


class SqliteStore:
    """Durable workflow state for the moderation demo.

    The hash-chained audit ledger remains append-only JSONL. This store is for cases, actions,
    and appeals so the service can survive a restart. The SQL is intentionally portable; the
    SQLite-specific pragmas are isolated in ``__init__`` and the next step is PostgreSQL with
    real migrations.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.lock = RLock()
        self.connection = sqlite3.connect(self.path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self._init()

    def _init(self) -> None:
        with self.lock, self.connection:
            self.connection.executescript(SCHEMA)
            self.connection.execute(
                "INSERT INTO meta (key, value) VALUES ('schema_version', ?) "
                "ON CONFLICT (key) DO NOTHING",
                (str(SCHEMA_VERSION),),
            )

    def reset(self) -> None:
        with self.lock, self.connection:
            self.connection.execute("DELETE FROM appeals")
            self.connection.execute("DELETE FROM actions")
            self.connection.execute("DELETE FROM cases")

    def save_case(self, case: dict) -> None:
        with self.lock, self.connection:
            self.connection.execute(
                """
                INSERT INTO cases
                    (id, status, subject_type, subject_id, data, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (id) DO UPDATE SET
                    status = excluded.status,
                    subject_type = excluded.subject_type,
                    subject_id = excluded.subject_id,
                    data = excluded.data,
                    updated_at = excluded.updated_at
                """,
                (
                    case["id"],
                    case["status"],
                    case["subject"]["type"],
                    case["subject"]["id"],
                    json.dumps(case, ensure_ascii=False),
                    case["created_at"],
                    case["updated_at"],
                ),
            )

    def get_case(self, case_id: str | None = None) -> dict | None:
        query = "SELECT data FROM cases"
        params: tuple[str, ...] = ()
        if case_id is not None:
            query += " WHERE id = ?"
            params = (case_id,)
        query += " ORDER BY created_at DESC LIMIT 1"
        with self.lock:
            row = self.connection.execute(query, params).fetchone()
        return json.loads(row["data"]) if row else None

    def save_action(self, action: dict) -> None:
        with self.lock, self.connection:
            self.connection.execute(
                """
                INSERT INTO actions (id, case_id, status, data, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (id) DO UPDATE SET
                    status = excluded.status,
                    data = excluded.data
                """,
                (
                    action["id"],
                    action["case_id"],
                    action["status"],
                    json.dumps(action, ensure_ascii=False),
                    action["created_at"],
                ),
            )

    def get_action(self, action_id: str | None = None) -> dict | None:
        query = "SELECT data FROM actions"
        params: tuple[str, ...] = ()
        if action_id is not None:
            query += " WHERE id = ?"
            params = (action_id,)
        query += " ORDER BY created_at DESC LIMIT 1"
        with self.lock:
            row = self.connection.execute(query, params).fetchone()
        return json.loads(row["data"]) if row else None

    def save_appeal(self, appeal: dict) -> None:
        with self.lock, self.connection:
            self.connection.execute(
                """
                INSERT INTO appeals (id, action_id, status, submitted_at, data)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (id) DO UPDATE SET
                    status = excluded.status,
                    data = excluded.data
                """,
                (
                    appeal["id"],
                    appeal["action_id"],
                    appeal["status"],
                    appeal["submitted_at"],
                    json.dumps(appeal, ensure_ascii=False),
                ),
            )

    def get_appeal(self, appeal_id: str) -> dict | None:
        with self.lock:
            row = self.connection.execute(
                "SELECT data FROM appeals WHERE id = ?", (appeal_id,)
            ).fetchone()
        return json.loads(row["data"]) if row else None

    def list_appeals(
        self, status: str | None = None, action_id: str | None = None
    ) -> list[dict]:
        clauses: list[str] = []
        params: list[str] = []
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if action_id is not None:
            clauses.append("action_id = ?")
            params.append(action_id)
        query = "SELECT data FROM appeals"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY submitted_at ASC, id ASC"
        with self.lock:
            rows = self.connection.execute(query, tuple(params)).fetchall()
        return [json.loads(row["data"]) for row in rows]

    def close(self) -> None:
        with self.lock:
            self.connection.close()
