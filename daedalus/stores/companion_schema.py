"""The companion's goals and its activity log, and the voice concierge's rows renamed to the companion's.

A goal is something the companion owns over time: a title, a brief, a status, an optional cadence on
which the host gives the companion a turn to check on it, and the agent sessions working on it. The
cadence is a cron expression or an interval in minutes, never both; a goal with neither is checked
only when somebody asks.

The activity log is what the companion did, in order, for the page's Activity tab: what it delegated,
what its agents reported, which goals it checked and what it said about them. It is a log, not a
store anything reads back to decide something, so it has no foreign keys — a goal that is deleted
leaves its lines behind, which is what a log is for.

The renames carry the one installation from the voice concierge to the companion in place: the
session flag and the parent mark the concierge's sessions carried, the system project, and the key
that remembers which session is the standing one. Each statement only touches rows that still have
the old name, so running it over a database that already has the new ones changes nothing.
"""

MIGRATION = """
CREATE TABLE IF NOT EXISTS companion_goals (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    brief TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'paused', 'done', 'dropped')),
    cron TEXT,
    interval_minutes INTEGER CHECK (interval_minutes IS NULL OR interval_minutes > 0),
    next_check_at TEXT,
    last_check_at TEXT,
    last_report TEXT NOT NULL DEFAULT '',
    last_report_at TEXT,
    sessions_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(sessions_json)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (cron IS NULL OR interval_minutes IS NULL)
);
CREATE INDEX IF NOT EXISTS companion_goals_due ON companion_goals(next_check_at) WHERE status = 'active' AND next_check_at IS NOT NULL;

CREATE TABLE IF NOT EXISTS companion_activity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL DEFAULT '',
    session_id TEXT,
    goal_id TEXT
);

UPDATE sessions SET metadata = json_remove(json_set(metadata, '$.companion', json('true')), '$.voice')
WHERE json_valid(metadata) AND json_type(metadata, '$.voice') IS NOT NULL;
UPDATE sessions SET metadata = json_remove(json_set(metadata, '$.companion_parent', json_extract(metadata, '$.voice_parent')), '$.voice_parent')
WHERE json_valid(metadata) AND json_type(metadata, '$.voice_parent') IS NOT NULL;

UPDATE projects SET name = CASE WHEN name = 'Voice' THEN 'Companion' ELSE name END,
    system = 'companion',
    settings = CASE WHEN json_valid(settings) THEN json_set(settings, '$.system', 'companion') ELSE settings END
WHERE system = 'voice';

INSERT OR IGNORE INTO kv(key, value) SELECT 'companion_session', value FROM kv WHERE key = 'voice_session';
DELETE FROM kv WHERE key = 'voice_session';
"""

__all__ = ["MIGRATION"]
