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
from typing import Deque, Literal, Optional
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

from .auth import (
    Principal,
    actor_payload,
    current_principal,
    require_roles,
)
from .storage import SqliteStore


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

DEFAULT_LEDGER_PATH = "data/ledger.jsonl"
DEFAULT_STATE_DB = "data/state.sqlite3"


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
    def __init__(self, ledger_path: str | None = None, state_db: str | None = None) -> None:
        self.lock = RLock()
        self.ledger = Ledger(ledger_path)
        self.store = SqliteStore(state_db or DEFAULT_STATE_DB)

    def reset(self, actor: Principal | None = None) -> None:
        actor = actor or Principal("system", "operator")
        with self.lock:
            self.store.reset()
            self.ledger.append("demo.reset", {"actor": actor_payload(actor)})

    def snapshot(self) -> dict:
        with self.lock:
            action = self.store.get_action()
            case = (
                self.store.get_case(action["case_id"]) if action else self.store.get_case()
            )
            appeals = (
                self.store.list_appeals(action_id=action["id"]) if action else []
            )
            return {
                "action": deepcopy(action),
                "appeal": deepcopy(appeals[-1]) if appeals else None,
                "appeals": deepcopy(appeals),
                "case": deepcopy(case),
                "ledger": deepcopy(self.ledger.records),
                "policy": deepcopy(DEMO_POLICY),
                "verification": self.ledger.verify(),
            }

    def create_action(self, body: ActionCreate, actor: Principal) -> dict:
        with self.lock:
            current = self.store.get_action()
            if current and current["status"] == "active":
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
                "human_reviewer": actor.id,
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
                "assignee": actor.id,
                "reviewer": actor.id,
                "decision_action_id": action_id,
                "created_at": created_at,
                "updated_at": created_at,
            }
            self.store.save_case(case)
            self.store.save_action(action)
            self.ledger.append(
                "moderation_case.opened",
                {"case": case, "actor": actor_payload(actor)},
            )
            record = self.ledger.append(
                "enforcement_action.created",
                {"action": action, "actor": actor_payload(actor)},
            )
            action["audit_record_hash"] = record["hash"]
            self.store.save_action(action)
            return deepcopy(action)

    def file_appeal(self, action_id: str, body: AppealCreate, actor: Principal) -> dict:
        with self.lock:
            action = self.store.get_action(action_id)
            if not action:
                raise HTTPException(status_code=404, detail="Action not found")
            if action["status"] != "active":
                raise HTTPException(
                    status_code=409,
                    detail="Only an active action can be appealed.",
                )
            deadline = datetime.fromisoformat(action["appeal_deadline"])
            if datetime.now(timezone.utc) > deadline:
                raise HTTPException(status_code=409, detail="The appeal window has closed.")

            appeals = self.store.list_appeals(action_id=action_id)
            if appeals and appeals[-1]["status"] == "pending":
                raise HTTPException(status_code=409, detail="An appeal is already pending.")
            if (
                appeals
                and appeals[-1]["status"] == "decided"
                and body.grounds not in {"new_evidence", "procedural_error"}
            ):
                raise HTTPException(
                    status_code=409,
                    detail="A second appeal requires new evidence or a procedural error.",
                )

            appeal = {
                "id": new_id("apl"),
                "action_id": action_id,
                "appellant": actor.id,
                "grounds": body.grounds,
                "statement": body.statement,
                "submitted_at": now(),
                "status": "pending",
                "reviewer": None,
                "decision": None,
                "reasoning": None,
                "decided_at": None,
            }
            self.store.save_appeal(appeal)
            self.ledger.append(
                "appeal.filed",
                {"appeal": appeal, "actor": actor_payload(actor)},
            )
            return deepcopy(appeal)

    def decide_appeal(self, appeal_id: str, body: AppealDecision, actor: Principal) -> dict:
        with self.lock:
            appeal = self.store.get_appeal(appeal_id)
            if not appeal:
                raise HTTPException(status_code=404, detail="Appeal not found")
            if appeal["status"] != "pending":
                raise HTTPException(status_code=409, detail="Appeal already decided")
            action = self.store.get_action(appeal["action_id"])
            if not action:
                raise HTTPException(status_code=409, detail="No action to update")
            if actor.id == appeal["appellant"]:
                raise HTTPException(
                    status_code=403,
                    detail="An appellant cannot decide their own appeal.",
                )
            if actor.id == action["human_reviewer"]:
                raise HTTPException(
                    status_code=403,
                    detail="The original decision-maker cannot decide the appeal.",
                )

            decided_at = now()
            appeal.update(
                {
                    "status": "decided",
                    "decision": body.decision,
                    "reasoning": body.reasoning,
                    "reviewer": actor.id,
                    "decided_at": decided_at,
                }
            )
            self.store.save_appeal(appeal)
            self.ledger.append(
                "appeal.decided",
                {"appeal": appeal, "actor": actor_payload(actor)},
            )

            if body.decision == "reverse":
                action["status"] = "reversed"
                record = self.ledger.append(
                    "enforcement_action.reversed",
                    {
                        "action_id": action["id"],
                        "appeal_id": appeal_id,
                        "decision": body.decision,
                        "reasoning": body.reasoning,
                        "reviewer": actor.id,
                        "decided_at": decided_at,
                        "actor": actor_payload(actor),
                    },
                )
                action["reversal_record_hash"] = record["hash"]
                self.store.save_action(action)
            else:
                self.ledger.append(
                    "enforcement_action.upheld",
                    {
                        "action_id": action["id"],
                        "appeal_id": appeal_id,
                        "decision": body.decision,
                        "reasoning": body.reasoning,
                        "reviewer": actor.id,
                        "decided_at": decided_at,
                        "actor": actor_payload(actor),
                    },
                )

            return self.snapshot()

    def list_appeals(self, status: str | None = None) -> list[dict]:
        with self.lock:
            return deepcopy(self.store.list_appeals(status=status))

    def get_appeal(self, appeal_id: str) -> dict | None:
        with self.lock:
            return deepcopy(self.store.get_appeal(appeal_id))

    def verify_ledger(self) -> dict:
        return self.ledger.verify()


state = DemoState(
    os.getenv("TUBEKEEL_LEDGER_PATH", DEFAULT_LEDGER_PATH),
    os.getenv("TUBEKEEL_STATE_DB", DEFAULT_STATE_DB),
)

RATE_LIMIT = int(os.getenv("TUBEKEEL_RATE_LIMIT", "30"))
RATE_WINDOW_SECONDS = 60.0
_rate_lock = Lock()
_rate_buckets: dict[str, Deque[float]] = defaultdict(deque)


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


@app.post("/v1/demo/reset", dependencies=[Depends(rate_limit)])
def demo_reset(
    principal: Principal = Depends(require_roles("moderator")),
) -> dict:
    state.reset(actor=principal)
    return state.snapshot()


@app.post(
    "/v1/actions",
    status_code=201,
    dependencies=[Depends(rate_limit)],
)
def create_action(
    body: ActionCreate,
    principal: Principal = Depends(require_roles("moderator")),
) -> dict:
    return state.create_action(body, principal)


@app.post(
    "/v1/actions/{action_id}/appeal",
    status_code=201,
    dependencies=[Depends(rate_limit)],
)
def file_appeal(
    action_id: str,
    body: AppealCreate,
    principal: Principal = Depends(require_roles("creator")),
) -> dict:
    return state.file_appeal(action_id, body, principal)


@app.post(
    "/v1/appeals/{appeal_id}/decide",
    dependencies=[Depends(rate_limit)],
)
def decide_appeal(
    appeal_id: str,
    body: AppealDecision,
    principal: Principal = Depends(require_roles("appeals")),
) -> dict:
    return state.decide_appeal(appeal_id, body, principal)


@app.get("/v1/whoami")
def whoami(principal: Principal = Depends(current_principal)) -> dict:
    return {"id": principal.id, "role": principal.role}


@app.get("/v1/appeals")
def list_appeals(
    status: Optional[Literal["pending", "decided"]] = None,
    principal: Principal = Depends(require_roles("appeals", "moderator")),
) -> dict:
    return {"appeals": state.list_appeals(status=status)}


@app.get("/v1/appeals/{appeal_id}")
def get_appeal(
    appeal_id: str,
    principal: Principal = Depends(require_roles("appeals", "moderator")),
) -> dict:
    appeal = state.get_appeal(appeal_id)
    if not appeal:
        raise HTTPException(status_code=404, detail="Appeal not found")
    return appeal


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
