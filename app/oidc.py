from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import timedelta
from threading import Lock
from typing import Any, cast
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import jwt
from fastapi import HTTPException
from fastapi import Request as FastAPIRequest

from .principals import ALLOWED_ROLES, Principal, Role, valid_channel_id

DEFAULT_ISSUER = "keel-core"
DEFAULT_AUDIENCE = "keel-moderation"
DEFAULT_ROLE_CLAIM = "role"
DEFAULT_CHANNEL_CLAIM = "channel_ids"
DEFAULT_SUBJECT_CLAIM = "sub"
DEFAULT_ALGORITHMS = ("RS256", "ES256")
DISCOVERY_TIMEOUT_SECONDS = 10
DEFAULT_JWKS_CACHE_SECONDS = 300
DEFAULT_LEEWAY_SECONDS = 60

_jwks_clients: dict[tuple[str, int], jwt.PyJWKClient] = {}
_jwks_lock = Lock()
_discovery_cache: dict[str, tuple[float, str]] = {}
_discovery_lock = Lock()


@dataclass(frozen=True)
class OIDCConfig:
    issuer: str
    audience: str | None
    jwks_url: str | None
    hmac_secret: str | None
    role_claim: str
    channel_claim: str
    subject_claim: str
    algorithms: tuple[str, ...]
    leeway_seconds: int
    jwks_cache_seconds: int

    @classmethod
    def from_env(cls) -> "OIDCConfig | None":
        issuer = os.getenv("KEELTUBE_OIDC_ISSUER", "").strip()
        if not issuer:
            return None

        hmac_secret = os.getenv("KEELTUBE_OIDC_HMAC_SECRET", "").strip() or None
        jwks_url = os.getenv("KEELTUBE_OIDC_JWKS_URL", "").strip() or None
        if hmac_secret is not None and len(hmac_secret) < 32:
            raise HTTPException(
                status_code=500,
                detail="KEELTUBE_OIDC_HMAC_SECRET must be at least 32 characters.",
            )
        if hmac_secret is None and jwks_url is None:
            raise HTTPException(
                status_code=500,
                detail=(
                    "KEELTUBE_OIDC_ISSUER requires KEELTUBE_OIDC_JWKS_URL "
                    "or KEELTUBE_OIDC_HMAC_SECRET."
                ),
            )

        raw_algorithms = os.getenv("KEELTUBE_OIDC_ALGORITHMS", "").strip()
        if raw_algorithms:
            algorithms = tuple(
                value.strip().upper() for value in raw_algorithms.split(",") if value.strip()
            )
        elif hmac_secret is not None and jwks_url is not None:
            algorithms = ("HS256",) + DEFAULT_ALGORITHMS
        elif hmac_secret is not None:
            algorithms = ("HS256",)
        else:
            algorithms = DEFAULT_ALGORITHMS

        if any(algorithm.startswith("HS") for algorithm in algorithms) and hmac_secret is None:
            raise HTTPException(
                status_code=500,
                detail="HS256 OIDC algorithms require KEELTUBE_OIDC_HMAC_SECRET.",
            )

        return cls(
            issuer=issuer,
            audience=os.getenv("KEELTUBE_OIDC_AUDIENCE", "").strip() or None,
            jwks_url=jwks_url,
            hmac_secret=hmac_secret,
            role_claim=os.getenv("KEELTUBE_OIDC_ROLE_CLAIM", DEFAULT_ROLE_CLAIM).strip()
            or DEFAULT_ROLE_CLAIM,
            channel_claim=os.getenv("KEELTUBE_OIDC_CHANNEL_CLAIM", DEFAULT_CHANNEL_CLAIM).strip()
            or DEFAULT_CHANNEL_CLAIM,
            subject_claim=os.getenv("KEELTUBE_OIDC_SUBJECT_CLAIM", DEFAULT_SUBJECT_CLAIM).strip()
            or DEFAULT_SUBJECT_CLAIM,
            algorithms=algorithms,
            leeway_seconds=_env_int("KEELTUBE_OIDC_LEEWAY_SECONDS", DEFAULT_LEEWAY_SECONDS),
            jwks_cache_seconds=_env_int(
                "KEELTUBE_OIDC_JWKS_CACHE_SECONDS", DEFAULT_JWKS_CACHE_SECONDS
            ),
        )


def verify_oidc_token(request: FastAPIRequest, config: OIDCConfig) -> Principal:
    token = _authorization(request)
    if not token:
        raise HTTPException(status_code=401, detail="A valid OIDC bearer token is required.")

    try:
        key = _signing_key(token, config)
        claims = jwt.decode(
            token,
            key=key,
            algorithms=list(config.algorithms),
            audience=config.audience,
            issuer=config.issuer,
            leeway=timedelta(seconds=config.leeway_seconds),
            options={"require": ["exp", "iat", "sub"]},
        )
    except jwt.ExpiredSignatureError as error:
        raise HTTPException(status_code=401, detail="The OIDC token has expired.") from error
    except jwt.InvalidAudienceError as error:
        raise HTTPException(
            status_code=401, detail="The OIDC token audience is invalid."
        ) from error
    except jwt.InvalidIssuerError as error:
        raise HTTPException(status_code=401, detail="The OIDC token issuer is invalid.") from error
    except jwt.InvalidTokenError as error:
        raise HTTPException(status_code=401, detail="A valid OIDC token is required.") from error
    except HTTPException:
        raise
    except (HTTPError, URLError, jwt.PyJWKClientError) as error:
        raise HTTPException(status_code=503, detail="The OIDC provider is unavailable.") from error
    except Exception as error:
        raise HTTPException(status_code=503, detail="The OIDC provider is unavailable.") from error

    if not isinstance(claims, dict):
        raise HTTPException(status_code=401, detail="A valid OIDC token is required.")
    return _principal_from_claims(claims, config)


def _signing_key(token: str, config: OIDCConfig) -> Any:
    header = jwt.get_unverified_header(token)
    algorithm = header.get("alg")
    if not isinstance(algorithm, str) or algorithm not in config.algorithms:
        raise jwt.InvalidAlgorithmError("The token algorithm is not allowed.")
    if algorithm.startswith("HS"):
        if config.hmac_secret is None:
            raise jwt.InvalidKeyError("The token algorithm is not configured.")
        return config.hmac_secret
    jwks_url = config.jwks_url or _discover_jwks_url(config)
    client = _jwks_client(jwks_url, config.jwks_cache_seconds)
    return client.get_signing_key_from_jwt(token).key


def _jwks_client(url: str, cache_seconds: int) -> jwt.PyJWKClient:
    key = (url, cache_seconds)
    with _jwks_lock:
        client = _jwks_clients.get(key)
        if client is None:
            client = jwt.PyJWKClient(
                url,
                cache_keys=True,
                cache_jwk_set=True,
                lifespan=cache_seconds,
            )
            _jwks_clients[key] = client
        return client


def _discover_jwks_url(config: OIDCConfig) -> str:
    now = time.monotonic()
    with _discovery_lock:
        cached = _discovery_cache.get(config.issuer)
        if cached is not None and now < cached[0]:
            return cached[1]

    discovery_url = config.issuer.rstrip("/") + "/.well-known/openid-configuration"
    try:
        request = Request(discovery_url, headers={"Accept": "application/json"})
        with urlopen(request, timeout=DISCOVERY_TIMEOUT_SECONDS) as response:
            payload = json.load(response)
    except (HTTPError, URLError, ValueError) as error:
        raise HTTPException(status_code=503, detail="The OIDC provider is unavailable.") from error

    jwks_url = payload.get("jwks_uri") if isinstance(payload, dict) else None
    if not isinstance(jwks_url, str) or not jwks_url:
        raise HTTPException(status_code=503, detail="The OIDC provider did not publish a JWKS URI.")

    with _discovery_lock:
        _discovery_cache[config.issuer] = (
            now + config.jwks_cache_seconds,
            jwks_url,
        )
    return jwks_url


def _principal_from_claims(claims: dict, config: OIDCConfig) -> Principal:
    subject = claims.get(config.subject_claim)
    if not isinstance(subject, str) or not subject.strip():
        raise HTTPException(status_code=403, detail="The OIDC token has no subject claim.")

    role_value = claims.get(config.role_claim)
    if isinstance(role_value, list):
        role_value = next(
            (value for value in role_value if isinstance(value, str) and value in ALLOWED_ROLES),
            None,
        )
    if not isinstance(role_value, str) or role_value not in ALLOWED_ROLES:
        raise HTTPException(status_code=403, detail="The OIDC token has no recognized role.")

    channel_values = claims.get(config.channel_claim)
    if channel_values is None:
        channel_values = claims.get("channel_id")
    if isinstance(channel_values, str):
        channel_values = [channel_values]
    if channel_values is None:
        channel_values = []
    if not isinstance(channel_values, list):
        raise HTTPException(status_code=403, detail="The OIDC token channel claim is invalid.")

    channel_ids: list[str] = []
    for value in channel_values:
        if not valid_channel_id(value):
            raise HTTPException(status_code=403, detail="The OIDC token channel claim is invalid.")
        if value not in channel_ids:
            channel_ids.append(value)

    return Principal(
        id=subject.strip(),
        role=cast(Role, role_value),
        channel_ids=tuple(channel_ids),
    )


def _authorization(request: FastAPIRequest) -> str:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return ""
    return token.strip()


def _env_int(name: str, fallback: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return fallback
    try:
        value = int(raw)
    except ValueError:
        return fallback
    return value if value > 0 else fallback
