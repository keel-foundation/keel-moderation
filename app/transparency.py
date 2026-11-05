from __future__ import annotations

import csv
import io
import json
from collections import Counter
from datetime import datetime, timezone
from statistics import mean
from typing import Iterable, Optional

SCHEMA_VERSION = "transparency-0.1"
SIGNAL_CATEGORIES = (
    "piracy",
    "copyright",
    "spam",
    "scam",
    "impersonation",
    "harassment",
    "csam",
    "terrorism",
    "other",
)


def _parse_time(value: object) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_bound(value: Optional[str], *, end: bool) -> Optional[datetime]:
    if not value:
        return None
    parsed = _parse_time(value)
    if parsed is not None:
        return parsed
    try:
        day = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    if end:
        return day.replace(hour=23, minute=59, second=59, microsecond=999999)
    return day


def _within(value: object, start: Optional[datetime], end: Optional[datetime]) -> bool:
    if start is None and end is None:
        return True
    parsed = _parse_time(value)
    if parsed is None:
        return False
    if start is not None and parsed < start:
        return False
    if end is not None and parsed > end:
        return False
    return True


def _percentile(values: list[float], fraction: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _seconds_between(start: object, end: object) -> Optional[float]:
    first = _parse_time(start)
    second = _parse_time(end)
    if first is None or second is None:
        return None
    return max(0.0, (second - first).total_seconds())


def _rate(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 4) if denominator else 0.0


def _case_source(case: dict) -> str:
    sources = {
        str(signal.get("source", ""))
        for signal in case.get("signals") or []
        if isinstance(signal, dict)
    }
    if "automated" in sources:
        return "automation"
    if "user_report" in sources:
        return "user_report"
    return "human_review"


def _case_categories(case: dict) -> set[str]:
    categories: set[str] = set()
    for signal in case.get("signals") or []:
        if isinstance(signal, dict):
            signal_type = str(signal.get("signal_type", "")).strip().lower()
            if signal_type in SIGNAL_CATEGORIES:
                categories.add(signal_type)
    for policy in case.get("policy_refs") or []:
        prefix = str(policy).split(".", 1)[0].strip().lower()
        if prefix in SIGNAL_CATEGORIES:
            categories.add(prefix)
    return categories


def _count_by(items: Iterable[str]) -> list[dict]:
    counts = Counter(item for item in items if item)
    return [
        {"key": key, "total": total}
        for key, total in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    ]


def build_report(
    *,
    cases: list[dict],
    actions: list[dict],
    appeals: list[dict],
    ledger: list[dict],
    verification: dict,
    policy_version: str,
    generated_at: Optional[str] = None,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
) -> dict:
    cases = [case for case in cases if _within(case.get("created_at"), start, end)]
    actions = [
        action
        for action in actions
        if _within(action.get("effective_at") or action.get("created_at"), start, end)
    ]
    action_ids = {action.get("id") for action in actions}
    appeals = [
        appeal
        for appeal in appeals
        if appeal.get("action_id") in action_ids
        and _within(appeal.get("submitted_at"), start, end)
    ]

    case_by_id = {case.get("id"): case for case in cases}
    action_sources = [
        _case_source(case_by_id.get(action.get("case_id"), {})) for action in actions
    ]
    source_counts = Counter(action_sources)

    by_policy: dict[str, dict[str, int]] = {}
    for action in actions:
        policy = str(action.get("policy_clause") or "unspecified")
        row = by_policy.setdefault(policy, {"total": 0, "reversed": 0})
        row["total"] += 1
        if action.get("status") == "reversed":
            row["reversed"] += 1
    policy_rows = [
        {
            "policy_clause": policy,
            "total": values["total"],
            "reversed": values["reversed"],
            "reversal_rate": _rate(values["reversed"], values["total"]),
        }
        for policy, values in sorted(by_policy.items())
    ]

    by_type = _count_by(str(action.get("action_type") or "unknown") for action in actions)
    reversed_actions = sum(1 for action in actions if action.get("status") == "reversed")
    active_actions = sum(1 for action in actions if action.get("status") == "active")

    decided_appeals = [appeal for appeal in appeals if appeal.get("status") == "decided"]
    pending_appeals = [appeal for appeal in appeals if appeal.get("status") == "pending"]
    decision_seconds = [
        seconds
        for appeal in decided_appeals
        if (seconds := _seconds_between(appeal.get("submitted_at"), appeal.get("decided_at")))
        is not None
    ]
    decisions = Counter(
        str(appeal.get("decision")) for appeal in decided_appeals if appeal.get("decision")
    )
    reversed_appeals = decisions.get("reverse", 0)
    decision_rows = [
        {"decision": decision, "total": total}
        for decision, total in sorted(decisions.items(), key=lambda item: (-item[1], item[0]))
    ]

    category_rows: dict[str, dict[str, int]] = {
        category: {"cases": 0, "actions": 0, "reversed": 0} for category in SIGNAL_CATEGORIES
    }
    for case in cases:
        for category in _case_categories(case):
            category_rows[category]["cases"] += 1
    for action in actions:
        case = case_by_id.get(action.get("case_id"), {})
        for category in _case_categories(case):
            category_rows[category]["actions"] += 1
            if action.get("status") == "reversed":
                category_rows[category]["reversed"] += 1

    categories = {
        category: {
            **values,
            "reversal_rate": _rate(values["reversed"], values["actions"]),
        }
        for category, values in category_rows.items()
    }

    demonetized = [
        action for action in actions if action.get("action_type") == "demonetize"
    ]
    demonetize_reversed = sum(
        1 for action in demonetized if action.get("status") == "reversed"
    )

    period = {
        "from": start.isoformat() if start else None,
        "to": end.isoformat() if end else None,
    }

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat(),
        "period": period,
        "instance": {"name": "KeelTube", "policy_version": policy_version},
        "summary": {
            "cases_total": len(cases),
            "cases_open": sum(
                1
                for case in cases
                if case.get("status") in {"open", "reviewing", "appealed"}
            ),
            "actions_total": len(actions),
            "actions_active": active_actions,
            "actions_reversed": reversed_actions,
            "appeals_total": len(appeals),
            "appeals_pending": len(pending_appeals),
            "appeals_decided": len(decided_appeals),
            "automation_share": _rate(source_counts.get("automation", 0), len(actions)),
            "user_report_share": _rate(source_counts.get("user_report", 0), len(actions)),
            "human_review_share": _rate(source_counts.get("human_review", 0), len(actions)),
            "appeal_rate": _rate(len(appeals), len(actions)),
            "reversal_rate": _rate(reversed_actions, len(actions)),
        },
        "enforcement_actions": {
            "by_policy": policy_rows,
            "by_type": by_type,
            "by_source": [
                {"source": source, "total": total}
                for source, total in sorted(
                    source_counts.items(), key=lambda item: (-item[1], item[0])
                )
            ],
            "by_instance": [
                {"instance": "local", "total": len(actions), "reversed": reversed_actions}
            ],
        },
        "appeals": {
            "total": len(appeals),
            "pending": len(pending_appeals),
            "decided": len(decided_appeals),
            "decision_time_seconds": {
                "count": len(decision_seconds),
                "median": _percentile(decision_seconds, 0.5),
                "p95": _percentile(decision_seconds, 0.95),
                "mean": round(mean(decision_seconds), 3) if decision_seconds else None,
            },
            "decisions": decision_rows,
            "reversed_actions": reversed_appeals,
            "reversal_rate": _rate(reversed_appeals, len(decided_appeals)),
        },
        "categories": categories,
        "content_protection": {
            "copyright_takedowns": categories["copyright"]["actions"],
            "piracy_takedowns": categories["piracy"]["actions"],
            "spam_removals": categories["spam"]["actions"],
            "scam_removals": categories["scam"]["actions"],
            "impersonation_cases": categories["impersonation"]["cases"],
            "account_takeovers": 0,
            "counter_notices": 0,
            "repeat_infringer_actions": 0,
            "demonetization_volume": len(demonetized),
            "demonetization_reversals": demonetize_reversed,
            "demonetization_reversal_rate": _rate(demonetize_reversed, len(demonetized)),
        },
        "finance": {
            "payout_reliability": None,
            "holdbacks": None,
            "disputes": None,
            "note": (
                "Finance data is owned by keel-core and is not included in this "
                "moderation report."
            ),
        },
        "creator_trust_index": {
            "score": None,
            "components": {},
            "note": (
                "The index is not computed in this release; publishing a partial index "
                "would be misleading."
            ),
        },
        "policy": {"policy_version": policy_version},
        "ledger": {
            "records": len(ledger),
            "valid": bool(verification.get("valid")),
            "head": verification.get("head"),
        },
        "methodology": {
            "source": (
                "Derived from the workflow store (cases, actions, appeals) and the "
                "audit ledger."
            ),
            "definitions": {
                "automation_share": "Actions whose case had an automated signal over all actions.",
                "user_report_share": (
                    "Actions whose case had a user-report signal over all actions."
                ),
                "reversal_rate": "Reversed actions over all actions in the period.",
                "decision_time_seconds": "Decided appeal time minus submission time.",
            },
            "notes": [
                "Signals create cases; only human decisions create enforcement actions.",
                "Counts are grouped by policy clause and action type.",
            ],
        },
        "limitations": [
            "The bundled classifier is a small auditable baseline, not a production safety model.",
            "The ledger is hash-chained but not externally anchored yet.",
            "Legal orders, security incidents, Ombudsperson complaints, and finance data are not "
            "collected by this service.",
            "The Creator Trust Index is not computed; no substitute value is published.",
            "Counts describe this local instance only.",
        ],
        "corrections": [],
    }


def export_csv(report: dict) -> str:
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["section", "metric", "value"])
    writer.writerow(["report", "schema_version", report["schema_version"]])
    writer.writerow(["report", "generated_at", report["generated_at"]])
    for key, value in report["summary"].items():
        writer.writerow(["summary", key, value])
    for row in report["enforcement_actions"]["by_policy"]:
        writer.writerow(
            [
                "enforcement_actions.by_policy",
                row["policy_clause"],
                f"total={row['total']};reversed={row['reversed']};"
                f"reversal_rate={row['reversal_rate']}",
            ]
        )
    for key, value in report["appeals"]["decision_time_seconds"].items():
        writer.writerow(["appeals.decision_time_seconds", key, value])
    for key, value in report["content_protection"].items():
        writer.writerow(["content_protection", key, value])
    return output.getvalue()


def export_markdown(report: dict) -> str:
    summary = report["summary"]
    period = report["period"]
    lines = [
        "# KeelTube transparency report",
        "",
        f"- Generated: `{report['generated_at']}`",
        f"- Period: `{period['from'] or 'all time'}` to `{period['to'] or 'now'}`",
        f"- Policy version: `{report['instance']['policy_version']}`",
        f"- Ledger: {report['ledger']['records']} records, "
        f"{'valid' if report['ledger']['valid'] else 'INVALID'}",
        "",
        "## Summary",
        "",
        "| Metric | Value |",
        "|---|---|",
    ]
    for key, value in summary.items():
        lines.append(f"| {key.replace('_', ' ')} | {value} |")

    lines.extend(
        [
            "",
            "## Enforcement actions by policy",
            "",
            "| Policy | Total | Reversed | Rate |",
            "|---|---:|---:|---:|",
        ]
    )
    for row in report["enforcement_actions"]["by_policy"]:
        lines.append(
            f"| {row['policy_clause']} | {row['total']} | {row['reversed']} | "
            f"{row['reversal_rate']} |"
        )

    lines.extend(["", "## Appeals", "", "| Metric | Value |", "|---|---|"])
    for key, value in report["appeals"]["decision_time_seconds"].items():
        lines.append(f"| {key} | {value} |")

    lines.extend(["", "## Limitations", ""])
    lines.extend(f"- {item}" for item in report["limitations"])
    return "\n".join(lines) + "\n"


def export_json(report: dict) -> str:
    return json.dumps(report, indent=2, sort_keys=True) + "\n"
