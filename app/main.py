from __future__ import annotations

from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI(
    title="TubeKeel Moderation",
    version="0.1.0",
    description="Signals, cases, appeals, audit ledger, and content protection.",
)


class Health(BaseModel):
    status: str
    service: str


class PolicySummary(BaseModel):
    policies: list[str]


@app.get("/health", response_model=Health)
def health() -> Health:
    return Health(status="ok", service="moderation")


@app.get("/v1/policies", response_model=PolicySummary)
def policies() -> PolicySummary:
    return PolicySummary(policies=[])
