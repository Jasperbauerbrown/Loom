-- 0004: the per-conversation file ledger behind the stale-write guard (05-backlog,
-- audit #2). `fs_read`, `fs_write`, `fs_edit` and `fs_patch` record what this
-- conversation last saw of a file; a later write refuses when the file on disk no
-- longer matches. A row, not memory, so a backend restart does not turn every next
-- write into a refusal.

CREATE TABLE file_reads (
    conversation_id TEXT NOT NULL REFERENCES conversations(id),
    path TEXT NOT NULL,             -- normalised absolute path (fs_guard.normalise)
    mtime_ns INTEGER NOT NULL,
    size INTEGER NOT NULL,
    seen_at TEXT NOT NULL,
    wrote INTEGER NOT NULL DEFAULT 0,  -- 1 once a writing tool left this file behind
    PRIMARY KEY (conversation_id, path)
);
