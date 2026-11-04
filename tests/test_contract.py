import json
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

SCHEMAS = Path(__file__).resolve().parents[2] / "keel-spec" / "schemas"
OPENAPI = SCHEMAS / "openapi.yaml"

if not SCHEMAS.exists():
    pytest.skip(
        "keel-spec schemas not found; check out keel-spec as a sibling of keel-moderation",
        allow_module_level=True,
    )

ACTION_PAYLOAD = {
    "video_id": "vid_demo_001",
    "policy_clause": "spam.mass_production",
    "reason": "An automated classifier flagged an original documentary as mass-produced spam.",
    "evidence": ["model: spam-v0.1", "confidence: 0.91", "signal: upload cadence"],
}

CREATOR = {"Authorization": "Bearer creator-token"}
MODERATOR = {"Authorization": "Bearer moderator-token"}
APPEALS = {"Authorization": "Bearer appeals-token"}

ACTION_POINTER = "/paths/~1v1~1actions/post/responses/201/content/application~1json/schema"
APPEAL_POINTER = (
    "/paths/~1v1~1actions~1{action_id}~1appeal/post/responses/201/"
    "content/application~1json/schema"
)
DECISION_POINTER = (
    "/paths/~1v1~1appeals~1{appeal_id}~1decide/post/responses/200/"
    "content/application~1json/schema"
)
STATE_POINTER = "/components/schemas/DemoState"
LEDGER_POINTER = "/paths/~1v1~1ledger/get/responses/200/content/application~1json/schema"
VERIFY_POINTER = "/paths/~1v1~1ledger~1verify/get/responses/200/content/application~1json/schema"
WHOAMI_POINTER = "/paths/~1v1~1whoami/get/responses/200/content/application~1json/schema"
APPEALS_POINTER = "/paths/~1v1~1appeals/get/responses/200/content/application~1json/schema"


def build_registry() -> Registry:
    registry = Registry()
    for path in sorted(SCHEMAS.glob("*.schema.json")):
        contents = json.loads(path.read_text(encoding="utf-8"))
        resource = Resource.from_contents(contents, default_specification=DRAFT202012)
        registry = registry.with_resource(path.as_uri(), resource)
        if "$id" in contents:
            registry = registry.with_resource(contents["$id"], resource)

    openapi = yaml.safe_load(OPENAPI.read_text(encoding="utf-8"))
    openapi_resource = Resource.from_contents(openapi, default_specification=DRAFT202012)
    return registry.with_resource(OPENAPI.as_uri(), openapi_resource)


@pytest.fixture(scope="session")
def registry() -> Registry:
    return build_registry()


def validate_pointer(registry: Registry, pointer: str, payload: dict) -> None:
    schema = {"$ref": f"{OPENAPI.as_uri()}#{pointer}"}
    Draft202012Validator(schema, registry=registry).validate(payload)


def validate_schema(registry: Registry, filename: str, payload: dict) -> None:
    schema = {"$ref": (SCHEMAS / filename).as_uri()}
    Draft202012Validator(schema, registry=registry).validate(payload)


def create_action(client) -> dict:
    response = client.post("/v1/actions", json=ACTION_PAYLOAD, headers=MODERATOR)
    assert response.status_code == 201
    return response.json()


def create_appeal(client, action: dict) -> dict:
    response = client.post(
        f"/v1/actions/{action['id']}/appeal",
        json={
            "grounds": "policy_misapplication",
            "statement": "Original documentary. All footage is owned and edited by the creator.",
        },
        headers=CREATOR,
    )
    assert response.status_code == 201
    return response.json()


def test_full_flow_matches_openapi_and_json_schemas(client, registry):
    action = create_action(client)
    validate_pointer(registry, ACTION_POINTER, action)
    validate_schema(registry, "enforcement-action.schema.json", action)

    state = client.get("/v1/demo/state").json()
    validate_pointer(registry, STATE_POINTER, state)
    if state["case"] is not None:
        validate_schema(registry, "moderation-case.schema.json", state["case"])

    appeal = create_appeal(client, action)
    validate_pointer(registry, APPEAL_POINTER, appeal)
    validate_schema(registry, "appeal.schema.json", appeal)

    decided = client.post(
        f"/v1/appeals/{appeal['id']}/decide",
        json={
            "decision": "reverse",
            "reasoning": "Human review confirmed original production. False positive.",
        },
        headers=APPEALS,
    )
    assert decided.status_code == 200
    validate_pointer(registry, DECISION_POINTER, decided.json())

    ledger = client.get("/v1/ledger").json()
    validate_pointer(registry, LEDGER_POINTER, ledger)
    for record in ledger["ledger"]:
        validate_schema(registry, "audit-record.schema.json", record)

    verification = client.get("/v1/ledger/verify").json()
    validate_pointer(registry, VERIFY_POINTER, verification)


def test_queue_and_identity_match_openapi(client, registry):
    who = client.get("/v1/whoami", headers=MODERATOR).json()
    validate_pointer(registry, WHOAMI_POINTER, who)
    assert who == {"id": "demo-reviewer", "role": "moderator"}

    action = create_action(client)
    appeal = create_appeal(client, action)
    queue = client.get("/v1/appeals?status=pending", headers=MODERATOR).json()
    validate_pointer(registry, APPEALS_POINTER, queue)
    assert [item["id"] for item in queue["appeals"]] == [appeal["id"]]


def test_unknown_fields_are_rejected(client):
    assert (
        client.post(
            "/v1/actions", json={**ACTION_PAYLOAD, "unexpected": True}, headers=MODERATOR
        ).status_code
        == 422
    )

    action = create_action(client)
    response = client.post(
        f"/v1/actions/{action['id']}/appeal",
        json={
            "grounds": "policy_misapplication",
            "statement": "Please review.",
            "unexpected": True,
        },
        headers=CREATOR,
    )
    assert response.status_code == 422

    appeal = create_appeal(client, action)
    response = client.post(
        f"/v1/appeals/{appeal['id']}/decide",
        json={"decision": "uphold", "reasoning": "The decision stands.", "unexpected": True},
        headers=APPEALS,
    )
    assert response.status_code == 422


@pytest.mark.parametrize(
    "field,value",
    [
        ("video_id", "vid_" + "a" * 77),
        ("policy_clause", "p" * 129),
        ("reason", "r" * 2001),
        ("evidence", [f"evidence-{index}" for index in range(51)]),
    ],
)
def test_action_declared_bounds_are_enforced(client, field, value):
    response = client.post(
        "/v1/actions", json={**ACTION_PAYLOAD, field: value}, headers=MODERATOR
    )
    assert response.status_code == 422


def test_appeal_declared_bounds_are_enforced(client):
    action = create_action(client)
    response = client.post(
        f"/v1/actions/{action['id']}/appeal",
        json={"grounds": "policy_misapplication", "statement": "s" * 4001},
        headers=CREATOR,
    )
    assert response.status_code == 422


def test_decision_declared_bounds_are_enforced(client):
    action = create_action(client)
    appeal = create_appeal(client, action)
    response = client.post(
        f"/v1/appeals/{appeal['id']}/decide",
        json={"decision": "uphold", "reasoning": "r" * 4001},
        headers=APPEALS,
    )
    assert response.status_code == 422


def test_openapi_documents_validation_and_rate_limit_responses():
    openapi = yaml.safe_load(OPENAPI.read_text(encoding="utf-8"))
    mutating = [
        ("/v1/demo/reset", "post"),
        ("/v1/actions", "post"),
        ("/v1/actions/{action_id}/appeal", "post"),
        ("/v1/appeals/{appeal_id}/decide", "post"),
    ]
    for path, method in mutating:
        responses = openapi["paths"][path][method]["responses"]
        assert "429" in responses, f"{path} is missing a 429 response"
    for path in ("/v1/actions", "/v1/actions/{action_id}/appeal", "/v1/appeals/{appeal_id}/decide"):
        assert "422" in openapi["paths"][path]["post"]["responses"]
