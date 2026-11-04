import os

import pytest

from app.auth import Principal
from app.main import (
    ActionCreate,
    AppealCreate,
    AppealDecision,
    CaseActionCreate,
    DemoState,
    SignalCreate,
)

DSN = os.getenv("KEELTUBE_TEST_DATABASE_URL", "").strip()

pytestmark = pytest.mark.skipif(not DSN, reason="KEELTUBE_TEST_DATABASE_URL is not set")

ACTION = ActionCreate(
    video_id="vid_pg_001",
    channel_id="ch_pg",
    policy_clause="spam.mass_production",
    reason="A PostgreSQL-backed moderation flow.",
    evidence=["model: spam-v0.1"],
)
APPEAL = AppealCreate(
    grounds="new_evidence",
    statement="New production files are attached.",
)
DECISION = AppealDecision(
    decision="reverse",
    reasoning="Independent review confirmed the new evidence.",
)

MODERATOR = Principal("demo-reviewer", "moderator")
CREATOR = Principal("creator:pg", "creator", ("ch_pg",))
APPEALS = Principal("demo-appeals-reviewer", "appeals")


def test_postgres_state_survives_restart_and_scopes_channels(tmp_path):
    ledger_path = str(tmp_path / "ledger.jsonl")
    state = DemoState(ledger_path, DSN)
    state.reset()
    try:
        action = state.create_action(ACTION, MODERATOR)
        appeal = state.file_appeal(action["id"], APPEAL, CREATOR)
        state.decide_appeal(appeal["id"], DECISION, APPEALS)
        state.store.close()

        reopened = DemoState(ledger_path, DSN)
        try:
            snapshot = reopened.snapshot()
            assert snapshot["action"]["status"] == "reversed"
            assert snapshot["appeal"]["decision"] == "reverse"
            assert snapshot["verification"]["valid"] is True

            cases = reopened.list_creator_cases("ch_pg", video_id="vid_pg_001")
            assert cases["total"] == 1
            assert reopened.list_creator_cases("ch_other")["total"] == 0
            assert reopened.get_creator_action(action["id"], "ch_other") is None
            assert reopened.public_video_record("vid_pg_001")["cases"]

            staff_cases = reopened.staff_cases(status="decided")
            assert staff_cases["total"] == 1
            assert staff_cases["cases"][0]["channel_id"] == "ch_pg"
            assert reopened.staff_actions(channel_id="ch_pg")["total"] == 1
            assert reopened.staff_summary()["decided_appeals"] == 1

            signal = reopened.create_signal(
                SignalCreate(
                    subject_type="video",
                    subject_id="vid_pg_002",
                    channel_id="ch_pg",
                    reason="spam",
                    details="A PostgreSQL staff case.",
                )
            )
            case = reopened.staff_case(signal["case"]["id"])
            assert case is not None and case["case"]["status"] == "open"
            recorded = reopened.create_case_action(
                signal["case"]["id"],
                CaseActionCreate(
                    action_type="remove",
                    policy_clause="spam.mass_production",
                    reason="Confirmed by a reviewer.",
                    evidence=["review:pg"],
                ),
                MODERATOR,
            )
            assert recorded["status"] == "active"
            assert reopened.staff_case(signal["case"]["id"])["case"]["status"] == "decided"
        finally:
            reopened.reset()
            reopened.store.close()
    finally:
        state.store.close()
