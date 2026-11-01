# moderation

The moderation and content-protection service: signals, cases, human review, appeals, audit ledger, and transparency aggregates.

**Status:** local demo. It models an automated signal, a case, an enforcement action, an independent appeal review, and an append-only hash-chained ledger. There is no database, production authentication, real classifier, review queue, or transparency generator yet.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | Liveness |
| `GET` | `/v1/policies` | Active demo policy |
| `GET` | `/v1/demo/state` | Current action, appeal, case, ledger, verification |
| `POST` | `/v1/demo/reset` | Clear action state and append a reset record |
| `POST` | `/v1/actions` | Create a demo enforcement action |
| `POST` | `/v1/actions/{action_id}/appeal` | File an appeal |
| `POST` | `/v1/appeals/{appeal_id}/decide` | Decide an appeal |
| `GET` | `/v1/ledger` | Ledger and verification result |
| `GET` | `/v1/ledger/verify` | Verify the hash chain |
| `GET` | `/v1/ledger/export?format=jsonl\|csv` | Export the ledger |

Mutating endpoints accept an optional `Authorization: Bearer <token>` when `TUBEKEEL_DEMO_TOKEN` is set.

## Ledger properties

- Each record has `seq`, `type`, `at`, `payload`, `prev_hash`, and `hash`.
- `hash = SHA-256(canonical_json({seq, type, at, payload, prev_hash}))`.
- The first record uses `prev_hash: "GENESIS"`.
- Records are appended to a JSONL file and verified on load.
- `POST /v1/demo/reset` clears action state but appends a reset record; it never truncates the ledger.
- The ledger can be verified and exported through the API. There is no external anchor yet.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `TUBEKEEL_LEDGER_PATH` | `data/ledger.jsonl` | Append-only ledger file |
| `TUBEKEEL_CORS_ORIGINS` | `http://localhost:3000` | Comma-separated allowed origins |
| `TUBEKEEL_RATE_LIMIT` | `30` | Mutating requests per IP per minute; `0` disables |
| `TUBEKEEL_DEMO_TOKEN` | unset | Optional bearer token for mutating endpoints |

## Run

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.lock
.venv/bin/pip install -e . --no-deps
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8082
.venv/bin/pytest -q
.venv/bin/ruff check app tests
```

`requirements-dev.lock` pins the full development dependency set. Regenerate it after changing
`pyproject.toml`.

The contract tests validate responses against `keel-spec`. Clone `keel-spec` as a sibling of
`keel-moderation` (`../keel-spec`) so the tests can find the schemas and OpenAPI document. If it
is absent, the contract tests skip.

## Next

1. Replace the JSONL demo ledger with a PostgreSQL append-only ledger and periodic anchor.
2. Add the policy bundle loader and signed policy versioning.
3. Add the review queue and SLA timers.
4. Add a real signal-ingestion API and classifier adapters.
5. Add the transparency report generator.

## Licensing

AGPL-3.0. See [LICENSE](LICENSE).
