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


def configure_roles(monkeypatch) -> None:
    monkeypatch.setenv("KEELTUBE_CREATOR_TOKEN", "creator-token")
    monkeypatch.setenv("KEELTUBE_MODERATOR_TOKEN", "moderator-token")
    monkeypatch.setenv("KEELTUBE_APPEALS_TOKEN", "appeals-token")


def create_action(client) -> dict:
    response = client.post("/v1/actions", json=ACTION_PAYLOAD, headers=MODERATOR)
    assert response.status_code == 201
    return response.json()


def create_appeal(client, action: dict) -> dict:
    response = client.post(
        f"/v1/actions/{action['id']}/appeal",
        json=APPEAL_PAYLOAD,
        headers=CREATOR,
    )
    assert response.status_code == 201
    return response.json()


def test_role_tokens_are_required_and_scoped(client, monkeypatch):
    configure_roles(monkeypatch)

    assert client.post("/v1/actions", json=ACTION_PAYLOAD).status_code == 401
    assert (
        client.post("/v1/actions", json=ACTION_PAYLOAD, headers=CREATOR).status_code == 403
    )
    assert (
        client.post("/v1/actions", json=ACTION_PAYLOAD, headers=MODERATOR).status_code == 201
    )

    who = client.get("/v1/whoami", headers=APPEALS)
    assert who.status_code == 200
    assert who.json() == {"id": "demo-appeals-reviewer", "role": "appeals"}


def test_creator_files_and_appeals_reviewer_decides(client, monkeypatch):
    configure_roles(monkeypatch)
    action = create_action(client)

    assert (
        client.post(
            f"/v1/actions/{action['id']}/appeal",
            json=APPEAL_PAYLOAD,
            headers=MODERATOR,
        ).status_code
        == 403
    )

    appeal = create_appeal(client, action)
    assert appeal["appellant"] == "creator:demo"

    assert (
        client.post(
            f"/v1/appeals/{appeal['id']}/decide",
            json=DECISION_PAYLOAD,
            headers=CREATOR,
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"/v1/appeals/{appeal['id']}/decide",
            json=DECISION_PAYLOAD,
            headers=MODERATOR,
        ).status_code
        == 403
    )

    decided = client.post(
        f"/v1/appeals/{appeal['id']}/decide",
        json=DECISION_PAYLOAD,
        headers=APPEALS,
    )
    assert decided.status_code == 200
    snapshot = decided.json()
    assert snapshot["appeal"]["reviewer"] == "demo-appeals-reviewer"
    assert snapshot["action"]["status"] == "reversed"


def test_queue_endpoints_require_privileged_roles(client, monkeypatch):
    configure_roles(monkeypatch)
    action = create_action(client)
    appeal = create_appeal(client, action)

    assert client.get("/v1/whoami").status_code == 401
    assert client.get("/v1/appeals", headers=CREATOR).status_code == 403
    assert client.get("/v1/appeals", headers=MODERATOR).status_code == 200
    assert client.get("/v1/appeals", headers=APPEALS).status_code == 200
    assert (
        client.get(f"/v1/appeals/{appeal['id']}", headers=CREATOR).status_code == 403
    )
    assert (
        client.get(f"/v1/appeals/{appeal['id']}", headers=APPEALS).status_code == 200
    )
    assert client.get("/v1/appeals/apl_missing", headers=APPEALS).status_code == 404


def test_original_decision_maker_cannot_decide_appeal(client, monkeypatch):
    from app.main import state

    configure_roles(monkeypatch)
    action = create_action(client)
    appeal = create_appeal(client, action)

    stored = state.store.get_action(action["id"])
    assert stored is not None
    stored["human_reviewer"] = "demo-appeals-reviewer"
    state.store.save_action(stored)

    rejected = client.post(
        f"/v1/appeals/{appeal['id']}/decide",
        json=DECISION_PAYLOAD,
        headers=APPEALS,
    )
    assert rejected.status_code == 403
    assert "original decision-maker" in rejected.json()["detail"]


def test_appellant_cannot_decide_own_appeal(client, monkeypatch):
    from app.main import state

    configure_roles(monkeypatch)
    action = create_action(client)
    appeal = create_appeal(client, action)

    stored = state.store.get_appeal(appeal["id"])
    assert stored is not None
    stored["appellant"] = "demo-appeals-reviewer"
    state.store.save_appeal(stored)

    rejected = client.post(
        f"/v1/appeals/{appeal['id']}/decide",
        json=DECISION_PAYLOAD,
        headers=APPEALS,
    )
    assert rejected.status_code == 403
    assert "own appeal" in rejected.json()["detail"]


def test_role_tokens_must_be_distinct(client, monkeypatch):
    configure_roles(monkeypatch)
    monkeypatch.setenv("KEELTUBE_APPEALS_TOKEN", "moderator-token")

    response = client.get("/v1/whoami", headers=MODERATOR)
    assert response.status_code == 500
    assert "must be distinct" in response.json()["detail"]



