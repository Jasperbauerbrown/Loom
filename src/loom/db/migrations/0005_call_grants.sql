-- 0005: "allow this call for the conversation" (05-backlog, TD audit F1). A third
-- approval answer between allow-once and allow_always: the same tool with the same
-- arguments runs without asking again in this conversation only.

CREATE TABLE call_grants (
    conversation_id TEXT NOT NULL REFERENCES conversations(id),
    tool TEXT NOT NULL,
    args_hash TEXT NOT NULL,        -- sha256 of db.dumps(args) (sorted keys)
    approval_id TEXT,               -- the approval that granted it
    created_at TEXT NOT NULL,
    PRIMARY KEY (conversation_id, tool, args_hash)
);
