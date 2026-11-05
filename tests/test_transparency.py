import json

CREATOR = {"Authorization": "Bearer creator-token"}
MODERATOR = {"Authorization": "Bearer moderator-token"}
APPEALS = {"Authorization": "Bearer appeals-token"}

ACTION = {
    "video_id": "vid_demo_001",
    "policy_clause": "spam.mass_production",
    "reason": "An automated classifier flagged mass-produced spam.",
    "evidence": ["model: linear-nb-v1"],
}


def create_action(client):
    response = client.post("/v1/actions", json=ACTION, headers=MODERATOR)
    assert response.status_code == 201
    return response.json()


def file_appeal(client, action_id):
    response = client.post(
        f"/v1/actions/{action_id}/appeal",
        json={"grounds": "policy_misapplication", "statement": "This is original work."},
        headers=CREATOR,
    )
    assert response.status_code == 201
    return response.json()


def decide_appeal(client, appeal_id, decision):
    response = client.post(
        f"/v1/appeals/{appeal_id}/decide",
        json={"decision": decision, "reasoning": "Independent review."},
        headers=APPEALS,
    )
    assert response.status_code == 200
    return response.json()


def test_transparency_report_counts_and_reversal_rate(client):
    action = create_action(client)
    appeal = file_appeal(client, action["id"])
    decide_appeal(client, appeal["id"], "reverse")

    report = client.get("/v1/transparency").json()
    assert report["schema_version"] == "transparency-0.1"
    assert report["summary"]["cases_total"] == 1
    assert report["summary"]["actions_total"] == 1
    assert report["summary"]["actions_reversed"] == 1
    assert report["summary"]["reversal_rate"] == 1.0
    assert report["summary"]["appeals_total"] == 1
    assert report["summary"]["appeals_decided"] == 1
    assert report["appeals"]["reversal_rate"] == 1.0
    assert report["enforcement_actions"]["by_policy"][0]["policy_clause"] == (
        "spam.mass_production"
    )
    assert report["categories"]["spam"]["actions"] == 1
    assert report["ledger"]["valid"] is True
    assert report["ledger"]["records"] >= 3


def test_transparency_report_supports_period_filter(client):
    create_action(client)
    report = client.get("/v1/transparency?since=2999-01-01").json()
    assert report["summary"]["actions_total"] == 0
    assert report["period"]["from"].startswith("2999-01-01")


def test_transparency_exports(client):
    create_action(client)
    csv_response = client.get("/v1/transparency/export?format=csv")
    assert csv_response.status_code == 200
    assert csv_response.headers["content-type"].startswith("text/csv")
    assert "report,schema_version,transparency-0.1" in csv_response.text

    markdown = client.get("/v1/transparency/export?format=markdown")
    assert markdown.status_code == 200
    assert markdown.headers["content-type"].startswith("text/markdown")
    assert markdown.text.startswith("# KeelTube transparency report")

    as_json = client.get("/v1/transparency/export?format=json")
    assert as_json.status_code == 200
    assert json.loads(as_json.text)["schema_version"] == "transparency-0.1"


def test_transparency_is_public(client):
    response = client.get("/v1/transparency")
    assert response.status_code == 200
