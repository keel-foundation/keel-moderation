from __future__ import annotations

import csv
import io
import json
import os
from collections import defaultdict, deque
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from threading import Lock, RLock
from time import monotonic
from typing import Deque, Literal
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field


def env_list(name: str, default: str) -> list[str]:
    raw = os.getenv(name, default)
    return [item.strip() for item in raw.split(",") if item.strip()]


app = FastAPI(
    title="TubeKeel Moderation",
    version="0.1.0",
    description="Signals, cases, appeals, audit ledger, and content protection.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=env_list("TUBEKEEL_CORS_ORIGINS", "http://localhost:3000"),
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
)

DEMO_POLICY = {
    "policy_version": "demo-0.1",
    "reason_code": "spam.mass_production",
    "human_review_required": True,
    "appeal_window_days": 365,
}

DEFAULT_REVIEWER = "demo-reviewer"
APPEALS_REVIEWER = "demo-appeals-reviewer"
DEFAULT_LEDGER_PATH = "data/ledger.jsonl"


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:12]}"


def canonical_record(record: dict) -> str:
    data = {key: value for key, value in record.items() if key != "hash"}
    return json.dumps(
        data,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


class Ledger:
    def __init__(self, path: str | None = None) -> None:
        self.path = Path(path) if path else None
        self.records: list[dict] = []
        self.lock = RLock()
        if self.path and self.path.exists():
            self._load()

    def _load(self) -> None:
        assert self.path is not None
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            self.records.append(json.loads(line))
        result = self.verify()
        if not result["valid"]:
            raise RuntimeError(
                f"ledger verification failed at record {result['first_broken']}: {result['reason']}"
            )

    def append(self, record_type: str, payload: dict) -> dict:
        with self.lock:
            seq = len(self.records) + 1
            previous = self.records[-1]["hash"] if self.records else "GENESIS"
            record = {
                "seq": seq,
                "type": record_type,
                "at": now(),
                "payload": deepcopy(payload),
                "prev_hash": previous,
            }
            digest = sha256(canonical_record(record).encode("utf-8")).hexdigest()
            record["hash"] = f"sha256:{digest}"
            self.records.append(record)
            if self.path:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
            return record

    def verify(self) -> dict:
        with self.lock:
            previous = "GENESIS"
            for index, record in enumerate(self.records, start=1):
                if record.get("seq") != index:
                    return {
                        "valid": False,
                        "records": len(self.records),
                        "head": previous,
                        "first_broken": index,
                        "reason": "sequence number mismatch",
                    }
                if record.get("prev_hash") != previous:
                    return {
                        "valid": False,
                        "records": len(self.records),
                        "head": previous,
                        "first_broken": index,
                        "reason": "previous hash mismatch",
                    }
                expected = "sha256:" + sha256(
                    canonical_record(record).encode("utf-8")
                ).hexdigest()
                if record.get("hash") != expected:
                    return {
                        "valid": False,
                        "records": len(self.records),
                        "head": previous,
                        "first_broken": index,
                        "reason": "record hash mismatch",
                    }
                previous = record["hash"]
            return {
                "valid": True,
                "records": len(self.records),
                "head": previous,
                "first_broken": None,
                "reason": None,
            }

    def export_jsonl(self) -> str:
        with self.lock:
            return "".join(
                json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
                for record in self.records
            )

    def export_csv(self) -> str:
        with self.lock:
            output = io.StringIO()
            writer = csv.DictWriter(
                output,
                fieldnames=["seq", "type", "at", "prev_hash", "hash", "payload"],
            )
            writer.writeheader()
            for record in self.records:
                writer.writerow(
                    {
                        "seq": record["seq"],
                        "type": record["type"],
                        "at": record["at"],
                        "prev_hash": record["prev_hash"],
                        "hash": record["hash"],
                        "payload": json.dumps(
                            record["payload"], ensure_ascii=False, sort_keys=True
                        ),
                    }
                )
            return output.getvalue()


class ActionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    video_id: str = Field(pattern=r"^vid_[a-zA-Z0-9_]+$", max_length=80)
    policy_clause: str = Field(default="spam.mass_production", min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=2000)
    evidence: list[str] = Field(min_length=1, max_length=50)


class AppealCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    grounds: Literal[
        "new_evidence",
        "procedural_error",
        "policy_misapplication",
        "identity",
        "other",
    ] = "policy_misapplication"
    statement: str = Field(min_length=1, max_length=4000)


class AppealDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["reverse", "uphold"]
    reasoning: str = Field(min_length=1, max_length=4000)


class DemoState:
    def __init__(self, ledger_path: str | None = None) -> None:
        self.lock = RLock()
        self.ledger = Ledger(ledger_path)
        self.action: dict | None = None
        self.case: dict | None = None
        self.appeals: list[dict] = []

    def reset(self) -> None:
        with self.lock:
            self.action = None
            self.case = None
            self.appeals = []
            self.ledger.append("demo.reset", {})

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "action": deepcopy(self.action),
                "appeal": deepcopy(self.appeals[-1]) if self.appeals else None,
                "appeals": deepcopy(self.appeals),
                "case": deepcopy(self.case),
                "ledger": deepcopy(self.ledger.records),
                "policy": deepcopy(DEMO_POLICY),
                "verification": self.ledger.verify(),
            }


state = DemoState(os.getenv("TUBEKEEL_LEDGER_PATH", DEFAULT_LEDGER_PATH))

RATE_LIMIT = int(os.getenv("TUBEKEEL_RATE_LIMIT", "30"))
RATE_WINDOW_SECONDS = 60.0
_rate_lock = Lock()
_rate_buckets: dict[str, Deque[float]] = defaultdict(deque)


def require_token(request: Request) -> None:
    expected = os.getenv("TUBEKEEL_DEMO_TOKEN")
    if not expected:
        return
    if request.headers.get("authorization") != f"Bearer {expected}":
        raise HTTPException(status_code=401, detail="A valid demo token is required.")


def rate_limit(request: Request) -> None:
    if RATE_LIMIT <= 0:
        return
    client = request.client.host if request.client else "unknown"
    current = monotonic()
    with _rate_lock:
        bucket = _rate_buckets[client]
        while bucket and current - bucket[0] > RATE_WINDOW_SECONDS:
            bucket.popleft()
        if len(bucket) >= RATE_LIMIT:
            raise HTTPException(status_code=429, detail="Too many requests. Try again shortly.")
        bucket.append(current)


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
    return state.snapshot()


@app.post("/v1/demo/reset", dependencies=[Depends(require_token), Depends(rate_limit)])
def demo_reset() -> dict:
    state.reset()
    return state.snapshot()


@app.post(
    "/v1/actions",
    status_code=201,
    dependencies=[Depends(require_token), Depends(rate_limit)],
)
def create_action(body: ActionCreate) -> dict:
    with state.lock:
        if state.action and state.action["status"] == "active":
            raise HTTPException(
                status_code=409,
                detail="An active demo action already exists. Reset the demo first.",
            )

        created_at = now()
        case_id = new_id("case")
        action_id = new_id("act")
        statement = {
            "summary": body.reason,
            "policy_version": DEMO_POLICY["policy_version"],
            "evidence_refs": body.evidence,
        }
        action = {
            "id": action_id,
            "case_id": case_id,
            "action_type": "label",
            "scope": "video",
            "policy_clause": body.policy_clause,
            "statement_of_reasons": statement,
            "human_reviewer": DEFAULT_REVIEWER,
            "status": "active",
            "effective_at": created_at,
            "appeal_deadline": (
                datetime.now(timezone.utc)
                + timedelta(days=DEMO_POLICY["appeal_window_days"])
            ).isoformat(),
            "audit_record_hash": "",
            "created_at": created_at,
        }
        case = {
            "id": case_id,
            "subject": {"type": "video", "id": body.video_id},
            "status": "decided",
            "severity": "medium",
            "policy_refs": [body.policy_clause],
            "signals": [
                {
                    "id": new_id("sig"),
                    "source": "automated",
                    "signal_type": "spam",
                    "confidence": 0.91,
                    "model_version": "spam-v0.1",
                    "evidence_ref": "model:spam-v0.1",
                    "created_at": created_at,
                }
            ],
            "evidence": [{"kind": "other", "ref": item} for item in body.evidence],
            "assignee": DEFAULT_REVIEWER,
            "reviewer": DEFAULT_REVIEWER,
            "decision_action_id": action_id,
            "created_at": created_at,
            "updated_at": created_at,
        }
        state.ledger.append("moderation_case.opened", case)
        record = state.ledger.append("enforcement_action.created", action)
        action["audit_record_hash"] = record["hash"]
        state.action = action
        state.case = case
        state.appeals = []
        return deepcopy(action)


@app.post(
    "/v1/actions/{action_id}/appeal",
    status_code=201,
    dependencies=[Depends(require_token), Depends(rate_limit)],
)
def file_appeal(action_id: str, body: AppealCreate) -> dict:
    with state.lock:
        if not state.action or state.action["id"] != action_id:
            raise HTTPException(status_code=404, detail="Action not found")
        if state.action["status"] != "active":
            raise HTTPException(
                status_code=409,
                detail="Only an active action can be appealed.",
            )
        deadline = datetime.fromisoformat(state.action["appeal_deadline"])
        if datetime.now(timezone.utc) > deadline:
            raise HTTPException(status_code=409, detail="The appeal window has closed.")
        if state.appeals and state.appeals[-1]["status"] == "pending":
            raise HTTPException(status_code=409, detail="An appeal is already pending.")
        if (
            state.appeals
            and state.appeals[-1]["status"] == "decided"
            and body.grounds not in {"new_evidence", "procedural_error"}
        ):
            raise HTTPException(
                status_code=409,
                detail="A second appeal requires new evidence or a procedural error.",
            )

        appeal = {
            "id": new_id("apl"),
            "action_id": action_id,
            "appellant": "creator:demo",
            "grounds": body.grounds,
            "statement": body.statement,
            "submitted_at": now(),
            "status": "pending",
            "reviewer": None,
            "decision": None,
            "reasoning": None,
            "decided_at": None,
        }
        state.appeals.append(appeal)
        state.ledger.append("appeal.filed", appeal)
        return deepcopy(appeal)


@app.post(
    "/v1/appeals/{appeal_id}/decide",
    dependencies=[Depends(require_token), Depends(rate_limit)],
)
def decide_appeal(appeal_id: str, body: AppealDecision) -> dict:
    with state.lock:
        appeal = next(
            (item for item in state.appeals if item["id"] == appeal_id), None
        )
        if not appeal:
            raise HTTPException(status_code=404, detail="Appeal not found")
        if appeal["status"] != "pending":
            raise HTTPException(status_code=409, detail="Appeal already decided")
        if not state.action:
            raise HTTPException(status_code=409, detail="No action to update")

        decided_at = now()
        appeal.update(
            {
                "status": "decided",
                "decision": body.decision,
                "reasoning": body.reasoning,
                "reviewer": APPEALS_REVIEWER,
                "decided_at": decided_at,
            }
        )
        state.ledger.append("appeal.decided", appeal)

        if body.decision == "reverse":
            state.action["status"] = "reversed"
            record = state.ledger.append(
                "enforcement_action.reversed",
                {
                    "action_id": state.action["id"],
                    "appeal_id": appeal_id,
                    "decision": body.decision,
                    "reasoning": body.reasoning,
                    "reviewer": APPEALS_REVIEWER,
                    "decided_at": decided_at,
                },
            )
            state.action["reversal_record_hash"] = record["hash"]
        else:
            state.ledger.append(
                "enforcement_action.upheld",
                {
                    "action_id": state.action["id"],
                    "appeal_id": appeal_id,
                    "decision": body.decision,
                    "reasoning": body.reasoning,
                    "reviewer": APPEALS_REVIEWER,
                    "decided_at": decided_at,
                },
            )

        return state.snapshot()


@app.get("/v1/ledger")
def ledger() -> dict:
    with state.lock:
        return {
            "ledger": deepcopy(state.ledger.records),
            "verification": state.ledger.verify(),
        }


@app.get("/v1/ledger/verify")
def verify_ledger() -> dict:
    return state.ledger.verify()


@app.get("/v1/ledger/export")
def export_ledger(format: Literal["jsonl", "csv"] = "jsonl") -> Response:
    if format == "csv":
        content = state.ledger.export_csv()
        media_type = "text/csv"
        filename = "ledger.csv"
    else:
        content = state.ledger.export_jsonl()
        media_type = "application/x-ndjson"
        filename = "ledger.jsonl"
    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
