from __future__ import annotations

import json
from pathlib import Path
from threading import RLock
from typing import Any

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def _load_psycopg():
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as error:  # pragma: no cover - exercised by misconfiguration
        raise RuntimeError(
            "PostgreSQL moderation state requires psycopg 3. "
            "Install the runtime dependencies or set KEELTUBE_STATE_DB to a SQLite path."
        ) from error
    return psycopg, dict_row


class PostgresStore:
    """Durable PostgreSQL workflow state for cases, actions, and appeals.

    The append-only JSONL ledger is intentionally separate. SQL uses the same
    shape as the SQLite store so both backends return identical domain objects.
    """

    def __init__(self, dsn: str) -> None:
        psycopg, dict_row = _load_psycopg()
        self.dsn = dsn
        self.lock = RLock()
        self.connection = psycopg.connect(dsn, autocommit=True, row_factory=dict_row)
        self._init()

    def _init(self) -> None:
        with self.lock:
            self.connection.execute(
                "SELECT pg_advisory_lock(hashtextextended('keel_moderation_migrations', 0))"
            )
            try:
                self.connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS moderation_schema_migrations (
                        version TEXT PRIMARY KEY,
                        applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                    """
                )
                applied = {
                    row["version"]
                    for row in self.connection.execute(
                        "SELECT version FROM moderation_schema_migrations"
                    ).fetchall()
                }
                for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
                    version = path.stem
                    if version in applied:
                        continue
                    body = path.read_text(encoding="utf-8")
                    with self.connection.transaction():
                        self.connection.execute(body)
                        self.connection.execute(
                            "INSERT INTO moderation_schema_migrations (version) VALUES (%s)",
                            (version,),
                        )
            finally:
                self.connection.execute(
                    "SELECT pg_advisory_unlock(hashtextextended('keel_moderation_migrations', 0))"
                )

    def _fetchone(self, query: str, params: tuple[Any, ...] = ()) -> dict | None:
        with self.lock:
            return self.connection.execute(query, params).fetchone()

    def _fetchall(self, query: str, params: tuple[Any, ...] = ()) -> list[dict]:
        with self.lock:
            return self.connection.execute(query, params).fetchall()

    def _execute(self, query: str, params: tuple[Any, ...] = ()) -> None:
        with self.lock:
            self.connection.execute(query, params)

    def reset(self) -> None:
        with self.lock, self.connection.transaction():
            self.connection.execute("DELETE FROM moderation_appeals")
            self.connection.execute("DELETE FROM moderation_actions")
            self.connection.execute("DELETE FROM moderation_cases")

    def save_case(self, case: dict, channel_id: str | None = None) -> None:
        self._execute(
            """
            INSERT INTO moderation_cases
                (id, channel_id, status, subject_type, subject_id, data, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s)
            ON CONFLICT (id) DO UPDATE SET
                status = EXCLUDED.status,
                subject_type = EXCLUDED.subject_type,
                subject_id = EXCLUDED.subject_id,
                data = EXCLUDED.data,
                updated_at = EXCLUDED.updated_at,
                channel_id = CASE
                    WHEN EXCLUDED.channel_id <> '' THEN EXCLUDED.channel_id
                    ELSE moderation_cases.channel_id
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
        query = "SELECT data FROM moderation_cases"
        params: tuple[Any, ...] = ()
        if case_id is not None:
            query += " WHERE id = %s"
            params = (case_id,)
        query += " ORDER BY created_at DESC LIMIT 1"
        row = self._fetchone(query, params)
        return row["data"] if row else None

    def get_creator_case(self, case_id: str, channel_id: str) -> dict | None:
        row = self._fetchone(
            "SELECT data FROM moderation_cases WHERE id = %s AND channel_id = %s",
            (case_id, channel_id),
        )
        return row["data"] if row else None

    def get_case_channel_id(self, case_id: str) -> str | None:
        return self._channel_id("moderation_cases", case_id)

    def list_cases(
        self,
        status: str | None = None,
        channel_id: str | None = None,
        video_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[dict], int]:
        clauses: list[str] = []
        params: list[Any] = []
        if status is not None:
            clauses.append("status = %s")
            params.append(status)
        if channel_id is not None:
            clauses.append("channel_id = %s")
            params.append(channel_id)
        if video_id is not None:
            clauses.extend(["subject_type = 'video'", "subject_id = %s"])
            params.append(video_id)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        total_row = self._fetchone(
            f"SELECT COUNT(*) AS total FROM moderation_cases{where}", tuple(params)
        )
        rows = self._fetchall(
            f"""
            SELECT data, channel_id FROM moderation_cases{where}
            ORDER BY created_at DESC, id DESC
            LIMIT %s OFFSET %s
            """,
            tuple([*params, limit, offset]),
        )
        items = []
        for row in rows:
            item = row["data"]
            item["channel_id"] = row["channel_id"]
            items.append(item)
        return items, _count(total_row, "total")

    def list_creator_cases(
        self,
        channel_id: str,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
        video_id: str | None = None,
    ) -> tuple[list[dict], int]:
        extra_clauses: list[str] = []
        extra_args: list[Any] = []
        if video_id is not None:
            extra_clauses.extend(["subject_type = 'video'", "subject_id = %s"])
            extra_args.append(video_id)
        return self._list_scoped(
            "moderation_cases",
            "created_at DESC, id DESC",
            channel_id,
            status,
            limit,
            offset,
            extra_clauses,
            extra_args,
        )

    def save_action(self, action: dict, channel_id: str | None = None) -> None:
        self._execute(
            """
            INSERT INTO moderation_actions (id, channel_id, case_id, status, data, created_at)
            VALUES (%s, %s, %s, %s, %s::jsonb, %s)
            ON CONFLICT (id) DO UPDATE SET
                status = EXCLUDED.status,
                data = EXCLUDED.data,
                channel_id = CASE
                    WHEN EXCLUDED.channel_id <> '' THEN EXCLUDED.channel_id
                    ELSE moderation_actions.channel_id
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
        query = "SELECT data FROM moderation_actions"
        params: tuple[Any, ...] = ()
        if action_id is not None:
            query += " WHERE id = %s"
            params = (action_id,)
        query += " ORDER BY created_at DESC LIMIT 1"
        row = self._fetchone(query, params)
        return row["data"] if row else None

    def get_active_action(self, channel_id: str) -> dict | None:
        row = self._fetchone(
            """
            SELECT data FROM moderation_actions
            WHERE channel_id = %s AND status = 'active'
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            (channel_id,),
        )
        return row["data"] if row else None

    def get_creator_action(self, action_id: str, channel_id: str) -> dict | None:
        row = self._fetchone(
            "SELECT data FROM moderation_actions WHERE id = %s AND channel_id = %s",
            (action_id, channel_id),
        )
        return row["data"] if row else None

    def list_actions(
        self,
        status: str | None = None,
        channel_id: str | None = None,
        video_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[dict], int]:
        clauses: list[str] = []
        params: list[Any] = []
        if status is not None:
            clauses.append("a.status = %s")
            params.append(status)
        if channel_id is not None:
            clauses.append("a.channel_id = %s")
            params.append(channel_id)
        join = ""
        if video_id is not None:
            join = " JOIN moderation_cases c ON c.id = a.case_id"
            clauses.extend(["c.subject_type = 'video'", "c.subject_id = %s"])
            params.append(video_id)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        total_row = self._fetchone(
            f"SELECT COUNT(*) AS total FROM moderation_actions a{join}{where}",
            tuple(params),
        )
        rows = self._fetchall(
            f"""
            SELECT a.data, a.channel_id FROM moderation_actions a{join}{where}
            ORDER BY a.created_at DESC, a.id DESC
            LIMIT %s OFFSET %s
            """,
            tuple([*params, limit, offset]),
        )
        items = []
        for row in rows:
            item = row["data"]
            item["channel_id"] = row["channel_id"]
            items.append(item)
        return items, _count(total_row, "total")

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
                "moderation_actions",
                "created_at DESC, id DESC",
                channel_id,
                status,
                limit,
                offset,
            )

        clauses = [
            "a.channel_id = %s",
            "c.channel_id = %s",
            "c.subject_type = 'video'",
            "c.subject_id = %s",
        ]
        params: list[Any] = [channel_id, channel_id, video_id]
        if status is not None:
            clauses.append("a.status = %s")
            params.append(status)
        where = " AND ".join(clauses)
        total_row = self._fetchone(
            f"""
            SELECT COUNT(*) AS total
            FROM moderation_actions a JOIN moderation_cases c ON c.id = a.case_id
            WHERE {where}
            """,
            tuple(params),
        )
        rows = self._fetchall(
            f"""
            SELECT a.data
            FROM moderation_actions a JOIN moderation_cases c ON c.id = a.case_id
            WHERE {where}
            ORDER BY a.created_at DESC, a.id DESC
            LIMIT %s OFFSET %s
            """,
            tuple([*params, limit, offset]),
        )
        return [row["data"] for row in rows], int(total_row["total"]) if total_row else 0

    def save_appeal(self, appeal: dict, channel_id: str | None = None) -> None:
        self._execute(
            """
            INSERT INTO moderation_appeals (id, channel_id, action_id, status, submitted_at, data)
            VALUES (%s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (id) DO UPDATE SET
                status = EXCLUDED.status,
                data = EXCLUDED.data,
                channel_id = CASE
                    WHEN EXCLUDED.channel_id <> '' THEN EXCLUDED.channel_id
                    ELSE moderation_appeals.channel_id
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
        row = self._fetchone("SELECT data FROM moderation_appeals WHERE id = %s", (appeal_id,))
        return row["data"] if row else None

    def get_creator_appeal(self, appeal_id: str, channel_id: str) -> dict | None:
        row = self._fetchone(
            "SELECT data FROM moderation_appeals WHERE id = %s AND channel_id = %s",
            (appeal_id, channel_id),
        )
        return row["data"] if row else None

    def list_creator_appeals(
        self,
        channel_id: str,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[dict], int]:
        return self._list_scoped(
            "moderation_appeals",
            "submitted_at ASC, id ASC",
            channel_id,
            status,
            limit,
            offset,
        )

    def list_appeals(self, status: str | None = None, action_id: str | None = None) -> list[dict]:
        clauses: list[str] = []
        params: list[Any] = []
        if status is not None:
            clauses.append("status = %s")
            params.append(status)
        if action_id is not None:
            clauses.append("action_id = %s")
            params.append(action_id)
        query = "SELECT data FROM moderation_appeals"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY submitted_at ASC, id ASC"
        return [row["data"] for row in self._fetchall(query, tuple(params))]

    def get_action_channel_id(self, action_id: str) -> str | None:
        return self._channel_id("moderation_actions", action_id)

    def get_appeal_channel_id(self, appeal_id: str) -> str | None:
        return self._channel_id("moderation_appeals", appeal_id)

    def creator_summary(self, channel_id: str) -> dict:
        cases = self._fetchone(
            """
            SELECT
                COUNT(*) AS total,
                COALESCE(SUM(CASE WHEN status IN ('open', 'reviewing', 'appealed')
                    THEN 1 ELSE 0 END), 0) AS open
            FROM moderation_cases WHERE channel_id = %s
            """,
            (channel_id,),
        )
        actions = self._fetchone(
            """
            SELECT
                COUNT(*) AS total,
                COALESCE(SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END), 0) AS active
            FROM moderation_actions WHERE channel_id = %s
            """,
            (channel_id,),
        )
        appeals = self._fetchone(
            """
            SELECT
                COUNT(*) AS total,
                COALESCE(SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END), 0) AS pending,
                COALESCE(SUM(CASE WHEN status = 'decided' THEN 1 ELSE 0 END), 0) AS decided
            FROM moderation_appeals WHERE channel_id = %s
            """,
            (channel_id,),
        )
        return {
            "channel_id": channel_id,
            "cases_total": _count(cases, "total"),
            "open_cases": _count(cases, "open"),
            "actions_total": _count(actions, "total"),
            "active_actions": _count(actions, "active"),
            "appeals_total": _count(appeals, "total"),
            "pending_appeals": _count(appeals, "pending"),
            "decided_appeals": _count(appeals, "decided"),
        }

    def staff_summary(self) -> dict:
        cases = self._fetchone(
            """
            SELECT COUNT(*) AS total,
                   COALESCE(SUM(CASE WHEN status IN ('open', 'reviewing', 'appealed')
                       THEN 1 ELSE 0 END), 0) AS open
            FROM moderation_cases
            """
        )
        actions = self._fetchone(
            """
            SELECT COUNT(*) AS total,
                   COALESCE(SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END), 0) AS active
            FROM moderation_actions
            """
        )
        appeals = self._fetchone(
            """
            SELECT COUNT(*) AS total,
                   COALESCE(SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END), 0) AS pending,
                   COALESCE(SUM(CASE WHEN status = 'decided' THEN 1 ELSE 0 END), 0) AS decided
            FROM moderation_appeals
            """
        )
        return {
            "cases_total": _count(cases, "total"),
            "open_cases": _count(cases, "open"),
            "actions_total": _count(actions, "total"),
            "active_actions": _count(actions, "active"),
            "appeals_total": _count(appeals, "total"),
            "pending_appeals": _count(appeals, "pending"),
            "decided_appeals": _count(appeals, "decided"),
        }

    def _channel_id(self, table: str, item_id: str) -> str | None:
        row = self._fetchone(f"SELECT channel_id FROM {table} WHERE id = %s", (item_id,))
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
        extra_args: list[Any] | None = None,
    ) -> tuple[list[dict], int]:
        clauses = ["channel_id = %s"]
        params: list[Any] = [channel_id]
        if status is not None:
            clauses.append("status = %s")
            params.append(status)
        if extra_clauses:
            clauses.extend(extra_clauses)
        if extra_args:
            params.extend(extra_args)
        where = " AND ".join(clauses)
        total_row = self._fetchone(
            f"SELECT COUNT(*) AS total FROM {table} WHERE {where}", tuple(params)
        )
        rows = self._fetchall(
            f"""
            SELECT data FROM {table}
            WHERE {where}
            ORDER BY {order}
            LIMIT %s OFFSET %s
            """,
            tuple([*params, limit, offset]),
        )
        return [row["data"] for row in rows], _count(total_row, "total")

    def close(self) -> None:
        with self.lock:
            self.connection.close()

    def public_video_record(self, video_id: str) -> dict:
        cases = self._fetchall(
            """
            SELECT data FROM moderation_cases
            WHERE subject_type = 'video' AND subject_id = %s
            ORDER BY created_at DESC, id DESC
            """,
            (video_id,),
        )
        actions = self._fetchall(
            """
            SELECT a.data FROM moderation_actions a
            JOIN moderation_cases c ON c.id = a.case_id
            WHERE c.subject_type = 'video' AND c.subject_id = %s
            ORDER BY a.created_at DESC, a.id DESC
            """,
            (video_id,),
        )
        appeals = self._fetchall(
            """
            SELECT ap.data FROM moderation_appeals ap
            JOIN moderation_actions a ON a.id = ap.action_id
            JOIN moderation_cases c ON c.id = a.case_id
            WHERE c.subject_type = 'video' AND c.subject_id = %s
            ORDER BY ap.submitted_at DESC, ap.id DESC
            """,
            (video_id,),
        )
        return {
            "video_id": video_id,
            "cases": [row["data"] for row in cases],
            "actions": [row["data"] for row in actions],
            "appeals": [row["data"] for row in appeals],
        }


def _count(row: dict | None, key: str) -> int:
    return int(row[key]) if row and row.get(key) is not None else 0
