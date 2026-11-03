from app.auth import Principal
from app.main import ActionCreate, AppealCreate, AppealDecision, DemoState

ACTION = ActionCreate(
    video_id="vid_demo_001",
    policy_clause="spam.mass_production",
    reason="An automated classifier flagged an original documentary as spam.",
    evidence=["model: spam-v0.1", "confidence: 0.91"],
)
APPEAL = AppealCreate(
    grounds="policy_misapplication",
    statement="Original documentary. All footage is owned and edited by the creator.",
)
DECISION = AppealDecision(
    decision="reverse",
    reasoning="Human review confirmed original production. False positive.",
)

MODERATOR = Principal("demo-reviewer", "moderator")
CREATOR = Principal("creator:demo", "creator")
APPEALS = Principal("demo-appeals-reviewer", "appeals")


def test_full_flow_survives_restart(tmp_path):
    ledger_path = str(tmp_path / "ledger.jsonl")
    state_path = str(tmp_path / "state.sqlite3")

    state = DemoState(ledger_path, state_path)
    action = state.create_action(ACTION, MODERATOR)
    appeal = state.file_appeal(action["id"], APPEAL, CREATOR)
    state.decide_appeal(appeal["id"], DECISION, APPEALS)
    state.store.close()

    reopened = DemoState(ledger_path, state_path)
    snapshot = reopened.snapshot()

    assert snapshot["action"]["status"] == "reversed"
    assert snapshot["appeal"]["decision"] == "reverse"
    assert snapshot["appeal"]["reviewer"] == "demo-appeals-reviewer"
    assert snapshot["verification"]["valid"] is True
    assert len(snapshot["ledger"]) >= 5


def test_reset_clears_state_and_survives_restart(tmp_path):
    ledger_path = str(tmp_path / "ledger.jsonl")
    state_path = str(tmp_path / "state.sqlite3")

    state = DemoState(ledger_path, state_path)
    state.create_action(ACTION, MODERATOR)
    state.reset()
    state.store.close()

    reopened = DemoState(ledger_path, state_path)
    snapshot = reopened.snapshot()

    assert snapshot["action"] is None
    assert snapshot["appeals"] == []
    assert snapshot["verification"]["valid"] is True
    assert any(record["type"] == "demo.reset" for record in snapshot["ledger"])


def test_new_action_does_not_show_previous_action_appeals(tmp_path):
    import time

    state = DemoState(
        str(tmp_path / "ledger.jsonl"), str(tmp_path / "state.sqlite3")
    )
    first = state.create_action(ACTION, MODERATOR)
    appeal = state.file_appeal(first["id"], APPEAL, CREATOR)
    state.decide_appeal(appeal["id"], DECISION, APPEALS)

    time.sleep(0.01)
    second = state.create_action(ACTION, MODERATOR)
    snapshot = state.snapshot()

    assert snapshot["action"]["id"] == second["id"]
    assert snapshot["appeal"] is None
    assert snapshot["appeals"] == []
    # The queue still retains the historical appeal for audit and review.
    assert len(state.list_appeals()) == 1


def test_appeals_are_listable_by_status(tmp_path):
    state = DemoState(
        str(tmp_path / "ledger.jsonl"), str(tmp_path / "state.sqlite3")
    )
    action = state.create_action(ACTION, MODERATOR)
    appeal = state.file_appeal(action["id"], APPEAL, CREATOR)

    pending = state.list_appeals(status="pending")
    assert [item["id"] for item in pending] == [appeal["id"]]
    assert state.list_appeals(status="decided") == []
    stored = state.get_appeal(appeal["id"])
    assert stored is not None
    assert stored["status"] == "pending"


def test_creator_channel_scope_survives_restart(tmp_path):
    ledger_path = str(tmp_path / "ledger.jsonl")
    state_path = str(tmp_path / "state.sqlite3")

    state = DemoState(ledger_path, state_path)
    scoped_action = state.create_action(
        ActionCreate(
            video_id="vid_demo_001",
            channel_id="ch_persist",
            policy_clause="spam.mass_production",
            reason="Scoped action.",
            evidence=["model: spam-v0.1"],
        ),
        MODERATOR,
    )
    state.store.close()

    reopened = DemoState(ledger_path, state_path)
    assert reopened.get_creator_case(scoped_action["case_id"], "ch_persist") is not None
    cases = reopened.list_creator_cases("ch_persist")
    assert cases["total"] == 1
    assert cases["cases"][0]["decision_action_id"] == scoped_action["id"]

    stored = reopened.store.get_action(scoped_action["id"])
    assert stored is not None
    stored["status"] = "expired"
    reopened.store.save_action(stored)
    assert reopened.get_creator_action(scoped_action["id"], "ch_persist") is not None
    assert reopened.get_creator_action(scoped_action["id"], "ch_other") is None


def test_legacy_schema_upgrades_with_channel_scope(tmp_path):
    import json
    import sqlite3

    from app.storage import SCHEMA_VERSION, SqliteStore

    path = tmp_path / "legacy.sqlite3"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE cases (
            id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            subject_type TEXT NOT NULL,
            subject_id TEXT NOT NULL,
            data TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE actions (
            id TEXT PRIMARY KEY,
            case_id TEXT NOT NULL,
            status TEXT NOT NULL,
            data TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE appeals (
            id TEXT PRIMARY KEY,
            action_id TEXT NOT NULL,
            status TEXT NOT NULL,
            submitted_at TEXT NOT NULL,
            data TEXT NOT NULL
        );
        """
    )
    case = {
        "id": "case_legacy",
        "subject": {"type": "video", "id": "vid_legacy"},
        "status": "decided",
        "severity": "low",
        "policy_refs": ["spam.mass_production"],
        "signals": [],
        "evidence": [],
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
    }
    connection.execute(
        """
        INSERT INTO cases (id, status, subject_type, subject_id, data, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            case["id"],
            case["status"],
            case["subject"]["type"],
            case["subject"]["id"],
            json.dumps(case),
            case["created_at"],
            case["updated_at"],
        ),
    )
    connection.commit()
    connection.close()

    store = SqliteStore(path)
    try:
        assert store.get_case("case_legacy") == case
        assert store.get_creator_case("case_legacy", "") == case
        with store.lock:
            version = store.connection.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()["value"]
            columns = {
                row["name"]
                for row in store.connection.execute("PRAGMA table_info(cases)")
            }
        assert version == str(SCHEMA_VERSION)
        assert "channel_id" in columns
    finally:
        store.close()
