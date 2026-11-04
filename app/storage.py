from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from threading import RLock

SCHEMA_VERSION = 2

POSTGRES_PREFIXES = ("postgres://", "postgresql://")


def create_store(location: str | Path):
    """Open the configured workflow store.

    PostgreSQL DSNs use the migration-backed store. Everything else keeps the
    SQLite path used by local tests and offline demos.
    """

    value = str(location)
    if value.startswith(POSTGRES_PREFIXES):
        from .postgres_store import PostgresStore

        return PostgresStore(value)
    return SqliteStore(value)


SCHEMA_TABLES = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cases (
    id TEXT PRIMARY KEY,
    channel_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    subject_type TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    data TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS actions (
    id TEXT PRIMARY KEY,
    channel_id TEXT NOT NULL DEFAULT '',
    case_id TEXT NOT NULL,
    status TEXT NOT NULL,
    data TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS appeals (
    id TEXT PRIMARY KEY,
    channel_id TEXT NOT NULL DEFAULT '',
    action_id TEXT NOT NULL,
    status TEXT NOT NULL,
    submitted_at TEXT NOT NULL,
    data TEXT NOT NULL
);
"""

SCHEMA_INDEXES = """
CREATE INDEX IF NOT EXISTS cases_channel_created ON cases (channel_id, created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS cases_channel_status ON cases (channel_id, status, created_at DESC);
CREATE INDEX IF NOT EXISTS actions_channel_created
    ON actions (channel_id, created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS actions_channel_status ON actions (channel_id, status, created_at DESC);
CREATE INDEX IF NOT EXISTS appeals_action_id ON appeals (action_id);
CREATE INDEX IF NOT EXISTS appeals_status ON appeals (status);
CREATE INDEX IF NOT EXISTS appeals_channel_submitted
    ON appeals (channel_id, submitted_at ASC, id ASC);
CREATE INDEX IF NOT EXISTS appeals_channel_status ON appeals (channel_id, status, submitted_at ASC);
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
            self.connection.executescript(SCHEMA_TABLES)
            self._ensure_column("cases", "channel_id", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column("actions", "channel_id", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column("appeals", "channel_id", "TEXT NOT NULL DEFAULT ''")
            self.connection.executescript(SCHEMA_INDEXES)
            self.connection.execute(
                """
                INSERT INTO meta (key, value) VALUES ('schema_version', ?)
                ON CONFLICT (key) DO UPDATE SET value = excluded.value
                """,
                (str(SCHEMA_VERSION),),
            )

    def _ensure_column(self, table: str, column: str, definition: str) -> None:
        columns = {
            row["name"] for row in self.connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column not in columns:
            self.connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def reset(self) -> None:
        with self.lock, self.connection:
            self.connection.execute("DELETE FROM appeals")
            self.connection.execute("DELETE FROM actions")
            self.connection.execute("DELETE FROM cases")

    def save_case(self, case: dict, channel_id: str | None = None) -> None:
        with self.lock, self.connection:
            self.connection.execute(
                """
                INSERT INTO cases
                    (id, channel_id, status, subject_type, subject_id, data, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (id) DO UPDATE SET
                    status = excluded.status,
                    subject_type = excluded.subject_type,
                    subject_id = excluded.subject_id,
                    data = excluded.data,
                    updated_at = excluded.updated_at,
                    channel_id = CASE
                        WHEN excluded.channel_id <> '' THEN excluded.channel_id
                        ELSE cases.channel_id
                    END
                """,
                (
                    case["id"],
                    channel_id or "",
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

    def get_creator_case(self, case_id: str, channel_id: str) -> dict | None:
        with self.lock:
            row = self.connection.execute(
                "SELECT data FROM cases WHERE id = ? AND channel_id = ?",
                (case_id, channel_id),
            ).fetchone()
        return json.loads(row["data"]) if row else None

    def get_case_channel_id(self, case_id: str) -> str | None:
        return self._channel_id("cases", case_id)

    def list_cases(
        self,
        status: str | None = None,
        channel_id: str | None = None,
        video_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[dict], int]:
        clauses: list[str] = []
        params: list[str | int] = []
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if channel_id is not None:
            clauses.append("channel_id = ?")
            params.append(channel_id)
        if video_id is not None:
            clauses.extend(["subject_type = 'video'", "subject_id = ?"])
            params.append(video_id)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self.lock:
            total_row = self.connection.execute(
                f"SELECT COUNT(*) AS total FROM cases{where}", tuple(params)
            ).fetchone()
            rows = self.connection.execute(
                f"""
                SELECT data, channel_id FROM cases{where}
                ORDER BY created_at DESC, id DESC
                LIMIT ? OFFSET ?
                """,
                tuple([*params, limit, offset]),
            ).fetchall()
        items = []
        for row in rows:
            item = json.loads(row["data"])
            item["channel_id"] = row["channel_id"]
            items.append(item)
        return items, total_row["total"]

    def list_creator_cases(
        self,
        channel_id: str,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
        video_id: str | None = None,
    ) -> tuple[list[dict], int]:
        extra_clauses: list[str] = []
        extra_args: list[str | int] = []
        if video_id is not None:
            extra_clauses.extend(["subject_type = 'video'", "subject_id = ?"])
            extra_args.append(video_id)
        return self._list_scoped(
            "cases",
            "created_at DESC, id DESC",
            channel_id,
            status,
            limit,
            offset,
            extra_clauses,
            extra_args,
        )

    def save_action(self, action: dict, channel_id: str | None = None) -> None:
        with self.lock, self.connection:
            self.connection.execute(
                """
                INSERT INTO actions (id, channel_id, case_id, status, data, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT (id) DO UPDATE SET
                    status = excluded.status,
                    data = excluded.data,
                    channel_id = CASE
                        WHEN excluded.channel_id <> '' THEN excluded.channel_id
                        ELSE actions.channel_id
                    END
                """,
                (
                    action["id"],
                    channel_id or "",
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

    def get_active_action(self, channel_id: str) -> dict | None:
        with self.lock:
            row = self.connection.execute(
                """
                SELECT data FROM actions
                WHERE channel_id = ? AND status = 'active'
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """,
                (channel_id,),
            ).fetchone()
        return json.loads(row["data"]) if row else None

    def get_creator_action(self, action_id: str, channel_id: str) -> dict | None:
        with self.lock:
            row = self.connection.execute(
                "SELECT data FROM actions WHERE id = ? AND channel_id = ?",
                (action_id, channel_id),
            ).fetchone()
        return json.loads(row["data"]) if row else None

    def list_actions(
        self,
        status: str | None = None,
        channel_id: str | None = None,
        video_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[dict], int]:
        clauses: list[str] = []
        params: list[str | int] = []
        if status is not None:
            clauses.append("a.status = ?")
            params.append(status)
        if channel_id is not None:
            clauses.append("a.channel_id = ?")
            params.append(channel_id)
        join = ""
        if video_id is not None:
            join = " JOIN cases c ON c.id = a.case_id"
            clauses.extend(["c.subject_type = 'video'", "c.subject_id = ?"])
            params.append(video_id)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self.lock:
            total_row = self.connection.execute(
                f"SELECT COUNT(*) AS total FROM actions a{join}{where}", tuple(params)
            ).fetchone()
            rows = self.connection.execute(
                f"""
                SELECT a.data, a.channel_id FROM actions a{join}{where}
                ORDER BY a.created_at DESC, a.id DESC
                LIMIT ? OFFSET ?
                """,
                tuple([*params, limit, offset]),
            ).fetchall()
        items = []
        for row in rows:
            item = json.loads(row["data"])
            item["channel_id"] = row["channel_id"]
            items.append(item)
        return items, total_row["total"]

    def list_creator_actions(
        self,
        channel_id: str,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
        video_id: str | None = None,
    ) -> tuple[list[dict], int]:
        if video_id is None:
            return self._list_scoped(
                "actions", "created_at DESC, id DESC", channel_id, status, limit, offset
            )

        clauses = [
            "a.channel_id = ?",
            "c.channel_id = ?",
            "c.subject_type = 'video'",
            "c.subject_id = ?",
        ]
        params: list[str | int] = [channel_id, channel_id, video_id]
        if status is not None:
            clauses.append("a.status = ?")
            params.append(status)
        where = " AND ".join(clauses)
        with self.lock:
            total_row = self.connection.execute(
                f"""
                SELECT COUNT(*) AS total
                FROM actions a JOIN cases c ON c.id = a.case_id
                WHERE {where}
                """,
                tuple(params),
            ).fetchone()
            rows = self.connection.execute(
                f"""
                SELECT a.data
                FROM actions a JOIN cases c ON c.id = a.case_id
                WHERE {where}
                ORDER BY a.created_at DESC, a.id DESC
                LIMIT ? OFFSET ?
                """,
                tuple([*params, limit, offset]),
            ).fetchall()
        return [json.loads(row["data"]) for row in rows], total_row["total"]

    def save_appeal(self, appeal: dict, channel_id: str | None = None) -> None:
        with self.lock, self.connection:
            self.connection.execute(
                """
                INSERT INTO appeals (id, channel_id, action_id, status, submitted_at, data)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT (id) DO UPDATE SET
                    status = excluded.status,
                    data = excluded.data,
                    channel_id = CASE
                        WHEN excluded.channel_id <> '' THEN excluded.channel_id
                        ELSE appeals.channel_id
                    END
                """,
                (
                    appeal["id"],
                    channel_id or "",
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

    def get_creator_appeal(self, appeal_id: str, channel_id: str) -> dict | None:
        with self.lock:
            row = self.connection.execute(
                "SELECT data FROM appeals WHERE id = ? AND channel_id = ?",
                (appeal_id, channel_id),
            ).fetchone()
        return json.loads(row["data"]) if row else None

    def list_creator_appeals(
        self,
        channel_id: str,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[dict], int]:
        return self._list_scoped(
            "appeals",
            "submitted_at ASC, id ASC",
            channel_id,
            status,
            limit,
            offset,
        )

    def list_appeals(self, status: str | None = None, action_id: str | None = None) -> list[dict]:
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

    def get_action_channel_id(self, action_id: str) -> str | None:
        return self._channel_id("actions", action_id)

    def get_appeal_channel_id(self, appeal_id: str) -> str | None:
        return self._channel_id("appeals", appeal_id)

    def creator_summary(self, channel_id: str) -> dict:
        with self.lock:
            cases = self.connection.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    COALESCE(SUM(CASE WHEN status IN ('open', 'reviewing', 'appealed')
                        THEN 1 ELSE 0 END), 0) AS open
                FROM cases WHERE channel_id = ?
                """,
                (channel_id,),
            ).fetchone()
            actions = self.connection.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    COALESCE(SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END), 0) AS active
                FROM actions WHERE channel_id = ?
                """,
                (channel_id,),
            ).fetchone()
            appeals = self.connection.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    COALESCE(SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END), 0) AS pending,
                    COALESCE(SUM(CASE WHEN status = 'decided' THEN 1 ELSE 0 END), 0) AS decided
                FROM appeals WHERE channel_id = ?
                """,
                (channel_id,),
            ).fetchone()
        return {
            "channel_id": channel_id,
            "cases_total": cases["total"],
            "open_cases": cases["open"],
            "actions_total": actions["total"],
            "active_actions": actions["active"],
            "appeals_total": appeals["total"],
            "pending_appeals": appeals["pending"],
            "decided_appeals": appeals["decided"],
        }

    def staff_summary(self) -> dict:
        with self.lock:
            cases = self.connection.execute(
                """
                SELECT COUNT(*) AS total,
                       COALESCE(SUM(CASE WHEN status IN ('open', 'reviewing', 'appealed')
                           THEN 1 ELSE 0 END), 0) AS open
                FROM cases
                """
            ).fetchone()
            actions = self.connection.execute(
                """
                SELECT COUNT(*) AS total,
                       COALESCE(SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END), 0) AS active
                FROM actions
                """
            ).fetchone()
            appeals = self.connection.execute(
                """
                SELECT COUNT(*) AS total,
                       COALESCE(SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END), 0) AS pending,
                       COALESCE(SUM(CASE WHEN status = 'decided' THEN 1 ELSE 0 END), 0) AS decided
                FROM appeals
                """
            ).fetchone()
        return {
            "cases_total": cases["total"],
            "open_cases": cases["open"],
            "actions_total": actions["total"],
            "active_actions": actions["active"],
            "appeals_total": appeals["total"],
            "pending_appeals": appeals["pending"],
            "decided_appeals": appeals["decided"],
        }

    def _channel_id(self, table: str, item_id: str) -> str | None:
        with self.lock:
            row = self.connection.execute(
                f"SELECT channel_id FROM {table} WHERE id = ?", (item_id,)
            ).fetchone()
        return row["channel_id"] if row else None

    def _list_scoped(
        self,
        table: str,
        order: str,
        channel_id: str,
        status: str | None,
        limit: int,
        offset: int,
        extra_clauses: list[str] | None = None,
        extra_args: list[str | int] | None = None,
    ) -> tuple[list[dict], int]:
        clauses = ["channel_id = ?"]
        params: list[str | int] = [channel_id]
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if extra_clauses:
            clauses.extend(extra_clauses)
        if extra_args:
            params.extend(extra_args)
        where = " AND ".join(clauses)
        with self.lock:
            total_row = self.connection.execute(
                f"SELECT COUNT(*) AS total FROM {table} WHERE {where}", tuple(params)
            ).fetchone()
            rows = self.connection.execute(
                f"""
                SELECT data FROM {table}
                WHERE {where}
                ORDER BY {order}
                LIMIT ? OFFSET ?
                """,
                tuple([*params, limit, offset]),
            ).fetchall()
        return [json.loads(row["data"]) for row in rows], total_row["total"]

    def close(self) -> None:
        with self.lock:
            self.connection.close()

    def public_video_record(self, video_id: str) -> dict:
        with self.lock:
            cases = self.connection.execute(
                """
                SELECT data FROM cases
                WHERE subject_type = 'video' AND subject_id = ?
                ORDER BY created_at DESC, id DESC
                """,
                (video_id,),
            ).fetchall()
            actions = self.connection.execute(
                """
                SELECT a.data FROM actions a
                JOIN cases c ON c.id = a.case_id
                WHERE c.subject_type = 'video' AND c.subject_id = ?
                ORDER BY a.created_at DESC, a.id DESC
                """,
                (video_id,),
            ).fetchall()
            appeals = self.connection.execute(
                """
                SELECT ap.data FROM appeals ap
                JOIN actions a ON a.id = ap.action_id
                JOIN cases c ON c.id = a.case_id
                WHERE c.subject_type = 'video' AND c.subject_id = ?
                ORDER BY ap.submitted_at DESC, ap.id DESC
                """,
                (video_id,),
            ).fetchall()
        return {
            "video_id": video_id,
            "cases": [json.loads(row["data"]) for row in cases],
            "actions": [json.loads(row["data"]) for row in actions],
            "appeals": [json.loads(row["data"]) for row in appeals],
        }
