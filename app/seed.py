from __future__ import annotations

import json
import os

from .main import (
    DEFAULT_LEDGER_PATH,
    ActionCreate,
    AppealCreate,
    AppealDecision,
    DemoState,
    state_location_from_env,
)
from .principals import Principal

DEMO_CHANNEL_ID = "ch_demo"
DEMO_VIDEO_ID = "vid_demo_001"


def seed_demo(state: DemoState, channel_id: str = DEMO_CHANNEL_ID) -> dict:
    """Create a deterministic creator moderation record.

    The fixture is idempotent: if the channel already has any enforcement
    actions, the seed leaves the existing record untouched.
    """

    existing = state.list_creator_actions(channel_id)
    if existing["total"] > 0:
        return {
            "seeded": False,
            "channel_id": channel_id,
            "actions": existing["total"],
        }

    moderator = Principal("demo-reviewer", "moderator", (channel_id,))
    creator = Principal("creator:demo", "creator", (channel_id,))
    appeals = Principal("demo-appeals-reviewer", "appeals", (channel_id,))

    historical = state.create_action(
        ActionCreate(
            video_id=DEMO_VIDEO_ID,
            channel_id=channel_id,
            policy_clause="copyright.unlicensed",
            reason=("An automated copyright scan matched this video before human review."),
            evidence=["scanner:copyright-v0.1", "match: 00:02-00:06"],
        ),
        moderator,
    )
    historical_appeal = state.file_appeal(
        historical["id"],
        AppealCreate(
            grounds="new_evidence",
            statement=(
                "The matched footage is original production with a signed release "
                "on file. The scan compared the wrong source."
            ),
        ),
        creator,
        channel_id,
    )
    state.decide_appeal(
        historical_appeal["id"],
        AppealDecision(
            decision="reverse",
            reasoning=(
                "Independent review confirmed the release and reversed the action. "
                "The audit record is preserved."
            ),
        ),
        appeals,
    )

    active = state.create_action(
        ActionCreate(
            video_id=DEMO_VIDEO_ID,
            channel_id=channel_id,
            policy_clause="spam.mass_production",
            reason=("A viewer report and the upload-cadence signal were grouped for human review."),
            evidence=["signal: user_report", "model: spam-v0.1 (confidence 0.91)"],
        ),
        moderator,
    )
    state.file_appeal(
        active["id"],
        AppealCreate(
            grounds="policy_misapplication",
            statement=(
                "This is an original documentary. The upload cadence reflects a "
                "production schedule, not automated republishing."
            ),
        ),
        creator,
        channel_id,
    )

    summary = state.creator_summary(channel_id)
    return {
        "seeded": True,
        "channel_id": channel_id,
        "actions": summary["actions_total"],
        "cases": summary["cases_total"],
        "appeals": summary["appeals_total"],
        "ledger_records": summary["ledger_records"],
    }


def main() -> None:
    state = DemoState(
        os.getenv("KEELTUBE_LEDGER_PATH", DEFAULT_LEDGER_PATH),
        state_location_from_env(),
    )
    result = seed_demo(state, os.getenv("KEELTUBE_DEMO_CHANNEL_ID", DEMO_CHANNEL_ID))
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
