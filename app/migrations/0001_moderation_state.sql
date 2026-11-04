-- Durable moderation workflow state.
--
-- The audit ledger remains an append-only JSONL file. These tables store the
-- cases, actions, and appeals that back the creator and reviewer APIs. Table
-- names are prefixed because the devstack may share a PostgreSQL database with
-- keel-core.

CREATE TABLE IF NOT EXISTS moderation_cases (
    id TEXT PRIMARY KEY,
    channel_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    subject_type TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    data JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS moderation_actions (
    id TEXT PRIMARY KEY,
    channel_id TEXT NOT NULL DEFAULT '',
    case_id TEXT NOT NULL,
    status TEXT NOT NULL,
    data JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS moderation_appeals (
    id TEXT PRIMARY KEY,
    channel_id TEXT NOT NULL DEFAULT '',
    action_id TEXT NOT NULL,
    status TEXT NOT NULL,
    submitted_at TIMESTAMPTZ NOT NULL,
    data JSONB NOT NULL
);

CREATE INDEX IF NOT EXISTS moderation_cases_channel_created
    ON moderation_cases (channel_id, created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS moderation_cases_channel_status
    ON moderation_cases (channel_id, status, created_at DESC);
CREATE INDEX IF NOT EXISTS moderation_cases_video
    ON moderation_cases (subject_type, subject_id, created_at DESC);
CREATE INDEX IF NOT EXISTS moderation_actions_channel_created
    ON moderation_actions (channel_id, created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS moderation_actions_channel_status
    ON moderation_actions (channel_id, status, created_at DESC);
CREATE INDEX IF NOT EXISTS moderation_actions_case_id
    ON moderation_actions (case_id);
CREATE INDEX IF NOT EXISTS moderation_appeals_action_id
    ON moderation_appeals (action_id);
CREATE INDEX IF NOT EXISTS moderation_appeals_status
    ON moderation_appeals (status);
CREATE INDEX IF NOT EXISTS moderation_appeals_channel_submitted
    ON moderation_appeals (channel_id, submitted_at ASC, id ASC);
CREATE INDEX IF NOT EXISTS moderation_appeals_channel_status
    ON moderation_appeals (channel_id, status, submitted_at ASC);
