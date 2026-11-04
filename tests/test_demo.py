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

CREATOR = {"Authorization": "Bearer creator-token"}
MODERATOR = {"Authorization": "Bearer moderator-token"}
APPEALS = {"Authorization": "Bearer appeals-token"}


def validate(path: Path, payload: dict) -> None:
    if not path.exists():
        pytest.skip(f"schema missing: {path}")
    jsonschema = pytest.importorskip("jsonschema")
    jsonschema.validate(payload, json.loads(path.read_text(encoding="utf-8")))


def test_full_flow_reverse_and_ledger(client):
    created = client.post("/v1/actions", json=ACTION_PAYLOAD, headers=MODERATOR)
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
        headers=CREATOR,
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
        headers=APPEALS,
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
    action = client.post(
        "/v1/actions", json=ACTION_PAYLOAD, headers=MODERATOR
    ).json()
    appeal = client.post(
        f"/v1/actions/{action['id']}/appeal",
        json={"grounds": "policy_misapplication", "statement": "Please review."},
        headers=CREATOR,
    ).json()
    client.post(
        f"/v1/appeals/{appeal['id']}/decide",
        json={"decision": "uphold", "reasoning": "The original decision stands."},
        headers=APPEALS,
    )

    rejected = client.post(
        f"/v1/actions/{action['id']}/appeal",
        json={"grounds": "other", "statement": "I disagree."},
        headers=CREATOR,
    )
    assert rejected.status_code == 409

    accepted = client.post(
        f"/v1/actions/{action['id']}/appeal",
        json={"grounds": "new_evidence", "statement": "New production files are attached."},
        headers=CREATOR,
    )
    assert accepted.status_code == 201
    assert accepted.json()["grounds"] == "new_evidence"


def test_closed_appeal_window_is_rejected(client):
    action = client.post(
        "/v1/actions", json=ACTION_PAYLOAD, headers=MODERATOR
    ).json()
    from app.main import state

    stored = state.store.get_action(action["id"])
    assert stored is not None
    stored["appeal_deadline"] = "2020-01-01T00:00:00+00:00"
    state.store.save_action(stored)

    rejected = client.post(
        f"/v1/actions/{action['id']}/appeal",
        json={"grounds": "policy_misapplication", "statement": "Too late."},
        headers=CREATOR,
    )
    assert rejected.status_code == 409


def test_reset_clears_state_but_keeps_ledger(client):
    client.post("/v1/actions", json=ACTION_PAYLOAD, headers=MODERATOR)
    before = client.get("/v1/ledger").json()
    reset = client.post("/v1/demo/reset", headers=MODERATOR)

    assert reset.status_code == 200
    after = reset.json()
    assert after["action"] is None
    assert after["appeal"] is None
    assert after["verification"]["valid"] is True
    assert after["verification"]["records"] == before["verification"]["records"] + 1


def test_authentication_is_required_when_not_configured(client, monkeypatch):
    from app.main import state

    for name in (
        "KEELTUBE_CREATOR_TOKEN",
        "KEELTUBE_MODERATOR_TOKEN",
        "KEELTUBE_APPEALS_TOKEN",
        "KEELTUBE_OIDC_ISSUER",
    ):
        monkeypatch.delenv(name, raising=False)
    state.reset()

    response = client.post("/v1/actions", json=ACTION_PAYLOAD)
    assert response.status_code == 503


def test_action_requires_evidence(client):
    payload = {**ACTION_PAYLOAD, "evidence": []}
    response = client.post("/v1/actions", json=payload, headers=MODERATOR)
    assert response.status_code == 422
