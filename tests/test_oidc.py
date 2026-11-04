import time

import jwt

ISSUER = "keel-core"
AUDIENCE = "keel-moderation"
SECRET = "test-claims-secret-0123456789abcdef"

ACTION_PAYLOAD = {
    "video_id": "vid_demo_001",
    "policy_clause": "spam.mass_production",
    "reason": "An automated classifier flagged an original documentary as spam.",
    "evidence": ["model: spam-v0.1"],
}


def configure_oidc(monkeypatch) -> None:
    monkeypatch.setenv("KEELTUBE_OIDC_ISSUER", ISSUER)
    monkeypatch.setenv("KEELTUBE_OIDC_AUDIENCE", AUDIENCE)
    monkeypatch.setenv("KEELTUBE_OIDC_HMAC_SECRET", SECRET)
    monkeypatch.setenv("KEELTUBE_OIDC_ALGORITHMS", "HS256")


def make_token(**overrides) -> str:
    now = int(time.time())
    claims = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": "usr_maravale",
        "role": "creator",
        "channel_ids": ["ch_maravale"],
        "iat": now,
        "exp": now + 60,
    }
    claims.update(overrides)
    return jwt.encode(claims, SECRET, algorithm="HS256")


def authorization(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_oidc_principal_uses_subject_and_channel_claims(client, monkeypatch):
    configure_oidc(monkeypatch)
    token = make_token()

    who = client.get("/v1/whoami", headers=authorization(token))
    assert who.status_code == 200
    assert who.json() == {"id": "usr_maravale", "role": "creator"}

    summary = client.get(
        "/v1/creator/summary?channel_id=ch_maravale",
        headers=authorization(token),
    )
    assert summary.status_code == 200
    assert summary.json()["channel_id"] == "ch_maravale"


def test_oidc_rejects_missing_and_invalid_tokens(client, monkeypatch):
    configure_oidc(monkeypatch)
    assert client.get("/v1/whoami").status_code == 401
    assert client.get("/v1/whoami", headers=authorization("not-a-token")).status_code == 401

    expired = make_token(exp=int(time.time()) - 120)
    assert client.get("/v1/whoami", headers=authorization(expired)).status_code == 401

    wrong_audience = make_token(aud="another-service")
    assert client.get("/v1/whoami", headers=authorization(wrong_audience)).status_code == 401

    wrong_issuer = make_token(iss="another-issuer")
    assert client.get("/v1/whoami", headers=authorization(wrong_issuer)).status_code == 401

    wrong_secret = jwt.encode(
        {
            "iss": ISSUER,
            "aud": AUDIENCE,
            "sub": "usr_maravale",
            "role": "creator",
            "channel_ids": ["ch_maravale"],
            "iat": int(time.time()),
            "exp": int(time.time()) + 60,
        },
        "wrong-secret-0123456789abcdefghi",
        algorithm="HS256",
    )
    assert client.get("/v1/whoami", headers=authorization(wrong_secret)).status_code == 401


def test_oidc_rejects_unrecognized_role_and_channel_claim(client, monkeypatch):
    configure_oidc(monkeypatch)

    unknown_role = make_token(role="admin")
    assert client.get("/v1/whoami", headers=authorization(unknown_role)).status_code == 403

    invalid_channel = make_token(channel_ids=["not-a-channel"])
    assert client.get("/v1/whoami", headers=authorization(invalid_channel)).status_code == 403

    missing_subject = make_token()
    claims = jwt.decode(missing_subject, options={"verify_signature": False})
    claims.pop("sub")
    unsigned = jwt.encode(claims, SECRET, algorithm="HS256")
    assert client.get("/v1/whoami", headers=authorization(unsigned)).status_code == 401


def test_oidc_creator_scope_uses_claim_channels(client, monkeypatch):
    configure_oidc(monkeypatch)

    single = make_token(channel_ids=["ch_maravale"])
    assert (
        client.get(
            "/v1/creator/summary?channel_id=ch_other",
            headers=authorization(single),
        ).status_code
        == 403
    )

    multi = make_token(channel_ids=["ch_maravale", "ch_keel_lab"])
    assert client.get("/v1/creator/summary", headers=authorization(multi)).status_code == 422
    assert (
        client.get(
            "/v1/creator/summary?channel_id=ch_keel_lab",
            headers=authorization(multi),
        ).status_code
        == 200
    )


def test_oidc_mode_takes_precedence_over_static_tokens(client, monkeypatch):
    configure_oidc(monkeypatch)
    monkeypatch.setenv("KEELTUBE_CREATOR_TOKEN", "creator-token")
    monkeypatch.setenv("KEELTUBE_MODERATOR_TOKEN", "moderator-token")
    monkeypatch.setenv("KEELTUBE_APPEALS_TOKEN", "appeals-token")

    response = client.get("/v1/whoami", headers={"Authorization": "Bearer creator-token"})
    assert response.status_code == 401


def test_oidc_moderator_role_can_create_action(client, monkeypatch):
    configure_oidc(monkeypatch)
    token = make_token(role="moderator", channel_ids=[])

    created = client.post("/v1/actions", json=ACTION_PAYLOAD, headers=authorization(token))
    assert created.status_code == 201
    assert created.json()["human_reviewer"] == "usr_maravale"


def test_oidc_creator_without_channel_claim_is_denied(client, monkeypatch):
    configure_oidc(monkeypatch)
    token = make_token(channel_ids=[])

    response = client.get("/v1/creator/summary", headers=authorization(token))
    assert response.status_code == 403
    assert "no channel scope" in response.json()["detail"]


def test_oidc_misconfiguration_fails_closed(client, monkeypatch):
    monkeypatch.setenv("KEELTUBE_OIDC_ISSUER", ISSUER)
    monkeypatch.delenv("KEELTUBE_OIDC_JWKS_URL", raising=False)
    monkeypatch.delenv("KEELTUBE_OIDC_HMAC_SECRET", raising=False)

    response = client.get("/v1/whoami", headers=authorization("token"))
    assert response.status_code == 500
    assert "KEELTUBE_OIDC_JWKS_URL" in response.json()["detail"]


def test_oidc_rejects_short_hmac_secret(client, monkeypatch):
    monkeypatch.setenv("KEELTUBE_OIDC_ISSUER", ISSUER)
    monkeypatch.setenv("KEELTUBE_OIDC_HMAC_SECRET", "short")

    response = client.get("/v1/whoami", headers=authorization("token"))
    assert response.status_code == 500
    assert "at least 32" in response.json()["detail"]
