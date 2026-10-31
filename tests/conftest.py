import os
import tempfile
from pathlib import Path

import pytest

_tmp = tempfile.mkdtemp(prefix="keel-moderation-tests-")
os.environ["TUBEKEEL_LEDGER_PATH"] = str(Path(_tmp) / "ledger.jsonl")
os.environ["TUBEKEEL_RATE_LIMIT"] = "0"
os.environ.pop("TUBEKEEL_DEMO_TOKEN", None)


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    from app.main import app, state

    state.reset()
    return TestClient(app)
