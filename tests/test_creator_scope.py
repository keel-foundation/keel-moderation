import json

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


def create_action(client, channel_id: str, video_id: str = "vid_demo_001") -> dict:
    response = client.post(
        "/v1/actions",
        json={**ACTION_PAYLOAD, "channel_id": channel_id, "video_id": video_id},
        headers=MODERATOR,
    )
    assert response.status_code == 201
    return response.json()


def file_and_reverse(client, action: dict) -> dict:
    appeal = client.post(
        f"/v1/actions/{action['id']}/appeal",
        json=APPEAL_PAYLOAD,
        headers=CREATOR,
    )
    assert appeal.status_code == 201
    decided = client.post(
        f"/v1/appeals/{appeal.json()['id']}/decide",
        json=DECISION_PAYLOAD,
        headers=APPEALS,
    )
    assert decided.status_code == 200
    return appeal.json()


def test_creator_reads_are_explicitly_channel_scoped(client, monkeypatch):
    monkeypatch.setenv(
        "KEELTUBE_CREATOR_CHANNEL_MAP", '{"creator:demo":["ch_scope_reads"]}'
    )
    demo_action = create_action(client, "ch_scope_reads", "vid_ch_other_001")
    demo_appeal = file_and_reverse(client, demo_action)
    other_action = create_action(client, "ch_other", "vid_demo_002")
    other_appeal = client.post(
        f"/v1/actions/{other_action['id']}/appeal",
        json=APPEAL_PAYLOAD,
        headers=CREATOR,
    )
    assert other_appeal.status_code == 201

    cases = client.get("/v1/creator/cases", headers=CREATOR).json()
    assert cases["channel_id"] == "ch_scope_reads"
    assert cases["total"] == 1
    assert cases["cases"][0]["decision_action_id"] == demo_action["id"]

    actions = client.get("/v1/creator/actions", headers=CREATOR).json()
    assert actions["channel_id"] == "ch_scope_reads"
    assert [item["id"] for item in actions["actions"]] == [demo_action["id"]]

    appeals = client.get("/v1/creator/appeals", headers=CREATOR).json()
    assert appeals["channel_id"] == "ch_scope_reads"
    assert [item["id"] for item in appeals["appeals"]] == [demo_appeal["id"]]

    assert (
        client.get(
            f"/v1/creator/cases/{other_action['case_id']}", headers=CREATOR
        ).status_code
        == 404
    )
    assert (
        client.get(
            f"/v1/creator/actions/{other_action['id']}", headers=CREATOR
        ).status_code
        == 404
    )
    assert (
        client.get(
            f"/v1/creator/appeals/{other_appeal.json()['id']}", headers=CREATOR
        ).status_code
        == 404
    )
    assert (
        client.get(
            f"/v1/creator/appeals/{demo_appeal['id']}", headers=CREATOR
        ).json()["id"]
        == demo_appeal["id"]
    )

    summary = client.get("/v1/creator/summary", headers=CREATOR).json()
    assert summary == {
        "channel_id": "ch_scope_reads",
        "cases_total": 1,
        "open_cases": 0,
        "actions_total": 1,
        "active_actions": 0,
        "appeals_total": 1,
        "pending_appeals": 0,
        "decided_appeals": 1,
        "ledger_records": 5,
    }


def test_creator_ledger_uses_payload_scope_not_string_matching(client, monkeypatch):
    monkeypatch.setenv(
        "KEELTUBE_CREATOR_CHANNEL_MAP", '{"creator:demo":["ch_scope_ledger"]}'
    )
    demo_action = create_action(client, "ch_scope_ledger", "vid_ch_other_001")
    file_and_reverse(client, demo_action)
    other_action = create_action(client, "ch_other", "vid_demo_002")

    ledger = client.get("/v1/creator/ledger", headers=CREATOR).json()
    assert ledger["channel_id"] == "ch_scope_ledger"
    assert ledger["total"] == 5
    assert ledger["verification"]["valid"] is True
    for record in ledger["ledger"]:
        assert record["payload"]["channel_id"] == "ch_scope_ledger"

    serialized = json.dumps(ledger["ledger"])
    assert other_action["id"] not in serialized
    assert other_action["case_id"] not in serialized

    exported = client.get("/v1/creator/ledger/export?format=jsonl", headers=CREATOR)
    assert exported.status_code == 200
    records = [json.loads(line) for line in exported.text.splitlines() if line]
    assert len(records) == ledger["total"]
    assert all(record["payload"]["channel_id"] == "ch_scope_ledger" for record in records)

    csv_export = client.get("/v1/creator/ledger/export?format=csv", headers=CREATOR)
    assert csv_export.status_code == 200
    assert "channel_id" in csv_export.text
    assert other_action["id"] not in csv_export.text
    assert client.get("/v1/creator/ledger/verify", headers=CREATOR).json()["valid"] is True


def test_creator_status_filters_and_pagination(client, monkeypatch):
    monkeypatch.setenv(
        "KEELTUBE_CREATOR_CHANNEL_MAP", '{"creator:demo":["ch_scope_pages"]}'
    )
    first = create_action(client, "ch_scope_pages")
    file_and_reverse(client, first)
    second = create_action(client, "ch_scope_pages", "vid_demo_003")

    reversed_actions = client.get("/v1/creator/actions?status=reversed", headers=CREATOR).json()
    assert [item["id"] for item in reversed_actions["actions"]] == [first["id"]]
    active_actions = client.get("/v1/creator/actions?status=active", headers=CREATOR).json()
    assert [item["id"] for item in active_actions["actions"]] == [second["id"]]

    page_one = client.get("/v1/creator/actions?limit=1&offset=0", headers=CREATOR).json()
    page_two = client.get("/v1/creator/actions?limit=1&offset=1", headers=CREATOR).json()
    assert page_one["total"] == 2
    assert page_two["total"] == 2
    assert page_one["actions"][0]["id"] != page_two["actions"][0]["id"]

    assert client.get("/v1/creator/cases?status=decided", headers=CREATOR).json()["total"] == 2
    assert client.get("/v1/creator/cases?status=open", headers=CREATOR).json()["total"] == 0


def test_creator_channel_can_be_configured(client, monkeypatch):
    monkeypatch.setenv(
        "KEELTUBE_CREATOR_CHANNEL_MAP", '{"creator:demo":["ch_other"]}'
    )
    action = create_action(client, "ch_other")

    cases = client.get("/v1/creator/cases", headers=CREATOR).json()
    assert cases["channel_id"] == "ch_other"
    assert cases["cases"][0]["decision_action_id"] == action["id"]
    assert client.get("/v1/creator/summary", headers=CREATOR).json()["channel_id"] == "ch_other"


def test_creator_endpoints_require_the_creator_role(client, monkeypatch):
    monkeypatch.setenv("KEELTUBE_CREATOR_TOKEN", "creator-token")
    monkeypatch.setenv("KEELTUBE_MODERATOR_TOKEN", "moderator-token")
    monkeypatch.setenv("KEELTUBE_APPEALS_TOKEN", "appeals-token")
    client.post(
        "/v1/actions",
        json=ACTION_PAYLOAD,
        headers=MODERATOR,
    )

    assert client.get("/v1/creator/cases").status_code == 401
    assert client.get("/v1/creator/cases", headers=MODERATOR).status_code == 403
    assert client.get("/v1/creator/cases", headers=APPEALS).status_code == 403
    assert client.get("/v1/creator/cases", headers=CREATOR).status_code == 200


def test_active_action_limit_is_per_channel(client):
    create_action(client, "ch_scope_a")
    create_action(client, "ch_scope_b")
    rejected = client.post(
        "/v1/actions",
        json={**ACTION_PAYLOAD, "channel_id": "ch_scope_a", "video_id": "vid_demo_004"},
        headers=MODERATOR,
    )
    assert rejected.status_code == 409


def test_multi_channel_creator_requires_authorized_channel(client, monkeypatch):
    monkeypatch.setenv(
        "KEELTUBE_CREATOR_CHANNEL_MAP",
        '{"creator:demo":["ch_multi_a","ch_multi_b"]}',
    )
    first = create_action(client, "ch_multi_a")
    second = create_action(client, "ch_multi_b", "vid_demo_002")

    assert client.get("/v1/creator/cases", headers=CREATOR).status_code == 422
    first_cases = client.get("/v1/creator/cases?channel_id=ch_multi_a", headers=CREATOR).json()
    second_cases = client.get("/v1/creator/cases?channel_id=ch_multi_b", headers=CREATOR).json()
    assert first_cases["cases"][0]["decision_action_id"] == first["id"]
    assert second_cases["cases"][0]["decision_action_id"] == second["id"]
    assert client.get("/v1/creator/cases?channel_id=ch_multi_c", headers=CREATOR).status_code == 403
    assert (
        client.get("/v1/creator/summary?channel_id=ch_multi_c", headers=CREATOR).status_code == 403
    )


def test_creator_without_channel_claim_is_denied(client, monkeypatch):
    monkeypatch.setenv("KEELTUBE_CREATOR_CHANNEL_MAP", "{}")
    assert client.get("/v1/creator/cases", headers=CREATOR).status_code == 403
    assert client.get("/v1/creator/summary", headers=CREATOR).status_code == 403


def test_invalid_creator_channel_claim_config_is_rejected(client, monkeypatch):
    monkeypatch.setenv("KEELTUBE_CREATOR_CHANNEL_MAP", "not-json")
    response = client.get("/v1/creator/cases", headers=CREATOR)
    assert response.status_code == 500
    assert "must be valid JSON" in response.json()["detail"]
