# Loom — technical design

**Status:** draft 2026-09-20, for hand-off to an implementation session. This document
turns `00-charter.md` into buildable detail. Where it says *decided*, build it that way;
where it says *lean*, use judgment but stay within the ethos (§2 of the charter).
Section numbers in the form §5.x refer to the charter.

The project is called **Loom** (provisional, 2026-09-20): package `loom`, CLI `loom`,
home directory `APP_HOME` (default `~/.loom`, override `LOOM_HOME`). If the name changes,
rename these identifiers mechanically; prose says "Loom" or "the assistant"
interchangeably.

**Rules for the implementer, restated:** every feature is built to its full reasonable
extent (every setting in the UI, every tool argument that obviously belongs). No stubs
presented as done. When the code and this document disagree, update this document.

---

## 1. System overview

```
                 ┌──────────────────────────── APP_HOME ────────────────────────────┐
                 │  state.db (SQLite, WAL)   vault/ (git)   artifacts/   releases/  │
                 └───────▲──────────────────────▲────────────────▲───────────▲──────┘
                         │                      │                │           │
┌────────────┐   ┌───────┴──────────────────────┴────────────────┴──────┐    │
│ supervisor │──►│                      backend (one process)           │    │
│ stdlib only│   │  surface API (REST+WS) ── conversation runner ── ... │    │
│ start/stop │   │  model client (priority queue) ── tool dispatcher    │    │
│ promote    │   │  knowledge index + watcher ── scheduler ── MCP client│    │
│ rollback   │   │  connector loader ── maintenance                     │    │
└─────▲──────┘   └───────▲───────────────────────────▲──────────────────┘    │
      │                  │ http/ws                   │ http                  │
      │           ┌──────┴──────┐  ┌──────┐   ┌──────┴───────┐   ┌──────────┴──┐
   promote tool   │   web UI    │  │ CLI  │   │ Ollama /     │   │ git worktree│
   (from a thread)│ (served by  │  │      │   │ OpenRouter   │   │ per release │
                  │  backend)   │  └──────┘   └──────────────┘   └─────────────┘
                  └─────────────┘
```

Two processes: a tiny **supervisor** and the **backend**. Everything else (indexer,
scheduler, MCP client, connectors) runs inside the backend as asyncio tasks. Surfaces
are clients of the backend's one API. Models are external HTTP services.

## 2. Repository layout

```
loom/
  pyproject.toml            # uv-managed, Python 3.12
  supervisor/               # stdlib only; never imports the package below
    __main__.py
  src/loom/
    __main__.py  cli.py  config.py  ids.py (ULID)  log.py
    db/            schema.sql  migrate.py  migrations/NNNN_*.sql
    models/        client.py (OpenAI-compatible + Ollama extras)  queue.py  roles.py  embed.py
    conversation/  runner.py (run_turn)  context.py (assemble)  compaction.py  inbox.py  status.py
    knowledge/     vault.py  index.py  resolve.py  watcher.py  git.py  maintenance.py
    tools/         registry.py  dispatch.py  permissions.py  shell_prefilter.py  fs_guard.py
                   native/ (fs.py shell.py http.py knowledge.py conversation.py schedule.py artifacts.py search.py)
    connectors/    loader.py  base.py  mcp/ (client.py transports.py)
    triggers/      scheduler.py  router.py  items.py
    server/        app.py  ws.py  routers/ (conversations approvals items vault settings system)
    secrets.py     artifacts.py  promote.py
  prompts/         *.md (all model-facing text; §16)
  connectors/      # native connector packages discovered at start (may be empty)
  ui/              # React + Vite + TS; builds into ui-dist/ (committed)
  ui-dist/
  tests/
  docs/
```

## 3. Processes

### 3.1 Supervisor
- Stdlib-only Python, ~200–300 lines, **protected path** (§15).
- Reads `APP_HOME/current` (a text file holding a release id) and runs
  `releases/<id>/` as the backend with `python -m loom serve`.
- Loopback HTTP on `port+1`, authenticated by a random token in `APP_HOME/supervisor.token`
  (owner-only permissions). Commands: `status`, `restart`, `promote {ref}`, `rollback`.
- Restarts the backend on crash with exponential backoff (1s → 60s cap); after 5 crashes
  in 10 minutes on a freshly promoted release, rolls back automatically.
- Windows: the backend is started with `CREATE_NEW_PROCESS_GROUP` and its tree is killed
  with `taskkill /T /F /PID` (decided 2026-09-20: a Job Object needs ctypes plumbing in
  the one file whose point is to be small and obviously correct). POSIX: `start_new_session`
  plus process-group signals.
- `promote`/`rollback` in the supervisor are the *process* half only: stop, switch
  `current`/`previous`, start, watch `/api/system/health` for
  `promotion_watchdog_seconds`, revert if it does not hold. The worktree, snapshot and
  probe steps of §15.2 are the self-modification package's and have named seams
  (`snapshot`, `create_release`, `probe`) that raise until it lands.
- `hydra.bat`-style launchers are replaced by `loom` (supervisor entry) for end users
  and `python -m loom serve` for development (no supervisor).

### 3.2 Backend
- FastAPI + uvicorn, single process, asyncio. Binds `127.0.0.1:8500` by default; LAN
  binding and a bearer token are settings.
- Startup order: migrate DB → load settings → init secrets → init model client → load
  vault index (rebuild if stale) → start watcher → load connectors → connect MCP servers
  → start scheduler → resume interrupted conversations (§6.3 step 0) → serve.
- Shutdown: stop accepting turns, let in-flight tool calls finish (bounded 10s),
  persist, exit. Persist-before-act (§4) makes abrupt kills safe.

### 3.3 First run (decided)
Simplicity ethos: from install to a working desk in under a minute, one required
choice. On `loom serve` with no `state.db`:
1. Create `APP_HOME`, migrate, insert the desk row, `git init` the vault and write the
   `core/` templates, write default settings, tool policies and protected paths.
2. Probe providers: local Ollama at `http://127.0.0.1:11434` (and `OLLAMA_HOST` if
   set); list models. Detect timezone from the OS. Default `projects_root` = `~/Projects`
   (created on first use, not now).
3. Print the URL and open the browser to the **Setup** screen — shown only while no
   `desk` role is configured. One page: provider (detected Ollama pre-selected; "add
   OpenRouter key" expands to a base URL + key field), **model** (the one required
   choice; picked from the probed list; saved as a preset with sensible defaults and
   assigned to the desk, worker and maintenance roles), embedding
   model (auto-selected if `nomic-embed-text` is present, otherwise the exact `ollama
   pull` command is shown and embeddings stay off until it appears), timezone (confirm),
   projects root (confirm). Finish → the Desk, with a first item on the now board:
   "Tell me about yourself, or point me at something to learn."
4. The CLI works the moment a model is set; before that, `loom send` returns a clear
   "no model configured — open <url>" error.
Everything on the Setup screen is an ordinary setting and can be changed later in
Settings; the screen never reappears unless the desk role is cleared.

## 4. Storage

### 4.1 APP_HOME layout
```
~/.loom/
  state.db  state.db-wal      # system of record
  vault/                      # git repo; markdown truth (§4.3)
  artifacts/<conv_id>/<event_id>.txt   # spilled tool output
  secrets.json                # fallback store only (§4.5)
  releases/<id>/              # git worktrees of the assistant's own repo (§15)
  current  previous           # release pointers (`previous` is what rollback returns to)
  snapshots/<ts>/state.db     # pre-promotion snapshots
  logs/backend.log            # rotating
  supervisor.token
```

### 4.2 SQLite schema (decided)
`sqlite3` stdlib, WAL mode, `foreign_keys=ON`. Migrations are numbered SQL files
applied by a ~50-line migrator keyed on `PRAGMA user_version`. No ORM.

```sql
settings(key TEXT PK, value TEXT /*json*/, updated_at TEXT)

providers(name TEXT PK, kind TEXT /*ollama|openai_compatible*/, base_url TEXT,
          api_key_ref TEXT, params TEXT /*json defaults*/, enabled INT)
presets(name TEXT PK, provider TEXT, model TEXT,
        params TEXT /*json: max_tokens, temperature, top_p, effective_ctx, num_ctx,
        keep_alive, timeout_s, reasoning (on|off|budget), token_factor_seed*/,
        notes TEXT, enabled INT, created_at TEXT, updated_at TEXT)
model_roles(role TEXT PK /*desk|worker|maintenance|utility|embedding*/, preset TEXT)

conversations(id TEXT PK /*ULID*/, kind TEXT /*desk|thread*/, parent_id TEXT,
              title TEXT, goal TEXT, scope_note TEXT /*vault path of project note*/,
              roots TEXT /*json list*/, depth INT, role TEXT /*model role*/,
              preset TEXT /*optional override of the role's preset*/,
              status TEXT /*idle|running|waiting_approval|waiting_input|done|failed|archived*/,
              step_budget INT, summary TEXT, summary_cutoff INT /*events.id*/,
              token_factor REAL, maint_watermark INT /*events.id*/,
              created_at, updated_at, finished_at, archived_at)

events(id INTEGER PK, conversation_id TEXT, kind TEXT
       /*user|assistant|tool_call|tool_result|thinking|system|compaction|status*/,
       content TEXT, meta TEXT /*json*/, ts TEXT)              -- append-only
events_fts  -- FTS5 external-content table over events.content for user/assistant/tool_result

inbox(id INTEGER PK, conversation_id TEXT, source TEXT
      /*user|conversation:<id>|schedule:<id>|connector:<name>|system*/,
      content TEXT, meta TEXT, created_at TEXT, delivered_at TEXT)

approvals(id TEXT PK, conversation_id TEXT, event_id INT /*the tool_call*/,
          tool TEXT, args TEXT, reason TEXT, options TEXT /*json e.g. add_root*/,
          status TEXT /*pending|allowed|denied*/, decided_at TEXT, created_at TEXT)

items(id TEXT PK, kind TEXT /*notification|setup|thread_event|schedule_result*/,
      title TEXT, body TEXT, conversation_id TEXT, source TEXT, action TEXT /*json*/,
      created_at TEXT, seen_at TEXT, dismissed_at TEXT)        -- the "now" board

schedules(id TEXT PK, spec TEXT, target TEXT /*conversation id or 'desk'*/,
          payload TEXT, policy TEXT /*interrupt|queue*/, created_by TEXT,
          enabled INT, next_run_at TEXT, last_run_at TEXT, runs INT)

artifacts(id TEXT PK, conversation_id TEXT, event_id INT, path TEXT, bytes INT,
          head TEXT, tail TEXT, created_at TEXT)

tool_policies(tool TEXT PK, mode TEXT /*allow|ask|classify*/, updated_at TEXT)
roots(path TEXT PK, added_at TEXT, added_by TEXT)
protected_paths(path TEXT PK)

mcp_servers(id TEXT PK, name TEXT, transport TEXT /*stdio|http*/, command TEXT,
            args TEXT, url TEXT, env TEXT /*json, values may be {{secret:X}}*/,
            enabled INT, status TEXT, last_error TEXT, tools_cache TEXT, cached_at TEXT)
connectors(name TEXT PK, enabled INT, config TEXT /*json*/, status TEXT,
           last_error TEXT, updated_at TEXT)

-- knowledge index (cache, rebuildable from vault/)
notes(path TEXT PK, type TEXT, title TEXT, aliases TEXT /*json*/, modified TEXT,
      mtime REAL, size INT, hash TEXT, core_order INT, embedding BLOB /*float32*/)
aliases(alias TEXT /*normalised*/, path TEXT)     -- index on alias
links(src TEXT, dst TEXT)
notes_fts  -- FTS5 over title + body
```

### 4.3 Vault (markdown truth)
`APP_HOME/vault/` is a git repository. Folder = entity type. Decided layout:

```
vault/
  core/           about-me.md  preferences.md  conventions.md   (always in context, §7)
  devices/  projects/  people/  connectors/  courses/  topics/   (one note per entity)
  procedures/<name>/SKILL.md [+ scripts/, references/]           (Agent Skills format)
  log/YYYY-MM-DD.md                                              (optional daily notes, agent-written)
```

Note format:
```markdown
---
title: Ender 3
aliases: [ender 3, the printer, 3d printer]
type: device            # optional; defaults from folder
tags: [lan, octoprint]
root: C:/Users/me/Projects/music-bot   # projects only: filesystem root
modified: 2026-09-20T14:02:11Z         # harness-stamped on every write
---
Body. Wikilinks like [[ender-3-control]] resolve by filename, then title, then alias.
```

Rules enforced by `knowledge_write` (not by the model): frontmatter is parsed and
re-emitted (PyYAML), `modified` is stamped, `aliases` are lower-cased and de-duplicated,
relative dates in the body are rejected with an error result ("use absolute dates").

**Status block** (projects; owned by the working thread):
```markdown
## Status
<!-- status:begin owner=01J8… -->
state: working            # planning|working|blocked|waiting|done
goal: …
done: …                   # bullets allowed
doing: …
next: …
blockers: …
updated: 2026-09-20T14:02Z
<!-- status:end -->
```
Written only through the `status_update` tool (§9.2), which formats the block; the
user may edit it in the UI or Obsidian, and the thread re-reads it every turn (§7 slot
[5]). This block is the plan (charter §5.3, takeaways §2).

**Procedures** are Agent Skills: `SKILL.md` with `name` + `description` frontmatter;
body loaded on demand via `procedure_load`; scripts run through `shell`, never read into
context unless asked. Third-party skills are installed by copying a folder; the watcher
detects it and posts a *setup* item asking the user to approve because skills may carry
code; unapproved skills are indexed but not loadable. A procedure the agent writes
itself (`knowledge_write` with the harness as actor) is stamped `approved: <iso>` at
write time — the approval gate exists for code that arrived from outside.

### 4.4 Artifacts
Tool output longer than `tool_output_cap` (default 8,000 chars) is written to
`artifacts/<conv>/<event>.txt`; the tool result carries head (2,000) + tail (1,000) +
`[artifact <id>: N chars — artifact_recall(id, offset, limit) for more]`. Artifacts are
never deleted while their conversation exists; archived conversations' artifacts are
pruned by maintenance after `artifact_retention_days` (default 90).

### 4.5 Secrets
`secrets.py` exposes `get(name) / set(name, value) / delete / list_names`. Backend:
`keyring` (Windows Credential Manager, macOS Keychain, Secret Service). Fallback when
no keyring backend is available (headless Linux): `APP_HOME/secrets.json` with 0600
permissions and a persistent warning banner in Settings. *Deviation from charter:* the
fallback is not encrypted; the stdlib has no AEAD cipher and importing `cryptography`
for one file violates the dependency rule. Documented, UI-visible.

Values never reach the model: tool arguments and MCP env values may contain
`{{secret:NAME}}`, substituted by the dispatcher immediately before execution and
redacted (`{{secret:NAME}}` restored) in the persisted `tool_call` event.

### 4.6 Settings
One `settings` table (json values) behind a typed schema in `config.py` (a dataclass
per group with defaults, types, ranges, and a `ui` hint). `GET /api/settings` returns
values *and* schema so the UI renders every setting from the schema — no hand-built
forms, no setting that exists without a UI. Provider/role/policy/root/MCP/connector
rows are separate tables with their own endpoints but are rendered on the same
Settings screen (§13.2). The bootstrap on first run writes defaults; there is no
config file to edit.

## 5. Model client

### 5.1 Providers
One client speaking OpenAI-compatible `/v1/chat/completions` (streaming SSE) with
`tools`. Two provider kinds differ only in URL, auth header, and extras:
- **ollama**: base `http://host:11434`, native `/api/chat` (streaming NDJSON, `tools`,
  `think`, `options.num_ctx` from `effective_ctx`, `keep_alive`). Decided 2026-09-20:
  no probe; the OpenAI-compatible path does not accept `options`. Embeddings via
  `/api/embed`; model list via `/api/tags`.
- **openai_compatible**: base URL + `Authorization: Bearer {{secret:…}}`. Covers
  OpenRouter and any hosted endpoint.

`httpx.AsyncClient`, per-provider timeout (default 600s streaming idle 120s), no SDK.

### 5.2 Providers → presets → roles (decided; Hydra's layering, kept)
Three layers, all UI-managed:
- **Provider**: a reachable endpoint — `local-ollama`, `macmini-ollama`, `openrouter`,
  any number of rows, each with kind, base URL, credential reference, default params
  (e.g. `timeout_s: 1200` for a host that swaps models) and `concurrency`.
- **Preset**: a named model configuration on a provider — `qwen36-35b-local`,
  `qwen36-27b-macmini`, `sonnet-openrouter` — carrying model id and every generation
  and window parameter (`max_tokens`, `temperature`, `top_p`, `effective_ctx`,
  `num_ctx`, `keep_alive`, `timeout_s`, reasoning mode) plus the machine-written
  `token_factor_seed`. Presets are reusable: many roles and threads may point at one.
  Params merge provider defaults < preset < per-call overrides, key-wise.
- **Role**: `desk`, `worker`, `maintenance`, `utility`, `embedding` → one preset each.
  Defaults after first run: desk/worker/maintenance → the preset created from the
  chosen bar model; utility → unset (falls back to desk); embedding → a preset on
  `nomic-embed-text` if present.
A conversation resolves its model as `conversation.preset` (explicit override) else
`model_roles[conversation.role]`. Threads may be created with a `preset` override
("build this on the Mac mini 27b"). Changing a role's preset takes effect on the next
model call; no restart. `GET /models?provider=` probes a provider's live model list so
the preset form offers real names.

### 5.3 Priority queue and preemption
All model calls pass through one asyncio queue keyed by provider. Priority: `0`
interactive (desk turns; thread turns triggered by a user message), `1` background
(thread steps from triggers or parent messages, classifier calls for background
conversations), `2` maintenance. A provider serves one request at a time by default
(`provider.concurrency`, default 1 for Ollama, 4 for hosted). Preemption is at
call boundaries only; background calls carry `max_tokens = min(role.max_tokens,
background_max_tokens)` (default 2,048) so an interactive call waits at most one
bounded generation. Queue depth and current occupant are exposed at `/api/system/queue`
and in the UI header.

### 5.4 Streaming and channels
The client yields `delta(channel=content|reasoning, text)`, `tool_call(...)`, `usage`,
`finish(reason)`. Reasoning arrives as the provider's `reasoning`/`reasoning_content`
field or as inline `<think>…</think>`; a splitter normalises both. The runner streams
both channels to surfaces and persists them separately (§6.3). On the way *out*,
an assistant message's replayed `reasoning` (§6.3) is rendered as `message.thinking`
for the `ollama` kind and `reasoning_content` for `openai_compatible`, and is omitted
when empty; `tool_calls[].function.arguments` is sent as an object to Ollama and as a
JSON string to OpenAI-compatible endpoints. One transformation, in the client.

### 5.5 Outcomes and retries
Every call ends in one of: `ok`, `transient` (network, timeout, 5xx, 408/409/425/429,
malformed stream, empty completion), `overflow` (any 4xx whose body mentions a context
or length limit, or `finish=length` with zero output), `fatal` (401/403, 404 model or
endpoint missing, unknown provider or preset, any other 4xx — a request we shaped wrong
will not succeed on a retry). The overflow test runs first, because a 400 about context
is an overflow whatever else it is. Per-provider timeouts: `timeout_s` total (default
600) and `stream_idle_s` between chunks (default 120); both end the call as `transient`. Policy: transient → retry once with
the same request; overflow → force-compact (§6.5) and rebuild, at most
`forced_compactions_per_turn` (2); fatal → try `fallback_role` if set, else end the
turn with a visible `system` event and status `failed`.

### 5.6 Embeddings
`embed(texts) -> list[float32 array]` through the embedding role. Stored as BLOB in
`notes.embedding`. Cosine search is brute force: `math.sumprod` over `array('f')`
vectors (Python 3.12), normalised at write time. Thousands of notes → tens of
milliseconds. No vector database, no numpy.

## 6. The conversation primitive

### 6.1 Rows and states
One table for desk and threads (§4.2). Exactly one row has `kind='desk'`; it is
created at bootstrap and cannot be archived. States:

```
idle ──► running ──► idle
            │ approval needed ──► waiting_approval ──(decision)──► running
            │ thread asked user ─► waiting_input ────(inbox)─────► running
            │ finish tool ───────► done ──(inactivity N days)────► archived
            └ fatal ─────────────► failed ──(new message)────────► running
```
`done`/`failed` conversations remain addressable: a new inbox message reopens them as
`running`. Every transition emits `conversation.updated`.

### 6.2 Inbox
All input arrives as `inbox` rows: user messages from any surface, messages from other
conversations (`conversation.send`), schedule firings, connector events. A row is
*claimed* (rendered into the prompt) at turn start and again between tool steps;
rows are marked `delivered_at` only when the turn completes successfully, so a crashed
turn redelivers. Non-user sources render with a one-line header
(`[from thread "3D map" 01J8…]`, `[schedule "weekly homework"]`) as user-role messages.
A wake on a `running` conversation is a no-op: the row is picked up mid-turn.
A row's `user` event is written the moment it is claimed (persist before act); a turn
that ends early (approval pause, crash) leaves the row undelivered, and the next turn
claims it again without re-rendering it (the claim query reports the persisted event
id). So a message is never lost and never duplicated.

### 6.3 `run_turn(conversation_id)` — the only agent loop
```
0  lock conversation (per-id asyncio lock; if held → return)
1  repair: if the transcript ends with tool_call(s) lacking tool_result:
     - if an approval for it is pending → return (wait)
     - if approval decided → execute (or synthesise "denied by user") → persist results
     - else synthesise "interrupted — result lost" results               (takeaways §2)
2  claim inbox → if empty and no repair happened → unlock, return
3  status=running; ctx = assemble(conv, claimed)                            (§7)
4  loop:
     a  budget line refreshed (threads)                                     (§7 slot [6])
     b  call model(role, ctx, tools, priority) streaming; emit deltas
     c  outcome handling                                                     (§5.5)
     d  persist thinking event (full), then assistant or tool_call event
     e  if no tool calls → break
     f  dispatch tool calls                                                  (§9.3)
          read-only calls run concurrently (gather, results in call order);
          others sequentially; an `ask` → persist approval, status=waiting_approval,
          emit approval.requested, unlock, RETURN (resume re-enters at step 1)
     g  persist tool_result events; append to ctx (bounded, spilled)
     h  claim inbox mid-turn → append as user messages
     i  brakes: in-flight estimate ≥ limit → force compact + rebuild ctx;
        steps ≥ step_budget → one final call with tools disabled, then break
5  post-turn (clean turns only):
     calibrate token_factor from usage                                       (§6.6)
     maybe_compact                                                           (§6.5)
     threads: status safety net — if no status_update this turn, run one
              utility/worker call producing the block from the turn's events
     mark inbox delivered; status = idle (or done if finish was called)
     threads: emit thread_event to parent inbox + item if state changed      (§6.7)
6  unlock
```
Reasoning replay within the turn: each step's reasoning is fed back into subsequent
steps of the *same* turn, head-capped at `reasoning_replay_max_chars` (4,000); never
replayed across turns; counted at zero in occupancy.

### 6.4 Budgets and brakes
- `step_budget`: desk 25, threads 60 (settings). Threads see
  `[steps remaining: N]`; the last step has tools disabled.
- `forced_compactions_per_turn` 2; `model_error_retries` 1.
- Tool execution timeout per tool (default 120s shell, 60s http, 300s MCP); a timed-out
  call returns an error result.
- Cancellation: `POST /conversations/{id}/cancel` sets a flag checked between steps and
  cancels the in-flight HTTP stream; the partial assistant text is persisted with
  `meta.cancelled=true`.

### 6.5 Compaction ladder (decided, from Hydra/Manus)
```
occupancy = chars(all messages + tool schemas as sent) / 4 × token_factor   (prefix included)
limit     = min(effective_ctx × compact_threshold(0.75),
                effective_ctx − min(max_tokens, effective_ctx / 2))
```
When `occupancy ≥ limit` (between turns; forced mid-turn on overflow):
1. **Evict** (reversible): replace the bodies of `tool_result` events older than the
   newest 30% with `[evicted; artifact_recall(<id>)]` stubs, spilling to artifacts if
   not already spilled. Recompute. If under limit, stop.
2. **Fold** (lossy): summarise the oldest 70% of remaining live events into `summary`
   (recursive: old summary + folded events → new summary) using the `maintenance` role
   and `prompts/summarize.md`. The prompt keeps: decisions, open problems, explicit user
   instructions, paths/identifiers, plan state, **and failures** (Manus). Raw tool
   output is not shown to the summariser; thinking excerpts (≤500 chars) are, so
   tool-only steps keep their intent. Write a `compaction` event with
   `folded_through`/`kept_from` ids; the fold boundary never splits a
   tool_call/tool_result pair.
3. **Fail loudly**: if two folds in a row did not reduce occupancy, end the turn with a
   visible error rather than thrash.
Transcript rows are never mutated; eviction and folding are applied at read time
(the replay builder consults `compaction` events). Anti-thrash and headroom math are
exactly Hydra's; they are proven on Ollama.

### 6.6 Calibration
After each clean turn with reported `prompt_tokens`:
`factor = prompt_tokens / (chars_in_prompt / 4)`, clamped [0.5, 4.0], stored on the
conversation. A reading at ≥98% of `effective_ctx` can only raise the factor. New
conversations seed from their preset's `token_factor_seed`, which each calibration
writes back (the factor is a property of model + content, so the preset is its home).

### 6.7 Threads
- **Spawn** (`conversation.create`): payload = `goal`, optional `scope_note` (project
  note path; created first if missing, with `root` under `projects_root` setting),
  optional `roots`, `excerpt` (model-selected text from the parent), optional `preset`
  override (a preset name; validated against enabled presets).
  `depth = parent.depth + 1`; refused above `max_thread_depth` (default 3).
  Row written and `conversation.created` emitted **before** the first turn is scheduled.
- **Scope in context**: the project note (with status block) is slot [5] every turn.
- **Status**: `status_update(fields)` rewrites the block; the safety net (§6.3 step 5)
  guarantees at most one turn of staleness.
- **Events to parent**: on state change (`blocked`, `waiting`, `done`, `failed`) the
  runner writes an inbox row to `parent_id` with a header and a summary, plus an `item`
  (policy from settings: thread events default *interrupt* → notification). The parent
  verifies by judgment (charter §5.7); nothing else fires.
- **Finish** (`conversation.finish(summary, walkthrough)`): writes the walkthrough as a
  linked note `projects/<name>/walkthrough-YYYY-MM-DD.md` (what changed, how verified,
  artifact ids of test output), sets status `done`, emits to parent.
- **User in a thread**: the same surface calls as the desk; user messages set priority 0
  for that turn.
- **Turn end without finish** (decided 2026-09-20): a thread turn that ends with plain
  text (a question, a report, or the wrap-up after the step budget) sets status
  `waiting_input` and forwards that text to the parent as a thread event. The parent
  decides by judgment whether to answer, redirect, or involve the person.
- **Archival**: maintenance archives `done`/`failed` threads idle for
  `thread_archive_days` (14). Archived threads stay searchable (FTS) and reopenable.

## 7. Context assembly (`assemble`, the only builder)

| Slot | Content | Stability |
|---|---|---|
| [1] | `prompts/system.md` (soul + operating rules) + `prompts/kind-desk.md` or `kind-thread.md` | stable |
| [2] | One line: "Tools are provided via the API; use `tool_search` for more." (schemas travel in the `tools` parameter) | stable |
| [3] | Core blocks: `vault/core/*.md` in `core_order`, each hard-capped (`core_block_chars`, default 1,500) — overflow is truncated with a visible marker, never silently | stable within a session; changes only when the user/agent edits core |
| [4] | Procedures index: one line per `SKILL.md` (`name — description`) | stable |
| — | *end of cache-friendly prefix* | |
| [5] | Desk: the **now board** (unseen items, running threads with one-line status). Thread: the **project note** including status block | volatile |
| [6] | `now: 2026-09-20 14:02 (America/…)` + threads: `[steps remaining: N]` | volatile |
| [7] | `summary` (if any) | volatile |
| [8] | Live events since `summary_cutoff`, replayed in order; `thinking`/`system`/`status` kinds skipped; evictions applied | volatile |
| [9] | Resolved entities for this input (§8.2–8.3), wrapped `<knowledge may_be_irrelevant="true">…</knowledge>`; ephemeral, never persisted | volatile |
| [10] | Claimed inbox messages as user-role messages | volatile |

Slots [1]–[4] are one system message and byte-stable across turns (no dates, sorted
keys) for Ollama's prompt cache. Everything volatile is after it, nearest the query.

## 8. Knowledge

### 8.1 Index
Built from `vault/` into the `notes/aliases/links/notes_fts` tables + embeddings.
- Full rebuild: `loom reindex` and at startup when any `mtime/size` differs from
  the table or the table is empty.
- Incremental: the watcher (§8.8) re-parses changed files; embeddings recomputed only
  when `hash` changes, and drained by a background task so a vault write never waits on
  the embedding model (the index is correct without vectors).
- Parsing: frontmatter via PyYAML; wikilinks via regex `\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]`;
  link resolution order filename stem → title → alias, where a procedure's stem is its
  folder name (`[[pla-print]]` → `procedures/pla-print/SKILL.md`); unresolved links kept
  as dangling rows (visible in the vault UI as "wanted notes").
- `notes_fts` content = title + body (frontmatter stripped).
- `core_order` from `core/` filenames sorted (users may prefix `10-`, `20-`).

### 8.2 Resolution (per turn, deterministic first)
Input: the claimed user text (not tool results, not model output).
1. **Alias scan**: normalise (lowercase, collapse whitespace, strip punctuation) and
   match every alias/title as a whole-word substring; longest match wins per span.
   Cost: one pass with a precompiled alternation regex rebuilt on index change.
2. **Embedding**: if fewer than `preload_max_entities` (3) matched, embed the input and
   take notes with cosine ≥ `retrieval_threshold` (0.55) up to the cap, excluding
   `core/`.
3. Threads additionally always have their `scope_note` (slot [5]), so resolution
   there fills only the remainder.
The model has `knowledge_search` for everything else; resolution is a cheap head start,
not a gate (charter §2.4). `Index.resolve` and `Index.search` are async because step 2
embeds the query; `preload`, `core_blocks` and the graph helpers are synchronous.

### 8.3 Pre-load rule (decided)
For each resolved note: inject the note body, then first-hop links in order
(procedures first, then same-type notes, then others), until `preload_budget_tokens`
(3,000 est.) is spent. Each injected note shows its path so the model can `knowledge_read`
for more. Never inject `core/` here (already in slot [3]).

### 8.4 Knowledge tools
| Tool | Args | Notes |
|---|---|---|
| `knowledge_search` | `query`, `type?`, `k=8` | fused: FTS5 rank + cosine, RRF; returns path, title, snippet |
| `knowledge_read` | `path`, `offset?`, `limit?` | full note or line slice |
| `knowledge_write` | `path`, `op: create|append|replace|rethink`, `content`, `old?` (replace), `frontmatter?` | `replace` = exact `old`→`content` (like fs_edit); `rethink` = whole-body rewrite, requires `reason`, logged as a `knowledge.rethink` item on the now board; stamps `modified`; rejects relative dates; commits to git (`loom: <op> <path>`) |
| `knowledge_links` | `path`, `direction: out|in` | neighbours |
| `procedure_load` | `name` | SKILL.md body (L2); scripts stay on disk |
| `transcript_search` | `query`, `conversation?`, `k` | FTS5 over `events_fts` — episodic last resort |
| `artifact_recall` | `id`, `offset?`, `limit?` | spilled outputs |

`knowledge_write` is `allow` by default (the vault is the agent's own memory), except
paths under `core/` which default to `ask` (they are always in context; a bad write is
expensive) — a policy row like any other.

### 8.5 Core blocks
`core/about-me.md`, `core/preferences.md`, `core/conventions.md` created at bootstrap
with short templates. The agent writes learned preferences there (charter §5.4 replaces
"prompt as learned artifact"). Hard cap per block; the UI shows fill level.

### 8.6 Maintenance role
One asyncio task, runs when the model queue has been idle ≥ `maintenance_idle_seconds`
(120) or on schedule (default nightly 03:00), under a single lock, priority 2.
Each pass, using `prompts/maintenance-*.md`:
1. **Consolidation sweep**: for each conversation with events past `maint_watermark`,
   show the new user/assistant events (tool results excluded) and the current related
   notes; ask for `knowledge_write` calls for durable facts missed inline. Advance
   watermark. Batches by conversation so facts form from coherent units (Supermemory
   "dreaming" lesson).
2. **Curation**: candidate pairs from alias collisions and cosine ≥ 0.92; propose
   merges/edits; apply via `knowledge_write` (targeted ops); `rethink` allowed here with
   a reason. Refresh dangling links.
3. **Housekeeping**: archive idle threads; prune artifacts of archived conversations
   past retention; `git commit` the vault as `maintenance: <date>`.
Everything it did is visible: the vault UI's *Recent changes* view is `git log` with
one-click revert (`git revert`/`checkout -- path`). No approval gate (takeaways §3).

### 8.7 Transcript recall
`events_fts` is maintained by triggers on `events`. `transcript_search` returns the
event, its conversation title and timestamp, and neighbours (±2 events) on request.

### 8.8 Watcher
Polling (`os.scandir` + mtime/size) every `vault_poll_seconds` (2). No `watchfiles`
dependency; thousands of files scan in milliseconds. External edits (Obsidian) are
indexed within one poll and emitted as `knowledge.changed` so open UI views refresh.
The vault git repo auto-commits external changes as `user: edit <path>` on the next
maintenance pass (so history stays complete without committing on every keystroke).

## 9. Tools

### 9.1 Tool contract
```python
Tool(name, description, parameters: JSONSchema,
     annotations={readOnlyHint, destructiveHint},   # MCP-standard names
     read_only_for=lambda args: ...,                 # per-call read-only test (http GET)
     path_params=("path", "cwd"),                    # args the fs guard checks
     timeout_s, handler)
```
`read_only_for` exists because read-only-ness is sometimes a property of the arguments,
not the tool: `http` GET/HEAD is a read, `http` POST is not. `Tool.is_read_only(args)`
is the one answer the permission layer asks for.

`ToolContext` carries, besides the conversation and the call: `priority` (the turn's
model-queue priority, which the classifier inherits), `meta` (the dispatcher writes
`{policy, decision, reason}` here and the runner persists it on the `tool_call` event)
and `extras` (per-turn scratch; `tool_search` puts the names it exposed in
`extras["exposed"]`).
Registry = native tools + MCP tools (prefixed `mcp_<server>_<tool>`) + connector tools
(prefixed `<connector>_<tool>`). All are exposed every turn unless the count exceeds
`tool_expose_max` (48), in which case native tools stay and the rest are reachable via
`tool_search(query)` which returns matching definitions to load for the rest of the turn.

### 9.2 Native tool inventory (decided) and default policies
| Tool | Purpose | readOnly | Default policy |
|---|---|---|---|
| `fs_read` | file or line slice; 8k cap; paging footer | yes | allow |
| `fs_list` | glob under a root | yes | allow |
| `fs_search` | regex/grep with context | yes | allow |
| `fs_write` | create/overwrite (guarded) | no | allow (within roots) |
| `fs_edit` | exact `old`→`new` replace, must be unique | no | allow (within roots) |
| `shell` | one command line, `cwd` in roots, env passthrough minus secrets, timeout, output cap; Windows: cmd.exe or Git Bash per setting | no | classify (pre-filter first) |
| `http` | GET/POST/… with headers/body; secret templating | no (GET treated read-only) | classify |
| `knowledge_*`, `procedure_load`, `transcript_search`, `artifact_recall` | §8.4 | mixed | allow (core/ → ask) |
| `status_update` | write the status block of the thread's scope note | no | allow |
| `conversation_create / send / read / list / finish` | §6.7 | read/list yes | allow |
| `schedule_create / list / delete` | §11 | list yes | allow (caps enforced) |
| `item_post` | put a notification/setup item on the now board | no | allow |
| `secret_set` | store a secret the user pasted (value never echoed) | no | ask |
| `tool_search` | load deferred tool definitions | yes | allow |
| `promote` | §15 | no | ask |

### 9.3 Dispatcher pipeline (one path for every call)
```
1 lookup tool → unknown → error result
2 parse/validate args against schema → invalid → error result "resend the call"
3 fs guard on path_params: resolve absolute; must be under a root (or APP_HOME/vault
  for knowledge tools, artifacts for recall); protected → ask; outside → ask with
  option add_root
4 policy = tool_policies[tool] or default (readOnly→allow, else ask):
     allow → go
     ask → approval
     classify → shell pre-filter (shell only) → allow, else classifier call → allow|ask
5 secrets substitution ({{secret:NAME}}) into a private copy of args
6 execute with timeout; exceptions → error result (never raise)
7 bound output (cap → artifact spill) → tool_result content
```
An approval persists `approvals(pending)`, emits `approval.requested`, and the turn
returns (§6.3). A decision `allowed_always` for `add_root` writes the root; for a tool it
sets `tool_policies[tool]=allow`. Both are ordinary settings the UI shows.

**Step 4 in full** (`tools/permissions.py`, `decide(app, tool, args) -> (mode, policy_key)`):

1. *Path-class rows* — a `tool_policies` row keyed `<tool>:<prefix>` applies when any
   string argument of the call starts with `<prefix>` (so `knowledge_write:core/`
   catches `knowledge_write(path="core/me.md")` without the guard, which knowledge
   tools bypass with `path_params=()`). The longest matching prefix wins.
2. *The tool's row* — `tool_policies[<tool>]`.
3. *No row* (every MCP and connector tool, registered long after the bootstrap and
   never given a row): read-only → `allow`, else `ask`.

On top of that sits one grant-only shortcut, the same principle as the shell pre-filter:
a call that is read-only *for these arguments* turns a matching `classify` row into
`allow`, because asking a model whether a GET is reversible spends a generation on a
foregone conclusion. It relaxes only `classify`, never an explicit `ask`, and nothing
static can produce a denial.

`policy_key` — the rule that decided — is stored in the approval's `options` so
`allow_always` widens exactly that rule (the path class, not the whole tool).

The guard's `add_root` option is the narrowest root that would let the call through:
the path itself when it is a directory, its parent otherwise.

### 9.4 Shell pre-filter (can only grant)
Split on `&&`, `||`, `;`, `|` respecting quotes. For each stage: first token (after
`env`-style assignments) ∈ `shell_readonly_commands` (setting; default: `ls dir cat
type head tail less more wc grep rg find where which echo pwd printenv git-status
git-log git-diff git-show git-branch python--version node--version npm-ls uv-pip-list`
with `git status` etc. matched as `git` + first arg), and no `>`/`>>`/`2>` redirection,
and no backticks or `$(`. All stages pass → allow. Otherwise → classifier. Parse
failure → classifier. Nothing here denies.

### 9.5 Classifier
`prompts/classify.md` with inputs: conversation kind and goal, the last user message,
the tool name and args (framed as untrusted data — Goose), the roots and protected
paths, and the policy text (reversible and in-scope → allow; destructive, external
side effects, credential access, or out-of-scope → ask). Output: strict JSON
`{"decision":"allow|ask","reason":"…"}`; any parse failure → ask. Role: `utility` if
set else the conversation's role; priority inherits. Result and reason are stored on
the tool_call event `meta` and shown in the UI on hover.

## 10. Connectors and MCP

### 10.1 MCP client (scratch, spec 2026-07-28 subset)
- Transports: **stdio** (spawn `command args`, JSON-RPC over newline-delimited
  frames, env from row with secrets substituted) and **streamable HTTP** (POST
  requests, SSE for streams; no session assumption — per-request `_meta`).
- Methods: `initialize`, `notifications/initialized`, `tools/list` (cached in
  `tools_cache`, refreshed on `notifications/tools/list_changed` or manual refresh),
  `tools/call`, `ping`. Resources/prompts/sampling/elicitation: not in v1; the client
  returns a clear error if a server requires them.
- Tool annotations (`readOnlyHint`, `destructiveHint`) flow into policy defaults.
- Failure: a server that fails to start or times out is marked `status=error` with
  `last_error`, its tools are removed from the registry, and a *setup* item is posted.
  Never fatal to the backend. Reconnect with backoff.

### 10.2 Native connectors
`connectors/<name>/__init__.py` exports `CONNECTOR = Connector(...)`:
```python
Connector(name, description,
          config_schema: dict,          # JSON schema → Settings form auto-rendered
          tools: list[Tool] = [],
          events: Optional[EventSource] = None,   # async generator yielding Event(kind, payload)
          surface: Optional[Surface] = None,      # adapter implementing the surface contract (§12.3)
          setup: Optional[callable] = None)       # returns a setup Item when user action is needed
```
Loader imports each package in a try/except; failure → `connectors.status=error`, item
posted, backend continues. Enable/disable and config edits take effect on `reload`
(POST) which re-imports the module; a full restart is never required for a connector
change but is always safe. Events from a connector go to the router (§11.2).

### 10.3 Knowledge entity per integration
When an MCP server or connector is added, the backend creates
`vault/connectors/<name>.md` (from `prompts/templates/connector-note.md`) with the
tool list and a placeholder for "when to use"; the agent fills it during first use.
Tier-1 integrations (charter §5.8) are just notes the agent writes.

## 11. Triggers and scheduler

### 11.1 Schedules
`spec` grammar (`triggers/spec.py`, no croniter), case-insensitive and
whitespace-tolerant: `every 15m|2h|1d|30s` (units s/m/h/d; also `every 90 minutes`,
`every day`), `daily at HH:MM`, `weekly on mon,thu at HH:MM` (full day names too),
`monthly on 15 at HH:MM`, `at 2026-09-26T18:00` or `at 2026-09-26 18:00` (one-shot), or
a 5-field cron `m h dom mon dow` with `*`, lists, ranges and steps (names for month and
weekday; dom+dow both set is cron's OR). Anything else raises `SpecError`, whose message
always lists the accepted forms. `Spec.next_after(dt)` computes in the wall clock of
`general.timezone` (so `daily at 09:00` stays 09:00 across a DST change) and returns an
aware UTC datetime; `next_run_at` is stored as an ISO UTC string. `Spec.min_interval()`
is what the interval cap is checked against (for a cron spec: the smallest gap in its
first few firings).

`Scheduler` (attached as `app.scheduler`) owns the table: `create`, `update`, `delete`,
`get`, `list`, `enable`, `run_now` (fires immediately and does **not** shift
`next_run_at`), `fire`, `tick`, `start`/`stop`. The task wakes every
`scheduler_tick_seconds` (30), fires enabled rows with `next_run_at ≤ now` through the
event router (§11.2), then writes `last_run_at`, `runs += 1` and the next time;
a one-shot sets `enabled=0` and `next_run_at=NULL`. An error inside a tick is logged per
row and never kills the loop; a row whose target conversation is gone is disabled rather
than retried. Missed firings while the backend was down fire once on startup if within
`schedule_catchup_hours` (6) — their item says "(missed while offline)" — and older
misses are rescheduled forward without firing. Caps: `max_active_schedules` (25, counted
over enabled rows), `min_interval_minutes` (5); past them `create` raises
`SchedulerError`, which `schedule_create` turns into an error result naming the cap and
the API turns into 409. Mutations emit `schedule.created|updated|deleted` (§12.2).

### 11.2 Event router
`route(app, TriggerEvent(kind, source, target, title, content, meta, policy))` is the one
write path from "something happened" to a turn. Target: thread events → parent
conversation (`meta.parent_id`); connector events → `meta.conversation_id` or the
conversation named in the connector config (default desk); schedule → its target;
`desk` and None resolve to the desk id. Every routed event = one board item + one inbox
row, in that order, so both exist before `conversation.post` wakes the turn — there is no
automation path, a trigger produces an ordinary turn. `route_connector_event(app, name,
title, content, meta=None, conversation_id=None)` is the convenience the connector loader
calls. Thread events are also written directly by `conversation.runner._notify_parent`
(the same two writes with the same policy setting).

### 11.3 Policy: interrupt vs queue
Per source: `trigger_policy.<class>` settings with defaults thread_event=interrupt,
schedule=queue, connector=queue; a schedule row may override. *Interrupt* → an `item`
plus a `notification` event pushed to every connected surface (the web UI shows a
toast + badge; a connector surface delivers if it registered as a notification target
in settings). *Queue* → item only; it appears in the desk's now board (slot [5]) and
the UI's board panel. Turns produced by triggers run at priority 1.

## 12. Surface API

### 12.1 REST (prefix `/api`; as built 2026-09-20)
```
GET  /conversations?kind&status&include_archived   POST /conversations {goal, scope_note?|project_name?, aliases?, roots?, preset?, title?, parent_id?, excerpt?}
GET  /conversations/{id}                PATCH /conversations/{id} {title?, preset?, step_budget?, clear_preset?}
GET  /conversations/{id}/events?after&before&limit&kinds
POST /conversations/{id}/messages {content}  → 202 {inbox_id}; 409 when no model is configured
POST /conversations/{id}/cancel         POST /conversations/{id}/archive
GET  /conversations/{id}/occupancy      GET  /conversations/{id}/artifacts     GET /artifacts/{id}?offset&limit
GET  /approvals?status                  GET  /approvals/{id}   POST /approvals/{id} {decision: allow|deny|allow_always, add_root?}
GET  /items?unseen&include_dismissed    GET  /items/{id}   POST /items/{id}/seen   POST /items/{id}/dismiss
GET  /vault/tree                        GET  /vault/note?path   PUT /vault/note {path, content}   DELETE /vault/note?path
GET  /vault/search?q&type&k             GET  /vault/history?path   GET /vault/recent-changes?n   GET /vault/diff?sha&path
POST /vault/revert {commit, path?}      GET  /vault/graph?center&depth   GET /vault/core   GET /vault/wanted
GET  /vault/procedures                  POST /vault/procedures/{name}/approve
GET  /vault/status?path                 PUT  /vault/status {path, fields}
GET  /settings  PATCH /settings {key: value}          (values + schema + groups; all-or-nothing validation)
GET/POST /providers  GET/PATCH/DELETE /providers/{name}   POST /providers/{name}/probe
GET/POST /presets    GET/PATCH/DELETE /presets/{name}     POST /presets/{name}/test
GET  /model-roles    PUT /model-roles/{role} {preset}     GET /models?provider → {providers: [{provider, models, error}]}
GET  /tool-policies  PUT/DELETE /tool-policies/{tool}     (tool may be a path-class key `<tool>:<prefix>`)
GET/POST /roots  DELETE /roots?path      GET/POST /protected-paths  DELETE /protected-paths?path → 202 + approval
GET  /secrets → {names, backend, warning}   PUT /secrets/{name} {value}   DELETE /secrets/{name}
GET/POST /mcp-servers  GET/PATCH/DELETE /mcp-servers/{id}   POST /mcp-servers/{id}/refresh|test|reload
GET  /connectors  GET/PATCH /connectors/{name} {enabled?, config?}   POST /connectors/{name}/reload
GET/POST /schedules  GET/PATCH/DELETE /schedules/{id}   POST /schedules/{id}/run
GET  /maintenance    POST /maintenance/run
GET  /system/health  /system/version  /system/queue  /system/logs?since&level&limit → {records}
GET  /system/setup   POST /system/setup {provider?, model, embedding_model?, timezone, projects_root, openrouter?}
POST /system/reindex
GET  /system/promote/plan?ref   POST /system/promote {ref, acknowledge_protected}   POST /system/rollback
GET  /system/promote/log   GET /system/snapshots   GET /system/supervisor   POST /system/restart
```
Update verbs: `PATCH` for partial row updates (providers, presets, conversations, mcp
servers, connectors, schedules, settings); `PUT` where the body replaces the whole value
(roles, tool policies, secrets, vault note/status). All mutations emit a matching WS
event so every surface stays consistent.

### 12.2 WebSocket `/ws`
One multiplexed socket; client sends `{subscribe: {conversations: [...] | "all"}}`.
Server events:
```
{type, ts, conversation_id?, data}
conversation.created|updated        data: row (status carries idle/busy)
message.start                       data: {role: assistant, step}
message.delta                       data: {channel: content|reasoning, text}
message.end                         data: {event_id, event}          (event = the persisted row)
event.appended                      data: event row  — every persisted event (user, assistant,
                                    tool_call, tool_result, thinking, system, compaction);
                                    the one mechanism that keeps every surface's transcript equal
tool.call                           data: {call_id, name, args(redacted), event_id}
tool.result                         data: {call_id, name, ok, preview, artifact_id?, event_id,
                                           policy, decision, reason}
inbox.posted                        data: {id, source}
models.changed                      data: {kind: provider|preset|role, name}
approval.requested|resolved         data: approval row
schedule.created|updated|deleted    data: schedule row
item.created|updated                data: item row
notification                        data: {title, body, item_id}
knowledge.changed                   data: {paths}
settings.changed                    data: {keys}
models.changed                      data: {kind: provider|preset|role, name}
connector.status|mcp.status         data: row
system.queue                        data: {depth, running}
promote.progress                    data: {stage, ok, message}
```
Surfaces render what they understand and ignore the rest (charter §5.2).

### 12.3 Surface adapter contract (for connector surfaces)
`Surface.start(api)` receives an in-process client with `send(conversation_id, text,
user_id)`, `subscribe(callback)`, `answer_approval(id, decision)`, `list_conversations()`.
A Discord/Telegram surface maps its channels to conversations (config: default desk; a
channel may bind to a thread) and renders `message.end`, `approval.requested`,
`notification`. Nothing else is required.

## 13. Web UI

React + Vite + TypeScript + Tailwind; no component library; state via a small store
over the WS stream (one reducer per event type) with REST for initial loads. Built into
`ui-dist/` and served by the backend at `/`. Dark/light follow the system.

### 13.1 Screens
- **Setup** (first run only, §3.3).
- **Desk** (default). Left: conversation area with streaming content, collapsible
  reasoning, tool call rows (name, args, policy badge, result preview, artifact link),
  inline approval cards. Right rail: **Now board** (items grouped: needs you / running
  threads / queued), each item actionable. Header: occupancy meter (used/limit,
  colour by proximity to compaction), queue indicator, model role in use.
- **Threads**. List (status, goal, last update, depth tree). Thread view = same
  conversation component + a **Status/plan panel** rendering the status block as an
  editable form (edits write through `PUT /vault/note`), links to walkthroughs and
  artifacts, "open project folder" and "spawned by" links. Create-thread dialog:
  goal, scope note (search/create), roots, role.
- **Vault**. Tree by type; note view/edit (Markdown editor with wikilink completion and
  frontmatter form); backlinks and outgoing links; search (fused); **Recent changes**
  (git log with diff and revert); wanted notes (dangling links); core block fill meters;
  optional graph view (`/vault/graph`) rendered with inline SVG — start focused, expand
  on demand, no physics simulation.
- **Approvals** badge + list (pending across all conversations), each with the
  classifier reason if any and the `allow always` option.
- **Settings** — every knob (§13.2), rendered from schema.
- **System**. Version and release pointer, promote/rollback with progress, logs tail,
  queue, connector and MCP status, reindex, snapshot list.

### 13.2 Settings inventory (all UI-visible; groups map to `config.py` dataclasses)
- **General**: display name, timezone, bind address, LAN token, projects root, theme.
- **Models**: three tabs. *Providers* (CRUD: name, kind, base URL, credential from the
  secret picker, default params, concurrency, enabled; probe button lists live
  models). *Presets* (CRUD: name, provider, model picked from the probe list or typed,
  every generation/window parameter with provider defaults shown as placeholders,
  reasoning mode, notes; "test" runs one short completion; shows calibrated
  `token_factor_seed` read-only; usage count of roles/threads pointing at it; delete
  blocked while referenced). *Roles* (five dropdowns → preset; `fallback_role`;
  `background_max_tokens`). Every conversation's header shows which preset it is
  using, and a thread's create dialog and detail panel let the user pick one.
- **Conversation**: step budgets (desk/thread), `compact_threshold`,
  `forced_compactions_per_turn`, `model_error_retries`, `reasoning_replay` +
  `reasoning_replay_max_chars`, `tool_output_cap`, artifact retention,
  `thread_archive_days`, `max_thread_depth`.
- **Knowledge**: `preload_budget_tokens`, `preload_max_entities`,
  `retrieval_threshold`, `core_block_chars`, vault poll interval, maintenance idle
  seconds and nightly time, curation similarity threshold, auto-commit external edits.
- **Permissions**: per-tool mode table (allow/ask/classify, grouped native / MCP /
  connector), shell read-only command list, allowed roots, protected paths, shell
  flavour (cmd/Git Bash/PowerShell/sh), classifier role.
- **Integrations**: MCP servers (CRUD, test connection, refresh tools, env with secret
  picker), connectors (enable, config form, reload, status), secrets (names only:
  add/replace/delete; values write-only).
- **Triggers**: schedules (CRUD, run now, enable), trigger policies per class,
  `max_active_schedules`, `min_interval_minutes`, catch-up hours, notification
  targets (web, each surface connector).
- **System**: supervisor status, release pointer, auto-rollback thresholds, snapshot
  retention, log level, reindex, reset (dev).

## 14. CLI
`loom serve [--port] [--no-supervisor]`, `send [--thread ID|--new "goal"] "text"`
(streams and blocks until the turn ends; exit code reflects status), `threads [--all]`,
`approvals [--allow ID|--deny ID]`, `items`, `status`, `reindex`, `vault search|read`,
`schedules`, `promote REF`, `rollback`. All commands are thin REST clients; the CLI
holds no logic of its own.

`send` follows a turn by polling `GET /conversations/{id}/events?after=` every 0.5s and
printing what is new, rather than opening the WebSocket: no stdlib WebSocket client
exists and §19 forbids adding one for this. Exit codes: 0 `idle`/`done`, 1 `failed`,
2 `waiting_input`/`waiting_approval`, 3 no model configured, 4 backend unreachable or
the endpoint does not exist yet.

Base URL: `--url`, else `$LOOM_URL`, else `http://127.0.0.1:<general.port read from the
home's state.db, or 8500>`. Token: `--token`, else `$LOOM_TOKEN`. `loom serve` runs the
supervisor (§3.1) as a child; `--no-supervisor` serves in-process (development, and what
`CLAUDE.md`'s command block means). `--fake` serves with the fake model client and a
fake provider/preset/roles so the UI can be developed with no Ollama running.

## 15. Self-modification and promotion

### 15.1 Layout
The assistant's own repo is cloned once into `APP_HOME/releases/src.git` (bare) with
worktrees `releases/<id>/` per release; `current` names the live one. A self-mod thread
gets its own worktree under `releases/dev-<thread>/` as a root, plus a scratch
`LOOM_HOME` pointing at `APP_HOME/scratch/<thread>/` so tests and a staging run
never touch live state.

### 15.2 `promote(ref)` flow (tool → backend → supervisor)
```
backend:  1 ref must be a commit reachable in src.git; worktree clean
          2 protected-path check: git diff --name-only <current>..<ref> ∩ protected_paths
              non-empty → approval required (reason lists the files); denied → stop
          3 run test suite in the ref worktree (python -m uv run pytest); failure → stop, report
          4 POST supervisor /promote {ref}
supervisor:
          5 snapshot: sqlite backup API → snapshots/<ts>/state.db; vault `git tag pre-<ts>`
          6 create releases/<id> worktree at ref; install deps (uv sync); build UI if ui/ changed
          7 probe: run `serve --probe --port P` against a COPY of state.db (migrations dry run),
              wait for /system/health ok within 60s; else abort
          8 write `current`, stop live backend, start new; watchdog 120s: health must stay ok
          9 failure → restore `current`, restart previous, restore snapshot if migrations ran
          10 emit promote.progress at every stage (via a file the new backend tails on start
              and republishes)
```
The turn that called `promote` ends at step 4 with a `system` event; after restart the
thread resumes (§6.3 step 1) and reads the outcome from the promote log.

### 15.3 Protected paths (default rows)
`supervisor/`, `src/loom/tools/permissions.py`, `src/loom/tools/fs_guard.py`,
`src/loom/tools/shell_prefilter.py`, `src/loom/promote.py`,
`src/loom/secrets.py`, `APP_HOME/secrets.json`, `APP_HOME/supervisor.token`.
Editable in Settings; removing an entry is itself an `ask`. Mechanically:
`DELETE /api/protected-paths?path=` deletes nothing — it creates an approval on the desk
for the pseudo-tool `protected_paths_remove` and answers `202` with the pending row.
`approvals.decide(allow)` performs the removal and, since no turn is waiting on it,
returns the conversation to `idle` instead of waking the runner. That is the only
internal (non-model) action approvals gate, and the only branch in `decide`.

### 15.4 Rollback
`rollback` → supervisor swaps `current` to the previous release and restarts; if the DB
schema advanced, restores the pre-promotion snapshot (state written since is lost, and
the UI says so before confirming).

## 16. Prompts (all model-facing text lives here)
```
prompts/system.md            soul + operating rules (memory discipline, ladder, when to spawn,
                             verification by judgment, no relative dates, evidence in walkthroughs)
prompts/kind-desk.md         desk-specific: now board semantics, routing to threads
prompts/kind-thread.md       thread-specific: status block duty, finish + walkthrough, budget line
prompts/tools/<tool>.md      one description per native tool (loaded into the registry)
prompts/classify.md          permission judge
prompts/summarize.md         compaction fold
prompts/maintenance-consolidate.md  / maintenance-curate.md
prompts/status-safety-net.md
prompts/preset-test.md       the one short completion `POST /presets/{name}/test` sends
prompts/templates/*.md       new note templates (device, project, connector, procedure SKILL.md)
```
Nothing in these files references mutable state (model names, paths, rosters).

## 17. Failure handling (one behaviour per class)
| Failure | Behaviour |
|---|---|
| Model transient | retry once; then visible system event, status idle (desk) / failed (thread → parent notified) |
| Context overflow | evict → fold → rebuild, ≤2 per turn; else visible error |
| Tool exception / timeout | error tool result; model continues |
| Malformed tool args | error tool result; never executed |
| Crash mid-turn | on restart: unpaired tool_call → synthetic "interrupted" result; undelivered inbox redelivered |
| Vault parse error | note indexed with `type=invalid`, shown in vault UI; never blocks |
| MCP server down | tools removed, item posted, reconnect with backoff |
| Connector import error | disabled, item posted |
| Promotion failure | automatic rollback, item posted with the log |
| Secrets backend missing | plain-file fallback + persistent warning |
| Provider unreachable | turn fails visibly; queue shows provider state; other providers unaffected |

## 18. Testing
- `pytest`, `asyncio_mode=auto`; fixture gives each test a temp `LOOM_HOME` with a
  migrated DB, an empty vault git repo, and a **fake model client** driven by scripted
  responses (content, reasoning, tool calls, usage, outcomes). No test calls a model.
- Unit: migrator, ULID, frontmatter/wikilink parser, alias resolution, pre-load budget,
  shell pre-filter (property: never returns deny), fs guard, compaction math and ladder,
  calibration, schedule spec parser, MCP framing (against an in-process fake server),
  secrets templating/redaction.
- Integration: the four walkthroughs (`01-walkthroughs.md`) as scripted end-to-end tests
  through the REST/WS API: printer (resolution + classify allow + secret substitution),
  music bot (spawn → status → finish → parent inbox), 3D map (status query answered
  from the note while the thread "runs"), homework (MCP fake server + inline
  `knowledge_write`). Plus: approval pause/resume across a simulated restart; crash
  repair; promotion protected-path refusal.
- UI: vitest for the store reducers and the settings form renderer; a Playwright smoke
  (desk message round-trip against a fake-model backend) is optional.

## 19. Dependencies (explicit; anything else needs a reason in this doc)
Backend runtime: `fastapi`, `uvicorn`, `httpx`, `pyyaml`, `keyring`, `tzdata`. Dev: `pytest`,
`pytest-asyncio`. `tzdata` is data, not code: Windows ships no IANA database, so
`zoneinfo.ZoneInfo("Europe/Amsterdam")` raises there and every local time in §11 and
§8.6 would silently become UTC (decided 2026-09-20). Stdlib for everything else: `sqlite3` (+FTS5, present in CPython
builds), `asyncio`, `json`, `re`, `array`/`math.sumprod`, `subprocess`, `shlex`.
Not used, deliberately: SQLAlchemy/alembic (hand SQL), LiteLLM/openai SDK (one HTTP
shape), LanceDB/numpy (brute-force cosine), APScheduler/croniter (80-line parser),
watchfiles (polling), GitPython (git CLI), cryptography (see §4.5).
UI: `react`, `react-dom`, `vite`, `typescript`, `tailwindcss`, one Markdown renderer
(`marked` or `react-markdown`), dev `vitest`. No state library, no component library.

## 20. Build order (each phase fully realised before the next)
0. **Foundations** — repo, `APP_HOME`, settings schema + table, migrator, ULID, logging,
   model client (both providers, streaming, outcomes, priority queue), supervisor
   (start/restart only), CLI `serve`/`send` against a bare desk with no tools.
1. **Conversation core** — events, inbox, `run_turn` with repair, tools `fs_*`/`shell`/
   `http`, dispatcher with permissions (allow/ask/classify, fs guard, pre-filter,
   approvals in CLI), artifacts, compaction ladder, calibration, cancel.
2. **Knowledge** — vault bootstrap (git, core templates), index + watcher + reindex,
   resolution + pre-load, knowledge tools, procedures/SKILL.md, transcript FTS,
   slots [3][4][9] live.
3. **Web UI v1** — desk, streaming, tool rows, approvals, vault browser/editor/search/
   recent changes, settings (all groups that exist by now), system page.
4. **Threads** — conversation tools, spawn payload, status block + `status_update` +
   safety net, events to parent, finish + walkthrough, depth cap, budget line,
   threads UI + status panel.
5. **Integrations** — MCP client (stdio + HTTP), MCP settings UI, connector loader +
   config forms, secrets store + picker, setup items, connector note template.
6. **Triggers** — schedules + parser + scheduler, router, items/now board, policies,
   notifications, caps, UI.
7. **Maintenance** — consolidation sweep, curation, housekeeping, vault git commits,
   recent-changes revert.
8. **Self-modification** — releases layout, `promote` end to end, protected paths,
   snapshots, rollback, System UI progress, auto-rollback in supervisor.

## 21. Implementation questions (decided 2026-09-20; see charter decision log)
- Ollama: native `/api/chat` for the `ollama` provider kind; no probe.
- Windows shell flavour: `auto` → Git Bash if present, else cmd.exe; setting exposed.
- Now board: all running threads, one line each, capped by `now_board_max_threads` (10).
- UI Markdown: `marked` + scratch wikilink extension + allowlist sanitiser.

### 21.1 API conventions (decided 2026-09-20)
- JSON in and out. Errors: FastAPI `{"detail": "message"}` with 4xx/5xx.
- Rows are returned as their table row with JSON columns decoded and `*_at` as ISO-8601
  UTC strings. Lists are plain arrays.
- Auth: when `general.lan_token` is set, every `/api` request carries
  `Authorization: Bearer <token>` and `/ws` carries `?token=`; loopback without a token
  needs nothing.
- Settings: `GET /api/settings` → `{"values": {...}, "schema": [Field...], "groups": {...}}`;
  `PATCH /api/settings` takes a partial `{key: value}` map, validates all-or-nothing,
  returns the new values, emits `settings.changed {keys}`.
- Every mutation emits its matching WS event before the HTTP response returns.
- Endpoints are `async def`; SQLite runs on the loop thread; blocking work uses
  `asyncio.to_thread`. A sync handler would run in Starlette's threadpool, where there
  is no running loop to schedule a turn from and where SQLite would be touched off the
  loop thread, against the 2026-09-20 decision.
