import os
import tempfile
from pathlib import Path

import pytest

_tmp = tempfile.mkdtemp(prefix="keel-moderation-tests-")
os.environ["KEELTUBE_LEDGER_PATH"] = str(Path(_tmp) / "ledger.jsonl")
os.environ["KEELTUBE_STATE_DB"] = str(Path(_tmp) / "state.sqlite3")
os.environ["KEELTUBE_RATE_LIMIT"] = "0"
for _name in (
    "KEELTUBE_DEMO_TOKEN",
    "KEELTUBE_CREATOR_TOKEN",
    "KEELTUBE_MODERATOR_TOKEN",
    "KEELTUBE_APPEALS_TOKEN",
    "KEELTUBE_CREATOR_CHANNEL_MAP",
):
    os.environ.pop(_name, None)


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    from app.main import app, state

    state.reset()
    return TestClient(app)
