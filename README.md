# moderation

The moderation and content-protection service: signals, cases, human review, appeals, audit
ledger, and transparency aggregates.

**Status:** UAT-ready with durable workflow state. Cases, actions, and appeals are stored in
PostgreSQL with versioned migrations; SQLite remains available as an explicit offline/test
fallback. The audit ledger remains append-only JSONL. OIDC claim verification and role-separated
creator, moderator, and appeals-reviewer tokens are supported. Creator-scoped case, action, appeal,
summary, and ledger reads, staff review endpoints, signal ingestion, creator notifications, and the
contract tests are implemented. `keel-moderation-web` is the staff portal that consumes the staff
endpoints. There is no real classifier or public transparency generator yet.

`pytest` and `ruff check app tests` pass. Set `KEELTUBE_TEST_DATABASE_URL` to also run the
PostgreSQL persistence test; it skips when no test database is configured.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | Liveness |
| `GET` | `/v1/policies` | Active demo policy |
| `GET` | `/v1/demo/state` | Current action, appeal, case, ledger, verification |
| `POST` | `/v1/demo/reset` | Clear workflow state and append a reset record |
| `GET` | `/v1/whoami` | Resolve the current principal and role |
| `POST` | `/v1/signals` | Ingest a viewer report as a signal and open case (service token) |
| `GET` | `/v1/public/videos/{video_id}/record` | Public creator-safe case/action/appeal record |
| `POST` | `/v1/actions` | Create a demo enforcement action |
| `POST` | `/v1/actions/{action_id}/appeal` | File an appeal |
| `POST` | `/v1/appeals/{appeal_id}/decide` | Decide an appeal |
| `GET` | `/v1/summary` | Staff queue counts and ledger status |
| `GET` | `/v1/cases` | List cases for staff review (`status`, `channel_id`, `video_id`, paging) |
| `GET` | `/v1/cases/{case_id}` | Read one case with channel scope |
| `POST` | `/v1/cases/{case_id}/actions` | Record enforcement for an existing case |
| `POST` | `/v1/cases/{case_id}/dismiss` | Close a case without enforcement |
| `GET` | `/v1/actions` | List enforcement actions (`status`, `channel_id`, `video_id`, paging) |
| `GET` | `/v1/actions/{action_id}` | Read one enforcement action |
| `GET` | `/v1/appeals?status=pending\|decided` | List appeals for the review queue |
| `GET` | `/v1/appeals/{appeal_id}` | Read one appeal |
| `GET` | `/v1/creator/summary` | Creator-scoped transparency counts |
| `GET` | `/v1/creator/cases` | Creator-scoped cases |
| `GET` | `/v1/creator/cases/{case_id}` | Read one creator case |
| `GET` | `/v1/creator/actions` | Creator-scoped enforcement actions |
| `GET` | `/v1/creator/actions/{action_id}` | Read one creator action |
| `GET` | `/v1/creator/appeals` | Creator-scoped appeals |
| `GET` | `/v1/creator/appeals/{appeal_id}` | Read one creator appeal |
| `GET` | `/v1/creator/ledger` | Creator-scoped audit records |
| `GET` | `/v1/creator/ledger/verify` | Verify the complete hash chain |
| `GET` | `/v1/creator/ledger/export?format=jsonl\|csv` | Export creator-scoped audit records |
| `GET` | `/v1/ledger` | Ledger and verification result |
| `GET` | `/v1/ledger/verify` | Verify the hash chain |
| `GET` | `/v1/ledger/export?format=jsonl\|csv` | Export the ledger |

## Authentication and roles

There are two modes. They are selected by environment variables at request time. OIDC mode takes
precedence when configured.

### OIDC mode

Set `KEELTUBE_OIDC_ISSUER` to accept bearer JWTs and derive the principal from claims:

| Variable | Default | Purpose |
|---|---|---|
| `KEELTUBE_OIDC_ISSUER` | unset | Enables OIDC mode; must match the token `iss` |
| `KEELTUBE_OIDC_AUDIENCE` | unset | Optional expected `aud`; recommended |
| `KEELTUBE_OIDC_JWKS_URL` | discovery | JWKS endpoint; defaults to issuer discovery |
| `KEELTUBE_OIDC_HMAC_SECRET` | unset | Shared HS256 secret for core-minted claims |
| `KEELTUBE_OIDC_ALGORITHMS` | `RS256,ES256` (or `HS256` with a secret) | Accepted JWT algorithms |
| `KEELTUBE_OIDC_SUBJECT_CLAIM` | `sub` | Principal ID claim |
| `KEELTUBE_OIDC_ROLE_CLAIM` | `role` | Role claim; one of `creator`, `moderator`, `appeals`, `operator` |
| `KEELTUBE_OIDC_CHANNEL_CLAIM` | `channel_ids` | Channel scope claim; a string or list of `ch_*` IDs |
| `KEELTUBE_OIDC_LEEWAY_SECONDS` | `60` | Clock skew allowance |
| `KEELTUBE_OIDC_JWKS_CACHE_SECONDS` | `300` | JWKS and discovery cache lifetime |

The issuer, signature, audience, expiry, and required `sub` claim are verified. A token with no
recognized role or an invalid channel claim returns `403`; a missing, expired, or badly signed token
returns `401`. Provider/JWKS failures return `503` (fail closed). When OIDC mode is enabled, the
static role tokens are ignored.

The core creator moderation gateway uses this mode with `KEELTUBE_OIDC_ISSUER=keel-core`,
`KEELTUBE_OIDC_AUDIENCE=keel-moderation`, `KEELTUBE_OIDC_ALGORITHMS=HS256`, and a shared
`KEELTUBE_OIDC_HMAC_SECRET` of at least 32 characters. Core mints short-lived claims containing the
real actor ID, role, and channel IDs from the authenticated session.

### Role-token mode

Set all three tokens to distinct values. If neither OIDC nor role tokens are configured, every
authenticated endpoint fails closed with `503`.

| Variable | Role | Principal |
|---|---|---|
| `KEELTUBE_CREATOR_TOKEN` | `creator` | `creator:demo` |
| `KEELTUBE_MODERATOR_TOKEN` | `moderator` | `demo-reviewer` |
| `KEELTUBE_APPEALS_TOKEN` | `appeals` | `demo-appeals-reviewer` |

Then:

- creating an action requires the moderator token;
- filing an appeal requires the creator token;
- deciding an appeal requires the appeals token;
- listing appeals requires the moderator or appeals token;
- creator-scoped read endpoints require the creator token.

Creator channel scope is an authenticated principal claim. In OIDC mode it comes from the
`channel_ids` claim. In role-token mode, it comes from `KEELTUBE_CREATOR_CHANNEL_MAP` (default
`{"creator:demo":["ch_demo"]}`). Production must use OIDC claims.

Creator read endpoints accept an optional `channel_id` query parameter. A single-channel principal
can omit it; a multi-channel principal must provide it; a channel outside the principal's claim
returns `403`. Appeal filing is also channel-scoped: a creator cannot appeal an action that belongs
to another channel. Creator cases and actions accept an optional `video_id` filter.

The service enforces reviewer independence:

- an appellant cannot decide their own appeal;
- the original decision-maker cannot decide the appeal;
- a role token can only call the endpoints allowed for its role.

Set all three role tokens or none. They must be distinct; otherwise requests fail with a
configuration error.

Read endpoints (`/v1/demo/state`, `/v1/ledger`, `/v1/ledger/verify`, and
`/v1/ledger/export`) remain unauthenticated in this local demo. Do not expose them to a shared or
public network. The `/v1/creator/*` endpoints are authorized and channel-scoped.

## Staff moderation portal

The private `keel-moderation-web` repository is the staff portal. Its Node BFF holds the moderator
and appeals role tokens server-side and calls the staff endpoints above. Moderator sessions can list
cases and actions, record enforcement on a case, and dismiss a case. Appeals sessions can inspect the
record and decide pending appeals. The portal never exposes a moderation token to the browser.

## Persistence

- Workflow state (cases, actions, appeals) is stored in PostgreSQL when
  `KEELTUBE_MODERATION_DATABASE_URL` or `KEELTUBE_DATABASE_URL` is set. Migrations live in
  `app/migrations` and are applied once on startup under `moderation_schema_migrations`.
- Set `KEELTUBE_STATE_DB` to a SQLite path only for offline development or tests. A PostgreSQL DSN
  in `KEELTUBE_STATE_DB` is also accepted; explicit database URL variables take precedence.
- Workflow tables are prefixed (`moderation_cases`, `moderation_actions`, `moderation_appeals`) so
  the service can share the devstack database with core without collisions.
- Cases, actions, and appeals carry a `channel_id` column for explicit creator scoping. The
  domain objects returned by the existing demo endpoints remain unchanged.
- The audit ledger remains append-only JSONL at `KEELTUBE_LEDGER_PATH`.
- State survives a restart. The ledger is verified on load.
- `POST /v1/demo/reset` clears the workflow tables but appends a `demo.reset` ledger record; it
  never truncates the ledger.

## Ledger properties

- Each record has `seq`, `type`, `at`, `payload`, `prev_hash`, and `hash`.
- `hash = SHA-256(canonical_json({seq, type, at, payload, prev_hash}))`.
- The first record uses `prev_hash: "GENESIS"`.
- Records are appended to a JSONL file and verified on load.
- The payload records the domain object, the acting principal, and the explicit channel scope, for
  example `{"action": {...}, "actor": {"id": "demo-reviewer", "role": "moderator"},
  "channel_id": "ch_demo"}`. Creator ledger reads filter on this explicit `channel_id`, never on
  string matching against case, action, or appeal IDs.
- The ledger can be verified and exported through the API. There is no external anchor yet.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `KEELTUBE_LEDGER_PATH` | `data/ledger.jsonl` | Append-only ledger file |
| `KEELTUBE_MODERATION_DATABASE_URL` | unset | PostgreSQL workflow state; preferred |
| `KEELTUBE_DATABASE_URL` | unset | PostgreSQL fallback when the moderation-specific URL is unset |
| `KEELTUBE_STATE_DB` | `data/state.sqlite3` | SQLite workflow state when PostgreSQL is unset |
| `KEELTUBE_CORS_ORIGINS` | `http://localhost:3000` | Comma-separated allowed origins |
| `KEELTUBE_RATE_LIMIT` | `30` | Mutating requests per IP per minute; `0` disables |
| `KEELTUBE_OIDC_ISSUER` | unset | Enables OIDC mode |
| `KEELTUBE_OIDC_AUDIENCE` | unset | Expected OIDC audience |
| `KEELTUBE_OIDC_JWKS_URL` | issuer discovery | JWKS endpoint |
| `KEELTUBE_OIDC_HMAC_SECRET` | unset | Shared HS256 secret for core-minted claims |
| `KEELTUBE_OIDC_ALGORITHMS` | `RS256,ES256` or `HS256` | Accepted JWT algorithms |
| `KEELTUBE_OIDC_SUBJECT_CLAIM` | `sub` | Principal ID claim |
| `KEELTUBE_OIDC_ROLE_CLAIM` | `role` | Role claim |
| `KEELTUBE_OIDC_CHANNEL_CLAIM` | `channel_ids` | Channel scope claim |
| `KEELTUBE_OIDC_LEEWAY_SECONDS` | `60` | Clock skew allowance |
| `KEELTUBE_OIDC_JWKS_CACHE_SECONDS` | `300` | JWKS/discovery cache lifetime |
| `KEELTUBE_CREATOR_CHANNEL_MAP` | `{"creator:demo":["ch_demo"]}` | Role-token channel claims |
| `KEELTUBE_SIGNAL_INGEST_TOKEN` | unset | Service token for `POST /v1/signals`; unset disables the route |
| `KEELTUBE_CORE_URL` | unset | Core base URL for creator notification delivery |
| `KEELTUBE_CORE_INTERNAL_TOKEN` | unset | Core internal notification token |
| `KEELTUBE_CLASSIFIER_URL` | unset | Optional external classifier endpoint |
| `KEELTUBE_CLASSIFIER_TOKEN` | unset | Optional classifier bearer token |
| `KEELTUBE_CLASSIFIER_TIMEOUT_SECONDS` | `5` | Classifier request timeout |
| `KEELTUBE_CREATOR_TOKEN` | unset | Creator token for role-enforced mode |
| `KEELTUBE_MODERATOR_TOKEN` | unset | Moderator token for role-enforced mode |
| `KEELTUBE_APPEALS_TOKEN` | unset | Appeals reviewer token for role-enforced mode |

## Run

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.lock
.venv/bin/pip install -e . --no-deps
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8082
.venv/bin/pytest -q
.venv/bin/ruff check app tests
```

Moderation sends best-effort creator notifications to core on new signals, enforcement actions, and
appeal decisions when `KEELTUBE_CORE_URL` and `KEELTUBE_CORE_INTERNAL_TOKEN` are configured. Signals
use the external classifier when `KEELTUBE_CLASSIFIER_URL` is set; otherwise the deterministic
keyword classifier fills signal type, confidence, and model version.

The devstack runs `python -m app.seed` before moderation starts. The seed is idempotent and creates
a deterministic case/action/appeal/ledger record for `ch_demo`; run it manually with:

```sh
.venv/bin/python -m app.seed
```

`requirements-dev.lock` pins the full development dependency set. Regenerate it after changing
`pyproject.toml`.

The contract tests validate responses against `keel-spec`. Clone `keel-spec` as a sibling of
`keel-moderation` (`../keel-spec`) so the tests can find the schemas and OpenAPI document. If it
is absent, the contract tests skip.

## Next

1. Move the ledger to PostgreSQL with a signed periodic external anchor.
2. Add the policy bundle loader and signed policy versioning.
3. Add SLA timers and escalation to the review queue.
4. Add production classifier models and richer signal correlation.
5. Add the public transparency report generator.

## Licensing

AGPL-3.0. See [LICENSE](LICENSE).
