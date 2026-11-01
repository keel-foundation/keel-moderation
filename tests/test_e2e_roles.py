ACTION_PAYLOAD = {
    "video_id": "vid_demo_001",
    "policy_clause": "spam.mass_production",
    "reason": "An automated classifier flagged an original documentary as spam.",
    "evidence": ["model: spam-v0.1", "confidence: 0.91"],
}

APPEAL_PAYLOAD = {
    "grounds": "policy_misapplication",
    "statement": "Original documentary. All footage is owned and edited by the creator.",
}

DECISION_PAYLOAD = {
    "decision": "reverse",
    "reasoning": "Human review confirmed original production. False positive.",
}

CREATOR = {"Authorization": "Bearer creator-token"}
MODERATOR = {"Authorization": "Bearer moderator-token"}
APPEALS = {"Authorization": "Bearer appeals-token"}


def test_end_to_end_flow_records_actor_in_ledger(client, monkeypatch):
    monkeypatch.setenv("KEELTUBE_CREATOR_TOKEN", "creator-token")
    monkeypatch.setenv("KEELTUBE_MODERATOR_TOKEN", "moderator-token")
    monkeypatch.setenv("KEELTUBE_APPEALS_TOKEN", "appeals-token")

    action = client.post("/v1/actions", json=ACTION_PAYLOAD, headers=MODERATOR).json()
    appeal = client.post(
        f"/v1/actions/{action['id']}/appeal",
        json=APPEAL_PAYLOAD,
        headers=CREATOR,
    ).json()
    decided = client.post(
        f"/v1/appeals/{appeal['id']}/decide",
        json=DECISION_PAYLOAD,
        headers=APPEALS,
    )
    assert decided.status_code == 200

    ledger = client.get("/v1/ledger").json()["ledger"]
    by_type = {record["type"]: record for record in ledger}

    assert by_type["enforcement_action.created"]["payload"]["actor"] == {
        "id": "demo-reviewer",
        "role": "moderator",
    }
    assert by_type["appeal.filed"]["payload"]["actor"] == {
        "id": "creator:demo",
        "role": "creator",
    }
    assert by_type["appeal.decided"]["payload"]["actor"] == {
        "id": "demo-appeals-reviewer",
        "role": "appeals",
    }
    assert by_type["enforcement_action.reversed"]["payload"]["actor"]["role"] == "appeals"

    queue = client.get("/v1/appeals?status=decided", headers=APPEALS).json()
    assert [item["id"] for item in queue["appeals"]] == [appeal["id"]]
