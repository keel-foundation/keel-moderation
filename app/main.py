from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from json import dumps
from threading import Lock
from typing import Literal
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

app = FastAPI(
    title="TubeKeel Moderation",
    version="0.1.0",
    description="Signals, cases, appeals, audit ledger, and content protection.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

DEMO_POLICY = {
    "policy_version": "demo-0.1",
    "reason_code": "spam.mass_production",
    "human_review_required": True,
    "appeal_window_days": 365,
}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:12]}"


class ActionCreate(BaseModel):
    video_id: str
    policy_clause: str = "spam.mass_production"
    reason: str
    evidence: list[str] = Field(default_factory=list)


class AppealCreate(BaseModel):
    grounds: str


class AppealDecision(BaseModel):
    decision: Literal["reverse", "uphold"]
    reasoning: str


class DemoState:
    def __init__(self) -> None:
        self.lock = Lock()
        self.action: dict | None = None
        self.appeal: dict | None = None
        self.ledger: list[dict] = []

    def reset(self) -> None:
        with self.lock:
            self.action = None
            self.appeal = None
            self.ledger = []

    def append(self, record_type: str, payload: dict) -> dict:
        payload = deepcopy(payload)
        previous = self.ledger[-1]["hash"] if self.ledger else "GENESIS"
        record = {
            "seq": len(self.ledger) + 1,
            "type": record_type,
            "at": now(),
            "payload": payload,
            "prev_hash": previous,
        }
        canonical = dumps(
            {"type": record_type, "at": record["at"], "payload": payload},
            sort_keys=True,
            separators=(",", ":"),
        )
        record["hash"] = "sha256:" + sha256(canonical.encode("utf-8")).hexdigest()
        self.ledger.append(record)
        return record

    def snapshot(self) -> dict:
        return {
            "action": self.action,
            "appeal": self.appeal,
            "ledger": self.ledger,
            "policy": DEMO_POLICY,
        }


state = DemoState()


class Health(BaseModel):
    status: str
    service: str


class PolicySummary(BaseModel):
    policies: list[dict]


@app.get("/health", response_model=Health)
def health() -> Health:
    return Health(status="ok", service="moderation")


@app.get("/v1/policies", response_model=PolicySummary)
def policies() -> PolicySummary:
    return PolicySummary(policies=[DEMO_POLICY])


@app.get("/v1/demo/state")
def demo_state() -> dict:
    with state.lock:
        return state.snapshot()


@app.post("/v1/demo/reset")
def demo_reset() -> dict:
    state.reset()
    with state.lock:
        return state.snapshot()


@app.post("/v1/actions", status_code=201)
def create_action(body: ActionCreate) -> dict:
    with state.lock:
        if state.action and state.action["status"] == "active":
            raise HTTPException(
                status_code=409,
                detail="An active demo action already exists. Reset the demo first.",
            )

        action_id = new_id("act")
        payload = {
            "id": action_id,
            "video_id": body.video_id,
            "policy_clause": body.policy_clause,
            "reason": body.reason,
            "evidence": body.evidence,
        }
        record = state.append("enforcement_action.created", payload)
        action = {
            **payload,
            "human_reviewer": "demo-reviewer",
            "status": "active",
            "created_at": now(),
            "appeal_deadline": (
                datetime.now(timezone.utc) + timedelta(days=365)
            ).isoformat(),
            "audit_record_hash": record["hash"],
        }
        state.action = action
        state.appeal = None
        return action


@app.post("/v1/actions/{action_id}/appeal", status_code=201)
def file_appeal(action_id: str, body: AppealCreate) -> dict:
    with state.lock:
        if not state.action or state.action["id"] != action_id:
            raise HTTPException(status_code=404, detail="Action not found")
        if state.action["status"] != "active":
            raise HTTPException(
                status_code=409,
                detail="Only an active action can be appealed.",
            )
        if state.appeal and state.appeal["status"] == "pending":
            raise HTTPException(status_code=409, detail="An appeal is already pending.")

        appeal = {
            "id": new_id("apl"),
            "action_id": action_id,
            "grounds": body.grounds,
            "status": "pending",
            "created_at": now(),
        }
        state.appeal = appeal
        state.append("appeal.filed", appeal)
        return appeal


@app.post("/v1/appeals/{appeal_id}/decide")
def decide_appeal(appeal_id: str, body: AppealDecision) -> dict:
    with state.lock:
        if not state.appeal or state.appeal["id"] != appeal_id:
            raise HTTPException(status_code=404, detail="Appeal not found")
        if state.appeal["status"] != "pending":
            raise HTTPException(status_code=409, detail="Appeal already decided")
        if not state.action:
            raise HTTPException(status_code=409, detail="No action to update")

        state.appeal.update(
            {
                "status": "decided",
                "decision": body.decision,
                "reasoning": body.reasoning,
                "decided_at": now(),
            }
        )
        state.append(
            "appeal.decided",
            {
                "appeal_id": appeal_id,
                "action_id": state.action["id"],
                "decision": body.decision,
                "reasoning": body.reasoning,
            },
        )

        if body.decision == "reverse":
            state.action["status"] = "reversed"
            record = state.append(
                "enforcement_action.reversed",
                {
                    "action_id": state.action["id"],
                    "appeal_id": appeal_id,
                    "reasoning": body.reasoning,
                },
            )
            state.action["reversal_record_hash"] = record["hash"]

        return state.snapshot()


@app.get("/v1/ledger")
def ledger() -> dict:
    with state.lock:
        return {"ledger": state.ledger}
