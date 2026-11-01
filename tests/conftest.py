import os
import tempfile
from pathlib import Path

import pytest

_tmp = tempfile.mkdtemp(prefix="keel-moderation-tests-")
os.environ["TUBEKEEL_LEDGER_PATH"] = str(Path(_tmp) / "ledger.jsonl")
os.environ["TUBEKEEL_STATE_DB"] = str(Path(_tmp) / "state.sqlite3")
os.environ["TUBEKEEL_RATE_LIMIT"] = "0"
for _name in (
    "TUBEKEEL_DEMO_TOKEN",
    "TUBEKEEL_CREATOR_TOKEN",
    "TUBEKEEL_MODERATOR_TOKEN",
    "TUBEKEEL_APPEALS_TOKEN",
):
    os.environ.pop(_name, None)


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    from app.main import app, state

    state.reset()
    return TestClient(app)
