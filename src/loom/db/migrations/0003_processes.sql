-- 0003: background processes (03 §9.6, decided 2026-09-22). A long-running program the
-- model starts with `shell(background=true)` is a row here, written before the spawn
-- (persist before act), supervised by `loom.processes` while the backend runs, and
-- reconciled on the next start. Its output goes to `log_path` beside the conversation's
-- artifacts so the child survives a backend restart (a pipe would break it).

CREATE TABLE processes (
    id TEXT PRIMARY KEY,            -- ULID
    conversation_id TEXT NOT NULL REFERENCES conversations(id),
    title TEXT NOT NULL DEFAULT '',
    command TEXT NOT NULL,
    cwd TEXT NOT NULL,
    pid INTEGER,
    status TEXT NOT NULL,           -- starting | running | exited | stopped | failed | lost
    exit_code INTEGER,
    error TEXT,                     -- why it failed to start, or how it was stopped
    log_path TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT
);
CREATE INDEX processes_conversation ON processes(conversation_id);
CREATE INDEX processes_status ON processes(status);
