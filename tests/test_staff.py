MODERATOR = {"Authorization": "Bearer moderator-token"}
APPEALS = {"Authorization": "Bearer appeals-token"}
CREATOR = {"Authorization": "Bearer creator-token"}
INGEST = {"Authorization": "Bearer ingest-token"}

SIGNAL = {
    "subject_type": "video",
    "subject_id": "vid_staff_001",
    "channel_id": "ch_demo",
    "reason": "harassment",
    "details": "A viewer report describes targeted harassment.",
}


def open_case(client) -> dict:
    response = client.post("/v1/signals", json=SIGNAL, headers=INGEST)
    assert response.status_code == 201
    return response.json()["case"]


def test_staff_can_list_open_cases_and_record_an_action(client):
    case = open_case(client)

    listed = client.get("/v1/cases?status=open", headers=MODERATOR)
    assert listed.status_code == 200
    payload = listed.json()
    assert payload["total"] == 1
    assert payload["cases"][0]["channel_id"] == "ch_demo"
    assert payload["cases"][0]["case"]["id"] == case["id"]

    detail = client.get(f"/v1/cases/{case['id']}", headers=APPEALS)
    assert detail.status_code == 200
    assert detail.json()["case"]["id"] == case["id"]

    action = client.post(
        f"/v1/cases/{case['id']}/actions",
        json={
            "action_type": "remove",
            "policy_clause": "harassment.targeted",
            "reason": "Human review confirmed targeted harassment.",
            "evidence": ["report:user", "review:moderator"],
        },
        headers=MODERATOR,
    )
    assert action.status_code == 201
    action_body = action.json()
    assert action_body["scope"] == "video"
    assert action_body["action_type"] == "remove"
    assert action_body["case_id"] == case["id"]

    updated = client.get(f"/v1/cases/{case['id']}", headers=MODERATOR).json()
    assert updated["case"]["status"] == "decided"
    assert updated["case"]["decision_action_id"] == action_body["id"]

    actions = client.get("/v1/actions?channel_id=ch_demo", headers=MODERATOR)
    assert actions.status_code == 200
    assert actions.json()["actions"][0]["action"]["id"] == action_body["id"]

    summary = client.get("/v1/summary", headers=MODERATOR)
    assert summary.status_code == 200
    assert summary.json()["cases_total"] == 1
    assert summary.json()["active_actions"] == 1
    assert summary.json()["ledger_valid"] is True


def test_staff_can_dismiss_a_case_without_enforcement(client):
    case = open_case(client)
    dismissed = client.post(
        f"/v1/cases/{case['id']}/dismiss",
        json={"reason": "The report was a duplicate and the content is allowed."},
        headers=MODERATOR,
    )
    assert dismissed.status_code == 200
    assert dismissed.json()["status"] == "closed"

    ledger = client.get("/v1/ledger", headers=MODERATOR).json()
    assert any(record["type"] == "moderation_case.dismissed" for record in ledger["ledger"])

    again = client.post(
        f"/v1/cases/{case['id']}/actions",
        json={
            "policy_clause": "spam.mass_production",
            "reason": "Too late.",
            "evidence": ["review:duplicate"],
        },
        headers=MODERATOR,
    )
    assert again.status_code == 409


def test_staff_roles_are_enforced(client):
    case = open_case(client)
    assert client.get("/v1/cases", headers=CREATOR).status_code == 403
    assert client.get("/v1/summary", headers=CREATOR).status_code == 403
    assert (
        client.post(
            f"/v1/cases/{case['id']}/actions",
            json={
                "policy_clause": "spam.mass_production",
                "reason": "Appeals reviewers cannot enforce.",
                "evidence": ["review:role"],
            },
            headers=APPEALS,
        ).status_code
        == 403
    )
