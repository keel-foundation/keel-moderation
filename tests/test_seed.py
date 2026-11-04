from app.main import DemoState
from app.seed import seed_demo


def test_seed_demo_creates_a_complete_record_and_is_idempotent(tmp_path):
    state = DemoState(
        str(tmp_path / "ledger.jsonl"),
        str(tmp_path / "state.sqlite3"),
    )

    first = seed_demo(state)
    assert first["seeded"] is True
    assert first["actions"] == 2
    assert first["cases"] == 2
    assert first["appeals"] == 2
    assert first["ledger_records"] >= 6
    assert state.verify_ledger()["valid"] is True

    actions = state.list_creator_actions("ch_demo")
    statuses = {action["status"] for action in actions["actions"]}
    assert statuses == {"active", "reversed"}

    appeals = state.list_creator_appeals("ch_demo")
    appeal_statuses = {appeal["status"] for appeal in appeals["appeals"]}
    assert appeal_statuses == {"pending", "decided"}

    second = seed_demo(state)
    assert second["seeded"] is False
    assert state.list_creator_actions("ch_demo")["total"] == 2
