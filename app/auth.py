from __future__ import annotations

import hmac
import json
import os
from typing import cast

from fastapi import HTTPException, Request

from .oidc import OIDCConfig, verify_oidc_token
from .principals import Principal, Role, valid_channel_id

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

CREATOR_CHANNEL_MAP_ENV = "KEELTUBE_CREATOR_CHANNEL_MAP"
DEFAULT_CREATOR_CHANNEL_MAP = {"creator:demo": ("ch_demo",)}


def _creator_channel_map() -> dict[str, tuple[str, ...]]:
    raw = os.getenv(CREATOR_CHANNEL_MAP_ENV, "").strip()
    if not raw:
        return DEFAULT_CREATOR_CHANNEL_MAP
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as error:
        raise HTTPException(
            status_code=500,
            detail=f"{CREATOR_CHANNEL_MAP_ENV} must be valid JSON.",
        ) from error
    if not isinstance(parsed, dict):
        raise HTTPException(
            status_code=500,
            detail=f"{CREATOR_CHANNEL_MAP_ENV} must map principal IDs to channel lists.",
        )
    mapping: dict[str, tuple[str, ...]] = {}
    for principal_id, channels in parsed.items():
        if not isinstance(principal_id, str) or not principal_id or not isinstance(channels, list):
            raise HTTPException(
                status_code=500,
                detail=f"{CREATOR_CHANNEL_MAP_ENV} must map principal IDs to channel lists.",
            )
        if not all(isinstance(channel, str) and valid_channel_id(channel) for channel in channels):
            raise HTTPException(
                status_code=500,
                detail=f"{CREATOR_CHANNEL_MAP_ENV} contains an invalid channel ID.",
            )
        mapping[principal_id] = tuple(dict.fromkeys(channels))
    return mapping


def _principal(role: str) -> Principal:
    principal_id = ROLE_PRINCIPALS[role]
    channel_ids = _creator_channel_map().get(principal_id, ()) if role == "creator" else ()
    return Principal(id=principal_id, role=cast(Role, role), channel_ids=channel_ids)


def _role_tokens() -> dict[str, str]:
    configured = {role: os.getenv(env_name, "").strip() for role, env_name in ROLE_ENV.items()}
    present = {role: token for role, token in configured.items() if token}
    if not present:
        return {}
    if len(present) != len(ROLE_ENV):
        raise HTTPException(
            status_code=500,
            detail=(
                "Set KEELTUBE_CREATOR_TOKEN, KEELTUBE_MODERATOR_TOKEN, and "
                "KEELTUBE_APPEALS_TOKEN together, or use OIDC mode."
            ),
        )
    if len(set(present.values())) != len(present):
        raise HTTPException(
            status_code=500,
            detail=(
                "KEELTUBE_CREATOR_TOKEN, KEELTUBE_MODERATOR_TOKEN, and "
                "KEELTUBE_APPEALS_TOKEN must be distinct."
            ),
        )
    return {token: role for role, token in present.items()}


def _authorization(request: Request) -> str:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return ""
    return token.strip()


def _match(token: str, expected: str) -> bool:
    return bool(token) and hmac.compare_digest(token.encode("utf-8"), expected.encode("utf-8"))


def resolve_principal(request: Request, required_roles: tuple[str, ...] | None = None) -> Principal:
    oidc_config = OIDCConfig.from_env()
    if oidc_config is not None:
        principal = verify_oidc_token(request, oidc_config)
        if required_roles is not None and principal.role not in required_roles:
            raise HTTPException(
                status_code=403,
                detail=f"One of these roles is required: {', '.join(required_roles)}.",
            )
        return principal

    role_tokens = _role_tokens()
    if not role_tokens:
        raise HTTPException(
            status_code=503,
            detail=(
                "Moderation authentication is not configured. Set OIDC or all three role tokens."
            ),
        )

    token = _authorization(request)
    for configured, role in role_tokens.items():
        if _match(token, configured):
            if required_roles is not None and role not in required_roles:
                raise HTTPException(
                    status_code=403,
                    detail=f"One of these roles is required: {', '.join(required_roles)}.",
                )
            return _principal(role)
    raise HTTPException(status_code=401, detail="A valid role token is required.")


def require_roles(*roles: str):
    def dependency(request: Request) -> Principal:
        return resolve_principal(request, roles)

    return dependency


def creator_channel(principal: Principal, requested: str | None = None) -> str:
    if not principal.channel_ids:
        raise HTTPException(
            status_code=403,
            detail="The creator principal has no channel scope.",
        )
    if requested is not None:
        if requested not in principal.channel_ids:
            raise HTTPException(
                status_code=403,
                detail="The creator principal is not authorized for that channel.",
            )
        return requested
    if len(principal.channel_ids) == 1:
        return principal.channel_ids[0]
    raise HTTPException(
        status_code=422,
        detail="channel_id is required for a multi-channel creator principal.",
    )


def current_principal(request: Request) -> Principal:
    return resolve_principal(request, None)
