import json
from pathlib import Path

import pytest

SCHEMAS = Path(__file__).resolve().parents[2] / "keel-spec" / "schemas"

ACTION_PAYLOAD = {
    "video_id": "vid_demo_001",
    "policy_clause": "spam.mass_production",
    "reason": "An automated classifier flagged an original documentary as mass-produced spam.",
    "evidence": ["model: spam-v0.1", "confidence: 0.91", "signal: upload cadence"],
}


def validate(path: Path, payload: dict) -> None:
    if not path.exists():
        pytest.skip(f"schema missing: {path}")
    jsonschema = pytest.importorskip("jsonschema")
    jsonschema.validate(payload, json.loads(path.read_text(encoding="utf-8")))


def test_full_flow_reverse_and_ledger(client):
    created = client.post("/v1/actions", json=ACTION_PAYLOAD)
    assert created.status_code == 201
    action = created.json()

    assert action["case_id"].startswith("case_")
    assert action["action_type"] == "label"
    assert action["scope"] == "video"
    assert action["statement_of_reasons"]["policy_version"] == "demo-0.1"
    assert action["audit_record_hash"].startswith("sha256:")
    validate(SCHEMAS / "enforcement-action.schema.json", action)

    state = client.get("/v1/demo/state").json()
    validate(SCHEMAS / "moderation-case.schema.json", state["case"])

    appeal = client.post(
        f"/v1/actions/{action['id']}/appeal",
        json={
            "grounds": "policy_misapplication",
            "statement": "Original documentary. All footage is owned and edited by the creator.",
        },
    )
    assert appeal.status_code == 201
    appeal_body = appeal.json()
    assert appeal_body["reviewer"] is None
    validate(SCHEMAS / "appeal.schema.json", appeal_body)

    decided = client.post(
        f"/v1/appeals/{appeal_body['id']}/decide",
        json={
            "decision": "reverse",
            "reasoning": (
                "Human review confirmed original production. "
                "The pattern match was a false positive."
            ),
        },
    )
    assert decided.status_code == 200
    snapshot = decided.json()

    assert snapshot["action"]["status"] == "reversed"
    assert snapshot["appeal"]["reviewer"] == "demo-appeals-reviewer"
    assert snapshot["action"]["reversal_record_hash"].startswith("sha256:")

    verified = client.get("/v1/ledger/verify").json()
    assert verified["valid"] is True
    assert verified["records"] >= 5

    exported = client.get("/v1/ledger/export?format=jsonl")
    assert exported.status_code == 200
    records = [json.loads(line) for line in exported.text.splitlines() if line]
    assert len(records) == verified["records"]
    assert records[-1]["hash"] == verified["head"]


def test_second_appeal_requires_new_evidence_or_procedure(client):
    action = client.post("/v1/actions", json=ACTION_PAYLOAD).json()
    appeal = client.post(
        f"/v1/actions/{action['id']}/appeal",
        json={"grounds": "policy_misapplication", "statement": "Please review."},
    ).json()
    client.post(
        f"/v1/appeals/{appeal['id']}/decide",
        json={"decision": "uphold", "reasoning": "The original decision stands."},
    )

    rejected = client.post(
        f"/v1/actions/{action['id']}/appeal",
        json={"grounds": "other", "statement": "I disagree."},
    )
    assert rejected.status_code == 409

    accepted = client.post(
        f"/v1/actions/{action['id']}/appeal",
        json={"grounds": "new_evidence", "statement": "New production files are attached."},
    )
    assert accepted.status_code == 201
    assert accepted.json()["grounds"] == "new_evidence"


def test_closed_appeal_window_is_rejected(client):
    action = client.post("/v1/actions", json=ACTION_PAYLOAD).json()
    from app.main import state

    state.action["appeal_deadline"] = "2020-01-01T00:00:00+00:00"

    rejected = client.post(
        f"/v1/actions/{action['id']}/appeal",
        json={"grounds": "policy_misapplication", "statement": "Too late."},
    )
    assert rejected.status_code == 409


def test_reset_clears_state_but_keeps_ledger(client):
    client.post("/v1/actions", json=ACTION_PAYLOAD)
    before = client.get("/v1/ledger").json()
    reset = client.post("/v1/demo/reset")

    assert reset.status_code == 200
    after = reset.json()
    assert after["action"] is None
    assert after["appeal"] is None
    assert after["verification"]["valid"] is True
    assert after["verification"]["records"] == before["verification"]["records"] + 1


def test_demo_token_is_enforced_when_configured(client, monkeypatch):
    from app.main import state

    monkeypatch.setenv("TUBEKEEL_DEMO_TOKEN", "secret")
    state.reset()

    rejected = client.post("/v1/actions", json=ACTION_PAYLOAD)
    assert rejected.status_code == 401

    accepted = client.post(
        "/v1/actions",
        json=ACTION_PAYLOAD,
        headers={"Authorization": "Bearer secret"},
    )
    assert accepted.status_code == 201


def test_action_requires_evidence(client):
    payload = {**ACTION_PAYLOAD, "evidence": []}
    response = client.post("/v1/actions", json=payload)
    assert response.status_code == 422
