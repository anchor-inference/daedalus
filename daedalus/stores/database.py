"""SQLite connection and schema migrations."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Callable, Iterable, Sequence
from contextlib import suppress
from pathlib import Path
from typing import Any

import aiosqlite

from daedalus.config import native_mode
from daedalus.stores.attempt_fault_schema import MIGRATION as ATTEMPT_FAULT_MIGRATION
from daedalus.stores.calendar_diagram_schema import migration as CALENDAR_DIAGRAM_MIGRATION
from daedalus.stores.capacity_schema import MIGRATION as CAPACITY_MIGRATION
from daedalus.stores.ci_schema import MIGRATION as CI_MIGRATION
from daedalus.stores.comparison_funding_schema import MIGRATION as COMPARISON_FUNDING_MIGRATION
from daedalus.stores.comparison_schema import MIGRATION as COMPARISON_MIGRATION
from daedalus.stores.contract_change_schema import MIGRATION as CONTRACT_CHANGE_MIGRATION
from daedalus.stores.control_schema import MIGRATION as CONTROL_MIGRATION
from daedalus.stores.coordinator_handoff_schema import MIGRATION as COORDINATOR_HANDOFF_MIGRATION
from daedalus.stores.execution_schema import MIGRATION as EXECUTION_MIGRATION
from daedalus.stores.extension_schema import EXTENSION_SCHEMA
from daedalus.stores.goal_budget_schema import MIGRATION as GOAL_BUDGET_MIGRATION
from daedalus.stores.goal_criteria_schema import MIGRATION as GOAL_CRITERIA_MIGRATION
from daedalus.stores.grant_lineage_schema import MIGRATION as GRANT_LINEAGE_MIGRATION
from daedalus.stores.historical_budget_schema import MIGRATION as HISTORICAL_BUDGET_MIGRATION
from daedalus.stores.historical_context_schema import MIGRATION as HISTORICAL_CONTEXT_MIGRATION
from daedalus.stores.inference_budget_schema import MIGRATION as INFERENCE_BUDGET_MIGRATION
from daedalus.stores.issue_sync_schema import ISSUE_SYNC_MIGRATION
from daedalus.stores.knowledge_invalidation_schema import MIGRATION as KNOWLEDGE_INVALIDATION_MIGRATION
from daedalus.stores.lifecycle_ownership_schema import MIGRATION as LIFECYCLE_OWNERSHIP_MIGRATION
from daedalus.stores.lifecycle_schema import MIGRATION as LIFECYCLE_MIGRATION
from daedalus.stores.orchestrator_contract_schema import MIGRATION as ORCHESTRATOR_CONTRACT_MIGRATION
from daedalus.stores.phase_clock_schema import MIGRATION as PHASE_CLOCK_MIGRATION
from daedalus.stores.planning_schema import MIGRATION as PLANNING_MIGRATION
from daedalus.stores.provider_hold_schema import MIGRATION as PROVIDER_HOLD_MIGRATION
from daedalus.stores.receipt_request_schema import MIGRATION as RECEIPT_REQUEST_MIGRATION
from daedalus.stores.recurring_schema import MIGRATION as RECURRING_MIGRATION
from daedalus.stores.resource_disk_schema import MIGRATION as RESOURCE_DISK_MIGRATION
from daedalus.stores.resource_schema import MIGRATION as RESOURCE_MIGRATION
from daedalus.stores.result_anchor_schema import MIGRATION as RESULT_ANCHOR_MIGRATION
from daedalus.stores.retry_schema import MIGRATION as RETRY_MIGRATION
from daedalus.stores.runtime_entry_schema import MIGRATION as RUNTIME_ENTRY_MIGRATION
from daedalus.stores.runtime_handoff_schema import MIGRATION as RUNTIME_HANDOFF_MIGRATION
from daedalus.stores.runtime_observation_schema import MIGRATION as RUNTIME_OBSERVATION_MIGRATION
from daedalus.stores.runtime_schema import RUNTIME_SCHEMA
from daedalus.stores.schedule_file_pin_schema import MIGRATION as SCHEDULE_FILE_PIN_MIGRATION
from daedalus.stores.schedule_proposal_schema import MIGRATION as SCHEDULE_PROPOSAL_MIGRATION
from daedalus.stores.staff_context_schema import MIGRATION as STAFF_CONTEXT_SCHEMA
from daedalus.stores.staff_report_schema import MIGRATION as STAFF_REPORT_MIGRATION
from daedalus.stores.update_drain_schema import MIGRATION as UPDATE_DRAIN_MIGRATION
from daedalus.stores.watch_authority_schema import MIGRATION as WATCH_AUTHORITY_MIGRATION
from daedalus.stores.watch_delivery_schema import WATCH_DELIVERY_MIGRATION
from daedalus.stores.workflow_scope_schema import MIGRATION as WORKFLOW_SCOPE_MIGRATION
from daedalus.stores.workspace_archive_schema import MIGRATION as WORKSPACE_ARCHIVE_MIGRATION
from daedalus.stores.writer_containment_schema import MIGRATION as WRITER_CONTAINMENT_MIGRATION

logger = logging.getLogger(__name__)

Migration = str | Callable[["Database"], str] | tuple[str, bool]
"""A migration is its SQL, or a function that writes the SQL from what the opening database knows
(the managed workspaces folder, the environment it runs in). A callable keeps a migration that
needs such a value in its numbered place instead of being special-cased by its index."""

MIGRATIONS: list[Migration] = [
    # 1 — core stores
    """
    CREATE TABLE sessions (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        title TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        last_message_at TEXT NOT NULL,
        metadata TEXT NOT NULL DEFAULT '{}'
    );
    CREATE TABLE session_messages (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL,
        tenant_id TEXT NOT NULL,
        message TEXT NOT NULL
    );
    CREATE INDEX session_messages_by_session ON session_messages(session_id, seq);
    CREATE TABLE runs (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        session_id TEXT NOT NULL,
        status TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        detail_blob_ref TEXT
    );
    CREATE INDEX runs_by_session ON runs(session_id, created_at);
    CREATE TABLE events (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        id TEXT NOT NULL,
        run_id TEXT NOT NULL,
        tenant_id TEXT NOT NULL,
        name TEXT NOT NULL,
        payload TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE INDEX events_by_run ON events(run_id, seq);
    CREATE TABLE snapshots (
        run_id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        session_id TEXT NOT NULL,
        state TEXT NOT NULL,
        snapshot TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE live_control (
        session_id TEXT PRIMARY KEY,
        steer_queue TEXT NOT NULL DEFAULT '[]',
        follow_up_queue TEXT NOT NULL DEFAULT '[]',
        model_name TEXT,
        thinking_enabled INTEGER,
        reasoning_effort TEXT,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE usage_events (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        at TEXT NOT NULL,
        provider_id TEXT NOT NULL,
        model TEXT NOT NULL,
        purpose TEXT NOT NULL,
        run_id TEXT,
        session_id TEXT,
        input_tokens INTEGER NOT NULL DEFAULT 0,
        output_tokens INTEGER NOT NULL DEFAULT 0,
        cache_read_tokens INTEGER NOT NULL DEFAULT 0,
        reasoning_tokens INTEGER NOT NULL DEFAULT 0,
        cost_usd REAL,
        duration_ms INTEGER NOT NULL DEFAULT 0,
        raw TEXT NOT NULL
    );
    CREATE INDEX usage_events_by_at ON usage_events(at);
    CREATE INDEX usage_events_by_session ON usage_events(session_id);
    CREATE TABLE memory_records (
        tenant_id TEXT NOT NULL,
        id TEXT NOT NULL,
        record TEXT NOT NULL,
        PRIMARY KEY (tenant_id, id)
    );
    CREATE TABLE workspace_units (
        key TEXT PRIMARY KEY,
        unit TEXT NOT NULL
    );
    CREATE TABLE kv (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    """,
    # 2 — chat bindings, approvals, schedules
    """
    CREATE TABLE topics (
        chat_id INTEGER NOT NULL,
        thread_id INTEGER NOT NULL,
        session_id TEXT NOT NULL UNIQUE,
        title TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        closed_at TEXT,
        PRIMARY KEY (chat_id, thread_id)
    );
    CREATE TABLE pending_questions (
        session_id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL,
        tool_call_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        payload TEXT NOT NULL,
        chat_id INTEGER,
        thread_id INTEGER,
        message_id INTEGER,
        answers TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL
    );
    CREATE TABLE change_proposals (
        id TEXT PRIMARY KEY,
        repo TEXT NOT NULL,
        branch TEXT NOT NULL,
        pr_number INTEGER,
        pr_url TEXT,
        title TEXT NOT NULL,
        summary TEXT NOT NULL,
        session_id TEXT,
        status TEXT NOT NULL,
        reason TEXT,
        created_at TEXT NOT NULL,
        decided_at TEXT,
        message_id INTEGER
    );
    CREATE TABLE schedules (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        cron TEXT,
        run_at TEXT,
        prompt TEXT NOT NULL,
        files TEXT NOT NULL DEFAULT '[]',
        model TEXT,
        recurring INTEGER NOT NULL DEFAULT 0,
        enabled INTEGER NOT NULL DEFAULT 1,
        workspace TEXT NOT NULL,
        last_summary TEXT,
        last_run_at TEXT,
        next_run_at TEXT,
        topic_thread_id INTEGER,
        created_by_session TEXT,
        created_at TEXT NOT NULL
    );
    CREATE TABLE balance_alerts (
        threshold TEXT PRIMARY KEY,
        fired_at TEXT
    );
    """,
    # transcript: everything the operator and the agent ever exchanged, for display.
    # session_messages is the model's working history and shrinks under compaction.
    """
    CREATE TABLE transcript (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL,
        key TEXT NOT NULL,
        message TEXT NOT NULL,
        UNIQUE (session_id, key)
    );
    CREATE INDEX transcript_session ON transcript(session_id, seq);
    ALTER TABLE live_control ADD COLUMN provider TEXT;
    """,
    # per-session model preset
    """
    ALTER TABLE live_control ADD COLUMN preset TEXT;
    """,
    # full-text index over the transcript: what the agent searches when older turns were compacted away
    """
    CREATE VIRTUAL TABLE transcript_fts USING fts5(
        session_id UNINDEXED, seq UNINDEXED, role UNINDEXED, text, tokenize = 'unicode61 remove_diacritics 2'
    );
    """,
    # inbox: what happened while the operator was away; schedule kinds, failure accounting; lazy reminders
    """
    CREATE TABLE inbox (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        at TEXT NOT NULL,
        kind TEXT NOT NULL,
        severity TEXT NOT NULL DEFAULT 'info',
        title TEXT NOT NULL,
        body TEXT NOT NULL DEFAULT '',
        session_id TEXT,
        run_id TEXT,
        read INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX inbox_unread ON inbox(read, at);
    ALTER TABLE schedules ADD COLUMN kind TEXT NOT NULL DEFAULT 'agent';
    ALTER TABLE schedules ADD COLUMN target_session TEXT;
    ALTER TABLE schedules ADD COLUMN failure_count INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE schedules ADD COLUMN last_error TEXT;
    CREATE TABLE lazy_notes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        schedule_id TEXT,
        session_id TEXT NOT NULL,
        text TEXT NOT NULL,
        fired_at TEXT NOT NULL,
        delivered_at TEXT,
        promoted_at TEXT
    );
    """,
    # delivery ledger: a final answer that was generated but never confirmed sent is the one thing a restart can lose
    """
    CREATE TABLE deliveries (
        run_id TEXT PRIMARY KEY,
        session_id TEXT NOT NULL,
        text TEXT NOT NULL,
        status TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0,
        error TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    """,
    # schedules remember their in-flight run across restarts; lazy notes count promotion attempts
    """
    ALTER TABLE schedules ADD COLUMN active_session_id TEXT;
    ALTER TABLE schedules ADD COLUMN active_run_id TEXT;
    ALTER TABLE lazy_notes ADD COLUMN promote_attempts INTEGER NOT NULL DEFAULT 0;
    """,
    # workspace checkpoints, verification receipts, learning records
    """
    CREATE TABLE checkpoints (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL,
        seq INTEGER,
        run_id TEXT,
        kind TEXT NOT NULL,
        sha TEXT NOT NULL,
        at TEXT NOT NULL
    );
    CREATE INDEX checkpoints_session ON checkpoints(session_id, seq);
    CREATE TABLE verifications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL,
        run_id TEXT,
        criterion TEXT NOT NULL,
        command TEXT NOT NULL,
        cwd TEXT,
        exit_code INTEGER NOT NULL,
        passed INTEGER NOT NULL,
        output_digest TEXT NOT NULL,
        output_head TEXT,
        duration_ms INTEGER,
        at TEXT NOT NULL
    );
    CREATE INDEX verifications_session ON verifications(session_id, at);
    CREATE TABLE learning_records (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL,
        run_id TEXT NOT NULL,
        at TEXT NOT NULL,
        ask TEXT,
        outcome TEXT NOT NULL,
        headline TEXT,
        tools TEXT NOT NULL DEFAULT '{}',
        failures TEXT NOT NULL DEFAULT '[]',
        iterations INTEGER,
        cost_usd REAL,
        duration_s REAL
    );
    """,
    # inbound events: webhook dedupe, standing intents
    """
    CREATE TABLE webhook_deliveries (
        provider TEXT NOT NULL,
        delivery_id TEXT NOT NULL,
        at TEXT NOT NULL,
        PRIMARY KEY (provider, delivery_id)
    );
    CREATE TABLE intents (
        id TEXT PRIMARY KEY,
        pattern TEXT NOT NULL,
        action TEXT NOT NULL,
        session_id TEXT,
        enabled INTEGER NOT NULL DEFAULT 1,
        cooldown_minutes INTEGER NOT NULL DEFAULT 60,
        max_fires INTEGER NOT NULL DEFAULT 20,
        fired_count INTEGER NOT NULL DEFAULT 0,
        last_fired_at TEXT,
        expires_at TEXT,
        created_by_session TEXT,
        created_at TEXT NOT NULL
    );
    """,
    # task board
    """
    CREATE TABLE board_tasks (
        id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        status TEXT NOT NULL,
        priority INTEGER NOT NULL DEFAULT 3,
        acceptance TEXT NOT NULL DEFAULT '',
        checklist TEXT NOT NULL DEFAULT '[]',
        depends_on TEXT NOT NULL DEFAULT '[]',
        session_id TEXT,
        run_id TEXT,
        notes TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        heartbeat_at TEXT
    );
    """,
    # 12 — a receipt says whether the check ran confined
    """
    ALTER TABLE verifications ADD COLUMN sandboxed INTEGER NOT NULL DEFAULT 0;
    """,
    # 13 — a scheduled task can run inside the session that created it
    """
    ALTER TABLE schedules ADD COLUMN run_in TEXT NOT NULL DEFAULT 'new';
    """,
    # 14 — a receipt names the shared dependencies its observation rests on
    """
    ALTER TABLE verifications ADD COLUMN dependencies TEXT NOT NULL DEFAULT '';
    """,
    # 15 — a loop: the standing task a session is woken up for, on an interval or when it says so
    """
    CREATE TABLE loops (
        session_id TEXT PRIMARY KEY,
        instruction TEXT NOT NULL,
        mode TEXT NOT NULL DEFAULT 'interval',
        interval_seconds INTEGER,
        max_runs INTEGER,
        status TEXT NOT NULL DEFAULT 'active',
        next_run_at TEXT,
        last_run_at TEXT,
        run_count INTEGER NOT NULL DEFAULT 0,
        last_reason TEXT,
        stop_reason TEXT,
        pause_note TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    """,
    # 16 — a service: a long-running process a session started and the operator can reach
    """
    CREATE TABLE services (
        session_id TEXT NOT NULL,
        name TEXT NOT NULL,
        command TEXT NOT NULL,
        cwd TEXT NOT NULL,
        port INTEGER,
        pid INTEGER,
        status TEXT NOT NULL DEFAULT 'running',
        restart INTEGER NOT NULL DEFAULT 1,
        log_path TEXT NOT NULL,
        note TEXT,
        started_at TEXT NOT NULL,
        stopped_at TEXT,
        PRIMARY KEY (session_id, name)
    );
    CREATE INDEX services_port ON services(port);
    """,
    # 17 — a service can be shared through the bot's public address: to anyone, or to whoever holds its key
    """
    ALTER TABLE services ADD COLUMN share_mode TEXT NOT NULL DEFAULT 'local';
    ALTER TABLE services ADD COLUMN share_slug TEXT;
    ALTER TABLE services ADD COLUMN share_key TEXT;
    CREATE INDEX services_share_slug ON services(share_slug);
    """,
    # 18 — one row per tool call (the waterfall of a run) and one per network host a call reached
    """
    CREATE TABLE tool_calls (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL,
        run_id TEXT,
        tool_call_id TEXT NOT NULL,
        name TEXT NOT NULL,
        at TEXT NOT NULL,
        duration_ms INTEGER NOT NULL,
        ok INTEGER NOT NULL DEFAULT 1
    );
    CREATE INDEX tool_calls_by_session ON tool_calls(session_id);
    CREATE INDEX tool_calls_by_run ON tool_calls(run_id);
    CREATE TABLE egress_log (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        at TEXT NOT NULL,
        session_id TEXT NOT NULL,
        run_id TEXT,
        tool TEXT NOT NULL,
        host TEXT NOT NULL,
        action TEXT NOT NULL
    );
    CREATE INDEX egress_log_by_session ON egress_log(session_id);
    CREATE INDEX usage_events_by_run ON usage_events(run_id);
    """,
    # 19 — a task remembers the session that created it: the plan file of a workspace lists its own tasks whoever holds them
    """
    ALTER TABLE board_tasks ADD COLUMN origin_session_id TEXT;
    UPDATE board_tasks SET origin_session_id = session_id WHERE origin_session_id IS NULL;
    """,
    # 20 — a receipt says which tree it ran against and whether it ran any tests: a green run that
    # executed nothing is not evidence, and a reviewer can see the commit the check belongs to
    """
    ALTER TABLE verifications ADD COLUMN tree TEXT NOT NULL DEFAULT '';
    ALTER TABLE verifications ADD COLUMN tests_run INTEGER;
    ALTER TABLE verifications ADD COLUMN tests_skipped INTEGER;
    """,
    # 21 — a receipt carries the content of the files it ran against, so "the check covered these
    # bytes" is a comparison of bytes rather than of clock readings, which a formatter or a rebase
    # moves without changing anything. The digest is taken in the checkout the command ran in.
    """
    ALTER TABLE verifications ADD COLUMN file_digests TEXT NOT NULL DEFAULT '';
    """,
    # 22 — the two ways into the app that need no Telegram: a one-time pairing link, and the
    # browser's own passkey. Codes are stored hashed, so the database never holds a usable link.
    """
    CREATE TABLE pairing_codes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code_hash TEXT NOT NULL UNIQUE,
        note TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        used_at TEXT
    );
    CREATE TABLE passkeys (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        credential_id TEXT NOT NULL UNIQUE,
        public_key TEXT NOT NULL,
        sign_count INTEGER NOT NULL DEFAULT 0,
        transports TEXT NOT NULL DEFAULT '[]',
        name TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        last_used_at TEXT
    );
    """,
    # 23 — the shape the app draws for a row, stored beside the row. Deriving it cost about a
    # millisecond a message, almost all of it secret redaction, and it was derived again for
    # every one of the six hundred turns of every session open. view_key names the code and the
    # redactor that produced the copy, so a row is recomputed when either has moved on.
    """
    ALTER TABLE transcript ADD COLUMN view TEXT;
    ALTER TABLE transcript ADD COLUMN view_key TEXT NOT NULL DEFAULT '';
    """,
    # 24 — the working history is written the way it changes: a round appends its new rows, and
    # only a rewrite of the sequence (a compaction, a revert, a clear) starts a generation, which
    # is written whole and replaces the one before it. Every round used to delete and re-insert
    # the lot. ``key`` is the message identity the append compares against, so it can see whether
    # what it was handed continues what is stored or replaces it.
    """
    ALTER TABLE session_messages ADD COLUMN gen INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE session_messages ADD COLUMN key TEXT NOT NULL DEFAULT '';
    CREATE INDEX session_messages_by_gen ON session_messages(session_id, gen, seq);
    """,
    # 25 — the full-text index stops keeping its own copy of every message. A plain fts5 table
    # stores the indexed text verbatim in a shadow table, which here was a second copy of the
    # transcript: a third of the whole database, duplicating text the transcript already holds.
    # Contentless, the index holds only the index; the row it points at is the transcript row,
    # which is where the text for a snippet comes from now. Dropping the kv watermark is what
    # makes the store rebuild the index from the transcript on the next start.
    """
    DROP TABLE transcript_fts;
    CREATE VIRTUAL TABLE transcript_fts USING fts5(
        text, content = '', contentless_delete = 1, tokenize = 'unicode61 remove_diacritics 2'
    );
    DELETE FROM kv WHERE key = 'transcript_fts_watermark';
    """,
    # 26 — projects: a folder the operator adds, shared by every session that works in it. A
    # session without one keeps the per-session directory under the workspaces root, which is why
    # the column is nullable and nothing backfills it: existing sessions are project-less and stay
    # exactly as they were. ``root`` is the absolute path as the operator gave it; in a container
    # it may name a directory that is not mounted yet, which is a state the API reports rather than
    # a row it refuses to keep.
    """
    CREATE TABLE projects (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        root TEXT NOT NULL,
        created_at TEXT NOT NULL,
        settings TEXT NOT NULL DEFAULT '{}'
    );
    ALTER TABLE sessions ADD COLUMN project_id TEXT;
    CREATE INDEX sessions_by_project ON sessions(project_id);
    """,
    # what retention took away, per session, so the app can say so instead of offering an undo
    # that would restore nothing
    """
    CREATE TABLE checkpoint_retention (
        session_id TEXT PRIMARY KEY,
        removed_before TEXT NOT NULL,
        removed INTEGER NOT NULL,
        at TEXT NOT NULL
    );
    """,
    # the installation's own projects are one per kind, and the database is what says so. The flag
    # lived only inside the ``settings`` JSON, where no constraint can reach it, and the code that
    # kept it unique read the table and then inserted — with two awaits in between. Two delegations
    # asked for at once therefore made two "Voice" folders, and a system project cannot be deleted,
    # so the installation could not be tidied up from the app at all. The column carries the flag
    # out of the JSON so a partial unique index can hold it; the rows before it are merged onto the
    # oldest of each kind and the sessions of the others are re-pointed at it, which is where their
    # agents were meant to be listed all along.
    """
    ALTER TABLE projects ADD COLUMN system TEXT NOT NULL DEFAULT '';
    UPDATE projects SET system = COALESCE(json_extract(settings, '$.system'), '')
    WHERE json_valid(settings);
    UPDATE sessions SET project_id = (
        SELECT k.id FROM projects k
        WHERE k.system = (SELECT d.system FROM projects d WHERE d.id = sessions.project_id)
        ORDER BY k.created_at, k.rowid LIMIT 1
    )
    WHERE project_id IN (SELECT id FROM projects WHERE system != '');
    DELETE FROM projects WHERE system != '' AND id NOT IN (
        SELECT (SELECT k.id FROM projects k WHERE k.system = p.system ORDER BY k.created_at, k.rowid LIMIT 1)
        FROM projects p WHERE p.system != ''
    );
    CREATE UNIQUE INDEX projects_system ON projects(system) WHERE system != '';
    """,
]


def _project_unification(db: Database) -> str:
    """Build the migration that turns every existing working directory into a project."""
    base = _sql_text(str(Path(os.path.normpath(db.workspaces_dir.expanduser()))))
    return f"""
    UPDATE sessions SET metadata = CASE WHEN json_valid(metadata)
        THEN CASE WHEN json_type(metadata) = 'object' THEN metadata ELSE '{{}}' END
        ELSE '{{}}' END;


    INSERT INTO projects(id, name, root, created_at, settings, system)
    SELECT 'project-' || substr(min(id), 1, 12), 'Voice',
           (SELECT CASE
              WHEN json_valid(v.metadata) AND json_type(v.metadata, '$.workspace') = 'text'
                AND trim(json_extract(v.metadata, '$.workspace')) != ''
              THEN rtrim(json_extract(v.metadata, '$.workspace'), '/')
              ELSE '{base}/' || v.id
            END
            FROM sessions v
            WHERE v.project_id IS NULL AND json_valid(v.metadata) AND json_extract(v.metadata, '$.voice') = 1
            ORDER BY v.created_at, v.id LIMIT 1),
           min(created_at),
           '{{"snapshots":false,"system":"voice"}}', 'voice'
    FROM sessions
    WHERE project_id IS NULL AND json_valid(metadata) AND json_extract(metadata, '$.voice') = 1
    HAVING count(*) > 0 AND NOT EXISTS (SELECT 1 FROM projects WHERE system = 'voice');

    CREATE TEMP TABLE duplicate_projects AS
    SELECT p.id, (SELECT k.id FROM projects k WHERE k.root = p.root
                  ORDER BY (k.system != '') DESC, k.created_at, k.rowid LIMIT 1) AS keeper
    FROM projects p;
    UPDATE sessions SET project_id = (SELECT keeper FROM duplicate_projects WHERE id = sessions.project_id)
    WHERE project_id IN (SELECT id FROM duplicate_projects WHERE id != keeper);
    DELETE FROM projects WHERE id IN (SELECT id FROM duplicate_projects WHERE id != keeper);
    DROP TABLE duplicate_projects;

    CREATE TEMP TABLE session_directories AS
    SELECT s.id,
           CASE
             WHEN p.id IS NOT NULL AND NOT COALESCE((
               COALESCE(json_extract(s.metadata, '$.own_workspace'), 0)
               AND json_type(s.metadata, '$.workspace') = 'text'
               AND trim(json_extract(s.metadata, '$.workspace')) != ''
             ), 0) THEN p.root
             WHEN json_valid(s.metadata) AND json_type(s.metadata, '$.workspace') = 'text'
               AND trim(json_extract(s.metadata, '$.workspace')) != ''
             THEN rtrim(json_extract(s.metadata, '$.workspace'), '/')
             ELSE '{base}/' || s.id
           END AS directory
    FROM sessions s LEFT JOIN projects p ON p.id = s.project_id;

    CREATE TEMP TABLE session_project_roots AS
    SELECT d.id, d.directory,
           COALESCE(
             (SELECT p.root FROM projects p
              WHERE d.directory = p.root OR substr(d.directory, 1, length(p.root) + 1) = p.root || '/'
              ORDER BY length(p.root), p.root LIMIT 1),
             (SELECT p.directory FROM session_directories p
              WHERE d.directory = p.directory OR substr(d.directory, 1, length(p.directory) + 1) = p.directory || '/'
              ORDER BY length(p.directory), p.directory LIMIT 1),
             d.directory
           ) AS root
    FROM session_directories d;

    INSERT INTO projects(id, name, root, created_at, settings, system)
    SELECT 'project-' || substr(min(s.id), 1, 12),
           COALESCE(NULLIF(trim((SELECT s2.title FROM sessions s2 JOIN session_project_roots r2 ON r2.id = s2.id
                                WHERE r2.root = r.root ORDER BY s2.created_at, s2.id LIMIT 1)), ''), 'Project'),
           r.root, min(s.created_at), '{{"snapshots":true,"system":"","auto_created":true}}', ''
    FROM session_project_roots r JOIN sessions s ON s.id = r.id
    WHERE NOT EXISTS (SELECT 1 FROM projects p WHERE p.root = r.root)
    GROUP BY r.root;

    UPDATE sessions
    SET project_id = (SELECT p.id FROM session_project_roots r JOIN projects p ON p.root = r.root WHERE r.id = sessions.id),
        metadata = CASE
          WHEN (SELECT directory FROM session_project_roots WHERE id = sessions.id) =
               (SELECT root FROM session_project_roots WHERE id = sessions.id)
          THEN json_remove(metadata, '$.workspace', '$.own_workspace', '$.directory')
          ELSE json_set(json_remove(metadata, '$.workspace', '$.own_workspace', '$.directory'), '$.directory',
               substr((SELECT directory FROM session_project_roots WHERE id = sessions.id),
                      length((SELECT root FROM session_project_roots WHERE id = sessions.id)) + 2))
        END;

    DROP TABLE session_project_roots;
    DROP TABLE session_directories;

    CREATE TABLE sessions_unified (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        title TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        last_message_at TEXT NOT NULL,
        metadata TEXT NOT NULL DEFAULT '{{}}',
        project_id TEXT NOT NULL REFERENCES projects(id)
    );
    INSERT INTO sessions_unified SELECT id, tenant_id, title, created_at, last_message_at, metadata, project_id FROM sessions;
    DROP TABLE sessions;
    ALTER TABLE sessions_unified RENAME TO sessions;
    CREATE INDEX sessions_by_project ON sessions(project_id);
    CREATE UNIQUE INDEX projects_root ON projects(root);
    """


def _sql_text(value: str) -> str:
    """A value from Python spliced into a migration script, which takes no parameters."""
    return value.replace("'", "''")


MIGRATIONS.append(_project_unification)


MIGRATIONS.append("""
CREATE TABLE search_vectors (
    seq INTEGER NOT NULL REFERENCES transcript(seq) ON DELETE CASCADE,
    block INTEGER NOT NULL,
    offset INTEGER NOT NULL,
    model TEXT NOT NULL,
    dimension INTEGER NOT NULL,
    vector BLOB NOT NULL,
    PRIMARY KEY(seq, block, offset)
);
CREATE TABLE search_pending (
    seq INTEGER PRIMARY KEY REFERENCES transcript(seq) ON DELETE CASCADE,
    block INTEGER NOT NULL DEFAULT 0,
    offset INTEGER NOT NULL DEFAULT 0,
    revision TEXT NOT NULL DEFAULT ''
);
INSERT INTO search_pending(seq) SELECT seq FROM transcript;
CREATE TRIGGER search_append AFTER INSERT ON transcript BEGIN
    INSERT OR REPLACE INTO search_pending(seq, revision) VALUES (new.seq, hex(randomblob(8)));
END;
CREATE TRIGGER search_replace AFTER UPDATE OF message ON transcript BEGIN
    DELETE FROM search_vectors WHERE seq = new.seq;
    INSERT OR REPLACE INTO search_pending(seq, revision) VALUES (new.seq, hex(randomblob(8)));
END;
CREATE TABLE search_titles (
    session_id TEXT PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    model TEXT NOT NULL,
    dimension INTEGER NOT NULL,
    vector BLOB NOT NULL
);
""")


MIGRATIONS.append("""
CREATE TABLE media_presentations (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    run_id TEXT NOT NULL,
    layout TEXT NOT NULL CHECK(layout IN ('single', 'album')),
    state TEXT NOT NULL CHECK(state IN ('staged', 'ready')),
    message_key TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX media_presentations_by_session ON media_presentations(session_id, created_at);
CREATE INDEX media_presentations_by_run ON media_presentations(session_id, run_id, state);
CREATE TABLE media_items (
    id TEXT PRIMARY KEY,
    presentation_id TEXT NOT NULL REFERENCES media_presentations(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL,
    blob_ref TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('image', 'animation', 'video', 'audio')),
    mime_type TEXT NOT NULL,
    filename TEXT NOT NULL,
    byte_size INTEGER NOT NULL,
    width INTEGER,
    height INTEGER,
    alt TEXT NOT NULL DEFAULT '',
    caption TEXT NOT NULL DEFAULT '',
    UNIQUE(presentation_id, ordinal)
);
CREATE INDEX media_items_by_blob ON media_items(blob_ref);
""")


# Session-wide cursors survive run changes, while input receipts make a client retry the same
# submission instead of creating a second queue item after an acknowledgement is lost.
MIGRATIONS.append("""
CREATE TABLE session_stream_state (
    session_id TEXT PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
    event_seq INTEGER NOT NULL DEFAULT 0,
    history_revision INTEGER NOT NULL DEFAULT 0,
    min_event_seq INTEGER NOT NULL DEFAULT 1,
    runtime_epoch TEXT NOT NULL DEFAULT ''
);
CREATE TABLE session_events (
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    event_seq INTEGER NOT NULL,
    run_id TEXT,
    history_revision INTEGER NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (session_id, event_seq)
);
CREATE INDEX session_events_by_run ON session_events(run_id, event_seq);
CREATE TABLE input_receipts (
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    client_message_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    content_digest TEXT NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL,
    run_id TEXT,
    step_id TEXT,
    message_seq INTEGER,
    error TEXT,
    queue_revision INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (session_id, client_message_id)
);
CREATE INDEX input_receipts_by_session_status
    ON input_receipts(session_id, status, queue_revision);
ALTER TABLE live_control ADD COLUMN queue_revision INTEGER NOT NULL DEFAULT 0;
""")


MIGRATIONS.append("""
CREATE TABLE request_manifests (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    manifest_id TEXT NOT NULL UNIQUE,
    run_id TEXT,
    session_id TEXT REFERENCES sessions(id) ON DELETE CASCADE,
    attempt_id TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    model TEXT NOT NULL,
    manifest TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX request_manifests_by_session ON request_manifests(session_id, seq);
CREATE INDEX request_manifests_by_run ON request_manifests(run_id, seq);
""")


# A link is not a blob. source_url holds it; blob_ref stays empty so pruning does not look for bytes.
MIGRATIONS.append("""
ALTER TABLE media_items ADD COLUMN source_url TEXT NOT NULL DEFAULT '';
""")


# The event bus's persisted ring. AUTOINCREMENT, because a sequence number a client or an
# orchestrator holds as its cursor must never be handed out again, not after a prune emptied the
# table and not after a restart; the plain rowid would reuse the highest number once it is deleted.
MIGRATIONS.append("""
CREATE TABLE app_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    type TEXT NOT NULL,
    project_id TEXT,
    session_id TEXT,
    staff_id TEXT,
    terminal_id TEXT,
    payload_json TEXT NOT NULL
);
CREATE INDEX app_events_by_at ON app_events(at);
CREATE INDEX app_events_by_project ON app_events(project_id, seq) WHERE project_id IS NOT NULL;
""")


def _projects_with_folders(db: Database) -> str:
    """Build the migration that gives a project folders, a brief, a journal and a team.

    A project was one ``root``; it becomes a row with one or more folders, and the old root is its
    first folder, in the environment this process runs in — the only one whose paths it ever wrote.
    The column is dropped rather than kept beside the folders: two answers to "where does this
    project live" is the shape the move to projects had to undo once already.

    ``auto_created`` becomes ``ephemeral``, its honest name: a project made implicitly by a new chat
    and removed with its last session. Everything a team of agents needs later is created here too,
    empty, so the whole model is one migration and one rehearsal on a copy of the real database.

    The per-project concurrency starts at 6 and its cap at 10. ``allowed_without_operator`` is a
    section of the brief like any other to the schema; that only the operator may write it is the
    store's rule, and the reason is there.
    """
    env = _sql_text(db.local_env)
    return f"""
    CREATE TABLE project_folders (
        id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        path TEXT NOT NULL UNIQUE,
        label TEXT NOT NULL DEFAULT '',
        env TEXT NOT NULL CHECK (env IN ('container', 'host')),
        is_git INTEGER NOT NULL DEFAULT 0,
        readonly INTEGER NOT NULL DEFAULT 0,
        position INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL
    );
    CREATE INDEX project_folders_by_project ON project_folders(project_id, position);
    INSERT INTO project_folders(id, project_id, path, env, position, created_at)
        SELECT 'f-' || id, id, root, '{env}', 0, created_at FROM projects;
    DROP INDEX projects_root;
    ALTER TABLE projects DROP COLUMN root;
    UPDATE projects SET settings = CASE WHEN json_valid(settings)
        THEN CASE WHEN json_type(settings) = 'object' THEN settings ELSE '{{}}' END
        ELSE '{{}}' END;
    UPDATE projects SET settings = json_set(
        json_remove(settings, '$.auto_created'),
        '$.ephemeral', json(CASE WHEN json_extract(settings, '$.auto_created') = 1 THEN 'true' ELSE 'false' END),
        '$.default_env', '{env}',
        '$.orchestrator', json('{{"enabled":false,"session_id":"","model":"","autonomy":"normal","concurrency":6,"concurrency_cap":10,"telegram_topic_id":0}}'));

    CREATE TABLE project_briefs (
        project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        section TEXT NOT NULL CHECK (section IN ('goals', 'constraints', 'preferences', 'done_when', 'allowed_without_operator', 'notes')),
        body TEXT NOT NULL DEFAULT '',
        updated_at TEXT NOT NULL,
        updated_by TEXT NOT NULL CHECK (updated_by IN ('operator', 'orchestrator', 'system')),
        PRIMARY KEY (project_id, section)
    );
    CREATE TABLE project_journal (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        at TEXT NOT NULL,
        author TEXT NOT NULL CHECK (author IN ('orchestrator', 'operator', 'system')),
        kind TEXT NOT NULL,
        text TEXT NOT NULL,
        refs_json TEXT NOT NULL DEFAULT '{{}}'
    );
    CREATE INDEX project_journal_by_project ON project_journal(project_id, id);

    CREATE TABLE staff (
        id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        name TEXT NOT NULL,
        color TEXT NOT NULL DEFAULT '',
        role TEXT NOT NULL DEFAULT '',
        harness TEXT NOT NULL CHECK (harness IN ('daedalus', 'claude', 'codex', 'grok', 'opencode', 'pi')),
        agent TEXT NOT NULL DEFAULT '',
        model TEXT NOT NULL DEFAULT '',
        effort TEXT NOT NULL DEFAULT '',
        permission_mode TEXT NOT NULL DEFAULT '',
        env TEXT NOT NULL DEFAULT '' CHECK (env IN ('', 'container', 'host')),
        default_folder_id TEXT REFERENCES project_folders(id) ON DELETE SET NULL,
        isolation TEXT NOT NULL DEFAULT 'worktree' CHECK (isolation IN ('shared', 'worktree', 'readonly')),
        instructions TEXT NOT NULL DEFAULT '',
        notes TEXT NOT NULL DEFAULT '',
        one_off INTEGER NOT NULL DEFAULT 0,
        created_by TEXT NOT NULL CHECK (created_by IN ('operator', 'orchestrator')),
        created_at TEXT NOT NULL,
        archived_at TEXT
    );
    CREATE UNIQUE INDEX staff_name ON staff(project_id, name COLLATE NOCASE) WHERE archived_at IS NULL;

    ALTER TABLE board_tasks ADD COLUMN project_id TEXT REFERENCES projects(id) ON DELETE CASCADE;
    ALTER TABLE board_tasks ADD COLUMN assignee_staff_id TEXT REFERENCES staff(id) ON DELETE SET NULL;
    ALTER TABLE board_tasks ADD COLUMN brief_json TEXT NOT NULL DEFAULT '{{}}';
    ALTER TABLE board_tasks ADD COLUMN folder_id TEXT REFERENCES project_folders(id) ON DELETE SET NULL;
    ALTER TABLE board_tasks ADD COLUMN branch TEXT;
    ALTER TABLE board_tasks ADD COLUMN merge_state TEXT NOT NULL DEFAULT '' CHECK (merge_state IN ('', 'proposed', 'merged', 'conflict', 'rejected'));
    UPDATE board_tasks SET project_id = (SELECT project_id FROM sessions WHERE sessions.id = board_tasks.origin_session_id)
        WHERE origin_session_id IS NOT NULL;
    CREATE INDEX board_tasks_by_project ON board_tasks(project_id, status);

    CREATE TABLE staff_sessions (
        id TEXT PRIMARY KEY,
        staff_id TEXT NOT NULL REFERENCES staff(id) ON DELETE CASCADE,
        kind TEXT NOT NULL CHECK (kind IN ('daedalus', 'cli')),
        session_id TEXT REFERENCES sessions(id) ON DELETE SET NULL,
        terminal_id TEXT,
        cli_session_id TEXT,
        transcript_ref TEXT,
        task_id TEXT REFERENCES board_tasks(id) ON DELETE SET NULL,
        status TEXT NOT NULL DEFAULT 'starting' CHECK (status IN ('starting', 'working', 'turn_done_unseen', 'idle', 'question', 'permission', 'error', 'exited', 'no_signal')),
        waiting_for TEXT NOT NULL DEFAULT '',
        status_at TEXT NOT NULL,
        last_signal_at TEXT,
        started_at TEXT NOT NULL,
        ended_at TEXT,
        end_reason TEXT NOT NULL DEFAULT '',
        predecessor_id TEXT REFERENCES staff_sessions(id) ON DELETE SET NULL,
        folder_id TEXT REFERENCES project_folders(id) ON DELETE SET NULL,
        worktree_path TEXT,
        branch TEXT,
        base_ref TEXT,
        pause_requested INTEGER NOT NULL DEFAULT 0,
        team_token_hash TEXT NOT NULL DEFAULT '',
        usage_json TEXT NOT NULL DEFAULT '{{}}'
    );
    CREATE UNIQUE INDEX staff_sessions_live ON staff_sessions(staff_id) WHERE ended_at IS NULL;
    CREATE INDEX staff_sessions_by_session ON staff_sessions(session_id);

    CREATE TABLE staff_messages (
        id TEXT PRIMARY KEY,
        staff_id TEXT NOT NULL REFERENCES staff(id) ON DELETE CASCADE,
        staff_session_id TEXT REFERENCES staff_sessions(id) ON DELETE SET NULL,
        origin TEXT NOT NULL CHECK (origin IN ('orchestrator', 'operator')),
        text TEXT NOT NULL,
        mode TEXT NOT NULL CHECK (mode IN ('queue', 'steer', 'interrupt')),
        state TEXT NOT NULL CHECK (state IN ('queued', 'written', 'submitted', 'acknowledged', 'failed')),
        attempts INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        error TEXT NOT NULL DEFAULT ''
    );
    CREATE INDEX staff_messages_by_staff ON staff_messages(staff_id, created_at);

    CREATE TABLE asks (
        id TEXT PRIMARY KEY,
        short_id TEXT NOT NULL,
        project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        origin TEXT NOT NULL CHECK (origin IN ('staff', 'orchestrator')),
        kind TEXT NOT NULL CHECK (kind IN ('question', 'permission', 'folder')),
        staff_id TEXT REFERENCES staff(id) ON DELETE CASCADE,
        staff_session_id TEXT REFERENCES staff_sessions(id) ON DELETE SET NULL,
        task_id TEXT REFERENCES board_tasks(id) ON DELETE SET NULL,
        request_ref TEXT NOT NULL DEFAULT '',
        text TEXT NOT NULL,
        detail_json TEXT NOT NULL DEFAULT '{{}}',
        routed_to TEXT NOT NULL CHECK (routed_to IN ('orchestrator', 'operator')),
        suggestion TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        routed_at TEXT NOT NULL,
        resolved_at TEXT,
        resolved_by TEXT CHECK (resolved_by IN ('orchestrator', 'operator', 'staff', 'system')),
        resolution_json TEXT NOT NULL DEFAULT '{{}}'
    );
    CREATE INDEX asks_open ON asks(project_id, resolved_at, routed_to);
    -- The short id is what a person types on a phone or in Telegram to answer. It only has to tell
    -- apart the requests still waiting, so it is unique among those and free again once answered.
    CREATE UNIQUE INDEX asks_short_id_open ON asks(short_id) WHERE resolved_at IS NULL;

    CREATE TABLE watches (
        id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        pattern_json TEXT NOT NULL,
        action_json TEXT NOT NULL,
        cooldown_s INTEGER NOT NULL DEFAULT 600,
        once INTEGER NOT NULL DEFAULT 0,
        note TEXT NOT NULL DEFAULT '',
        created_by TEXT NOT NULL CHECK (created_by IN ('operator', 'orchestrator')),
        created_at TEXT NOT NULL,
        last_fired_at TEXT,
        fire_count INTEGER NOT NULL DEFAULT 0,
        enabled INTEGER NOT NULL DEFAULT 1,
        state_json TEXT NOT NULL DEFAULT '{{}}'
    );
    CREATE INDEX watches_by_project ON watches(project_id, enabled);
    """


MIGRATIONS.append(_projects_with_folders)

# The inbox becomes notifications, rows kept and ids continued: one table for everything that wants
# the operator's attention, whatever channel it later goes out on. The old severity splits in two —
# how loud (level: an "info" entry was a record, never worth a badge) and what colour (tone) — and
# "read" becomes the moment it was seen. The counter is carried over as well as the rows: an entry
# deleted from the top of the inbox left its number used, and a client still holding it must not
# find a different notification under it. The request, hold and resolution columns are used once
# notifications can be answered; they are here so the table is reshaped once.
MIGRATIONS.append("""
CREATE TABLE notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    kind TEXT NOT NULL,
    category TEXT NOT NULL,
    level TEXT NOT NULL DEFAULT 'normal' CHECK (level IN ('quiet', 'normal', 'urgent')),
    tone TEXT NOT NULL DEFAULT 'info' CHECK (tone IN ('ok', 'info', 'warning', 'error')),
    title TEXT NOT NULL,
    body TEXT NOT NULL DEFAULT '',
    link TEXT NOT NULL DEFAULT '',
    session_id TEXT,
    run_id TEXT,
    project_id TEXT,
    staff_id TEXT,
    terminal_id TEXT,
    source TEXT NOT NULL DEFAULT 'system',
    dedupe_key TEXT,
    count INTEGER NOT NULL DEFAULT 1,
    actions_json TEXT NOT NULL DEFAULT '[]',
    request_ref TEXT,
    held_until TEXT,
    seen_at TEXT,
    resolved_at TEXT,
    resolution TEXT,
    delivered_json TEXT NOT NULL DEFAULT '{}',
    event_seq INTEGER
);
INSERT INTO notifications(id, at, updated_at, kind, category, level, tone, title, body,
                          session_id, run_id, source, seen_at)
SELECT id, at, at, kind,
       CASE WHEN kind IN ('run_failed', 'run_cap') THEN 'run_failed'
            WHEN kind IN ('reminder', 'schedule_run', 'heartbeat') THEN 'reminder'
            WHEN kind = 'loop_paused' THEN 'question'
            WHEN kind = 'loop' THEN 'agent_notify'
            ELSE 'system' END,
       CASE severity WHEN 'info' THEN 'quiet' ELSE 'normal' END,
       CASE severity WHEN 'error' THEN 'error' WHEN 'warning' THEN 'warning' ELSE 'info' END,
       title, body, session_id, run_id, 'system',
       CASE WHEN read = 1 THEN at END
FROM inbox;
DELETE FROM sqlite_sequence WHERE name = 'notifications';
INSERT INTO sqlite_sequence(name, seq)
SELECT 'notifications', max(COALESCE((SELECT seq FROM sqlite_sequence WHERE name = 'inbox'), 0),
                            COALESCE((SELECT max(id) FROM notifications), 0));
DROP TABLE inbox;
CREATE INDEX notifications_unseen ON notifications(seen_at, level, id);
CREATE INDEX notifications_open ON notifications(request_ref) WHERE request_ref IS NOT NULL AND resolved_at IS NULL;
CREATE INDEX notifications_dedupe ON notifications(dedupe_key) WHERE dedupe_key IS NOT NULL AND resolved_at IS NULL;
CREATE INDEX notifications_by_session ON notifications(session_id, id);
CREATE INDEX notifications_by_project ON notifications(project_id, id) WHERE project_id IS NOT NULL;
""")


# Terminals: the host's mirror of what each environment's terminal daemon runs, and the audit of what
# was done to them. No foreign keys to sessions, projects or staff: an owner is one of four kinds,
# cleanup goes through the owners' delete hooks, and a later reshaping of any of those tables must
# not have to rebuild this one. The audit has no key to the terminal either, because it outlives the
# row: an ended host terminal's attach history is exactly what someone reads after the fact.
MIGRATIONS.append("""
CREATE TABLE terminals (
    id TEXT PRIMARY KEY,
    env TEXT NOT NULL,
    project_id TEXT,
    owner_kind TEXT NOT NULL,
    owner_id TEXT,
    title TEXT NOT NULL DEFAULT '',
    cwd TEXT NOT NULL,
    argv_json TEXT NOT NULL DEFAULT '[]',
    profile TEXT NOT NULL DEFAULT 'shell',
    sandbox INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'running',
    exit_code INTEGER,
    created_at TEXT NOT NULL,
    exited_at TEXT,
    last_output_at TEXT,
    last_input_at TEXT,
    cols INTEGER NOT NULL DEFAULT 80,
    rows INTEGER NOT NULL DEFAULT 24,
    ptyd_instance TEXT NOT NULL DEFAULT '',
    exit_signal TEXT,
    shell_integration INTEGER NOT NULL DEFAULT 1,
    last_command_json TEXT,
    final_preview_json TEXT,
    created_by TEXT NOT NULL DEFAULT 'operator'
);
CREATE INDEX terminals_by_owner ON terminals(owner_kind, owner_id);
CREATE INDEX terminals_by_project ON terminals(project_id);
CREATE INDEX terminals_by_status ON terminals(status);
CREATE TABLE terminal_audit (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    terminal_id TEXT NOT NULL,
    env TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX terminal_audit_by_terminal ON terminal_audit(terminal_id, seq);
CREATE INDEX terminal_audit_by_at ON terminal_audit(at);
""")


# Command-line agents as staff. The orchestrator owns staff sessions, their messages and the
# requests they raise; these tables hold only what is particular to a CLI in a terminal, keyed to
# those rows and dropped with them, so neither side writes the other's columns.
# - harness_catalog: per environment and CLI, what is installed, what is latest, whether it is
#   signed in, its agents and models, and the last self-check. Whether a version is tested is not
#   stored: it follows from the adapter's range, which changes with the code, not with the data.
# - harness_launches: the launch a staff session runs in — the daemon's launch id that hook posts
#   are routed by, the companion terminal, the CLI's session id — so a restarted host can attach to
#   a CLI that kept running. At most one open launch per staff session.
# - harness_deliveries: how a message reached the CLI (paste, pointer, a channel), whether steer had
#   to become something else, and the id the CLI echoes for it, which is how a restarted host tells a
#   message that arrived from one to send again.
MIGRATIONS.append("""
CREATE TABLE harness_catalog (
    env TEXT NOT NULL CHECK (env IN ('container', 'host')),
    harness TEXT NOT NULL,
    installed_version TEXT NOT NULL DEFAULT '',
    latest_version TEXT NOT NULL DEFAULT '',
    install_method TEXT NOT NULL DEFAULT '',
    binary_path TEXT NOT NULL DEFAULT '',
    logged_in TEXT NOT NULL DEFAULT 'unknown' CHECK (logged_in IN ('yes', 'no', 'unknown')),
    login_detail TEXT NOT NULL DEFAULT '',
    agents_json TEXT NOT NULL DEFAULT '[]',
    models_json TEXT NOT NULL DEFAULT '[]',
    modes_json TEXT NOT NULL DEFAULT '[]',
    efforts_json TEXT NOT NULL DEFAULT '[]',
    profiles_json TEXT NOT NULL DEFAULT '[]',
    self_check_json TEXT NOT NULL DEFAULT '{}',
    checked_at TEXT,
    latest_checked_at TEXT,
    error TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (env, harness)
);
CREATE TABLE harness_launches (
    launch_id TEXT PRIMARY KEY,
    staff_session_id TEXT NOT NULL REFERENCES staff_sessions(id) ON DELETE CASCADE,
    harness TEXT NOT NULL,
    env TEXT NOT NULL CHECK (env IN ('container', 'host')),
    terminal_id TEXT,
    companion_terminal_id TEXT,
    launch_dir TEXT NOT NULL DEFAULT '',
    session_ref TEXT NOT NULL DEFAULT '',
    harness_version TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL,
    ended_at TEXT
);
CREATE UNIQUE INDEX harness_launches_open ON harness_launches(staff_session_id) WHERE ended_at IS NULL;
CREATE INDEX harness_launches_by_terminal ON harness_launches(terminal_id);
CREATE TABLE harness_deliveries (
    message_id TEXT PRIMARY KEY REFERENCES staff_messages(id) ON DELETE CASCADE,
    launch_id TEXT NOT NULL REFERENCES harness_launches(launch_id) ON DELETE CASCADE,
    via TEXT NOT NULL DEFAULT '',
    degraded_to TEXT NOT NULL DEFAULT '' CHECK (degraded_to IN ('', 'queue', 'interrupt')),
    client_ref TEXT NOT NULL DEFAULT '',
    enters INTEGER NOT NULL DEFAULT 0,
    written_at TEXT,
    submitted_at TEXT,
    acknowledged_at TEXT
);
CREATE INDEX harness_deliveries_by_launch ON harness_deliveries(launch_id, client_ref);
""")


# The browsers and phones Web Push reaches, one row per push subscription. The endpoint is the
# subscription's identity (a browser that subscribes again gets a new one), so it is unique and an
# upsert replaces the keys that come with it. No foreign keys: a subscription belongs to the
# installation, not to a session or a project. ``failures`` counts consecutive failed sends and is
# reset by a success; a row that has not succeeded for a week is dropped by the sender.
MIGRATIONS.append("""
CREATE TABLE push_subscriptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    endpoint TEXT NOT NULL UNIQUE,
    p256dh TEXT NOT NULL,
    auth TEXT NOT NULL,
    user_agent TEXT NOT NULL DEFAULT '',
    device TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    last_ok_at TEXT,
    failures INTEGER NOT NULL DEFAULT 0,
    last_error TEXT
);
""")


# The main orchestrator's hand-overs. A dispatch is one piece of work the operator gave a project
# through the main orchestrator; it stays open until the project's orchestrator closes it with a
# report, and its follow-ups and reports are its messages. ``seq`` numbers a project's dispatches, so
# the one that set a new project up is #1 wherever it is read. ``stalled_at`` is when the watchdog
# said it had gone quiet, so it says so once per silence and not at every tick.
#
# The requests table is rebuilt rather than altered, because three of its constraints change at once:
# a request may now belong to no project yet (the confirmation that creates one), may come from the
# main orchestrator, and may be of the kind "project"; and it gains the dispatch it is shown under.
# Nothing references the requests table, so the rebuild is a copy, a drop and a rename. A project
# that the main orchestrator set up carries ``setup_by`` until its first dispatch is closed as done or
# the operator finishes the setup by hand; while it does, every request of the project is linked.
MIGRATIONS.append("""
CREATE TABLE dispatches (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    from_session TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT 'work' CHECK (kind IN ('work', 'setup')),
    title TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'done', 'blocked', 'cancelled')),
    result TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    closed_at TEXT,
    stalled_at TEXT
);
CREATE UNIQUE INDEX dispatches_seq ON dispatches(project_id, seq);
CREATE INDEX dispatches_open ON dispatches(status, updated_at);
CREATE TABLE dispatch_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dispatch_id TEXT NOT NULL REFERENCES dispatches(id) ON DELETE CASCADE,
    at TEXT NOT NULL,
    author TEXT NOT NULL CHECK (author IN ('dispatcher', 'orchestrator', 'operator', 'system')),
    kind TEXT NOT NULL,
    text TEXT NOT NULL
);
CREATE INDEX dispatch_messages_by_dispatch ON dispatch_messages(dispatch_id, id);

CREATE TABLE asks_rebuilt (
    id TEXT PRIMARY KEY,
    short_id TEXT NOT NULL,
    project_id TEXT REFERENCES projects(id) ON DELETE CASCADE,
    origin TEXT NOT NULL CHECK (origin IN ('staff', 'orchestrator', 'dispatcher')),
    kind TEXT NOT NULL CHECK (kind IN ('question', 'permission', 'folder', 'project')),
    staff_id TEXT REFERENCES staff(id) ON DELETE CASCADE,
    staff_session_id TEXT REFERENCES staff_sessions(id) ON DELETE SET NULL,
    task_id TEXT REFERENCES board_tasks(id) ON DELETE SET NULL,
    request_ref TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}',
    routed_to TEXT NOT NULL CHECK (routed_to IN ('orchestrator', 'operator')),
    suggestion TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    routed_at TEXT NOT NULL,
    resolved_at TEXT,
    resolved_by TEXT CHECK (resolved_by IN ('orchestrator', 'operator', 'staff', 'system')),
    resolution_json TEXT NOT NULL DEFAULT '{}',
    dispatch_id TEXT REFERENCES dispatches(id) ON DELETE SET NULL
);
INSERT INTO asks_rebuilt(id, short_id, project_id, origin, kind, staff_id, staff_session_id, task_id, request_ref, text,
                         detail_json, routed_to, suggestion, created_at, routed_at, resolved_at, resolved_by, resolution_json)
SELECT id, short_id, project_id, origin, kind, staff_id, staff_session_id, task_id, request_ref, text,
       detail_json, routed_to, suggestion, created_at, routed_at, resolved_at, resolved_by, resolution_json
FROM asks;
DROP TABLE asks;
ALTER TABLE asks_rebuilt RENAME TO asks;
CREATE INDEX asks_open ON asks(project_id, resolved_at, routed_to);
CREATE UNIQUE INDEX asks_short_id_open ON asks(short_id) WHERE resolved_at IS NULL;
CREATE INDEX asks_by_dispatch ON asks(dispatch_id) WHERE dispatch_id IS NOT NULL;

ALTER TABLE projects ADD COLUMN setup_by TEXT NOT NULL DEFAULT '';
""")


# What a command-line adapter needs to take a launch up again after the host restarted, beyond the
# launch row's own columns: OpenCode's port and the password of the server inside its TUI, which
# lived only in the adapter's memory, so a restarted host could not reach a member still running.
# Opaque to everything but the adapter; blanked when the launch ends.
MIGRATIONS.append("""
ALTER TABLE harness_launches ADD COLUMN adapter_state TEXT NOT NULL DEFAULT '';
""")


# A folder may belong to more than one project, once to each: the uniqueness of a folder's path moves
# from the whole table to the project. SQLite cannot drop a column's UNIQUE, so the table is rebuilt.
# Three columns refer to a folder by id with ON DELETE SET NULL, and a migration runs inside a
# transaction, where foreign keys cannot be switched off: dropping the old table would quietly blank
# every member's default folder, every task's folder and every staff session's folder. They are kept
# aside first and written back once the rebuilt table holds the same ids.
MIGRATIONS.append("""
CREATE TEMP TABLE folder_references AS
    SELECT 'staff' AS holder, id, default_folder_id AS folder_id FROM staff WHERE default_folder_id IS NOT NULL
    UNION ALL SELECT 'board_tasks', id, folder_id FROM board_tasks WHERE folder_id IS NOT NULL
    UNION ALL SELECT 'staff_sessions', id, folder_id FROM staff_sessions WHERE folder_id IS NOT NULL;
CREATE TABLE project_folders_rebuilt (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    path TEXT NOT NULL,
    label TEXT NOT NULL DEFAULT '',
    env TEXT NOT NULL CHECK (env IN ('container', 'host')),
    is_git INTEGER NOT NULL DEFAULT 0,
    readonly INTEGER NOT NULL DEFAULT 0,
    position INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    UNIQUE (project_id, path)
);
INSERT INTO project_folders_rebuilt(id, project_id, path, label, env, is_git, readonly, position, created_at)
    SELECT id, project_id, path, label, env, is_git, readonly, position, created_at FROM project_folders;
DROP TABLE project_folders;
ALTER TABLE project_folders_rebuilt RENAME TO project_folders;
CREATE INDEX project_folders_by_project ON project_folders(project_id, position);
CREATE INDEX project_folders_by_path ON project_folders(path);
UPDATE staff SET default_folder_id = (SELECT r.folder_id FROM folder_references r WHERE r.holder = 'staff' AND r.id = staff.id)
    WHERE id IN (SELECT id FROM folder_references WHERE holder = 'staff');
UPDATE board_tasks SET folder_id = (SELECT r.folder_id FROM folder_references r WHERE r.holder = 'board_tasks' AND r.id = board_tasks.id)
    WHERE id IN (SELECT id FROM folder_references WHERE holder = 'board_tasks');
UPDATE staff_sessions SET folder_id = (SELECT r.folder_id FROM folder_references r WHERE r.holder = 'staff_sessions' AND r.id = staff_sessions.id)
    WHERE id IN (SELECT id FROM folder_references WHERE holder = 'staff_sessions');
DROP TABLE folder_references;
""")

# A request gains a short title of its own. An orchestrator asks in batches now, and the operator
# answers them from a list: a list of whole paragraphs could not be scanned. Older rows keep an
# empty title and are shown by the first line of their text.
MIGRATIONS.append("""
ALTER TABLE asks ADD COLUMN title TEXT NOT NULL DEFAULT '';
""")


# Files handed between the operator, the orchestrators and staff travel by handle, not by path: a
# path means something in one environment only. The bytes are in the blob store; a row names them,
# the scopes (the main orchestrator, a project) that may use the handle, the tasks that carry them,
# and every movement of one — who, what, where, the size and the hash.
MIGRATIONS.append("""
CREATE TABLE files (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    mime TEXT NOT NULL,
    size INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    origin TEXT NOT NULL,
    origin_ref TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE file_access (
    file_id TEXT NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    scope TEXT NOT NULL,
    added_at TEXT NOT NULL,
    added_by TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (file_id, scope)
);
CREATE INDEX file_access_by_scope ON file_access(scope, added_at);
CREATE TABLE task_files (
    task_id TEXT NOT NULL REFERENCES board_tasks(id) ON DELETE CASCADE,
    file_id TEXT NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    added_at TEXT NOT NULL,
    added_by TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (task_id, file_id)
);
CREATE INDEX task_files_by_file ON task_files(file_id);
CREATE TABLE file_transfers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    file_id TEXT REFERENCES files(id) ON DELETE SET NULL,
    action TEXT NOT NULL,
    actor TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT '',
    target TEXT NOT NULL DEFAULT '',
    env TEXT NOT NULL DEFAULT '',
    size INTEGER NOT NULL DEFAULT 0,
    sha256 TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT ''
);
CREATE INDEX file_transfers_by_file ON file_transfers(file_id, id);
""")


# A message to staff is named by when it goes in — now, after the turn, or with an interrupt — rather
# than by the queue and steer it was once implemented with: an orchestrator reading "queue" as the
# ordinary choice sent every correction after the turn it was meant for. The values sit in two CHECK
# constraints, so both tables are rebuilt. The delivery facts reference the messages with ON DELETE
# CASCADE and foreign keys cannot be switched off inside a migration's transaction, so they are kept
# aside and the facts table is dropped before the messages are, then filled again. Rowids are copied
# so that the order of messages created in the same instant stays as it was. A watch that tells a
# member keeps its timing under the new key.
MIGRATIONS.append("""
CREATE TEMP TABLE deliveries_aside AS SELECT * FROM harness_deliveries;
DROP TABLE harness_deliveries;
CREATE TABLE staff_messages_rebuilt (
    id TEXT PRIMARY KEY,
    staff_id TEXT NOT NULL REFERENCES staff(id) ON DELETE CASCADE,
    staff_session_id TEXT REFERENCES staff_sessions(id) ON DELETE SET NULL,
    origin TEXT NOT NULL CHECK (origin IN ('orchestrator', 'operator')),
    text TEXT NOT NULL,
    mode TEXT NOT NULL CHECK (mode IN ('now', 'after_turn', 'interrupt')),
    state TEXT NOT NULL CHECK (state IN ('queued', 'written', 'submitted', 'acknowledged', 'failed')),
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    error TEXT NOT NULL DEFAULT ''
);
INSERT INTO staff_messages_rebuilt(rowid, id, staff_id, staff_session_id, origin, text, mode, state, attempts, created_at, updated_at, error)
    SELECT rowid, id, staff_id, staff_session_id, origin, text,
           CASE mode WHEN 'queue' THEN 'after_turn' WHEN 'steer' THEN 'now' ELSE mode END,
           state, attempts, created_at, updated_at, error
    FROM staff_messages;
DROP TABLE staff_messages;
ALTER TABLE staff_messages_rebuilt RENAME TO staff_messages;
CREATE INDEX staff_messages_by_staff ON staff_messages(staff_id, created_at);
CREATE TABLE harness_deliveries (
    message_id TEXT PRIMARY KEY REFERENCES staff_messages(id) ON DELETE CASCADE,
    launch_id TEXT NOT NULL REFERENCES harness_launches(launch_id) ON DELETE CASCADE,
    via TEXT NOT NULL DEFAULT '',
    degraded_to TEXT NOT NULL DEFAULT '' CHECK (degraded_to IN ('', 'after_turn', 'interrupt')),
    client_ref TEXT NOT NULL DEFAULT '',
    enters INTEGER NOT NULL DEFAULT 0,
    written_at TEXT,
    submitted_at TEXT,
    acknowledged_at TEXT
);
INSERT INTO harness_deliveries(message_id, launch_id, via, degraded_to, client_ref, enters, written_at, submitted_at, acknowledged_at)
    SELECT message_id, launch_id, via, CASE degraded_to WHEN 'queue' THEN 'after_turn' ELSE degraded_to END,
           client_ref, enters, written_at, submitted_at, acknowledged_at
    FROM deliveries_aside;
CREATE INDEX harness_deliveries_by_launch ON harness_deliveries(launch_id, client_ref);
DROP TABLE deliveries_aside;
UPDATE watches SET action_json = json_remove(json_set(action_json, '$.when',
        CASE json_extract(action_json, '$.mode') WHEN 'queue' THEN 'after_turn' WHEN 'steer' THEN 'now' ELSE json_extract(action_json, '$.mode') END),
    '$.mode')
    WHERE json_extract(action_json, '$.action') = 'tell' AND json_type(action_json, '$.mode') IS NOT NULL;
""")

# The agent's browser. The browser daemon owns Chromium and its profiles; these tables hold what the
# host knows of them and keeps across its own restarts, which the daemon outlives:
# - browser_groups: one per owner (a session, a command-line staff member) and profile — its tabs in
#   the profile's browser, who owns it, and who holds its controls. The id is the host's and is
#   reused when the owner opens its browser again, so the audit of one owner's browser is one thread.
# - browsers: the Chromium processes the daemons reported, for the settings page and the load.
# - browser_profiles: the profiles the host asked for, with the scope that names them, so a profile
#   can be listed, cleared or deleted after every browser on it has closed.
# - browser_audit: append-only, like terminal_audit. What an agent typed is kept as a length and a
#   hash, and what a person typed while driving as a count. The text an agent typed into a field that
#   is not secret sits in a column of its own, for the action log the operator reviews, and is
#   emptied when the session it belongs to is deleted.
# - A group also keeps what it asks of the operator now (needs_json) and its last action, for the
#   app's lists without a round trip to the daemon per row.
MIGRATIONS.append("""
CREATE TABLE browsers (
    id TEXT NOT NULL,
    env TEXT NOT NULL CHECK (env IN ('container', 'host')),
    profile TEXT NOT NULL,
    pid INTEGER,
    status TEXT NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'exited', 'lost')),
    reason TEXT NOT NULL DEFAULT '',
    chromium_version TEXT NOT NULL DEFAULT '',
    daemon_instance TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL,
    exited_at TEXT,
    PRIMARY KEY (env, id)
);
CREATE INDEX browsers_by_status ON browsers(status);
CREATE TABLE browser_profiles (
    id TEXT NOT NULL,
    env TEXT NOT NULL CHECK (env IN ('container', 'host')),
    scope TEXT NOT NULL CHECK (scope IN ('project', 'session', 'staff', 'ephemeral')),
    project_id TEXT,
    session_id TEXT,
    staff_id TEXT,
    created_at TEXT NOT NULL,
    last_used_at TEXT NOT NULL,
    PRIMARY KEY (env, id)
);
CREATE TABLE browser_groups (
    id TEXT PRIMARY KEY,
    env TEXT NOT NULL CHECK (env IN ('container', 'host')),
    profile TEXT NOT NULL,
    browser_id TEXT NOT NULL DEFAULT '',
    owner_kind TEXT NOT NULL CHECK (owner_kind IN ('session', 'staff')),
    owner_id TEXT NOT NULL,
    project_id TEXT,
    session_id TEXT,
    staff_id TEXT,
    fresh INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed', 'lost')),
    close_reason TEXT NOT NULL DEFAULT '',
    control_owner TEXT NOT NULL DEFAULT 'agent' CHECK (control_owner IN ('agent', 'human', 'paused')),
    control_reason TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    tabs INTEGER NOT NULL DEFAULT 0,
    daemon_instance TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    last_activity_at TEXT NOT NULL,
    closed_at TEXT,
    needs_json TEXT,
    last_action_json TEXT
);
CREATE INDEX browser_groups_by_owner ON browser_groups(owner_kind, owner_id);
CREATE INDEX browser_groups_by_session ON browser_groups(session_id);
CREATE INDEX browser_groups_by_staff ON browser_groups(staff_id);
CREATE INDEX browser_groups_by_project ON browser_groups(project_id);
CREATE INDEX browser_groups_by_status ON browser_groups(status, env);
CREATE TABLE browser_audit (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    group_id TEXT NOT NULL,
    env TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}',
    typed TEXT
);
CREATE INDEX browser_audit_by_group ON browser_audit(group_id, seq);
CREATE INDEX browser_audit_by_at ON browser_audit(at);
""")


# A dialog can be opened from outside the app the way a service can: not at all, by anyone who
# has the link, or by whoever also has its key. The slug is random. It stays when sharing is
# turned off, so the same link works again, and it is the only secret of a public share.
MIGRATIONS.append("""
ALTER TABLE sessions ADD COLUMN share_mode TEXT NOT NULL DEFAULT 'local';
ALTER TABLE sessions ADD COLUMN share_slug TEXT;
ALTER TABLE sessions ADD COLUMN share_key TEXT;
CREATE UNIQUE INDEX sessions_share_slug ON sessions(share_slug);
""")


# The operator's standing grants to a command-line member: what "Always" allowed, as the CLI's own
# allow rule (``mcp__browser__BrowserNavigate``, or ``mcp__browser`` for every tool of a server).
# Kept by the host and written into each launch, because the CLI keeps a grant only for the session
# it was given in, and the project folder where it would otherwise keep one is the operator's.
MIGRATIONS.append("""
CREATE TABLE staff_allow_rules (
    staff_id TEXT NOT NULL REFERENCES staff(id) ON DELETE CASCADE,
    rule TEXT NOT NULL,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL DEFAULT 'operator',
    PRIMARY KEY (staff_id, rule)
);
""")


# Review used to collect every task a member reported done, and a task the orchestrator wrote and
# handed out without a branch never left it: the orchestrator read the report and moved on, and the
# operator found a column of rounds nobody had asked them about. Such a task now goes to done when
# its member reports it; the ones already parked are closed the same way, with the member's last
# done report kept on the card. The operator's own tasks (no origin session) and every task with a
# branch to merge stay in review.
MIGRATIONS.append("""
UPDATE board_tasks
SET notes = substr(ltrim(
        notes
        || COALESCE(char(10) || (
            SELECT '[' || replace(substr(e.at, 1, 16), 'T', ' ') || '] result: '
                || substr(replace(json_extract(e.payload_json, '$.text'), char(10), ' '), 1, 1500)
            FROM app_events e
            WHERE e.type = 'staff.report' AND json_extract(e.payload_json, '$.kind') = 'done'
              AND json_extract(e.payload_json, '$.task_id') = board_tasks.id
            ORDER BY e.seq DESC LIMIT 1
        ), '')
        || char(10) || '[' || strftime('%Y-%m-%d %H:%M', 'now') || '] closed: handed in by its member and read by the orchestrator; review is for what the operator must look at',
        char(10)), -8000),
    status = 'done', session_id = NULL, run_id = NULL, updated_at = strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now')
WHERE status = 'review'
  AND project_id IS NOT NULL
  AND assignee_staff_id IS NOT NULL
  AND origin_session_id IS NOT NULL
  AND COALESCE(branch, '') = '';
""")


# The operator's recorded steps in the agent's browser (docs/architecture/browser.md, Recording a
# person's steps): one row per recording, its steps as the daemon kept them. The daemon forgets a
# recording once it has published it; this is where it lives until the retention of recordings
# removes it, or the operator drafts a procedure from it (note_id, the site note it became) or
# discards it. A step holds no secret by construction, so nothing here is emptied with a session.
MIGRATIONS.append("""
CREATE TABLE browser_workflows (
    id TEXT PRIMARY KEY,
    group_id TEXT NOT NULL,
    env TEXT NOT NULL CHECK (env IN ('container', 'host')),
    project_id TEXT,
    session_id TEXT,
    staff_id TEXT,
    status TEXT NOT NULL DEFAULT 'recording' CHECK (status IN ('recording', 'stopped')),
    values_mode TEXT NOT NULL DEFAULT 'slots' CHECK (values_mode IN ('slots', 'literal')),
    stop_reason TEXT NOT NULL DEFAULT '',
    start_url TEXT NOT NULL DEFAULT '',
    start_title TEXT NOT NULL DEFAULT '',
    steps_json TEXT NOT NULL DEFAULT '[]',
    note_id TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL,
    stopped_at TEXT
);
CREATE INDEX browser_workflows_by_group ON browser_workflows(group_id, started_at);
CREATE INDEX browser_workflows_by_stopped ON browser_workflows(stopped_at);
""")

# The contract of a task, its acceptance and the results nobody has decided on.
#
# A requirement is a row of its own, with where it came from and what became of it: the operator's
# words said once in the orchestrator's chat were lost to the card and to the member who worked it
# (the list of quality demands for a video lived only in a conversation; two reference videos were
# promised to a scriptwriter and never sent). Its deliveries say which member was given it, how, and
# whether they confirmed it or opened the file it names.
#
# Acceptance is a second dimension of a card beside its column: "done" on the board is where the
# work is, and a member's report used to be all it took to get there. ``handed_in`` is the member's
# word, ``accepted`` the orchestrator's check of each item, ``operator_approved`` the operator's own
# acceptance, ``returned`` a check that sent it back. Cards already finished are given what they
# were: a merge the operator made is theirs, a card an orchestrator closed on a report in the last
# week is only handed in, anything older counts as accepted rather than raising old work again.
#
# An open result is a report of done, stuck or needs-input, or a card nobody owns, that no decision
# of the orchestrator has followed yet: it stays in the state block until one does.
MIGRATIONS.append("""
CREATE TABLE task_requirements (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES board_tasks(id) ON DELETE CASCADE,
    project_id TEXT NOT NULL,
    number INTEGER NOT NULL,
    text TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('quality', 'scope', 'input', 'constraint')),
    source TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'active' CHECK (state IN ('active', 'superseded', 'withdrawn')),
    replaces TEXT,
    file_id TEXT,
    evidence_json TEXT NOT NULL DEFAULT '{}',
    mark_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (task_id, number)
);
CREATE INDEX task_requirements_by_project ON task_requirements(project_id, state);
CREATE TABLE requirement_deliveries (
    requirement_id TEXT NOT NULL REFERENCES task_requirements(id) ON DELETE CASCADE,
    staff_session_id TEXT NOT NULL,
    staff_id TEXT NOT NULL,
    via TEXT NOT NULL CHECK (via IN ('brief', 'message')),
    message_id TEXT NOT NULL DEFAULT '',
    path TEXT NOT NULL DEFAULT '',
    delivered_at TEXT NOT NULL,
    acknowledged_at TEXT,
    opened_at TEXT,
    PRIMARY KEY (requirement_id, staff_session_id)
);
CREATE INDEX requirement_deliveries_by_session ON requirement_deliveries(staff_session_id);
ALTER TABLE board_tasks ADD COLUMN acceptance_state TEXT NOT NULL DEFAULT ''
    CHECK (acceptance_state IN ('', 'handed_in', 'accepted', 'operator_approved', 'returned'));
UPDATE board_tasks SET acceptance_state = CASE
        WHEN status = 'review' THEN 'handed_in'
        WHEN COALESCE(branch, '') != '' AND merge_state = 'merged' THEN 'operator_approved'
        WHEN origin_session_id IS NOT NULL AND updated_at >= strftime('%Y-%m-%dT%H:%M:%f', 'now', '-7 days') THEN 'handed_in'
        ELSE 'accepted'
    END
WHERE project_id IS NOT NULL AND status IN ('review', 'done');
CREATE TABLE open_loops (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    task_id TEXT,
    staff_id TEXT,
    cause TEXT NOT NULL CHECK (cause IN ('report_done', 'report_stuck', 'report_needs_input', 'task_unowned')),
    event_seq INTEGER NOT NULL DEFAULT 0,
    summary TEXT NOT NULL DEFAULT '',
    opened_at TEXT NOT NULL,
    reminded_at TEXT,
    closed_at TEXT,
    closed_by TEXT NOT NULL DEFAULT '',
    decision TEXT NOT NULL DEFAULT ''
);
CREATE INDEX open_loops_open ON open_loops(project_id, closed_at);
""")

# Changing the staff harness constraint requires rebuilding its parent table. Foreign-key checks
# are restored immediately after the atomic copy; leaving them on during DROP would cascade every
# member's sessions and task assignments.
MIGRATIONS.append(("""
CREATE TABLE staff_rebuilt (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    color TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL DEFAULT '',
    harness TEXT NOT NULL CHECK (harness IN ('daedalus', 'claude', 'codex', 'cursor', 'grok', 'opencode', 'pi')),
    agent TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    effort TEXT NOT NULL DEFAULT '',
    permission_mode TEXT NOT NULL DEFAULT '',
    env TEXT NOT NULL DEFAULT '' CHECK (env IN ('', 'container', 'host')),
    default_folder_id TEXT REFERENCES project_folders(id) ON DELETE SET NULL,
    isolation TEXT NOT NULL DEFAULT 'worktree' CHECK (isolation IN ('shared', 'worktree', 'readonly')),
    instructions TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    one_off INTEGER NOT NULL DEFAULT 0,
    created_by TEXT NOT NULL CHECK (created_by IN ('operator', 'orchestrator')),
    created_at TEXT NOT NULL,
    archived_at TEXT
);
INSERT INTO staff_rebuilt SELECT * FROM staff;
DROP TABLE staff;
ALTER TABLE staff_rebuilt RENAME TO staff;
CREATE UNIQUE INDEX staff_name ON staff(project_id, name COLLATE NOCASE) WHERE archived_at IS NULL;
ALTER TABLE staff_sessions ADD COLUMN launch_cwd TEXT;
""", True))

MIGRATIONS.append(CONTROL_MIGRATION)
MIGRATIONS.append(ORCHESTRATOR_CONTRACT_MIGRATION)
MIGRATIONS.append(EXTENSION_SCHEMA)
MIGRATIONS.append(EXECUTION_MIGRATION)
MIGRATIONS.append(RESULT_ANCHOR_MIGRATION)
MIGRATIONS.append(RECEIPT_REQUEST_MIGRATION)
MIGRATIONS.append(RUNTIME_SCHEMA)
MIGRATIONS.append(PLANNING_MIGRATION)
MIGRATIONS.append(KNOWLEDGE_INVALIDATION_MIGRATION)
MIGRATIONS.append(LIFECYCLE_MIGRATION)
MIGRATIONS.append(LIFECYCLE_OWNERSHIP_MIGRATION)
MIGRATIONS.append(STAFF_REPORT_MIGRATION)
MIGRATIONS.append(RUNTIME_OBSERVATION_MIGRATION)
MIGRATIONS.append(WORKSPACE_ARCHIVE_MIGRATION)
MIGRATIONS.append(GRANT_LINEAGE_MIGRATION)
MIGRATIONS.append(COMPARISON_MIGRATION)
MIGRATIONS.append(CI_MIGRATION)
MIGRATIONS.append(ISSUE_SYNC_MIGRATION)
MIGRATIONS.append(WATCH_DELIVERY_MIGRATION)
MIGRATIONS.append(INFERENCE_BUDGET_MIGRATION)
MIGRATIONS.append(WATCH_AUTHORITY_MIGRATION)
MIGRATIONS.append(WORKFLOW_SCOPE_MIGRATION)
MIGRATIONS.append(COMPARISON_FUNDING_MIGRATION)
MIGRATIONS.append(CONTRACT_CHANGE_MIGRATION)
MIGRATIONS.append(STAFF_CONTEXT_SCHEMA)
MIGRATIONS.append(RUNTIME_ENTRY_MIGRATION)
MIGRATIONS.append(GOAL_BUDGET_MIGRATION)
MIGRATIONS.append(HISTORICAL_CONTEXT_MIGRATION)
MIGRATIONS.append(PROVIDER_HOLD_MIGRATION)
MIGRATIONS.append(HISTORICAL_BUDGET_MIGRATION)
MIGRATIONS.append(RECURRING_MIGRATION)
MIGRATIONS.append(COORDINATOR_HANDOFF_MIGRATION)
MIGRATIONS.append(RESOURCE_MIGRATION)
MIGRATIONS.append(SCHEDULE_PROPOSAL_MIGRATION)
MIGRATIONS.append(RESOURCE_DISK_MIGRATION)
MIGRATIONS.append(UPDATE_DRAIN_MIGRATION)
MIGRATIONS.append(RUNTIME_HANDOFF_MIGRATION)
MIGRATIONS.append(GOAL_CRITERIA_MIGRATION)
MIGRATIONS.append(SCHEDULE_FILE_PIN_MIGRATION)
MIGRATIONS.append(PHASE_CLOCK_MIGRATION)
MIGRATIONS.append(CAPACITY_MIGRATION)
MIGRATIONS.append(ATTEMPT_FAULT_MIGRATION)
MIGRATIONS.append(RETRY_MIGRATION)
MIGRATIONS.append(CALENDAR_DIAGRAM_MIGRATION)
MIGRATIONS.append(WRITER_CONTAINMENT_MIGRATION)

BRANCH_BASE_SCHEMA = MIGRATIONS.index(CONTROL_MIGRATION)

CACHE_PAGES = -65536
"""Page cache, as negative kibibytes: 64 MiB. The default is two megabytes, which a session
open walks straight through."""
WAL_AUTOCHECKPOINT_PAGES = 2000
"""Write-ahead log pages before a checkpoint folds them back into the file (~8 MiB). Under one
long-lived connection the default lets the log grow into the tens of megabytes."""
VACUUM_PAGES = 1000
"""Free pages handed back to the filesystem per maintenance pass (~4 MiB), so reclaiming a
large freelist is spread over passes rather than holding the lock for all of it at once."""


class Database:
    """A single shared aiosqlite connection guarded by a lock."""

    def __init__(self, path: Path, *, workspaces_dir: Path | None = None, local_env: str | None = None) -> None:
        self.path = path
        self.workspaces_dir = workspaces_dir or path.parent / "workspaces"
        self.local_env = local_env or ("host" if native_mode() else "container")
        """The environment this process's folders live in. A migration that turns a path it finds
        into a project folder records it there: in a container the paths are the container's."""
        self._conn: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()
        self._authority_effect_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._warned_about_freelist = False

    def authority_effect_lock(self, scope_kind: str, scope_id: str) -> asyncio.Lock:
        """Serialize a scope's approval changes with in-process effects, outside SQLite."""
        key = (scope_kind, scope_id)
        if key not in self._authority_effect_locks:
            self._authority_effect_locks[key] = asyncio.Lock()
        return self._authority_effect_locks[key]

    async def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path, isolation_level=None)
        try:
            self._conn.row_factory = aiosqlite.Row
            await self._conn.execute("PRAGMA journal_mode=WAL")
            await self._conn.execute("PRAGMA busy_timeout=5000")
            await self._conn.execute("PRAGMA synchronous=NORMAL")
            await self._conn.execute("PRAGMA foreign_keys=ON")
            # Takes effect for a database created here; an existing one keeps whatever it was made
            # with until a full VACUUM rewrites it (``daedalus db vacuum``).
            await self._conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
            await self._conn.execute(f"PRAGMA cache_size={CACHE_PAGES}")
            await self._conn.execute("PRAGMA temp_store=MEMORY")
            await self._conn.execute(f"PRAGMA wal_autocheckpoint={WAL_AUTOCHECKPOINT_PAGES}")
            await self._migrate()
        except BaseException:
            await self.close()
            raise

    async def reclaim(self) -> int:
        """Hand a bounded number of free pages back to the filesystem; returns how many.

        Deleted rows leave their pages on the freelist, and nothing gives them back on its own.
        A database that was created before incremental auto-vacuum was asked for cannot do this at
        all — it says so, once, naming the command that converts it, because the alternative is a
        file that quietly keeps hundreds of megabytes nothing will ever use again.
        """
        row = await self.fetchone("PRAGMA auto_vacuum")
        if row is None or int(row[0]) != 2:
            await self._warn_about_a_freelist_nothing_can_reclaim()
            return 0
        before = await self.fetchone("PRAGMA freelist_count")
        if before is None or not int(before[0]):
            return 0
        await self.execute(f"PRAGMA incremental_vacuum({VACUUM_PAGES})")
        after = await self.fetchone("PRAGMA freelist_count")
        return int(before[0]) - int(after[0]) if after is not None else 0

    FREELIST_WARNING_SHARE = 0.05
    """How much of the file may sit on an unreclaimable freelist before it is worth saying so."""

    async def _warn_about_a_freelist_nothing_can_reclaim(self) -> None:
        """A database made before the setting keeps it until a full VACUUM rewrites the file, and
        incremental reclaim silently does nothing there. Said once per process, with the command."""
        if self._warned_about_freelist:
            return
        self._warned_about_freelist = True
        free = await self.fetchone("PRAGMA freelist_count")
        size = await self.fetchone("PRAGMA page_count")
        pages, total = int(free[0]) if free else 0, int(size[0]) if size else 0
        if not total or pages < total * self.FREELIST_WARNING_SHARE:
            return
        page_size = await self.fetchone("PRAGMA page_size")
        bytes_free = pages * (int(page_size[0]) if page_size else 4096)
        logger.warning(
            "%.0f MB of %s (%d free pages) cannot be given back: this database was created before incremental auto-vacuum and keeps that setting until the file is rewritten. Run `daedalus db vacuum` to reclaim it.",
            bytes_free / 1e6,
            self.path,
            pages,
        )

    async def vacuum(self) -> tuple[int, int]:
        """Rewrite the whole file, converting it to incremental auto-vacuum; returns (before, after) bytes.

        This is the one-off that a database made before the setting needs, and the only way to
        give back a freelist that has already grown large. It holds the connection for the
        whole rewrite, which on a several-hundred-megabyte file is measured in seconds.
        """
        before = self.path.stat().st_size
        await self.execute("PRAGMA auto_vacuum=INCREMENTAL")
        await self.execute("VACUUM")
        await self.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return before, self.path.stat().st_size

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("database is not open")
        return self._conn

    async def _migrate(self) -> None:
        await self.conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
        row = await (await self.conn.execute("SELECT version FROM schema_version")).fetchone()
        current = int(row["version"]) if row else 0
        known_schema = len(MIGRATIONS)
        if current > known_schema:
            # A database written by a newer build. Opening it anyway works and fails later, at the
            # first write that touches a table the old code remembers differently — which is inside
            # the transaction that appends a message, so the bot runs, answers, and quietly stops
            # keeping any transcript at all.
            raise RuntimeError(
                f"the database is at schema {current} and this build knows {known_schema}: it was written by a newer version of Daedalus. "
                "Run the newer version, or restore the database from before the downgrade."
            )
        if BRANCH_BASE_SCHEMA < current <= BRANCH_BASE_SCHEMA + 2:
            tables = {item["name"] for item in await (await self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
                " AND name IN ('calendar_accounts', 'domain_collection_revisions')"
            )).fetchall()}
            if "calendar_accounts" in tables and "domain_collection_revisions" not in tables:
                # Calendar and orchestration each extended the same base schema. A database from
                # the calendar line must run the orchestration migrations it has not seen; the
                # final calendar migration preserves its already populated tables.
                current = BRANCH_BASE_SCHEMA
        for index, script in enumerate(MIGRATIONS, start=1):
            if index <= current:
                continue
            if callable(script):
                script = script(self)
            foreign_keys_off = isinstance(script, tuple) and script[1]
            if isinstance(script, tuple):
                script = script[0]
            # The version is written inside the migration's own transaction. Written after it, a
            # process killed in between would leave the schema at N and the version at N-1, and the
            # next start would run migration N again — on an ALTER TABLE, which is not idempotent,
            # that is an install that cannot open its own database and has no way back.
            version = f"INSERT INTO schema_version(version) VALUES ({index});" if current == 0 and index == 1 else f"UPDATE schema_version SET version = {index};"
            async with self._lock:
                if foreign_keys_off:
                    await self.conn.execute("PRAGMA foreign_keys=OFF")
                try:
                    await self.conn.executescript(f"BEGIN;\n{script}\n{version}\nCOMMIT;")
                except BaseException:
                    with suppress(Exception):
                        await self.conn.execute("ROLLBACK")
                    raise
                finally:
                    if foreign_keys_off:
                        await self.conn.execute("PRAGMA foreign_keys=ON")
            current = index

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        async with self._lock:
            await self.conn.execute(sql, tuple(params))

    class _Transaction:
        """Holds the connection lock across BEGIN … COMMIT so multi-statement writes are atomic."""

        def __init__(self, db: Database) -> None:
            self.db = db

        async def __aenter__(self) -> aiosqlite.Connection:
            await self.db._lock.acquire()
            try:
                await self.db.conn.execute("BEGIN IMMEDIATE")
            except BaseException:
                # A cancellation is a BaseException, and catching only Exception let a task cancelled
                # while its BEGIN was in flight keep the lock forever: every later query waited on it,
                # which hung a shutdown (and the test suite, about one run in ten) with nothing in the
                # log. The BEGIN still runs on the connection's thread after a cancellation, so a
                # ROLLBACK is queued behind it before the lock is let go; the next writer would
                # otherwise find a transaction already open.
                try:
                    with suppress(Exception):
                        await self.db.conn.execute("ROLLBACK")
                finally:
                    self.db._lock.release()
                raise
            return self.db.conn

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
            try:
                await self.db.conn.execute("ROLLBACK" if exc_type else "COMMIT")
            finally:
                self.db._lock.release()

    def transaction(self) -> Database._Transaction:
        return Database._Transaction(self)

    async def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> None:
        async with self._lock:
            await self.conn.executemany(sql, [tuple(r) for r in rows])

    async def fetchone(self, sql: str, params: Sequence[Any] = ()) -> aiosqlite.Row | None:
        async with self._lock:
            cursor = await self.conn.execute(sql, tuple(params))
            row = await cursor.fetchone()
            await cursor.close()
            return row

    async def fetchall(self, sql: str, params: Sequence[Any] = ()) -> list[aiosqlite.Row]:
        async with self._lock:
            cursor = await self.conn.execute(sql, tuple(params))
            rows = await cursor.fetchall()
            await cursor.close()
            return list(rows)

    # -- tiny KV for settings that are not worth a table ---------------------------

    async def kv_get(self, key: str, default: Any = None) -> Any:
        row = await self.fetchone("SELECT value FROM kv WHERE key = ?", (key,))
        return json.loads(row["value"]) if row else default

    async def kv_set(self, key: str, value: Any) -> None:
        await self.execute(
            "INSERT INTO kv(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value)),
        )


__all__ = ["Database", "MIGRATIONS"]
