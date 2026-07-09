-- ---------------------------------------------------------------------------
-- Projects — the top-level container. Simmer is project 1.
-- ---------------------------------------------------------------------------
CREATE TABLE projects (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL UNIQUE,
    status          TEXT NOT NULL DEFAULT 'active',   -- active | paused | done
    priority        INTEGER NOT NULL DEFAULT 0,       -- higher = more important
    description     TEXT,
    created_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- ---------------------------------------------------------------------------
-- Tasks — concrete actionable items, linked to a project.
-- ---------------------------------------------------------------------------
CREATE TABLE tasks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id      INTEGER REFERENCES projects(id),
    title           TEXT NOT NULL,
    detail          TEXT,
    status          TEXT NOT NULL DEFAULT 'open',     -- open | in_progress | done | dropped
    is_next         INTEGER NOT NULL DEFAULT 0,       -- 1 = this is the next action
    created_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at    TEXT
);

CREATE INDEX idx_tasks_project_status ON tasks(project_id, status);
CREATE INDEX idx_tasks_next ON tasks(is_next) WHERE is_next = 1;

-- ---------------------------------------------------------------------------
-- Captured items — thoughts, links, follow-ups, anything the user dumps.
-- ---------------------------------------------------------------------------
CREATE TABLE captured_items (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    content         TEXT NOT NULL,
    source          TEXT NOT NULL DEFAULT 'telegram', -- telegram | voice | brief
    category        TEXT,                             -- thought | followup | idea | link | task_candidate
    project_id      INTEGER REFERENCES projects(id),
    processed       INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_captured_processed ON captured_items(processed, created_at);

-- FTS5 virtual table + triggers for keyword search on captured content.
CREATE VIRTUAL TABLE captured_items_fts USING fts5(
    content,
    content='captured_items',
    content_rowid='id'
);

CREATE TRIGGER captured_items_ai AFTER INSERT ON captured_items BEGIN
    INSERT INTO captured_items_fts(rowid, content) VALUES (new.id, new.content);
END;
CREATE TRIGGER captured_items_ad AFTER DELETE ON captured_items BEGIN
    INSERT INTO captured_items_fts(captured_items_fts, rowid, content) VALUES('delete', old.id, old.content);
END;
CREATE TRIGGER captured_items_au AFTER UPDATE ON captured_items BEGIN
    INSERT INTO captured_items_fts(captured_items_fts, rowid, content) VALUES('delete', old.id, old.content);
    INSERT INTO captured_items_fts(rowid, content) VALUES (new.id, new.content);
END;

-- ---------------------------------------------------------------------------
-- Blockers — "stuck on X because Y."
-- ---------------------------------------------------------------------------
CREATE TABLE blockers (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id      INTEGER REFERENCES projects(id),
    task_id         INTEGER REFERENCES tasks(id),
    description     TEXT NOT NULL,
    reason          TEXT,
    resolution_idea TEXT,
    status          TEXT NOT NULL DEFAULT 'open',     -- open | resolved
    created_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    resolved_at     TEXT
);

CREATE INDEX idx_blockers_status ON blockers(status, project_id);

-- Proactive time-based messages: one-shot reminders and recurring check-ins.
-- fire_at is UTC in CURRENT_TIMESTAMP format; is_prompt=1 means message_or_prompt
-- is a prompt for Claude rather than a literal message.
CREATE TABLE scheduled_triggers (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    kind                TEXT NOT NULL,                    -- reminder | checkin | recurring
    fire_at             TEXT NOT NULL,                    -- UTC 'YYYY-MM-DD HH:MM:SS'
    recurrence          TEXT,                             -- daily | weekly | NULL = one-shot
    message_or_prompt   TEXT NOT NULL,
    is_prompt           INTEGER NOT NULL DEFAULT 0,       -- 1 = generate via Claude
    related_project_id  INTEGER REFERENCES projects(id),
    status              TEXT NOT NULL DEFAULT 'pending',  -- pending | fired | cancelled
    created_at          TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_triggers_pending ON scheduled_triggers(status, fire_at);

-- ---------------------------------------------------------------------------
-- Workspace facts — long-term persistent context (the "long-term memory" layer).
-- ---------------------------------------------------------------------------
CREATE TABLE workspace_facts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    category        TEXT NOT NULL,                    -- project | preference | pattern | context | phase2_candidate
    content         TEXT NOT NULL,
    confidence      REAL NOT NULL DEFAULT 1.0,
    last_referenced TEXT,
    created_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_facts_category ON workspace_facts(category);

CREATE VIRTUAL TABLE workspace_facts_fts USING fts5(
    content,
    content='workspace_facts',
    content_rowid='id'
);

CREATE TRIGGER workspace_facts_ai AFTER INSERT ON workspace_facts BEGIN
    INSERT INTO workspace_facts_fts(rowid, content) VALUES (new.id, new.content);
END;
CREATE TRIGGER workspace_facts_ad AFTER DELETE ON workspace_facts BEGIN
    INSERT INTO workspace_facts_fts(workspace_facts_fts, rowid, content) VALUES('delete', old.id, old.content);
END;
CREATE TRIGGER workspace_facts_au AFTER UPDATE ON workspace_facts BEGIN
    INSERT INTO workspace_facts_fts(workspace_facts_fts, rowid, content) VALUES('delete', old.id, old.content);
    INSERT INTO workspace_facts_fts(rowid, content) VALUES (new.id, new.content);
END;

-- ---------------------------------------------------------------------------
-- Conversation log — every message in/out (the "working memory" layer).
-- ---------------------------------------------------------------------------
CREATE TABLE conversation_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    role            TEXT NOT NULL,                    -- user | assistant | tool_result
    content         TEXT NOT NULL,
    tool_calls      TEXT,                             -- JSON if assistant made tool calls
    tokens_in       INTEGER,
    tokens_out      INTEGER,
    cost_cents      REAL,
    created_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_convo_recent ON conversation_log(created_at DESC);

-- ---------------------------------------------------------------------------
-- Schedule intents — V1 captures intent only, does NOT touch Calendar.
-- ---------------------------------------------------------------------------
CREATE TABLE schedule_intents (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    description     TEXT NOT NULL,
    when_text       TEXT,
    duration_text   TEXT,
    attendees       TEXT,
    status          TEXT NOT NULL DEFAULT 'pending',  -- pending | scheduled | dropped
    project_id      INTEGER REFERENCES projects(id),
    created_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    scheduled_at    TEXT
);

-- ---------------------------------------------------------------------------
-- Daily briefs — record of each morning brief.
-- ---------------------------------------------------------------------------
CREATE TABLE daily_briefs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    brief_date      TEXT NOT NULL UNIQUE,
    content         TEXT NOT NULL,
    top_priority    TEXT,
    delivered_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- ---------------------------------------------------------------------------
-- Stall events — record of named stalls (so assistant doesn't repeat itself).
-- ---------------------------------------------------------------------------
CREATE TABLE stall_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id      INTEGER NOT NULL REFERENCES projects(id),
    description     TEXT NOT NULL,
    user_response   TEXT,
    resolved        INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_stall_project_recent ON stall_events(project_id, created_at DESC);
