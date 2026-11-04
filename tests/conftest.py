import os
import tempfile
from pathlib import Path

import pytest

_tmp = tempfile.mkdtemp(prefix="keel-moderation-tests-")
os.environ["KEELTUBE_LEDGER_PATH"] = str(Path(_tmp) / "ledger.jsonl")
os.environ["KEELTUBE_STATE_DB"] = str(Path(_tmp) / "state.sqlite3")
os.environ["KEELTUBE_RATE_LIMIT"] = "0"
os.environ["KEELTUBE_CREATOR_TOKEN"] = "creator-token"
os.environ["KEELTUBE_MODERATOR_TOKEN"] = "moderator-token"
os.environ["KEELTUBE_APPEALS_TOKEN"] = "appeals-token"
for _name in (
    "KEELTUBE_CREATOR_CHANNEL_MAP",
    "KEELTUBE_OIDC_ISSUER",
    "KEELTUBE_OIDC_AUDIENCE",
    "KEELTUBE_OIDC_JWKS_URL",
    "KEELTUBE_OIDC_HMAC_SECRET",
    "KEELTUBE_OIDC_ALGORITHMS",
    "KEELTUBE_OIDC_ROLE_CLAIM",
    "KEELTUBE_OIDC_CHANNEL_CLAIM",
    "KEELTUBE_OIDC_SUBJECT_CLAIM",
    "KEELTUBE_OIDC_LEEWAY_SECONDS",
    "KEELTUBE_OIDC_JWKS_CACHE_SECONDS",
):
    os.environ.pop(_name, None)


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    from app.main import app, state

    state.reset()
    return TestClient(app)
