from __future__ import annotations

import hmac
import os
from dataclasses import dataclass
from typing import Literal, cast

from fastapi import HTTPException, Request

Role = Literal["creator", "moderator", "appeals", "operator"]

ROLE_ENV = {
    "creator": "KEELTUBE_CREATOR_TOKEN",
    "moderator": "KEELTUBE_MODERATOR_TOKEN",
    "appeals": "KEELTUBE_APPEALS_TOKEN",
}

# Stable local identities. In role-enforced mode these are the principal IDs that appear in
# enforcement actions, appeals, and ledger records.
ROLE_PRINCIPALS = {
    "creator": "creator:demo",
    "moderator": "demo-reviewer",
    "appeals": "demo-appeals-reviewer",
    "operator": "demo:operator",
}

LEGACY_TOKEN_ENV = "KEELTUBE_DEMO_TOKEN"


@dataclass(frozen=True)
class Principal:
    id: str
    role: Role


def actor_payload(principal: Principal) -> dict:
    return {"id": principal.id, "role": principal.role}


def _role_tokens() -> dict[str, str]:
    role_to_token: dict[str, str] = {}
    for role, env_name in ROLE_ENV.items():
        value = os.getenv(env_name, "").strip()
        if value:
            role_to_token[role] = value
    if len(set(role_to_token.values())) != len(role_to_token):
        raise HTTPException(
            status_code=500,
            detail=(
                "KEELTUBE_CREATOR_TOKEN, KEELTUBE_MODERATOR_TOKEN, and "
                "KEELTUBE_APPEALS_TOKEN must be distinct."
            ),
        )
    return {token: role for role, token in role_to_token.items()}


def _authorization(request: Request) -> str:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return ""
    return token.strip()


def _match(token: str, expected: str) -> bool:
    return bool(token) and hmac.compare_digest(
        token.encode("utf-8"), expected.encode("utf-8")
    )


def resolve_principal(
    request: Request, required_roles: tuple[str, ...] | None = None
) -> Principal:
    role_tokens = _role_tokens()
    token = _authorization(request)

    if role_tokens:
        for configured, role in role_tokens.items():
            if _match(token, configured):
                if required_roles is not None and role not in required_roles:
                    raise HTTPException(
                        status_code=403,
                        detail=f"One of these roles is required: {', '.join(required_roles)}.",
                    )
                return Principal(id=ROLE_PRINCIPALS[role], role=cast(Role, role))
        raise HTTPException(status_code=401, detail="A valid role token is required.")

    legacy = os.getenv(LEGACY_TOKEN_ENV, "").strip()
    if legacy:
        if not _match(token, legacy):
            raise HTTPException(status_code=401, detail="A valid demo token is required.")
        if required_roles:
            role = required_roles[0]
            return Principal(id=ROLE_PRINCIPALS[role], role=cast(Role, role))
        return Principal(id=ROLE_PRINCIPALS["operator"], role="operator")

    # Open local demo mode. This is intentionally unauthenticated and must never be used on a
    # network-exposed deployment.
    if required_roles:
        role = required_roles[0]
        return Principal(id=ROLE_PRINCIPALS[role], role=cast(Role, role))
    return Principal(id=ROLE_PRINCIPALS["operator"], role="operator")


def require_roles(*roles: str):
    def dependency(request: Request) -> Principal:
        return resolve_principal(request, roles)

    return dependency


def require_role(role: str):
    return require_roles(role)


def current_principal(request: Request) -> Principal:
    return resolve_principal(request, None)
