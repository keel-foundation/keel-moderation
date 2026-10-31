# moderation

The moderation and content-protection service: signals, cases, human review, appeals, audit ledger, and transparency aggregates.

**Status:** scaffold. Only `/health` and an empty `/v1/policies` exist.

## Responsibilities

- Ingest signals from automation, user reports, priority flaggers, and legal orders.
- Open cases and assemble evidence bundles.
- Enforce human review gates and penalty ranges from the Constitution.
- Record every action, review, and appeal in a hash-chained ledger.
- Generate the quarterly transparency report and Creator Trust Index.
- Run content protection: piracy matching, impersonation, spam/scam/fraud signals.

## Layout

```
app/main.py           FastAPI entrypoint
app/policies/         policy bundles as code (to be added)
app/classifiers/      signal classifiers and model adapters (to be added)
```

## Run

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
uvicorn app.main:app --reload --port 8082
```

## Next

1. Define the `Signal`, `ModerationCase`, `EnforcementAction`, and `Appeal` schemas.
2. Add the policy bundle loader and versioning.
3. Add the append-only, hash-chained ledger.
4. Add the human review queue and SLA timers.
5. Add the transparency report generator.
