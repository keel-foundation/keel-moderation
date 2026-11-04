SIGNAL_PAYLOAD = {
    "subject_type": "video",
    "subject_id": "vid_demo_001",
    "channel_id": "ch_demo",
    "reason": "harassment",
    "details": "A comment and the video description target a named person.",
    "reporter_id": "usr_viewer",
}


def test_signal_ingest_requires_the_service_token(client):
    response = client.post("/v1/signals", json=SIGNAL_PAYLOAD)
    assert response.status_code == 401

    missing = client.post(
        "/v1/signals",
        json=SIGNAL_PAYLOAD,
        headers={"Authorization": "Bearer wrong"},
    )
    assert missing.status_code == 401


def test_signal_ingest_creates_an_open_case_and_ledger_record(client):
    response = client.post(
        "/v1/signals",
        json=SIGNAL_PAYLOAD,
        headers={"Authorization": "Bearer ingest-token"},
    )
    assert response.status_code == 201
    payload = response.json()
    assert payload["signal"]["source"] == "user_report"
    assert payload["signal"]["signal_type"] == "harassment"
    assert payload["case"]["status"] == "open"
    assert payload["case"]["severity"] == "medium"
    assert payload["case"]["subject"] == {"type": "video", "id": "vid_demo_001"}
    assert payload["case"]["evidence"][0]["description"] == SIGNAL_PAYLOAD["details"]

    creator_cases = client.get(
        "/v1/creator/cases", headers={"Authorization": "Bearer creator-token"}
    )
    assert creator_cases.status_code == 200
    assert creator_cases.json()["total"] == 1
    assert creator_cases.json()["cases"][0]["signals"][0]["source"] == "user_report"

    ledger = client.get(
        "/v1/creator/ledger?limit=100", headers={"Authorization": "Bearer creator-token"}
    )
    assert ledger.status_code == 200
    records = ledger.json()["ledger"]
    signal_records = [record for record in records if record["type"] == "signal.received"]
    assert signal_records
    assert signal_records[-1]["payload"]["signal"]["source"] == "user_report"
    assert ledger.json()["verification"]["valid"] is True


def test_signal_ingest_rejects_unknown_fields(client):
    response = client.post(
        "/v1/signals",
        json={**SIGNAL_PAYLOAD, "unexpected": True},
        headers={"Authorization": "Bearer ingest-token"},
    )
    assert response.status_code == 422
