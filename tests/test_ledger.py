from app.main import DemoState, Ledger


def test_chain_is_valid(tmp_path):
    ledger = Ledger(str(tmp_path / "ledger.jsonl"))
    first = ledger.append("one", {"value": 1})
    second = ledger.append("two", {"value": 2})

    assert first["prev_hash"] == "GENESIS"
    assert second["prev_hash"] == first["hash"]
    assert ledger.verify()["valid"] is True


def test_tampering_with_previous_hash_is_detected(tmp_path):
    ledger = Ledger(str(tmp_path / "ledger.jsonl"))
    ledger.append("one", {"value": 1})
    ledger.append("two", {"value": 2})

    ledger.records[-1]["prev_hash"] = "sha256:" + "0" * 64
    result = ledger.verify()

    assert result["valid"] is False
    assert result["first_broken"] == 2
    assert "previous hash" in result["reason"]


def test_tampering_with_payload_is_detected(tmp_path):
    ledger = Ledger(str(tmp_path / "ledger.jsonl"))
    ledger.append("one", {"value": 1})

    ledger.records[0]["payload"]["value"] = 99
    result = ledger.verify()

    assert result["valid"] is False
    assert result["first_broken"] == 1


def test_reordering_is_detected(tmp_path):
    ledger = Ledger(str(tmp_path / "ledger.jsonl"))
    ledger.append("one", {"value": 1})
    ledger.append("two", {"value": 2})

    ledger.records.reverse()
    result = ledger.verify()

    assert result["valid"] is False
    assert result["first_broken"] == 1


def test_persistence_reloads_and_verifies(tmp_path):
    path = str(tmp_path / "ledger.jsonl")
    first = Ledger(path)
    first.append("one", {"value": 1})
    first.append("two", {"value": 2})

    second = Ledger(path)

    assert second.verify()["valid"] is True
    assert len(second.records) == 2
    assert second.records[-1]["hash"] == first.records[-1]["hash"]


def test_reset_preserves_the_ledger(tmp_path):
    state = DemoState(str(tmp_path / "ledger.jsonl"))
    before = len(state.ledger.records)
    state.reset()

    assert state.action is None
    assert len(state.ledger.records) == before + 1
    assert state.ledger.verify()["valid"] is True
