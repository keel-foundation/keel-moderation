from __future__ import annotations

import csv
import hmac
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

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

from .auth import creator_channel, current_principal, require_roles
from .classifiers import classifier_from_env
from .notifications import notify_channel_async
from .principals import Principal, actor_payload
from .storage import create_store


def env_list(name: str, default: str) -> list[str]:
    raw = os.getenv(name, default)
    return [item.strip() for item in raw.split(",") if item.strip()]


app = FastAPI(
    title="KeelTube Moderation",
    version="0.1.0",
    description="Signals, cases, appeals, audit ledger, and content protection.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=env_list("KEELTUBE_CORS_ORIGINS", "http://localhost:3000"),
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
MODERATION_DATABASE_ENVS = (
    "KEELTUBE_MODERATION_DATABASE_URL",
    "KEELTUBE_DATABASE_URL",
)


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

    def append(self, record_type: str, payload: dict, channel_id: str | None = None) -> dict:
        with self.lock:
            seq = len(self.records) + 1
            previous = self.records[-1]["hash"] if self.records else "GENESIS"
            record_payload = deepcopy(payload)
            if channel_id:
                record_payload["channel_id"] = channel_id
            record = {
                "seq": seq,
                "type": record_type,
                "at": now(),
                "payload": record_payload,
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
                expected = "sha256:" + sha256(canonical_record(record).encode("utf-8")).hexdigest()
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

    def export_jsonl(self, records: list[dict] | None = None) -> str:
        with self.lock:
            selected = self.records if records is None else records
            return "".join(
                json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n" for record in selected
            )

    def export_csv(self, records: list[dict] | None = None) -> str:
        with self.lock:
            selected = self.records if records is None else records
            output = io.StringIO()
            writer = csv.DictWriter(
                output,
                fieldnames=["seq", "type", "at", "prev_hash", "hash", "payload"],
            )
            writer.writeheader()
            for record in selected:
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
    channel_id: str = Field(default="ch_demo", pattern=r"^ch_[a-zA-Z0-9_]+$", max_length=80)
    policy_clause: str = Field(default="spam.mass_production", min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=2000)
    evidence: list[str] = Field(min_length=1, max_length=50)


class CaseActionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action_type: Literal["label", "remove", "age_restrict", "demonetize"] = "label"
    policy_clause: str = Field(default="spam.mass_production", min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=2000)
    evidence: list[str] = Field(min_length=1, max_length=50)


class CaseDismiss(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=2000)


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


class SignalCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject_type: Literal["video", "channel"]
    subject_id: str = Field(min_length=1, max_length=80)
    channel_id: str = Field(pattern=r"^ch_[a-zA-Z0-9_]+$", max_length=80)
    reason: Literal[
        "spam",
        "scam",
        "piracy",
        "impersonation",
        "harassment",
        "csam",
        "copyright",
        "terrorism",
        "other",
    ] = "other"
    details: str = Field(min_length=1, max_length=4000)
    reporter_id: str = Field(default="", max_length=80)


class AppealDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["reverse", "uphold"]
    reasoning: str = Field(min_length=1, max_length=4000)


CaseStatus = Literal["open", "reviewing", "decided", "appealed", "closed"]
ActionStatus = Literal["active", "reversed", "expired"]
AppealStatus = Literal["pending", "decided"]


class DemoState:
    def __init__(self, ledger_path: str | None = None, state_db: str | None = None) -> None:
        self.lock = RLock()
        self.ledger = Ledger(ledger_path)
        location = state_db or state_location_from_env()
        self.store = create_store(location)

    def reset(self, actor: Principal | None = None) -> None:
        actor = actor or Principal("system", "operator")
        with self.lock:
            self.store.reset()
            self.ledger.append("demo.reset", {"actor": actor_payload(actor)})

    def snapshot(self) -> dict:
        with self.lock:
            action = self.store.get_action()
            case = self.store.get_case(action["case_id"]) if action else self.store.get_case()
            appeals = self.store.list_appeals(action_id=action["id"]) if action else []
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
            channel_id = body.channel_id
            current = self.store.get_active_action(channel_id)
            if current:
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
                    datetime.now(timezone.utc) + timedelta(days=DEMO_POLICY["appeal_window_days"])
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
            self.store.save_case(case, channel_id=channel_id)
            self.store.save_action(action, channel_id=channel_id)
            self.ledger.append(
                "moderation_case.opened",
                {"case": case, "actor": actor_payload(actor)},
                channel_id=channel_id,
            )
            record = self.ledger.append(
                "enforcement_action.created",
                {"action": action, "actor": actor_payload(actor)},
                channel_id=channel_id,
            )
            action["audit_record_hash"] = record["hash"]
            self.store.save_action(action, channel_id=channel_id)
            return deepcopy(action)

    def create_signal(self, body: SignalCreate) -> dict:
        with self.lock:
            created_at = now()
            case_id = new_id("case")
            signal_id = new_id("sig")
            classification = classifier_from_env().classify(body.details)
            signal_type = body.reason
            if body.reason == "other" and classification.signal_type != "other":
                signal_type = classification.signal_type
            severity = {
                "csam": "critical",
                "terrorism": "critical",
                "copyright": "medium",
                "harassment": "medium",
                "impersonation": "medium",
                "piracy": "medium",
                "spam": "low",
                "scam": "medium",
                "other": "low",
            }[signal_type]
            signal = {
                "id": signal_id,
                "source": "user_report",
                "signal_type": signal_type,
                "confidence": classification.confidence,
                "model_version": classification.model_version,
                "evidence_ref": f"report:{signal_id}",
                "created_at": created_at,
            }
            case = {
                "id": case_id,
                "subject": {"type": body.subject_type, "id": body.subject_id},
                "status": "open",
                "severity": severity,
                "policy_refs": [signal_type],
                "signals": [signal],
                "evidence": [
                    {
                        "kind": "user_report",
                        "ref": signal_id,
                        "description": body.details,
                    }
                ],
                "created_at": created_at,
                "updated_at": created_at,
            }
            self.store.save_case(case, channel_id=body.channel_id)
            self.ledger.append(
                "signal.received",
                {
                    "case_id": case_id,
                    "signal": signal,
                    "reporter": {"id": body.reporter_id or "anonymous"},
                },
                channel_id=body.channel_id,
            )
            return {"case": deepcopy(case), "signal": deepcopy(signal)}

    def file_appeal(
        self,
        action_id: str,
        body: AppealCreate,
        actor: Principal,
        requested_channel_id: str | None = None,
    ) -> dict:
        with self.lock:
            action = self.store.get_action(action_id)
            if not action:
                raise HTTPException(status_code=404, detail="Action not found")
            action_channel_id = self.store.get_action_channel_id(action_id) or ""
            if actor.channel_ids or requested_channel_id is not None:
                scoped_channel_id = creator_channel(actor, requested_channel_id)
                if action_channel_id != scoped_channel_id:
                    raise HTTPException(status_code=404, detail="Action not found")
            else:
                scoped_channel_id = action_channel_id
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

            channel_id = scoped_channel_id
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
            self.store.save_appeal(appeal, channel_id=channel_id)
            self.ledger.append(
                "appeal.filed",
                {"appeal": appeal, "actor": actor_payload(actor)},
                channel_id=channel_id,
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
            channel_id = (
                self.store.get_appeal_channel_id(appeal_id)
                or self.store.get_action_channel_id(action["id"])
                or ""
            )
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
            self.store.save_appeal(appeal, channel_id=channel_id)
            self.ledger.append(
                "appeal.decided",
                {"appeal": appeal, "actor": actor_payload(actor)},
                channel_id=channel_id,
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
                    channel_id=channel_id,
                )
                action["reversal_record_hash"] = record["hash"]
                self.store.save_action(action, channel_id=channel_id)
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
                    channel_id=channel_id,
                )

            return self.snapshot()

    def create_case_action(
        self,
        case_id: str,
        body: CaseActionCreate,
        actor: Principal,
    ) -> dict:
        with self.lock:
            case = self.store.get_case(case_id)
            if not case:
                raise HTTPException(status_code=404, detail="Case not found")
            channel_id = self.store.get_case_channel_id(case_id) or ""
            if not channel_id:
                raise HTTPException(status_code=409, detail="Case has no channel scope.")
            if case["status"] in {"decided", "closed"}:
                raise HTTPException(
                    status_code=409,
                    detail="The case already has a final decision.",
                )
            created_at = now()
            action_id = new_id("act")
            subject_type = case.get("subject", {}).get("type", "video")
            action = {
                "id": action_id,
                "case_id": case_id,
                "action_type": body.action_type,
                "scope": subject_type,
                "policy_clause": body.policy_clause,
                "statement_of_reasons": {
                    "summary": body.reason,
                    "policy_version": DEMO_POLICY["policy_version"],
                    "evidence_refs": body.evidence,
                },
                "human_reviewer": actor.id,
                "status": "active",
                "effective_at": created_at,
                "appeal_deadline": (
                    datetime.now(timezone.utc) + timedelta(days=DEMO_POLICY["appeal_window_days"])
                ).isoformat(),
                "audit_record_hash": "",
                "created_at": created_at,
            }
            case["status"] = "decided"
            case["assignee"] = actor.id
            case["reviewer"] = actor.id
            case["decision_action_id"] = action_id
            case["policy_refs"] = list(
                dict.fromkeys([*case.get("policy_refs", []), body.policy_clause])
            )
            case["evidence"] = [
                *case.get("evidence", []),
                *[
                    {"kind": "other", "ref": item, "description": "Reviewer evidence"}
                    for item in body.evidence
                ],
            ]
            case["updated_at"] = created_at
            self.store.save_case(case, channel_id=channel_id)
            self.store.save_action(action, channel_id=channel_id)
            self.ledger.append(
                "moderation_case.decided",
                {"case": case, "action": action, "actor": actor_payload(actor)},
                channel_id=channel_id,
            )
            record = self.ledger.append(
                "enforcement_action.created",
                {"action": action, "actor": actor_payload(actor)},
                channel_id=channel_id,
            )
            action["audit_record_hash"] = record["hash"]
            self.store.save_action(action, channel_id=channel_id)
            return deepcopy(action)

    def dismiss_case(
        self,
        case_id: str,
        body: CaseDismiss,
        actor: Principal,
    ) -> dict:
        with self.lock:
            case = self.store.get_case(case_id)
            if not case:
                raise HTTPException(status_code=404, detail="Case not found")
            channel_id = self.store.get_case_channel_id(case_id) or ""
            if not channel_id:
                raise HTTPException(status_code=409, detail="Case has no channel scope.")
            if case["status"] in {"decided", "closed"}:
                raise HTTPException(
                    status_code=409,
                    detail="The case already has a final decision.",
                )
            dismissed_at = now()
            case["status"] = "closed"
            case["assignee"] = actor.id
            case["reviewer"] = actor.id
            case["updated_at"] = dismissed_at
            self.store.save_case(case, channel_id=channel_id)
            self.ledger.append(
                "moderation_case.dismissed",
                {
                    "case": case,
                    "reason": body.reason,
                    "actor": actor_payload(actor),
                },
                channel_id=channel_id,
            )
            return deepcopy(case)

    def staff_cases(
        self,
        status: str | None = None,
        channel_id: str | None = None,
        video_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        with self.lock:
            cases, total = self.store.list_cases(
                status=status,
                channel_id=channel_id,
                video_id=video_id,
                limit=limit,
                offset=offset,
            )
            items = []
            for case in cases:
                scoped_channel = case.pop("channel_id", "")
                items.append({"channel_id": scoped_channel, "case": deepcopy(case)})
            return {"cases": items, "total": total, "limit": limit, "offset": offset}

    def staff_case(self, case_id: str) -> dict | None:
        with self.lock:
            case = self.store.get_case(case_id)
            if not case:
                return None
            channel_id = self.store.get_case_channel_id(case_id) or ""
            return {"channel_id": channel_id, "case": deepcopy(case)}

    def staff_actions(
        self,
        status: str | None = None,
        channel_id: str | None = None,
        video_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        with self.lock:
            actions, total = self.store.list_actions(
                status=status,
                channel_id=channel_id,
                video_id=video_id,
                limit=limit,
                offset=offset,
            )
            items = []
            for action in actions:
                scoped_channel = action.pop("channel_id", "")
                items.append({"channel_id": scoped_channel, "action": deepcopy(action)})
            return {"actions": items, "total": total, "limit": limit, "offset": offset}

    def staff_action(self, action_id: str) -> dict | None:
        with self.lock:
            action = self.store.get_action(action_id)
            if not action:
                return None
            channel_id = self.store.get_action_channel_id(action_id) or ""
            return {"channel_id": channel_id, "action": deepcopy(action)}

    def staff_summary(self) -> dict:
        with self.lock:
            summary = self.store.staff_summary()
            summary["ledger_records"] = len(self.ledger.records)
            summary["ledger_valid"] = self.ledger.verify()["valid"]
            return summary

    def list_appeals(self, status: str | None = None) -> list[dict]:
        with self.lock:
            return deepcopy(self.store.list_appeals(status=status))

    def get_appeal(self, appeal_id: str) -> dict | None:
        with self.lock:
            return deepcopy(self.store.get_appeal(appeal_id))

    def list_creator_cases(
        self,
        channel_id: str,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
        video_id: str | None = None,
    ) -> dict:
        with self.lock:
            cases, total = self.store.list_creator_cases(
                channel_id,
                status=status,
                limit=limit,
                offset=offset,
                video_id=video_id,
            )
            return {
                "cases": deepcopy(cases),
                "total": total,
                "limit": limit,
                "offset": offset,
                "channel_id": channel_id,
            }

    def get_creator_case(self, case_id: str, channel_id: str) -> dict | None:
        with self.lock:
            case = self.store.get_creator_case(case_id, channel_id)
            return deepcopy(case) if case else None

    def list_creator_actions(
        self,
        channel_id: str,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
        video_id: str | None = None,
    ) -> dict:
        with self.lock:
            actions, total = self.store.list_creator_actions(
                channel_id,
                status=status,
                limit=limit,
                offset=offset,
                video_id=video_id,
            )
            return {
                "actions": deepcopy(actions),
                "total": total,
                "limit": limit,
                "offset": offset,
                "channel_id": channel_id,
            }

    def get_creator_action(self, action_id: str, channel_id: str) -> dict | None:
        with self.lock:
            action = self.store.get_creator_action(action_id, channel_id)
            return deepcopy(action) if action else None

    def list_creator_appeals(
        self,
        channel_id: str,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        with self.lock:
            appeals, total = self.store.list_creator_appeals(
                channel_id, status=status, limit=limit, offset=offset
            )
            return {
                "appeals": deepcopy(appeals),
                "total": total,
                "limit": limit,
                "offset": offset,
                "channel_id": channel_id,
            }

    def get_creator_appeal(self, appeal_id: str, channel_id: str) -> dict | None:
        with self.lock:
            appeal = self.store.get_creator_appeal(appeal_id, channel_id)
            return deepcopy(appeal) if appeal else None

    def creator_ledger(self, channel_id: str, limit: int = 50, offset: int = 0) -> dict:
        with self.lock:
            records = [
                deepcopy(record)
                for record in self.ledger.records
                if record.get("payload", {}).get("channel_id") == channel_id
            ]
            total = len(records)
            return {
                "ledger": records[offset : offset + limit],
                "verification": self.ledger.verify(),
                "total": total,
                "limit": limit,
                "offset": offset,
                "channel_id": channel_id,
            }

    def creator_ledger_export(self, channel_id: str, format: str) -> str:
        with self.lock:
            records = [
                deepcopy(record)
                for record in self.ledger.records
                if record.get("payload", {}).get("channel_id") == channel_id
            ]
            if format == "csv":
                return self.ledger.export_csv(records)
            return self.ledger.export_jsonl(records)

    def creator_summary(self, channel_id: str) -> dict:
        with self.lock:
            summary = self.store.creator_summary(channel_id)
            summary["ledger_records"] = sum(
                1
                for record in self.ledger.records
                if record.get("payload", {}).get("channel_id") == channel_id
            )
            return summary

    def public_video_record(self, video_id: str) -> dict:
        with self.lock:
            return self.store.public_video_record(video_id)

    def verify_ledger(self) -> dict:
        return self.ledger.verify()


def state_location_from_env() -> str:
    for name in MODERATION_DATABASE_ENVS:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return os.getenv("KEELTUBE_STATE_DB", DEFAULT_STATE_DB)


state = DemoState(
    os.getenv("KEELTUBE_LEDGER_PATH", DEFAULT_LEDGER_PATH),
    state_location_from_env(),
)

RATE_LIMIT = int(os.getenv("KEELTUBE_RATE_LIMIT", "30"))
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


def require_signal_token(request: Request) -> None:
    expected = os.getenv("KEELTUBE_SIGNAL_INGEST_TOKEN", "").strip()
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="Signal ingestion is not configured.",
        )
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(
        token.strip().encode("utf-8"), expected.encode("utf-8")
    ):
        raise HTTPException(status_code=401, detail="A valid ingest token is required.")


class Health(BaseModel):
    status: str
    service: str


class PolicySummary(BaseModel):
    policies: list[dict]


@app.get("/health", response_model=Health)
def health() -> Health:
    return Health(status="ok", service="moderation")


@app.get("/v1/public/videos/{video_id}/record")
def public_video_record(video_id: str) -> dict:
    return state.public_video_record(video_id)


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
    action = state.create_action(body, principal)
    notify_channel_async(
        body.channel_id,
        kind="moderation.action",
        title="An enforcement action was recorded",
        body=f"A human reviewer recorded {action['action_type']} under {action['policy_clause']}.",
        href=f"/studio/moderation/cases/{action['case_id']}",
        dedupe_key=f"moderation:action:{action['id']}",
        payload={"action_id": action["id"], "case_id": action["case_id"]},
    )
    return action


@app.post(
    "/v1/signals",
    status_code=201,
    dependencies=[Depends(rate_limit), Depends(require_signal_token)],
)
def create_signal(body: SignalCreate) -> dict:
    result = state.create_signal(body)
    notify_channel_async(
        body.channel_id,
        kind="moderation.signal",
        title="A new report opened a moderation case",
        body=f"A {body.reason} report was received and grouped for review.",
        href=f"/studio/moderation/cases/{result['case']['id']}",
        dedupe_key=f"moderation:signal:{result['signal']['id']}",
        payload={"case_id": result["case"]["id"], "signal_id": result["signal"]["id"]},
    )
    return result


@app.post(
    "/v1/actions/{action_id}/appeal",
    status_code=201,
    dependencies=[Depends(rate_limit)],
)
def file_appeal(
    action_id: str,
    body: AppealCreate,
    channel_id: Optional[str] = Query(None, pattern=r"^ch_[a-zA-Z0-9_]+$", max_length=80),
    principal: Principal = Depends(require_roles("creator")),
) -> dict:
    appeal = state.file_appeal(action_id, body, principal, channel_id)
    notify_channel_async(
        state.store.get_action_channel_id(action_id) or "",
        kind="moderation.appeal_filed",
        title="Your appeal was received",
        body="An independent reviewer now has the full record.",
        href=f"/studio/moderation/appeals/{appeal['id']}",
        dedupe_key=f"moderation:appeal:{appeal['id']}",
        payload={"appeal_id": appeal["id"], "action_id": action_id},
    )
    return appeal


@app.post(
    "/v1/appeals/{appeal_id}/decide",
    dependencies=[Depends(rate_limit)],
)
def decide_appeal(
    appeal_id: str,
    body: AppealDecision,
    principal: Principal = Depends(require_roles("appeals")),
) -> dict:
    result = state.decide_appeal(appeal_id, body, principal)
    appeal = result.get("appeal") or {}
    notify_channel_async(
        state.store.get_appeal_channel_id(appeal_id) or "",
        kind="moderation.appeal_decided",
        title="Your appeal was decided",
        body=f"The independent reviewer chose to {body.decision} the action.",
        href=f"/studio/moderation/appeals/{appeal_id}",
        dedupe_key=f"moderation:appeal-decided:{appeal_id}",
        payload={"appeal_id": appeal_id, "action_id": appeal.get("action_id", "")},
    )
    return result


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


@app.get("/v1/summary")
def staff_summary(
    principal: Principal = Depends(require_roles("moderator", "appeals", "operator")),
) -> dict:
    return state.staff_summary()


@app.get("/v1/cases")
def staff_cases(
    status: Optional[CaseStatus] = None,
    channel_id: Optional[str] = Query(None, pattern=r"^ch_[a-zA-Z0-9_]+$", max_length=80),
    video_id: Optional[str] = Query(None, pattern=r"^vid_[a-zA-Z0-9_]+$", max_length=80),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    principal: Principal = Depends(require_roles("moderator", "appeals", "operator")),
) -> dict:
    return state.staff_cases(
        status=status,
        channel_id=channel_id,
        video_id=video_id,
        limit=limit,
        offset=offset,
    )


@app.get("/v1/cases/{case_id}")
def staff_case(
    case_id: str,
    principal: Principal = Depends(require_roles("moderator", "appeals", "operator")),
) -> dict:
    case = state.staff_case(case_id)
    if not case:
        raise HTTPException(status_code=404, detail="Case not found")
    return case


@app.post(
    "/v1/cases/{case_id}/actions",
    status_code=201,
    dependencies=[Depends(rate_limit)],
)
def staff_case_action(
    case_id: str,
    body: CaseActionCreate,
    principal: Principal = Depends(require_roles("moderator")),
) -> dict:
    action = state.create_case_action(case_id, body, principal)
    channel_id = state.store.get_case_channel_id(case_id) or ""
    notify_channel_async(
        channel_id,
        kind="moderation.action",
        title="An enforcement action was recorded",
        body=f"A human reviewer recorded {action['action_type']} under {action['policy_clause']}.",
        href=f"/studio/moderation/cases/{case_id}",
        dedupe_key=f"moderation:action:{action['id']}",
        payload={"action_id": action["id"], "case_id": case_id},
    )
    return action


@app.post(
    "/v1/cases/{case_id}/dismiss",
    dependencies=[Depends(rate_limit)],
)
def staff_case_dismiss(
    case_id: str,
    body: CaseDismiss,
    principal: Principal = Depends(require_roles("moderator")),
) -> dict:
    case = state.dismiss_case(case_id, body, principal)
    channel_id = state.store.get_case_channel_id(case_id) or ""
    notify_channel_async(
        channel_id,
        kind="moderation.case_dismissed",
        title="A moderation case was closed",
        body="A human reviewer found no enforcement action was warranted.",
        href=f"/studio/moderation/cases/{case_id}",
        dedupe_key=f"moderation:case-dismissed:{case_id}",
        payload={"case_id": case_id},
    )
    return case


@app.get("/v1/actions")
def staff_actions(
    status: Optional[ActionStatus] = None,
    channel_id: Optional[str] = Query(None, pattern=r"^ch_[a-zA-Z0-9_]+$", max_length=80),
    video_id: Optional[str] = Query(None, pattern=r"^vid_[a-zA-Z0-9_]+$", max_length=80),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    principal: Principal = Depends(require_roles("moderator", "appeals", "operator")),
) -> dict:
    return state.staff_actions(
        status=status,
        channel_id=channel_id,
        video_id=video_id,
        limit=limit,
        offset=offset,
    )


@app.get("/v1/actions/{action_id}")
def staff_action(
    action_id: str,
    principal: Principal = Depends(require_roles("moderator", "appeals", "operator")),
) -> dict:
    action = state.staff_action(action_id)
    if not action:
        raise HTTPException(status_code=404, detail="Action not found")
    return action


@app.get("/v1/creator/summary")
def creator_summary(
    channel_id: Optional[str] = Query(None, pattern=r"^ch_[a-zA-Z0-9_]+$", max_length=80),
    principal: Principal = Depends(require_roles("creator")),
) -> dict:
    return state.creator_summary(creator_channel(principal, channel_id))


@app.get("/v1/creator/cases")
def creator_cases(
    status: Optional[CaseStatus] = None,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    video_id: Optional[str] = Query(None, pattern=r"^vid_[a-zA-Z0-9_]+$", max_length=80),
    channel_id: Optional[str] = Query(None, pattern=r"^ch_[a-zA-Z0-9_]+$", max_length=80),
    principal: Principal = Depends(require_roles("creator")),
) -> dict:
    return state.list_creator_cases(
        creator_channel(principal, channel_id),
        status=status,
        limit=limit,
        offset=offset,
        video_id=video_id,
    )


@app.get("/v1/creator/cases/{case_id}")
def creator_case(
    case_id: str,
    channel_id: Optional[str] = Query(None, pattern=r"^ch_[a-zA-Z0-9_]+$", max_length=80),
    principal: Principal = Depends(require_roles("creator")),
) -> dict:
    case = state.get_creator_case(case_id, creator_channel(principal, channel_id))
    if not case:
        raise HTTPException(status_code=404, detail="Case not found")
    return case


@app.get("/v1/creator/actions")
def creator_actions(
    status: Optional[ActionStatus] = None,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    video_id: Optional[str] = Query(None, pattern=r"^vid_[a-zA-Z0-9_]+$", max_length=80),
    channel_id: Optional[str] = Query(None, pattern=r"^ch_[a-zA-Z0-9_]+$", max_length=80),
    principal: Principal = Depends(require_roles("creator")),
) -> dict:
    return state.list_creator_actions(
        creator_channel(principal, channel_id),
        status=status,
        limit=limit,
        offset=offset,
        video_id=video_id,
    )


@app.get("/v1/creator/actions/{action_id}")
def creator_action(
    action_id: str,
    channel_id: Optional[str] = Query(None, pattern=r"^ch_[a-zA-Z0-9_]+$", max_length=80),
    principal: Principal = Depends(require_roles("creator")),
) -> dict:
    action = state.get_creator_action(action_id, creator_channel(principal, channel_id))
    if not action:
        raise HTTPException(status_code=404, detail="Action not found")
    return action


@app.get("/v1/creator/appeals")
def creator_appeals(
    status: Optional[AppealStatus] = None,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    channel_id: Optional[str] = Query(None, pattern=r"^ch_[a-zA-Z0-9_]+$", max_length=80),
    principal: Principal = Depends(require_roles("creator")),
) -> dict:
    return state.list_creator_appeals(
        creator_channel(principal, channel_id), status=status, limit=limit, offset=offset
    )


@app.get("/v1/creator/appeals/{appeal_id}")
def creator_appeal(
    appeal_id: str,
    channel_id: Optional[str] = Query(None, pattern=r"^ch_[a-zA-Z0-9_]+$", max_length=80),
    principal: Principal = Depends(require_roles("creator")),
) -> dict:
    appeal = state.get_creator_appeal(appeal_id, creator_channel(principal, channel_id))
    if not appeal:
        raise HTTPException(status_code=404, detail="Appeal not found")
    return appeal


@app.get("/v1/creator/ledger")
def creator_ledger(
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    channel_id: Optional[str] = Query(None, pattern=r"^ch_[a-zA-Z0-9_]+$", max_length=80),
    principal: Principal = Depends(require_roles("creator")),
) -> dict:
    return state.creator_ledger(creator_channel(principal, channel_id), limit=limit, offset=offset)


@app.get("/v1/creator/ledger/verify")
def creator_ledger_verify(
    channel_id: Optional[str] = Query(None, pattern=r"^ch_[a-zA-Z0-9_]+$", max_length=80),
    principal: Principal = Depends(require_roles("creator")),
) -> dict:
    creator_channel(principal, channel_id)
    return state.verify_ledger()


@app.get("/v1/creator/ledger/export")
def creator_ledger_export(
    format: Literal["jsonl", "csv"] = "jsonl",
    channel_id: Optional[str] = Query(None, pattern=r"^ch_[a-zA-Z0-9_]+$", max_length=80),
    principal: Principal = Depends(require_roles("creator")),
) -> Response:
    content = state.creator_ledger_export(creator_channel(principal, channel_id), format)
    if format == "csv":
        media_type = "text/csv"
        filename = "creator-ledger.csv"
    else:
        media_type = "application/x-ndjson"
        filename = "creator-ledger.jsonl"
    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


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
