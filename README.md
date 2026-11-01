# moderation

The moderation and content-protection service: signals, cases, human review, appeals, audit
ledger, and transparency aggregates.

**Status:** local demo with durable workflow state. Cases, actions, and appeals are stored in
SQLite; the audit ledger remains append-only JSONL. Role-separated creator, moderator, and
appeals-reviewer tokens are supported. There is no production authentication, real classifier,
or transparency generator yet.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | Liveness |
| `GET` | `/v1/policies` | Active demo policy |
| `GET` | `/v1/demo/state` | Current action, appeal, case, ledger, verification |
| `POST` | `/v1/demo/reset` | Clear workflow state and append a reset record |
| `GET` | `/v1/whoami` | Resolve the current principal and role |
| `POST` | `/v1/actions` | Create a demo enforcement action |
| `POST` | `/v1/actions/{action_id}/appeal` | File an appeal |
| `POST` | `/v1/appeals/{appeal_id}/decide` | Decide an appeal |
| `GET` | `/v1/appeals?status=pending\|decided` | List appeals for the review queue |
| `GET` | `/v1/appeals/{appeal_id}` | Read one appeal |
| `GET` | `/v1/ledger` | Ledger and verification result |
| `GET` | `/v1/ledger/verify` | Verify the hash chain |
| `GET` | `/v1/ledger/export?format=jsonl\|csv` | Export the ledger |

## Authentication and roles

There are three modes. They are selected by environment variables at request time.

### Open local mode (default)

No tokens are configured. Every endpoint is allowed and each endpoint acts as its fixed demo
identity. This is for local development only. Never expose it to a network.

### Legacy demo token

Set `TUBEKEEL_DEMO_TOKEN`. The same bearer token simulates the whole flow, using the fixed demo
identities (`creator:demo`, `demo-reviewer`, `demo-appeals-reviewer`). This keeps the existing
web demo working.

### Role-enforced mode

Set all three tokens to distinct values:

| Variable | Role | Principal |
|---|---|---|
| `TUBEKEEL_CREATOR_TOKEN` | `creator` | `creator:demo` |
| `TUBEKEEL_MODERATOR_TOKEN` | `moderator` | `demo-reviewer` |
| `TUBEKEEL_APPEALS_TOKEN` | `appeals` | `demo-appeals-reviewer` |

Then:

- creating an action requires the moderator token;
- filing an appeal requires the creator token;
- deciding an appeal requires the appeals token;
- listing appeals requires the moderator or appeals token.

The service enforces reviewer independence:

- an appellant cannot decide their own appeal;
- the original decision-maker cannot decide the appeal;
- a role token can only call the endpoints allowed for its role.

The role tokens must be distinct. If they are not, requests fail with a configuration error.

Read endpoints (`/v1/demo/state`, `/v1/ledger`, `/v1/ledger/verify`, and
`/v1/ledger/export`) are unauthenticated in this local demo. Do not expose them to a shared or
public network. Production must add read authorization, per-creator scoping, and redaction.

## Persistence

- Workflow state (cases, actions, appeals) is stored in SQLite at `TUBEKEEL_STATE_DB`.
- The audit ledger remains append-only JSONL at `TUBEKEEL_LEDGER_PATH`.
- State survives a restart. The ledger is verified on load.
- `POST /v1/demo/reset` clears the workflow tables but appends a `demo.reset` ledger record; it
  never truncates the ledger.
- The schema is created on startup. This is a pre-alpha store; PostgreSQL plus migrations is the
  next persistence step.

## Ledger properties

- Each record has `seq`, `type`, `at`, `payload`, `prev_hash`, and `hash`.
- `hash = SHA-256(canonical_json({seq, type, at, payload, prev_hash}))`.
- The first record uses `prev_hash: "GENESIS"`.
- Records are appended to a JSONL file and verified on load.
- The payload records the domain object and the acting principal, for example
  `{"action": {...}, "actor": {"id": "demo-reviewer", "role": "moderator"}}`.
- The ledger can be verified and exported through the API. There is no external anchor yet.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `TUBEKEEL_LEDGER_PATH` | `data/ledger.jsonl` | Append-only ledger file |
| `TUBEKEEL_STATE_DB` | `data/state.sqlite3` | SQLite workflow state |
| `TUBEKEEL_CORS_ORIGINS` | `http://localhost:3000` | Comma-separated allowed origins |
| `TUBEKEEL_RATE_LIMIT` | `30` | Mutating requests per IP per minute; `0` disables |
| `TUBEKEEL_DEMO_TOKEN` | unset | Legacy single-token demo mode |
| `TUBEKEEL_CREATOR_TOKEN` | unset | Creator token for role-enforced mode |
| `TUBEKEEL_MODERATOR_TOKEN` | unset | Moderator token for role-enforced mode |
| `TUBEKEEL_APPEALS_TOKEN` | unset | Appeals reviewer token for role-enforced mode |

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

1. Replace SQLite workflow state with PostgreSQL and real migrations.
2. Move the ledger to PostgreSQL with a signed periodic external anchor.
3. Add the policy bundle loader and signed policy versioning.
4. Add SLA timers and escalation to the review queue.
5. Add a real signal-ingestion API and classifier adapters.
6. Add the transparency report generator.

## Licensing

AGPL-3.0. See [LICENSE](LICENSE).
