-- 0001: initial schema (03-technical-design.md §4.2). Hand-written; applied by db/__init__.py.

CREATE TABLE settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,            -- json
    updated_at TEXT NOT NULL
);

CREATE TABLE providers (
    name TEXT PRIMARY KEY,
    kind TEXT NOT NULL,             -- ollama | openai_compatible
    base_url TEXT NOT NULL,
    api_key_ref TEXT,               -- secret name, or NULL
    params TEXT NOT NULL DEFAULT '{}',  -- json defaults (timeout_s, concurrency, ...)
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE presets (
    name TEXT PRIMARY KEY,
    provider TEXT NOT NULL REFERENCES providers(name),
    model TEXT NOT NULL,
    params TEXT NOT NULL DEFAULT '{}',  -- json: max_tokens, temperature, top_p, effective_ctx,
                                        -- num_ctx, keep_alive, timeout_s, reasoning, token_factor_seed
    notes TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE model_roles (
    role TEXT PRIMARY KEY,          -- desk | worker | maintenance | utility | embedding
    preset TEXT REFERENCES presets(name)
);

CREATE TABLE conversations (
    id TEXT PRIMARY KEY,            -- ULID
    kind TEXT NOT NULL,             -- desk | thread
    parent_id TEXT REFERENCES conversations(id),
    title TEXT NOT NULL DEFAULT '',
    goal TEXT NOT NULL DEFAULT '',
    scope_note TEXT,                -- vault path of the project note
    roots TEXT NOT NULL DEFAULT '[]',   -- json list of extra allowed roots
    depth INTEGER NOT NULL DEFAULT 0,
    role TEXT NOT NULL,             -- model role
    preset TEXT,                    -- optional override of the role's preset
    status TEXT NOT NULL DEFAULT 'idle',
        -- idle | running | waiting_approval | waiting_input | done | failed | archived
    step_budget INTEGER,
    summary TEXT NOT NULL DEFAULT '',
    summary_cutoff INTEGER NOT NULL DEFAULT 0,   -- events.id; live events are > cutoff
    token_factor REAL NOT NULL DEFAULT 1.0,
    maint_watermark INTEGER NOT NULL DEFAULT 0,  -- events.id
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    finished_at TEXT,
    archived_at TEXT
);
CREATE INDEX conversations_parent ON conversations(parent_id);
CREATE INDEX conversations_status ON conversations(status);

CREATE TABLE events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL REFERENCES conversations(id),
    kind TEXT NOT NULL,
        -- user | assistant | tool_call | tool_result | thinking | system | compaction | status
    content TEXT NOT NULL DEFAULT '',
    meta TEXT NOT NULL DEFAULT '{}',    -- json
    ts TEXT NOT NULL
);
CREATE INDEX events_conversation ON events(conversation_id, id);

CREATE VIRTUAL TABLE events_fts USING fts5(content, content='events', content_rowid='id');
CREATE TRIGGER events_fts_ai AFTER INSERT ON events
    WHEN new.kind IN ('user', 'assistant', 'tool_result')
BEGIN
    INSERT INTO events_fts(rowid, content) VALUES (new.id, new.content);
END;

CREATE TABLE inbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL REFERENCES conversations(id),
    source TEXT NOT NULL,           -- user | conversation:<id> | schedule:<id> | connector:<name> | system
    content TEXT NOT NULL,
    meta TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    delivered_at TEXT
);
CREATE INDEX inbox_pending ON inbox(conversation_id, delivered_at);

CREATE TABLE approvals (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id),
    event_id INTEGER NOT NULL,      -- the tool_call event
    call_id TEXT NOT NULL,          -- the model's tool call id within that event
    tool TEXT NOT NULL,
    args TEXT NOT NULL,             -- json (redacted)
    reason TEXT NOT NULL DEFAULT '',
    options TEXT NOT NULL DEFAULT '{}', -- json e.g. {"add_root": "C:/..."}
    status TEXT NOT NULL DEFAULT 'pending',  -- pending | allowed | denied
    decision TEXT,                  -- allow | deny | allow_always
    decided_at TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX approvals_status ON approvals(status);

CREATE TABLE items (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,             -- notification | setup | thread_event | schedule_result | knowledge
    title TEXT NOT NULL,
    body TEXT NOT NULL DEFAULT '',
    conversation_id TEXT,
    source TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL DEFAULT '{}',  -- json
    created_at TEXT NOT NULL,
    seen_at TEXT,
    dismissed_at TEXT
);

CREATE TABLE schedules (
    id TEXT PRIMARY KEY,
    spec TEXT NOT NULL,
    target TEXT NOT NULL,           -- conversation id or 'desk'
    payload TEXT NOT NULL,
    policy TEXT,                    -- interrupt | queue | NULL (setting default)
    created_by TEXT NOT NULL,       -- user | conversation:<id>
    enabled INTEGER NOT NULL DEFAULT 1,
    next_run_at TEXT,
    last_run_at TEXT,
    runs INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE artifacts (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    event_id INTEGER,
    path TEXT NOT NULL,
    bytes INTEGER NOT NULL,
    head TEXT NOT NULL DEFAULT '',
    tail TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX artifacts_conversation ON artifacts(conversation_id);

CREATE TABLE tool_policies (
    tool TEXT PRIMARY KEY,
    mode TEXT NOT NULL,             -- allow | ask | classify
    updated_at TEXT NOT NULL
);

CREATE TABLE roots (
    path TEXT PRIMARY KEY,
    added_at TEXT NOT NULL,
    added_by TEXT NOT NULL          -- bootstrap | user | approval:<id>
);

CREATE TABLE protected_paths (
    path TEXT PRIMARY KEY
);

CREATE TABLE mcp_servers (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    transport TEXT NOT NULL,        -- stdio | http
    command TEXT,
    args TEXT NOT NULL DEFAULT '[]',    -- json list
    url TEXT,
    env TEXT NOT NULL DEFAULT '{}',     -- json; values may be {{secret:X}}
    enabled INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'disconnected',  -- disconnected | connecting | connected | error
    last_error TEXT,
    tools_cache TEXT,               -- json
    cached_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE connectors (
    name TEXT PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 1,
    config TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'disabled',  -- disabled | loaded | error
    last_error TEXT,
    updated_at TEXT NOT NULL
);

-- knowledge index (cache; rebuildable from vault/)
CREATE TABLE notes (
    path TEXT PRIMARY KEY,          -- vault-relative, forward slashes
    type TEXT NOT NULL,             -- folder name or frontmatter type; 'invalid' on parse error
    title TEXT NOT NULL,
    aliases TEXT NOT NULL DEFAULT '[]',
    modified TEXT,
    mtime REAL NOT NULL,
    size INTEGER NOT NULL,
    hash TEXT NOT NULL,
    core_order INTEGER,
    embedding BLOB
);

CREATE TABLE aliases (
    alias TEXT NOT NULL,            -- normalised
    path TEXT NOT NULL
);
CREATE INDEX aliases_alias ON aliases(alias);
CREATE INDEX aliases_path ON aliases(path);

CREATE TABLE links (
    src TEXT NOT NULL,
    dst TEXT NOT NULL,              -- resolved path, or the raw target if dangling
    resolved INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX links_src ON links(src);
CREATE INDEX links_dst ON links(dst);

CREATE VIRTUAL TABLE notes_fts USING fts5(path UNINDEXED, title, body);
