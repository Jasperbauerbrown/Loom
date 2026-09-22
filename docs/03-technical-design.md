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
    connectors/    loader.py  base.py  mcp/ (client.py transports.py manager.py)
    triggers/      scheduler.py  router.py  items.py
    server/        app.py  ws.py  routers/ (conversations approvals items vault settings system)
    secrets.py     artifacts.py  promote.py
  prompts/         *.md (all model-facing text; §16)
  connectors/      # native connector packages discovered at start; README.md + example_echo/
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
- `promote`/`rollback` in the supervisor are §15.2 steps 5–10 whole: snapshot, build the
  release worktree, probe it, switch `current`/`previous`, restart, watch
  `/api/system/health` for `promotion_watchdog_seconds`, and restore pointers (and the
  database, when the release migrated it) if it does not hold. The three seams
  `snapshot`, `create_release` and `probe` are implemented as of the self-modification
  package; they stay module-level functions so tests drive them without a process.
- Every stage appends one JSON line `{ts, stage, ok, message, ref, conversation_id}` to
  `APP_HOME/promote.log`. The supervisor cannot tell the backend directly — it is
  restarting it — so the file is the channel and the backend republishes it (§15.2).
- `hydra.bat`-style launchers are replaced by `loom` (supervisor entry) for end users
  and `python -m loom serve` for development (no supervisor).

### 3.2 Backend
- FastAPI + uvicorn, single process, asyncio. Binds `127.0.0.1:8500` by default; LAN
  binding and a bearer token are settings.
- Startup order: migrate DB → load settings → init secrets → reconcile background processes (§9.6) → init model client → load
  vault index (rebuild if stale) → start watcher → load connectors → connect MCP servers
  → start scheduler → resume interrupted conversations (§6.3 step 0) → serve.
- Shutdown: stop accepting turns, let in-flight tool calls finish (bounded 10s),
  persist, exit. Persist-before-act (§4) makes abrupt kills safe. Background processes
  are left running on purpose (§9.6).

### 3.3 First run (decided)
Simplicity ethos: from install to a working desk in under a minute, one required
choice. On `loom serve` with no `state.db`:
1. Create `APP_HOME`, migrate, insert the desk row, `git init` the vault and write the
   `core/` templates, write default settings, tool policies and protected paths.
2. Probe providers: local Ollama at `http://127.0.0.1:11434` (and `OLLAMA_HOST` if
   set); list models. Detect timezone from the OS.
3. Print the URL and open the browser to the **Setup** screen — shown only while no
   `desk` role is configured. One page: provider (detected Ollama pre-selected; "add
   OpenRouter key" expands to a base URL + key field), **model** (the one required
   choice; picked from the probed list; saved as a preset with sensible defaults and
   assigned to the desk, worker and maintenance roles), embedding
   model (auto-selected if `nomic-embed-text` is present, otherwise the exact `ollama
   pull` command is shown and embeddings stay off until it appears), timezone (confirm).
   Finish → the Desk, with a first item on the now board:
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
  artifacts/<conv_id>/proc_<id>.log    # background process output (§9.6)
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
        keep_alive, timeout_s, reasoning (on|off|budget), repetition_penalty,
        presence_penalty, frequency_penalty, token_factor_seed,
        multimodal (bool), image_tokens (per-image prompt-token estimate)*/,
        notes TEXT, enabled INT, created_at TEXT, updated_at TEXT)
model_roles(role TEXT PK /*desk|worker|maintenance|utility|embedding*/, preset TEXT)

conversations(id TEXT PK /*ULID*/, kind TEXT /*desk|thread*/, parent_id TEXT,
              title TEXT, goal TEXT, pinned_notes TEXT /*json list of vault paths, slot [5]*/,
              roots TEXT /*json list*/, depth INT, role TEXT /*model role*/,
              preset TEXT /*optional override of the role's preset*/,
              status TEXT /*idle|running|waiting_approval|waiting_input|done|failed|archived*/,
              step_budget INT, summary TEXT, summary_cutoff INT /*events.id*/,
              token_factor REAL, maint_watermark INT /*events.id*/,
              created_at, updated_at, finished_at, archived_at,
              walkthrough TEXT /*log/ note the finish walkthrough landed in*/)
              -- 0002 (2026-09-22): pinned_notes + walkthrough replaced scope_note

events(id INTEGER PK, conversation_id TEXT, kind TEXT
       /*user|assistant|tool_call|tool_result|thinking|system|compaction|status*/,
       content TEXT, meta TEXT /*json*/, ts TEXT)              -- append-only
events_fts  -- FTS5 external-content table over events.content for user/assistant/tool_result

inbox(id INTEGER PK, conversation_id TEXT, source TEXT
      /*user|conversation:<id>|schedule:<id>|connector:<name>|process:<id>|system*/,
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

processes(id TEXT PK /*ULID*/, conversation_id TEXT, title TEXT, command TEXT, cwd TEXT,
          pid INT, status TEXT /*starting|running|exited|stopped|failed|lost*/,
          exit_code INT, error TEXT, log_path TEXT /*artifacts/<conv>/proc_<id>.log*/,
          started_at TEXT, ended_at TEXT)                     -- 0003 (2026-09-22), §9.6

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
  log/YYYY-MM-DD.md                                              (daily notes: thread walkthroughs land here as sections)
```

Note format:
```markdown
---
title: Ender 3
aliases: [ender 3, the printer, 3d printer]
type: device            # optional; defaults from folder
tags: [lan, octoprint]
root: C:/Users/me/Projects/music-bot   # projects only: where it lives, recorded by the model
modified: 2026-09-20T14:02:11Z         # harness-stamped on every write
---
Body. Wikilinks like [[ender-3-control]] resolve by filename, then title, then alias.
```

Rules enforced by `knowledge_write` (not by the model): frontmatter is parsed and
re-emitted (PyYAML), `modified` is stamped, `aliases` are lower-cased and de-duplicated,
relative dates in the body are rejected with an error result ("use absolute dates").

**Project notes** are ordinary entity notes. The model writes one when it recognises
that the thing it works on will persist and be returned to (the soul rule, decided
2026-09-22): what it is, where it lives (`root`, a recorded fact, never a default), how
to run and check it, decisions and why, links to the `log/` sections that touched it.
The harness never creates one and keeps no block in one; a thread's status lives in its
own transcript (§6.7). Until 2026-09-22 a thread scoped to a harness-created project
note and rewrote a delimited status block inside it; that shape is gone.

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

**Paged tools** (2026-09-22). Tools that page their own output — `fs_read`, `knowledge_read` and
`artifact_recall` — are marked `paged` in the registry and are never spilled: their page
is `tool_output_cap` characters (the default and the clamp for any larger `limit`) and
the paging footer rides on top, so a paged result may exceed the cap by one footer line.
Everything else is bounded by the dispatcher. Before this, a recall page plus its footer
was itself over the cap and was spilled into a new artifact, so a model could never read
past the first page of anything; the Godot-debug thread looped on that for fifty calls.

**Images** (2026-09-21). Image bytes — uploaded by the person, returned by `fs_read`,
`http` or `screenshot` — are stored beside the spills as
`artifacts/<conv>/img_<ulid>.<png|jpg|webp|gif>` with no table row; the strict id shape
is the traversal guard. Transcript rows carry only refs, `meta.images = [{id, mime}]`, on
`user` and `tool_result` events (and on inbox rows); data URLs are built at
context-assembly time and live in memory only, so no chars/4 estimate and no FTS row ever
sees base64. Cap: `conversation.image_max_mb` (default 10), enforced at upload, in the
tools and in the store. Pruning sweeps `img_*` in the same folders as the spills.

### 4.5 Secrets
`secrets.py` exposes `get(name) / set(name, value) / delete / list_names`. Backend:
`keyring` (Windows Credential Manager, macOS Keychain, Secret Service). Fallback when
no keyring backend is available (headless Linux): `APP_HOME/secrets.json` with 0600
permissions and a persistent warning banner in Settings. *Deviation from charter:* the
fallback is not encrypted; the stdlib has no AEAD cipher and importing `cryptography`
for one file violates the dependency rule. Documented, UI-visible.

Secret names are case-insensitive (Windows credential targets are; a twin differing
only by case once shadowed the real key), and the existing spelling wins. Tests never
touch the machine's credential store: the `app` fixture installs a dict-backed keyring.

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
  `num_ctx`, `keep_alive`, `timeout_s`, reasoning mode, `multimodal` — the model
  accepts image input — and `image_tokens`, the per-image prompt-token estimate used
  for occupancy and compaction, default 1000) plus the machine-written
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
              utility/worker call producing the status fields from the turn's events
     mark inbox delivered; status = idle (or done if finish was called)
     threads: emit thread_event to parent inbox + item if state changed      (§6.7)
6  unlock
```
Reasoning replay within the turn: each step's reasoning is fed back into subsequent
steps of the *same* turn, tail-capped at `reasoning_replay_max_chars` (4,000: the last N
characters behind an elision marker, the part that led to the step's calls, never the
head where a degenerate loop's seed sits); never
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
(the replay builder consults `compaction` events).

**Manual clear** (decided 2026-09-21). "Clear the desk" is the same cut: `POST
/conversations/{id}/clear` writes a `compaction` event of kind `clear`, empties the
summary and moves `summary_cutoff` to the last event. Nothing is deleted: the transcript
stays visible below a divider and searchable with `transcript_search`. Refused (409)
while a turn is running or waiting for approval. Available in the UI header and as
`loom clear [--thread ID]`. Anti-thrash and headroom math are
exactly Hydra's; they are proven on Ollama.

### 6.6 Calibration
After each clean turn with reported `prompt_tokens`:
`factor = prompt_tokens / (chars_in_prompt / 4)`, clamped [0.5, 4.0], stored on the
conversation. A reading at ≥98% of `effective_ctx` can only raise the factor. New
conversations seed from their preset's `token_factor_seed`, which each calibration
writes back (the factor is a property of model + content, so the preset is its home).

### 6.7 Threads
A thread is Loom's shape for scoped work: a conversation with a goal, its own roots, its
status, its pinned notes and its transcript, temporary by design. A *project* is not a
shape Loom maintains: it is whatever the person calls one — a codebase, a folder of
images, an ongoing effort — and it exists in Loom as an ordinary note the model writes
(§4.3). Decided 2026-09-22; before that a thread scoped to a harness-created project
note, and a thread without one was a "task". Every thread is now that.
- **Spawn** (`conversation.create`): payload = `goal`, optional `excerpt` (model-selected
  text from the parent), `notes` (vault paths to pin; each must exist), `roots`, `preset`
  override (validated against enabled presets), `title`. `depth = parent.depth + 1`;
  refused above `max_thread_depth` (default 3). Row written and `conversation.created`
  emitted **before** the first turn is scheduled. *When* to spawn is prose, not
  mechanism (`system.md`, `kind-desk.md`; decided 2026-09-22): the test is whether the
  end of the work can be named from where the desk stands — a survey, an investigation,
  a build cannot, and go to a thread before the desk's first step — and a result that
  raises a new question instead of answering one is the sign that the desk is exploring.
- **Pinned notes** (`conversations.pinned_notes`, json list; `conversation/pins.py` is
  the one reader and writer): the notes a conversation keeps in slot [5] every turn,
  each capped at `pinned_note_chars` (6,000) with a visible marker. Pinned by the thread
  through `knowledge_read(path, pin=true)` (unpinned with `pin=false`), by the spawning
  conversation at spawn, and by the person in the thread panel (`PATCH` `pinned_notes`).
  A pin to a missing note is refused. Knowledge resolution (§8.3) skips pinned paths.
- **Context**: slot [5] every turn is the goal, the current status fields, then the pinned
  notes.
- **Status**: one reader and one writer (`conversation/status.py`). `status_update(fields)`
  appends a `status` event to the thread's own transcript (`content` = one-line summary,
  `meta = {fields, owner}`; merged with the previous event, never replayed to the model).
  The now board, the status line and the thread panel read through the same call; the
  person edits it through `PUT /conversations/{id}/status`; the safety net (§6.3 step 5)
  guarantees at most one turn of staleness.
- **Events to parent**: on state change (`blocked`, `waiting`, `done`, `failed`) the
  runner writes an inbox row to `parent_id` with a header (`[blocked]`, `[stopped without
  finishing]`, `[finished]`, `[failed]`) and a summary, plus an `item`
  (policy from settings: thread events default *interrupt* → notification). The parent
  verifies by judgment (charter §5.7); nothing else fires.
- **Finish** (`conversation.finish(summary, walkthrough)`): appends the walkthrough (what
  changed, how verified, artifact ids of test output) as a section `## <title> (<id>)`
  to the daily note `log/YYYY-MM-DD.md` (created on first use), records that note's path
  in `conversations.walkthrough`, sets status `done`, emits to parent. Linking the
  section from a project note is the model's knowledge work, not the harness's.
- **User in a thread**: the same surface calls as the desk; user messages set priority 0
  for that turn.
- **Turn end without finish** (decided 2026-09-20): a thread turn that ends with plain
  text (a question, a report, or the wrap-up after the step budget) sets status
  `waiting_input` and forwards that text to the parent as a thread event. The parent
  decides by judgment whether to answer, redirect, or involve the person. The header
  says `[stopped without finishing]`, not `[needs input]` (2026-09-22): the harness
  cannot tell a question from a report, and three of four finished threads in the game
  test ended with a report instead of `finish`; the UI labels the status the same way.
- **Sub-threads**: a thread may spawn threads down to `max_thread_depth` for independent
  parts of a larger effort, each with its own goal, status and pins. Nothing is shared
  by construction, so nothing needs refusing.
- **Archival**: maintenance archives `done`/`failed` threads idle for
  `thread_archive_days` (14). Archived threads stay searchable (FTS) and reopenable.

## 7. Context assembly (`assemble`, the only builder)

| Slot | Content | Role / position | Stability |
|---|---|---|---|
| [1] | `prompts/system.md` (soul + operating rules) + `prompts/kind-desk.md` or `kind-thread.md` | the one system message | stable |
| [2] | One line: "Tools are provided via the API; use `tool_search` for more." (schemas travel in the `tools` parameter) | system | stable |
| [3] | Core blocks: `vault/core/*.md` in `core_order`, each hard-capped (`core_block_chars`, default 1,500) — overflow is truncated with a visible marker, never silently | system | stable within a session; changes only when the user/agent edits core |
| [4] | Procedures index: one line per `SKILL.md` (`name — description`) | system | stable |
| — | *end of cache-friendly prefix* | | |
| [7] | `summary` (if any), fenced `<context from="harness">` | user, right after the prefix | changes only on fold |
| [8] | Live events since `summary_cutoff`, replayed in order; `thinking`/`system`/`status` kinds skipped; evictions applied. A `user` event's `meta.images` become data URLs on its message; a `tool_result` with images stays text (one `[image <id> attached to this result]` marker per image) and is followed, after the step's last tool message, by a user-role *follower* carrying the images (`prompts/context/tool-image-note.md`) — tool-role messages cannot carry image parts. Evicted results lose their images with their content. | assistant / user / tool | stable within a turn |
| [5]+[6] | Desk: the **now board** (pending approvals, unseen items, running threads with one-line status, then one line per running background process). Thread: the goal + its status fields. Both: the **pinned notes** (`# Pinned notes`, each capped at `pinned_note_chars`). Then a **Filesystem** block: the conversation's effective roots (global + its own, the first being what relative paths resolve against) and the protected paths, one line each; then a `loom:` line naming this backend — the address it serves, its pid, its home — so the model can tell its own process from anything else it finds (2026-09-22). Then `now: 2026-09-20 14:02 (America/…)`. One fenced `<context from="harness">` block, **built once per turn and reused verbatim for every step** (2026-09-22) | user, after the history and before this turn's messages | per turn, not per step |
| [9] | Resolved entities for this input (§8.2–8.3), wrapped `<knowledge may_be_irrelevant="true">…</knowledge>`; ephemeral, never persisted | user | volatile |
| [10] | Claimed inbox messages as user-role messages (with their images), then the turn's own assistant / tool messages and image followers | user / assistant / tool | volatile |
| [11] | Threads only: `[steps remaining: N]` in its own fenced block, **after** the turn's messages — the one thing that changes per step, isolated so the turn stays cached. The desk has no step line and therefore no block | user, last | per step |

Images on the wire: inside Loom `content` is always a string and images ride an
in-memory `images` key of data URLs. `models.client.messages_for` renders them per
provider and preset: a `multimodal` preset on an OpenAI-compatible provider gets the
content-parts array (`text` + `image_url` parts), on Ollama the `images` array of bare
base64; a text-only preset gets one placeholder line per image
(`prompts/context/image-placeholder.md`) so a strict chat template never sees a list.
Each image counts as `image_tokens × 4` characters in every estimate (`ChatRequest.chars`,
`Context.chars`, occupancy, compaction) — the base64 is never counted.

Exactly one system message, first (decided 2026-09-20: Qwen 3.x chat templates reject a
system message anywhere else). Slots [1]–[4] are byte-stable across turns (no dates,
sorted keys) for Ollama's prompt cache; the summary and history extend the cached prefix
within a turn.

**Within a turn nothing before the turn's own messages moves** (decided 2026-09-22). The
harness block used to be rebuilt on every step and to carry the step counter, so every
step invalidated the cache immediately before the turn's messages and re-prefilled
everything the turn had accumulated — quadratic on a long thread turn. `assemble` now
builds the block once, `messages()` reuses that exact string, and the counter rides slot
[11] at the end. The trade is deliberate and visible: the now board, the clock, the status
and the pinned notes are a snapshot from turn start, so a thread that pins a note or
writes its status mid-turn sees it in the block next turn, with the content in its own
transcript meanwhile. For a thread the last message is therefore the budget block, not the
person's words. Three things still break the cache mid-turn, all of them known: a roots or
protected-path change, a forced compaction, and `tool_search` widening the tool list —
schemas sit at the very front, so that one invalidates everything.

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
3. Pinned notes are already in slot [5], so resolution skips them and fills only the
   remainder.
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
| `knowledge_read` | `path`, `offset?`, `limit?`, `pin?` | full note or line slice; one page of `tool_output_cap` chars with a paging footer, never spilled (`paged`) |
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
`knowledge/maintenance.py` — `app.maintenance`. One asyncio task; one `asyncio.Lock`;
every model call at priority 2 with `purpose="maintenance"` and the `maintenance` role.
A pass is always the same three stages in the same order, whatever started it.

**Scheduling.** The task polls `models.queue_state()` every 15s. A pass runs when either

- the queue has been idle ≥ `maintenance_idle_seconds` (120), the previous pass is at
  least that far behind, **and** there is work a pass *clears* (a conversation with
  events past its watermark, an archivable thread, or a dirty vault — curation
  candidates deliberately do not count: a pair the model leaves alone would re-trigger a
  pass every idle window forever; curation rides along on passes other work started); or
- the nightly slot (`maintenance_nightly_at`, default 03:00 local) has passed and no
  pass has run for it — nightly always runs, work or not.

The nightly rule compares the date of the most recent slot against the last slot a pass
ran for. At startup the current slot counts as handled, so a restart never fires a
catch-up pass and a clock change cannot fire one twice. Nothing is persisted: an idle
pass covers anything a missed nightly would have.

A second `run()` while a pass is in flight returns the in-progress report; passes never
queue, because the next pass would see the same world. `maintenance_enabled` off stops
the schedule, not `POST /maintenance/run`.

**1. Consolidation sweep.** For each non-archived conversation with `events.id >
maint_watermark`: take the `user`, `assistant` and `tool_call` events (tool *results*
are never shown), render them with the compaction renderer, and split into batches of
≈ 12,000 characters, oldest first. Each batch is one model conversation: the excerpt,
plus the notes `index.search` returns for it (6, each capped at 1,500 chars) so the
model edits what exists instead of duplicating it. Tools offered: `knowledge_search`,
`knowledge_read`, `knowledge_write`, `knowledge_links`, and at most 6 tool-call rounds.
The watermark advances to the last event of a batch only after that batch completes, and
to the conversation's max event id when every batch does — so a model failure (logged in
`report.errors`) re-shows exactly the events that were not consolidated. Conversations
whose new events are all of unshown kinds advance the watermark with no model call.

**2. Curation.** Candidates, deterministic and cheap: alias collisions (one normalised
alias, two paths), near-duplicate pairs (`index.nearest`, k=3, cosine ≥
`curation_similarity`, default 0.92), and dangling links (`index.dangling()`). Capped at
10 per pass. Each candidate is one model conversation showing both note bodies (capped
at 3,000 chars, older path labelled, because a merge keeps the older path) or the
dangling target and the lines that link to it. `rethink` is allowed here, with a reason.

**3. Housekeeping.** Archive `done`/`failed` threads whose `updated_at` is older than
`thread_archive_days`; prune archived conversations' artifacts past
`artifact_retention_days` (files then rows); commit external vault edits (§8.8); then a
safety-net `maintenance: <YYYY-MM-DD>` commit, normally a no-op because the knowledge
tools commit per write.

**Approvals during a pass (decided 2026-09-20).** `knowledge_write:core/` is `ask`, and a
pass has no turn to pause and no user waiting. One behaviour, no special case in the
dispatcher: the call goes through the ordinary dispatcher, and an `ApprovalNeeded` is
**denied immediately** — so no stale pending row sits on the now board pointing at a turn
that will never resume — and the proposal is posted as a `knowledge` item carrying
`action = {kind: "knowledge_write", args: {...}}`, which the person can act on. The model
is told the write was skipped and not to retry it, and the sweep continues; the watermark
still advances, because the pass did consider those events. Denying the row would wake
the desk, so the desk's turn lock is held across the decision (an already-locked
conversation needs no help — `wake` declines it too).

**Visibility instead of approval** (takeaways §3). The pass emits
`maintenance.progress {stage, message}`, keeps the last report on the instance
(`GET /maintenance`), posts one `knowledge` item summarising a pass that changed
something (`notify=False`), and every vault change is a git commit in the vault UI's
*Recent changes* view, one click from revert. No approval gate on the pass itself.

**Report** (`GET /maintenance` → `{running, enabled, reason, idle_seconds,
idle_seconds_remaining, next_nightly_at, last_report}`):
`{started_at, finished_at, reason, consolidated: [{conversation_id, events, writes}],
curated: {candidates, applied}, archived, pruned_artifacts, committed, external_commit,
errors}`.

### 8.7 Transcript recall
`events_fts` is maintained by triggers on `events`. `transcript_search` returns the
event, its conversation title and timestamp, and neighbours (±2 events) on request.

### 8.8 Watcher
Polling (`os.scandir` + mtime/size) every `vault_poll_seconds` (2). No `watchfiles`
dependency; thousands of files scan in milliseconds. External edits (Obsidian) are
indexed within one poll and emitted as `knowledge.changed` so open UI views refresh.
The vault git repo auto-commits external changes on the next maintenance pass (so
history stays complete without committing on every keystroke): `user: edit <path>` for
one file, `user: edit <n> files: a, b, … (+k more)` for several, naming up to five.
`auto_commit_external_edits` off means the pass commits *nothing*: the safety-net commit
stages the whole worktree, so running it would commit the very edits the setting says to
leave alone.

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
(prefixed `<connector>_<tool>`). Each tool declares the conversation `kinds` that see it (default both);
`conversation_finish` and `status_update` are thread-only and never reach the desk
(2026-09-22). All are exposed every turn unless the count exceeds
`tool_expose_max` (48), in which case native tools stay and the rest are reachable via
`tool_search(query)` which returns matching definitions to load for the rest of the turn.

### 9.2 Native tool inventory (decided) and default policies
| Tool | Purpose | readOnly | Default policy |
|---|---|---|---|
| `fs_read` | file or line slice; one page of `tool_output_cap` chars with a paging footer, never spilled (`paged`); an image file (png/jpeg/webp/gif under `image_max_mb`) is returned as the image | yes | allow |
| `fs_list` | glob under a root | yes | allow |
| `fs_search` | regex/grep with context | yes | allow |
| `fs_write` | create/overwrite (guarded) | no | allow (within roots) |
| `fs_edit` | exact `old`→`new` replace, must be unique | no | allow (within roots) |
| `shell` | one command line, `cwd` in roots, env passthrough minus secrets, timeout, output cap; `background=true` (+ `title`) starts a supervised long-running process instead and returns its id, pid and log path at once (§9.6); Windows: cmd.exe or Git Bash per setting (restart-required), whose quirks are appended to this tool's description from `prompts/context/shell-<flavour>.md` at registration rather than injected into the harness block every turn (2026-09-22); Git Bash is located from the Git install (registry `GitForWindows\InstallPath`, the standard folders, then the `git` on PATH), never from a bare `bash.exe` on PATH, which on Windows is the WSL launcher in System32 (2026-09-22); everything a foreground call starts ends with the call (kill-on-close Job Object on Windows, process group elsewhere) | no | classify (pre-filter first) |
| `process` | `list` / `logs` / `stop` for background processes (§9.6); `logs` is the tail by default, paged forward with a footer (`paged`, never spilled) | list, logs yes | allow |
| `http` | GET/POST/… with headers/body; secret templating; an image response is returned as the image | no (GET treated read-only) | classify |
| `screenshot` | capture a monitor, the whole virtual screen, a region or a window (by title, Windows) as an image; `scale` downsizes; via PowerShell/System.Drawing on Windows, `screencapture` on macOS, `grim`/`gnome-screenshot`/`import`/`scrot` on Linux — no dependency | yes | allow |
| `knowledge_*`, `procedure_load`, `transcript_search`, `artifact_recall` | §8.4 | mixed | allow (core/ → ask) |
| `status_update` | publish the thread's status as a `status` event in its own transcript; threads only (`kinds`) | no | allow |
| `conversation_create / send / read / list / finish` | §6.7; `create` takes `notes` to pin; `finish` is threads only (`kinds`) | read/list yes | allow |
| `schedule_create / list / delete` | §11 | list yes | allow (caps enforced) |
| `item_post` | put a notification/setup item on the now board | no | allow |
| `secret_set` | store a secret the user pasted (value never echoed) | no | ask |
| `tool_search` | load deferred tool definitions | yes | allow |
| `promote_plan` | §15: what promoting a ref would change | yes | allow (no row; read-only) |
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
and no backticks or `$(`, and no word that, taken as a path relative to the shell's
working directory, is a protected path or sits under one (a `$VAR`/`%VAR%` path it
cannot resolve counts as one: 2026-09-22, `cat ~/.loom/secrets.json` was granted while
the fs tools would have asked). All stages pass → allow. Otherwise → classifier. Parse
failure → classifier. Nothing here denies.

### 9.5 Classifier
`prompts/classify.md` with inputs: conversation kind and goal, the last user message,
the tool name and args (framed as untrusted data — Goose), the roots and protected
paths, and the policy text (reversible and in-scope → allow; destructive, external
side effects, credential access, or out-of-scope → ask). Output: strict JSON
`{"decision":"allow|ask","reason":"…"}`; any parse failure → ask. Role: `utility` if
set else the conversation's role; priority inherits. Result and reason are stored on
the tool_call event `meta` and shown in the UI on hover.

### 9.6 Background processes (decided 2026-09-22)
A program that must outlive a tool call — a dev server, a bot, a watcher — is started
with `shell(command, cwd?, env?, background=true, title?)`. The command is judged exactly
like a foreground one (pre-filter, then classifier); `timeout_s` and `stdin` do not
apply. The tool writes the `processes` row first (persist before act, status
`starting`), spawns the child through the same shell flavour with stdout and stderr
appended to `artifacts/<conv>/proc_<id>.log`, stdin closed and its own process group,
then marks the row `running` with the pid and returns id, pid and log path at once. A
spawn that fails leaves a `failed` row carrying the error.

`loom.processes` (installed right after `loom.artifacts`) supervises. One watcher task
per process waits for it, marks the row `exited` with the exit code and posts an inbox
message to the owning conversation — source `process:<id>`, rendered
`[process "<title>" exited]`, carrying the last 600 chars of output — so the thread
learns of the exit at its next step boundary like any other event. `stop` (the tool,
the REST call or the UI) kills the process tree with the same helper the foreground
timeout uses and marks the row `stopped`; when someone other than the owning
conversation stopped it, the owner is told the same way.

The model reads and manages processes through one tool, `process(action, id?, offset?,
limit?)`: `list` (every row, newest first, with status, pid, owner and log size),
`logs` (paged like `artifact_recall`: the tail by default, `offset` pages forward with a
footer; `paged`, never spilled) and `stop`. Default policy `allow`: Loom started it. The
desk's now board carries one line per running process after the thread lines; every
conversation's panel lists the processes it started; the board's Processes group has
Stop and a log drawer.

Why a log file and not a pipe: a child whose stdout is a pipe into the backend dies
with the backend (EPIPE on its next write), and the whole point of a background process
is that it stays up. So children deliberately outlive a backend restart, and shutdown
leaves them alone. On start every `running` row is reconciled: pid alive → re-attached
with a 5-second poll (a process that is not our child cannot be waited on) and later
marked `exited` with an unknown exit code; pid dead → `lost`; `starting` (crashed
between the row and the spawn) → `failed`. Nothing is restarted and nothing is posted
for a reconciled row; the board simply stops listing it.

Containment (2026-09-22): on Windows every spawn goes into a Job Object through
`ctypes`, no `pywin32`. The foreground shell's job is kill-on-close, so everything a
foreground call started ends with the call, timeout or not — `taskkill /T` follows
parent links, and a `node … &` orphan whose bash had exited kept a port for hours. A
background process's job is not kill-on-close (the child must outlive a restart), but
while the handle lives `stop` terminates the job and reaches reparented descendants; a
restart loses the handle (a Job Object is a handle, not a row) and `stop` falls back to
the pid tree. Elsewhere the foreground shell starts its own session and the timeout
kills the group. Every Job Object call is best effort: a sandbox that refuses one is
logged and the old behaviour continues.

Deliberately left out, each a knob to add when something needs it: a restart policy
(Hydra had never / on_failure / always; Loom restarts nothing on its own), Job Object
memory caps, and log rotation (a rotating writer would have to sit on a pipe, see
above; the log grows until the conversation's artifacts are pruned, and `list` shows
its size so a chatty child is visible). Rows of archived conversations are pruned with
their artifacts.

## 10. Connectors and MCP

### 10.1 MCP client (scratch; built, protocol version `2025-06-18`)
- Transports: **stdio** (spawn `command args`, JSON-RPC 2.0 over newline-delimited
  frames, env from the row with secrets substituted; stderr kept in a 50-line ring and
  quoted in `last_error`; Windows spawns with `CREATE_NO_WINDOW` and resolves `command`
  through `shutil.which`) and **streamable HTTP** (POST per request with
  `Accept: application/json, text/event-stream`; the answer is one JSON message or an
  SSE stream read until the matching response arrives, dispatching notifications on the
  way; `Mcp-Session-Id` is echoed on later requests once seen). *Cut visibly:* the
  optional standalone GET listening stream is not implemented, so a server can push
  notifications only while a request of ours is open — which `tools/list_changed`
  survives, since a refresh is a request.
- Methods: `initialize`, `notifications/initialized`, `tools/list` (cached in
  `tools_cache`, refreshed on `notifications/tools/list_changed` or manual refresh),
  `tools/call`, `ping`. Resources/prompts/sampling/elicitation: not in v1 — a
  server→client request for any of them is answered with JSON-RPC error -32601 rather
  than ignored, so the server fails clearly instead of hanging.
- Tool results are flattened into the one string a `ToolResult` carries: text parts
  concatenated, other parts noted as `[image <mime>, N bytes]`; `isError` → `ok=False`.
- Tool annotations (`readOnlyHint`, `destructiveHint`) flow into policy defaults; no
  `tool_policies` row is inserted (§9.3 rule 3 already fails closed).
- `app.mcp` (`connectors/mcp/manager.py`) owns the rows: `connect/disconnect/reload/
  refresh/test/create/update/delete`, each keeping the live client, the registry and the
  row in step and emitting `mcp.status`. `start()` spawns one task per enabled row, so a
  slow server never delays startup.
- Failure: a server that fails to start or times out is marked `status=error` with
  `last_error`, its tools are removed from the registry, and one *setup* item is posted
  (de-duplicated while an undismissed one exists, so a retry loop cannot bury the now
  board). Never fatal to the backend. Reconnect with backoff 5s → 5min while enabled.

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
(POST) which re-executes the module; a full restart is never required for a connector
change but is always safe. Events from a connector go to the router (§11.2), as
`route_connector_event(app, name, title, content, meta, conversation_id)` — the
connector's `config.conversation_id`, or the desk.

Decided while building:
- **Statuses** are `disabled | needs_config | loaded | error`. A discovered package is
  `enabled = 1` on first sight (it is in `connectors/` because the user put it there),
  but one whose `config_schema` has unset **required** fields stops at `needs_config`
  with a setup item rather than running half-configured.
- **`config_schema`** is the subset the Settings form renders: `properties` with
  `type` / `enum` / `default` / `title` / `description`, plus `required`. `set_config`
  validates against exactly that (unknown key, wrong type or bad enum → 400) and fills
  defaults, so no setting exists that the UI cannot show. `conversation_id` is always
  accepted and routes the connector's events to a thread instead of the desk.
- **Tools** are renamed `<connector>_<tool>` and sourced `connector:<name>` by the
  loader, so a connector author writes plain `Tool`s.
- **Event tasks** run under `app.spawn("connector:<name>:events")`; an exception marks
  the connector `error`, posts an item and restarts with backoff 5s → 5min.
- **Surfaces** get a `SurfaceApi` (§12.3): `send`, `subscribe` (returns an unsubscribe),
  `answer_approval`, `list_conversations` — thin wrappers over `conversation.post`, the
  bus, `approvals.decide` and the conversations table, so a surface has the web UI's
  powers and no private path.
- The connectors folder is `<repo>/connectors/` (`LOOM_CONNECTORS_DIR` overrides it for
  tests). `connectors/README.md` is the author's guide and `connectors/example_echo/`
  the reference package: one read-only `echo` tool and an optional tick event source.

### 10.3 Knowledge entity per integration
When an MCP server or connector is added, the backend creates
`vault/connectors/<name>.md` (from `prompts/templates/connector-note.md`) with the
tool list and a placeholder for "when to use"; the agent fills it during first use.
The tool list lives between `<!-- tools:begin -->` and `<!-- tools:end -->` and is the
only part a refresh rewrites, so whatever the agent wrote around it survives.
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
GET  /conversations?kind&status&include_archived   POST /conversations {goal, notes?, roots?, preset?, title?, parent_id?, excerpt?}
GET  /conversations/{id}                PATCH /conversations/{id} {title?, preset?, step_budget?, clear_preset?, pinned_notes?}
GET  /conversations/{id}/events?after&before&limit&kinds&tail   (tail=1 → the newest `limit`, ascending; the UI's first page)
POST /conversations/{id}/messages {content, images?: [image_id]}  → 202 {inbox_id}; 400 empty (no text and no images) or unknown image id; 409 when no model is configured
POST /conversations/{id}/images  (raw image bytes; Content-Type image/*) → {id, mime, bytes}; 415 not an image, 413 over image_max_mb
GET  /conversations/{id}/images/{image_id} → the bytes, immutable cache headers; 404 on miss or a malformed id
POST /conversations/{id}/cancel         POST /conversations/{id}/archive     POST /conversations/{id}/clear (409 while running)
GET  /conversations/{id}/occupancy      GET  /conversations/{id}/artifacts     GET /artifacts/{id}?offset&limit
GET  /processes?conversation_id&status  GET  /processes/{id}   GET /processes/{id}/logs?offset&limit   POST /processes/{id}/stop   (§9.6; starting one is a model action, `shell(background=true)`)
GET  /conversations/{id}/inbox          rows posted but not yet in the transcript (surfaces render them as *queued*)
GET  /conversations/{id}/status         → {fields|null, owner|null, source: note|transcript|none}   PUT /conversations/{id}/status {fields} (owner `user`; a note block keeps its owner)
GET  /approvals?status                  GET  /approvals/{id}   POST /approvals/{id} {decision: allow|deny|allow_always, add_root?}
GET  /items?unseen&include_dismissed    GET  /items/{id}   POST /items/{id}/seen   POST /items/{id}/dismiss
GET  /vault/tree                        GET  /vault/note?path   PUT /vault/note {path, content}   DELETE /vault/note?path   DELETE /vault/folder?path (core/ refused)
GET  /vault/search?q&type&k             GET  /vault/history?path   GET /vault/recent-changes?n   GET /vault/diff?sha&path
POST /vault/revert {commit, path?}      GET  /vault/graph?center&depth   GET /vault/core   GET /vault/wanted
GET  /vault/procedures                  POST /vault/procedures/{name}/approve
GET  /settings  PATCH /settings {key: value}          (values + schema + groups; all-or-nothing validation)
GET/POST /providers  GET/PATCH/DELETE /providers/{name}   POST /providers/{name}/probe
GET/POST /presets    GET/PATCH/DELETE /presets/{name}     POST /presets/{name}/test
GET  /model-roles    PUT /model-roles/{role} {preset}     GET /models?provider → {providers: [{provider, models, error}]}
GET  /tool-policies  PUT/DELETE /tool-policies/{tool}     (tool may be a path-class key `<tool>:<prefix>`)
GET/POST /roots  DELETE /roots?path      GET/POST /protected-paths  DELETE /protected-paths?path → 202 + approval
GET  /secrets → {names, backend, warning}   PUT /secrets/{name} {value}   DELETE /secrets/{name}
GET/POST /mcp-servers  GET/PATCH/DELETE /mcp-servers/{id}   POST /mcp-servers/{id}/refresh|test|reload
GET  /connectors  GET/PATCH /connectors/{name} {enabled?, config?}   POST /connectors/{name}/reload
     (connector rows carry config_schema, description, facets, tool names and missing_config)
GET/POST /schedules  GET/PATCH/DELETE /schedules/{id}   POST /schedules/{id}/run
GET  /maintenance    POST /maintenance/run
GET  /system/health  /system/version  /system/queue  /system/logs?since&level&limit → {records}
GET  /system/setup   POST /system/setup {provider?, model, embedding_model?, timezone, openrouter?}
POST /system/reindex                    POST /system/open {path}  (folder in the OS file manager; APP_HOME or allowed roots only)
GET  /system/promote/plan?ref   POST /system/promote {ref, acknowledge_protected}   POST /system/rollback
GET  /system/promote/log   GET /system/snapshots   GET /system/supervisor   POST /system/restart
```
Update verbs: `PATCH` for partial row updates (providers, presets, conversations, mcp
servers, connectors, schedules, settings); `PUT` where the body replaces the whole value
(roles, tool policies, secrets, vault note). All mutations emit a matching WS
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
tool.result                         data: {call_id, name, ok, preview, artifact_id?, images: [{id, mime}], event_id,
                                           policy, decision, reason}
inbox.posted                        data: {id, source, content, created_at, meta} — fired the moment a
                                    message is posted; a surface drops the row from its *queued* list
                                    when the `user` event carrying `meta.inbox_id` is appended
models.changed                      data: {kind: provider|preset|role, name}
approval.requested|resolved         data: approval row
process.started|exited|stopped|failed|lost   data: process row (§9.6)
schedule.created|updated|deleted    data: schedule row
item.created|updated                data: item row
notification                        data: {title, body, item_id}
knowledge.changed                   data: {paths}
settings.changed                    data: {keys}
models.changed                      data: {kind: provider|preset|role, name}
connector.status|mcp.status         data: row
system.queue                        data: {depth, running}
maintenance.progress                data: {stage: start|consolidate|curate|housekeep|done,
                                           message}
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
`ui-dist/` and served by the backend at `/`. Appearance is two settings: `general.theme`
(system / light / dark → the `dark` class on `<html>`) and `general.palette` (default,
slate, warm, forest, nord, mono → `data-palette` on `<html>`). A palette only overrides
the Tailwind colour variables the components already use (`neutral-*` surfaces, `sky-*`
accent, `white`), so no component knows palettes exist; the last applied pair is
remembered in `localStorage` so a reload does not flash the default look.

### 13.1 Screens
- **Setup** (first run only, §3.3).
- **Desk** (default). Left: conversation area with streaming content, collapsible
  reasoning, tool call rows (name, args, policy badge, result preview, artifact link),
  inline approval cards. Messages typed while a turn runs (or waits for an approval)
  show as **queued** cards at the foot of the transcript — `inbox.posted` puts them
  there, the `user` event with the matching `meta.inbox_id` replaces them — so the
  person sees that the text landed and when it will be read. The composer draft is kept
  per conversation (sessionStorage) and survives navigating away and back. Images attach
  by paperclip, paste or drop; each uploads at once (`POST …/images`) and shows as a
  thumbnail chip until sent; user bubbles, queued cards and tool rows with images show a
  thumbnail strip served from `GET …/images/{id}` (click opens the full image). Right rail:
  **Now board** (items grouped: needs you / threads / queued / processes — each
  running process with Stop and a log drawer), each item actionable. Inbox rows posted
  by a background process carry a *from process* caption. Header: occupancy meter (used/limit, colour by proximity to compaction),
  queue indicator, model role in use.
- **Threads**. List (status, goal, last update, depth tree). Thread view = same
  conversation component + a **Status/plan panel** rendering the status as an editable
  form (`GET`/`PUT /conversations/{id}/status`), the **pinned notes** (links; unpin; add
  by path → `PATCH pinned_notes`), a link to the walkthrough (`conversations.walkthrough`,
  the daily-log note), artifacts, a **Processes** section (the processes this
  conversation started: status, pid, log size, Stop, log drawer; §9.6), and "spawned by" links. Create-thread dialog: goal,
  notes to pin (optional), roots, role.
- **Vault**. Tree by type; note view/edit (Markdown editor with wikilink completion and
  frontmatter form); backlinks and outgoing links; search (fused); **Recent changes**
  (git log with diff and revert); wanted notes (dangling links); core block fill meters;
  optional graph view (`/vault/graph`) rendered with inline SVG — start focused, expand
  on demand, no physics simulation.
- **Approvals** badge + list (pending across all conversations), each with the
  classifier reason if any and the `allow always` option; when the guard suggested a
  root, adding it is an opt-in checkbox (unchecked by default) that pairs with
  `allow always` — the same on the inline card and the Approvals screen.
- **Settings** — every knob (§13.2), rendered from schema.
- **System**. Version and release pointer, supervisor status, promote/rollback with
  progress (a ref → the plan from `GET /system/promote/plan`, with the protected files
  shown as an explicit "I have read these" checkbox that sets `acknowledge_protected`;
  stages stream from `promote.progress` and the history comes from
  `GET /system/promote/log`), logs tail,
  queue, connector and MCP status, reindex, snapshot list, and maintenance: whether a
  pass is running, when the nightly one is next due, the last pass's report, and a
  *run maintenance now* button (`POST /maintenance/run`, live via `maintenance.progress`).

### 13.2 Settings inventory (all UI-visible; groups map to `config.py` dataclasses)
- **General**: display name, timezone, bind address, LAN token, theme
  (system/light/dark), palette (default/slate/warm/forest/nord/mono).
- **Models**: three tabs. *Providers* (CRUD: name, kind, base URL, credential from the
  secret picker, default params, concurrency, enabled; probe button lists live
  models). *Presets* (CRUD: name, provider, model picked from the probe list or typed,
  every generation/window parameter with provider defaults shown as placeholders,
  reasoning mode, repetition/presence/frequency penalties, a *multimodal* toggle with
  the per-image token estimate, notes;
  "test" runs one short completion; shows calibrated
  `token_factor_seed` read-only; usage count of roles/threads pointing at it; delete
  blocked while referenced). *Roles* (five dropdowns → preset; `fallback_role`;
  `background_max_tokens`). Every conversation's header shows which preset it is
  using, and a thread's create dialog and detail panel let the user pick one.
- **Conversation**: step budgets (desk/thread), `compact_threshold`,
  `forced_compactions_per_turn`, `model_error_retries`, `reasoning_replay` +
  `reasoning_replay_max_chars`, `tool_output_cap`, `pinned_note_chars`, artifact
  retention, `thread_archive_days`, `max_thread_depth`.
- **Knowledge**: `preload_budget_tokens`, `preload_max_entities`,
  `retrieval_threshold`, `core_block_chars`, vault poll interval, maintenance idle
  seconds and nightly time, curation similarity threshold, auto-commit external edits.
- **Permissions**: per-tool mode table (allow/ask/classify, grouped native / MCP /
  connector), shell read-only command list, allowed roots, protected paths, shell
  flavour (cmd/Git Bash/PowerShell/sh), classifier role, classifier timeout (a verdict slower
  than this becomes an ask; one retry when the provider is busy).
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
`schedules`, `promote REF [--acknowledge-protected]` (a promotion whose diff touches protected paths is refused with the file list until the flag is given), `rollback`. All commands are thin REST clients; the CLI
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

That workflow is a **procedure note, not code** (charter §5.9): `promote.install`
ensures `procedures/self-modification/SKILL.md` exists in the vault from
`prompts/templates/procedure-self-modification.md` (written by Loom, so approved at
write time, §8.4). Changing how self-modification is done is a vault edit; only
isolation, promotion-with-rollback and protected paths are Python.

### 15.2 `promote(ref)` flow (tool → backend → supervisor)
```
backend:  1 ref must resolve to a commit in the repo the backend runs from
              (bootstrap.repo_root); the worktree that holds it must be clean
          2 protected-path check: git diff --name-only <current>..<ref> ∩ protected_paths
              (compared repo-relative and absolute) → non-empty and not acknowledged →
              error result naming the files; the person confirms on the System page or
              the model calls promote(ref, acknowledge_protected=true)
          3 run the suite in the ref's worktree (python -m uv run pytest -q -x, 600s; a ref
              checked out nowhere gets a throwaway worktree releases/test-<sha7>);
              failure → error result with the output (spilled to an artifact when long)
          4 POST supervisor /promote {ref, conversation_id}
supervisor:
          5 snapshot: sqlite backup API → snapshots/<ts>/state.db; vault `git tag pre-<ts>`;
              prune to system.snapshot_retention
          6 create_release: fetch into releases/src.git, `git worktree add --detach`
              releases/<YYYYMMDD-HHMMSS>-<sha7>, `uv sync --frozen` (plain sync as fallback),
              `npm --prefix ui ci && run build` only when ui/ moved and ui-dist/ is missing
              or older; python_for(release) is the release's own .venv
          7 probe: start the release with LOOM_HOME pointing at a scratch dir holding a COPY
              of state.db (so migrations run on the copy) on port+2, wait for
              /api/system/health ok within 60s, stop it; else abort
          8 write `previous`, then `current`; stop live backend, start new; watchdog
              system.promotion_watchdog_seconds: health must be reached and stay ok
          9 failure → pointers back, restart the previous release, restore the snapshot when
              PRAGMA user_version advanced
          10 every stage appends {ts, stage, ok, message, ref, conversation_id} to
              APP_HOME/promote.log; stages are snapshot|release|probe|swap|watchdog|rollback
              and the terminal done|failed
```
**Two tools, decided 2026-09-20.** `promote` is `ask` by policy, so the dispatcher asks
before the handler runs — which means the handler cannot put the protected-file list
into the approval's reason. So the list comes from a read-only companion:
`promote_plan(ref)` (`GET /system/promote/plan?ref=` for the UI) returns the commit, the
diff, the protected files and whether the tree is clean. The tool description tells the
model to call it first and report it; the person therefore sees the files before the
approval card, and `acknowledge_protected` is an assertion that they did — the call
itself still goes through the ordinary `ask`. `promote.request_promotion` is the single
codepath for steps 1–4, called by both the tool and `POST /system/promote`, so a click
and a model call cannot disagree; failure classes come back as a `code`
(`no_repo|bad_ref|dirty|protected|tests_failed|no_supervisor|supervisor_error`) which
the REST layer maps to one status each (409 for protected/dirty, 422 for a failing
suite, 503 for no supervisor).

The turn that called `promote` ends at step 4 with a `system` event; after restart the
thread resumes (§6.3 step 1) and reads the outcome from the promote log. The backend
tails `promote.log` (`promote.read_progress`, a durable byte offset in
`promote.log.offset`), republishes each line as `promote.progress`, and on a terminal
line posts a now-board item (`notify` on failure, §17) and appends the outcome as a
`system` event to the conversation recorded in the log. The first read happens in
`promote.start`, because the stages that matter most are written while the backend is
not running.

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
`rollback` (`POST /system/rollback`, or the supervisor's own `/rollback`) → the
supervisor swaps `current` to the previous release, writes the release it left as the
new `previous`, and restarts; if the live `PRAGMA user_version` is ahead of the newest
snapshot, that snapshot is restored (state written since is lost, and the UI says so
before confirming). It logs `rollback` then `done` like any other flow.

`POST /system/restart` and `GET /system/supervisor` are the same client's other two
buttons. With no supervisor running — development under `--no-supervisor` — every one
of them answers `503` with "supervisor not running — start Loom with `loom serve`", and
the `promote` tool returns the same sentence rather than pretending.

## 16. Prompts (all model-facing text lives here)
```
prompts/system.md            soul + operating rules (memory discipline, ladder, when to spawn,
                             verification by judgment, no relative dates, evidence in walkthroughs)
prompts/kind-desk.md         desk-specific: now board semantics, routing to threads
prompts/kind-thread.md       thread-specific: status duty, pins, finish + walkthrough, budget line
prompts/tools/<tool>.md      one description per native tool (loaded into the registry)
prompts/classify.md          permission judge
prompts/summarize.md         compaction fold
prompts/maintenance-consolidate.md  / maintenance-curate.md
prompts/status-safety-net.md
prompts/preset-test.md       the one short completion `POST /presets/{name}/test` sends
prompts/templates/*.md       new note templates (device, project, connector, procedure SKILL.md,
                             procedure-self-modification: the vault's own self-mod workflow)
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
| Background process exits | row `exited` with the code; the owning conversation gets an inbox message with the last output; nothing restarts it |
| Background process stopped by someone else | row `stopped`; the owning conversation is told |
| Background process gone at backend start | row `lost` (`failed` if it never got a pid); no message; the board stops listing it |
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
watchfiles (polling), GitPython (git CLI), cryptography (see §4.5), pywin32 (the Job
Objects of §9.6 are reached through `ctypes`).
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
4. **Threads** — conversation tools, spawn payload, `status_update` + safety net,
   pinned notes, events to parent, finish + walkthrough, depth cap, budget line,
   threads UI + status panel.
5. **Integrations** — MCP client (stdio + HTTP), MCP settings UI, connector loader +
   config forms, secrets store + picker, setup items, connector note template.
6. **Triggers** — schedules + parser + scheduler, router, items/now board, policies,
   notifications, caps, UI.
7. **Maintenance** — consolidation sweep, curation, housekeeping, vault git commits,
   recent-changes revert.
8. **Self-modification** — releases layout, `promote` end to end, protected paths,
   snapshots, rollback, System UI progress, auto-rollback in supervisor.
   *Built 2026-09-20 except the System screen's promote panel, which belongs to the UI
   package and is specified in §13.1; everything it needs is served.*

## 21. Implementation questions (decided 2026-09-20; see charter decision log)
- Ollama: native `/api/chat` for the `ollama` provider kind; no probe.
- Windows shell flavour: `auto` → Git Bash if present, else cmd.exe; setting exposed, and
  restart-required because the flavour's quirks are composed into the `shell` tool's
  description at registration (§9.2).
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
