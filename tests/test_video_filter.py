MODERATOR = {"Authorization": "Bearer moderator-token"}
CREATOR = {"Authorization": "Bearer creator-token"}


def test_creator_cases_and_actions_filter_by_video(client):
    response = client.post(
        "/v1/actions",
        json={
            "video_id": "vid_demo_001",
            "channel_id": "ch_demo",
            "policy_clause": "spam.mass_production",
            "reason": "A classifier flagged the upload.",
            "evidence": ["model: spam-v0.1"],
        },
        headers=MODERATOR,
    )
    assert response.status_code == 201

    cases = client.get("/v1/creator/cases?video_id=vid_demo_001", headers=CREATOR).json()
    assert cases["total"] == 1
    assert cases["cases"][0]["subject"]["id"] == "vid_demo_001"

    actions = client.get("/v1/creator/actions?video_id=vid_demo_001", headers=CREATOR).json()
    assert actions["total"] == 1

    no_cases = client.get("/v1/creator/cases?video_id=vid_demo_999", headers=CREATOR).json()
    assert no_cases["total"] == 0

    no_actions = client.get("/v1/creator/actions?video_id=vid_demo_999", headers=CREATOR).json()
    assert no_actions["total"] == 0
