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
