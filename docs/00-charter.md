# Loom — charter

**Name:** Loom (provisional, chosen 2026-09-20; subject to change).

**Status:** brainstorm, 2026-09-20. This document is the living design for a new agentic
assistant. It was drafted inside the Hydra repo so Hydra and the harness survey could serve as
reference material; the reference documents travel with these docs under
`reference/`. Loom is *not* a Hydra feature; it is a separate project.

Companions (reading order in `README.md`): `01-walkthroughs.md` traces the four example
requests through this design; `02-research-takeaways.md` records what was adopted,
adapted and rejected from the harness survey; `03-technical-design.md` is the
implementation-level design; `04-soul.md` is the draft system prompt (identity and
operating rules); `reference/` holds the survey and Hydra's context write-ups.

Sections grow as the idea develops. §1–§3 are the rules and are meant to stay stable.
§4 onward is the assistant itself and is expected to churn.

---

## 1. Purpose of this document

Two audiences, one text:

1. **The user**, who reads it at a bird's-eye / block-diagram level to confirm the shape
   is right. Code-level detail is not required here; *what happens behind the scenes and
   why* is.
2. **The implementer** (a future Claude Code session), who is handed this document and
   expected to fill in the technicals. The implementer inherits the ethos below and is
   given wide latitude on design details, in exchange for one obligation: **anything
   implemented is implemented to the full extent it is reasonable to assume it should
   be.** A settings screen has every relevant setting. A tool has every argument it
   obviously needs. No proof-of-concept stubs presented as done.

The short-term goal is a fully fleshed-out idea: high-level prose plus technical detail
wherever it can already be pinned down.

## 2. Ethos

Each item is a cause/effect pair: the principle, then what it forces in practice.

### 2.1 Robustness
Handle the widest range of situations with the simplest possible mechanism.

- **Effect:** few, general codepaths. A behaviour that only works when circumstances are
  perfect is a bug, not a feature.
- **Effect:** no special-casing. If two situations are handled differently, there must
  be a reason that would survive a "why not one path?" question.
- **Effect:** failure is an expected input. Every mechanism defines what it does when the
  model, the tool, the network, or the user misbehaves, and that behaviour is the same
  general behaviour, not a per-site patch.

### 2.2 Consistency
One professional, smooth experience across UIs, circumstances, and machines.

- **Effect:** UI and backend are never out of step. If the backend has a setting, the UI
  exposes it; if the UI shows state, the backend owns it.
- **Effect:** no common setting requires editing a config file. Config files may exist as
  a storage format, never as the only interface.
- **Effect:** the same action produces the same result on every surface and every
  machine the assistant runs on. Platform differences are absorbed in one place, not
  leaked into behaviour.

### 2.3 Simplicity
Using the assistant is easy. No task should require ten minutes of user-driven setup.

- **Effect:** sensible defaults everywhere; configuration is an override, not a
  prerequisite.
- **Effect:** the assistant does the configuring where it can (discovering, inferring,
  remembering), and asks only when a wrong guess would be costly.
- **Effect:** the interaction model is small and learnable. Power comes from depth of the
  few concepts, not breadth of features.

### 2.4 Capable-model bar
The reference model is **Qwen 3.6 35b-a3b** (the dense Qwen 3.6/3.8 27b class is
equivalent): open-weight, fast, and genuinely capable, hosted locally or via OpenRouter.
Larger models are an optional escalation. Models in the 4b–9b range are *extras*: usable
for narrow, well-specified jobs (summarising, classification, embedding) or not at all.

- **Effect:** the architecture assumes a competent agent. It plans, uses tools, and
  searches knowledge itself. No scaffolding whose only purpose is to absorb a weaker
  model's mistakes — that scaffolding calcifies and bites later.
- **Effect:** context is still a budget, for latency, cost and focus — not because the
  model cannot cope. Turns are assembled from what the request touches.
- **Effect:** deterministic helpers (alias index, link graph) are used where they are
  cheaper and more reliable than a model call, never as a substitute for model
  judgment. The model sees the full tool set and decides for itself.
- **Effect:** a small model never holds a role that needs judgment (desk, worker). It may
  hold a narrowly scoped utility role behind a fixed contract.

### 2.5 (Reserved)
Further items are appended here in the same cause/effect form.

## 3. Technical constraints

These bound *how* things are built, independent of *what* is built.

- **Minimal.** The smallest structure that does the job. Dead generality is as much a
  smell as special-casing.
- **Clean.** Readable, consistently styled, obvious data flow. One owner per concern.
- **Efficient.** No wasted model calls, no redundant context, no busy loops. Cost in
  tokens and latency is a first-class design input.
- **Dependency-light.** Dependencies are allowed but earned. Prefer building a small
  element from scratch over importing a large package to use a sliver of it. When a
  dependency is taken, it should be used broadly enough to justify its weight.
- **Fully realised.** Restating §1: whatever is built is built completely. Scope may be
  cut deliberately and visibly, never silently.

## 4. Relationship to Hydra

Hydra is reference, not foundation. Things worth carrying over as *lessons* (not as
code) include:

- Single context builder, single agent-loop entrypoint — no parallel paths.
- System-of-record written before acting on it; caches rebuildable from human-editable
  truth.
- Fail-closed permissions.
- Model-facing prose kept as editable text, never hardcoded.
- Agent identity separated from mutable state.
- "No small-model crutches": stay model-flexible; do not build around a model's gaps.

Where the new assistant deliberately diverges from Hydra, this document says so and
says why.

## 5. The assistant

### 5.1 What it is / who it is for

A personal second brain for one user: a single always-available assistant that can act
on anything the user has connected or told it. Not a coding tool with memory bolted on;
not a chatbot with a plugin list. The distinguishing requirement is that it draws on
*everything it knows and is connected to* without the user having to re-establish
context per request.

Representative requests (illustrative, not a specification):

1. "Heat up the Ender 3." — the printer was introduced once, long ago; the assistant
   must know what it is, where it is, and how.
2. "Add a play-random feature to the music bot." — an existing Node project on disk.
3. "I need a program to visualise coordinates on a 3D map." — a new project, built in a
   background thread in a known project space, while the desk stays free. Later:
   "What's the state of the 3D map project?" answered from live work state.
4. "What homework do I have this week?" — pulls from Canvas plus poorly formatted
   assignments buried in school email and syllabi, via previously made connections.

Request weight varies: most are one-shot (quick question, small task), some grow into a
discussion, some launch a long-running task with back-and-forth. The assistant must
move between these without a mode switch.

**Identity (decided 2026-09-20).** Loom is a general assistant with a technical focus.
It prioritises getting things done and managing the many virtual pieces the user
interacts with: devices on the network, projects on disk, accounts and services.
Its communication is concise and curt unless directed otherwise, organised as cause and
effect. The full identity and operating rules are drafted in `04-soul.md`; that file is
the source for `prompts/system.md` and contains no mutable state.

### 5.2 Interaction surfaces

- **Web UI — first class, full capability.** Desk, thread list and scoped thread chats,
  vault browser/editor, settings, approvals, the "now" board (active threads, queued
  items, today), transcripts and logs.
- **CLI — second.** Same capabilities that make sense in a terminal: talk to the desk or a
  thread, answer approvals, status, scriptable one-shots.
- **Others** (Discord, Telegram, ...) arrive as the *surface* facet of a connector
  (§5.8). None are needed now; adding one must be easy.

**One surface contract.** The backend exposes a single API to all surfaces: list/open
conversations, send a message, subscribe to the event stream (tokens, tool calls,
approval requests, status changes, notifications), answer an approval. Every surface
consumes the same stream and renders what it can; unrendered event kinds are ignored.
Notifications are ordinary events; a surface registers itself as a notification target
(a setting). Effect: no surface-specific logic in the backend; the web UI is simply the
most complete client.

### 5.3 Core structure (proposed 2026-09-20)

**Diagnosis.** The session is a transcript with a lifetime. Making it the unit of memory
yields memory *of conversations*; every common workaround (tiered memory, embedding past
sessions, search-by-context tools) is retrieval over conversations. The examples above
need facts, procedures, and live work state — not recalled dialogue. So the unit of
memory changes, not just its retrieval.

**Four blocks.**

```
 Surfaces (UI / CLI / messaging)
        │
        ▼
 ┌─────────────┐   resolves referents    ┌──────────────────────┐
 │    Desk     │ ──────────────────────► │      Knowledge        │
 │ one endless │                          │ entities: devices,    │
 │ conversation│ ◄────────────────────── │ projects, people,     │
 └──────┬──────┘   injects only what      │ connectors, procedures│
        │          the request touches    └──────────▲───────────┘
        │ spawns when work is long                   │ written back
        ▼                                            │ after every unit
 ┌─────────────┐                          ┌──────────┴───────────┐
 │   Threads   │ ──── status ───────────► │    Capabilities       │
 │ parallel    │                          │ tools, connectors,    │
 │ workers     │ ──── use ──────────────► │ skills                │
 └─────────────┘                          └──────────────────────┘
```

- **Desk.** One persistent conversation with the user. Never started or ended. Rolling
  transcript window; short and cheap *because* nothing important may live only there.
  Effect: no session boundary to lose context across.
- **Knowledge.** The memory. Entities, not transcripts (§5.4). Agent-written,
  human-editable markdown, indexed for retrieval. "Familiarise yourself with X"
  *produces* an entity and its procedures. Transcripts are retained as logs and
  searchable as a last resort, but they are not the memory.
- **Capabilities.** Tools, connectors (printer, Canvas, mail, filesystem, ...), skills.
  Connecting something is usually just a knowledge entity (§5.8); code only when auth,
  protocol, events or a surface demand it. All tools are always available.
- **Threads.** Long work spawned from the desk, each with its own context and
  transcript, running in parallel. A thread publishes status (goal, progress, blockers,
  decisions) into its project entity as it works. The desk answers "what's the state
  of X?" by reading that entity, not the thread transcript. Threads are where the
  session model is *correct*: task-bound, focused, disposable; their durable output is
  knowledge and files.

**One conversation primitive.** Desk and thread are the same thing configured
differently: a *conversation* = scope + lifetime + transcript. Desk: scope
"everything", lifetime infinite. Thread: scope "one project/task", lifetime until done.
The user talks primarily to the desk but may enter any thread directly. Messages into a
working conversation (from the user or from the desk) land in one inbox and are
injected at the next step boundary. Effect: one codepath for both; no separate
"delegation protocol".

The primitive also carries: **conversation tools** (`create / send / read / list` — the
surface contract exposed to the model, so the desk spawns and talks to threads the same
way a surface does; a spawn payload is goal + scope + a model-selected excerpt, never the
whole window); **compaction** (rolling window + summary, allowed to be lossy because
durable facts live in the vault); **lifecycle** (finished threads stay addressable and
resumable; maintenance archives them after configurable inactivity); and **persistence
per step**, so a restart resumes where it stopped.

**Per-request flow.** Resolve referents against knowledge → assemble context from only
the touched entities → choose a rung on one ladder:
*answer / act inline / discuss / spawn thread*. Escalation (a question becomes a
project) is moving up a rung, never a new mode. The alias/link index pre-loads obvious referents cheaply; the model has
knowledge-search tools and its own judgment for everything else.

**Consolidation invariant.** Anything durable learned in a unit of work reaches
knowledge. Mechanics: inline, the model calls `knowledge_write` when warranted (no forced
trailing step — most desk exchanges record nothing); asynchronously, the maintenance role
sweeps recent transcripts for durable facts that were missed. Threads get one safety net:
a thread turn that wrote no status gets a status write at turn end. This is what keeps
the desk window small and prevents regression to transcript memory.

### 5.4 Knowledge: an Obsidian-style vault (proposed 2026-09-20)

Memory is *semantic* (what is true: entities) and *procedural* (how: procedures,
skills) first; *episodic* (what happened: transcripts) last.

- **Format.** A folder of markdown notes with YAML frontmatter and `[[wikilinks]]`,
  organised by entity type (`devices/`, `projects/`, `people/`, `connectors/`,
  `procedures/`, ...). The user can open the vault in Obsidian. Effect: memory has no
  UI/disk disagreement by construction, and the user can curate directly.
- **Resolution for free.** Frontmatter `aliases:` ("the printer", "ender 3") gives
  referent resolution; links give the graph. Both are deterministic and cost no model
  call.
- **Pre-load rule.** A resolved entity is injected with its first-hop linked notes,
  procedures first, within a fixed token budget. One rule for every entity type; no
  per-entity configuration. Everything deeper the model reads with knowledge tools.
- **Granularity.** One note per entity holding stable facts; linked notes for
  procedures and decisions. A project note carries a delimited *status* section that
  its thread owns and rewrites (goal, progress, blockers, next). Working position — may
  move to a separate linked status note if churn is a problem.
- **Indexes are caches.** Alias table, link graph, embeddings: all rebuildable from the
  markdown, rebuilt on file change. Personal scale (thousands of notes) → brute-force
  cosine over a flat embedding file; no vector database.
- **Curation.** Drift (duplicates, stale facts) handled by a background maintenance turn
  using the same consolidation mechanism, plus direct user edits.

### 5.5 Tools and permissions (proposed 2026-09-20)

Deliberately lighter than Hydra's two-layer model: Hydra governed many agents with
different purposes; this is one assistant acting for one user. Policy is **global and
static**, evaluated at the tool-call boundary. Nothing about permissions or scope is
assembled per turn.

- **Tool set.** All tools are always available, with short descriptions. If the set
  grows large (many MCP servers), the general fix is deferred loading: a `tool_search`
  the model calls itself. The model decides what it needs; no heuristic gating.
- **Per tool, one setting:** `allow` / `ask` / `classify`, editable in the UI.
  Defaults: read-only tools *allow*; side-effecting tools *ask*; shell *classify*.
  Tools declare read-only via the standard MCP tool annotation, so connectors and MCP
  servers get sane defaults without extra configuration.
- **Shell pre-filter can only grant, never deny.** The command is parsed into pipeline
  stages; if every stage is a known read-only command, it runs with no model call.
  Anything else — including a parse failure or chaining with `&&` — falls through to
  `classify`. Nothing static ever blocks a command (Hydra's static filters did, and
  models fought them).
- **`classify`.** The model judges the specific call against a short written policy
  (prompt file), seeing the user's request or thread goal, the proposed call, and the
  policy: reversible and in-scope → run; destructive, external, or out-of-scope → ask.
  One mechanism, used only where blanket rules cannot decide (shell, raw http). One
  extra model call; sound with the bar model.
- **Filesystem guard — the only argument rule.** *Allowed roots* (project spaces, vault,
  scratch) and a *protected list* (supervisor, permission config, secrets). Applied at
  the path layer, so every tool that touches a path inherits it. Outside roots → ask,
  with an "always allow this root" option that writes the root to settings. Roots grow
  *only* this way; a knowledge note can never widen access.
- **Secrets never reach the model.** Tool arguments may carry `{{secret:NAME}}`; the
  harness substitutes just before execution and redacts in transcripts.
- **Protected paths are enforced at promotion** (§5.9), not only at edit time. Shell
  cannot be path-guarded reliably, so the backstop is: promotion refuses commits that
  touch protected paths without explicit user approval — robust regardless of how the
  edit was made.
- **Approval is an event** on the surface stream. The turn pauses; any surface can
  answer; approvals pending from proactive turns sit on the now board.
- **Fail-closed** remains: an unknown tool or an unset policy behaves as *ask*.

### 5.6 Models and providers (proposed 2026-09-20)

- **Providers → presets → roles** (Hydra's layering, kept because it works). Providers
  are endpoints (several Ollama hosts, OpenRouter, more later), presets are named
  model configurations on a provider (model + every generation and window parameter),
  roles point at presets: *desk* (conversation, routing, quick answers), *worker*
  (threads: coding, architecture), *maintenance* (consolidation, curation, run when
  idle), *utility* (optional small model behind a fixed contract: summarise,
  classify), *embedding*. All three layers are managed in the UI; a thread may be
  given a specific preset. Default: desk, worker and maintenance share the bar-model
  preset (§2.4) — one resident model.
- **One protocol.** Ollama and OpenRouter both expose OpenAI-compatible chat
  completions. One scratch-built client; provider-specific knobs (Ollama `keep_alive`,
  `num_ctx`) as thin options. No LiteLLM-class dependency.
- **Local contention.** One GPU. With one shared model the problem is request
  ordering, not model swapping: background work (threads, maintenance, proactive turns)
  yields to user-facing turns via a small priority queue in the model client. Preemption
  happens at model-call boundaries, so background steps carry a max-tokens bound to keep
  desk latency predictable.

### 5.7 Triggers and proactivity (proposed 2026-09-20)

The assistant may act without a user message.

- **Trigger sources:** schedule (created by the user in the UI or by the model via a
  `schedule` tool — "remind me the day before" is the model scheduling explicitly, so
  no separate "knowledge-derived" trigger exists); connector event (new mail,
  assignment posted, print finished); thread event (needs input, blocked/failed,
  finished). A child's events route to the conversation that spawned it (desk or
  parent thread); that conversation verifies the work by judgment — reading the
  walkthrough and evidence, spot-checking, or spawning a verifying thread for large
  builds. No hardcoded verifier call.
- **One turn primitive.** A trigger produces a turn whose input is an event instead of
  user text, on the desk or on the relevant thread. No separate "automation" path.
- **Policy per trigger:** *interrupt* (notify the user now) or *queue* (appears on the
  desk's "now" board next time the user looks). UI-visible. Defaults: thread events
  *interrupt*; schedule and connector events *queue* unless their creator set
  otherwise.
- **Guardrails.** Proactive turns may act within their scope; anything beyond needs
  approval (§5.5). Background priority (§5.6).

### 5.8 Connectors (proposed 2026-09-20)

**Most connections need no code.** Integration has three tiers; use the lowest that
works.

| Tier | What it is | Covers | Examples |
|---|---|---|---|
| 1. **Knowledge only** | an entity + procedure notes; executed via shell / http | tools | Ender 3 on the LAN, existing repos, most network devices |
| 2. **MCP server** | external server over the standard protocol; configured in settings | tools | Canvas, mail, anything needing real auth/protocol handling |
| 3. **Native connector** | in-process package | tools, **events**, **surface** | Discord, Telegram, a mail watcher |

- Tier 1 is the default and what "familiarise yourself with X" produces. Nothing to
  install; the procedure *is* the integration.
- Tier 2 reuses the MCP ecosystem. The client is a small protocol (JSON-RPC over
  stdio/HTTP) — scratch-built, no SDK dependency.
- Tier 3 exists only for events (§5.7) and surfaces (§5.2), which MCP does not provide.
  Discovered from a `connectors/` folder; import failure disables and reports, never
  fatal. Its contract is small so the assistant can build one for itself (§5.9).
- Every integration, at any tier, is also a knowledge entity: when and how to use it,
  setup state, a *reference* to credentials. Secrets live in the OS keyring (permission-restricted file fallback for headless
  machines — not encrypted; see `03-technical-design.md` §4.5), never in the vault.
- Setup that needs the user ("log in when ready") is a queued item on the now board.

### 5.9 Self-modification (proposed 2026-09-20)

Goal: "look at this transcript, why did the tool call fail?" → diagnose and fix its own
code. "Give yourself a Discord connector" → build, test, bring live, ask for login.

**Position.** The assistant's own repository is an ordinary project entity worked on by
ordinary threads. Versioning is git; development branches are git worktrees. No bespoke
self-modification framework — that would be exactly the special codepath §2.1 forbids.
The workflow lives as a procedure note in the vault. Code exists only for the three
things a process cannot provide:

1. **Isolation.** A self-mod thread edits a git worktree with its own scratch state
   directory, never the running tree. It can run the test suite and a staging instance
   on another port.
2. **Promotion with rollback.** A tiny, boring *supervisor* runs the live checkout.
   Promotion is a tool: commit → supervisor starts the new version on a probe port →
   health check → swap and restart → automatic rollback on failure or on a
   post-promotion watchdog. Staging = test suite + health probe on a probe port, not a
   full parallel instance. The state directory is snapshotted before every promotion
   (SQLite copy); the vault is itself git-tracked, so memory has history and rollback
   through the same mechanism.
3. **Protected paths.** The supervisor, the promotion logic, permission configuration,
   secrets. The filesystem guard (§5.5) asks before editing them, and promotion refuses
   commits touching them without explicit user approval. The assistant cannot silently
   widen its own permissions.

**Restart is the deploy.** No hot reload. Conversations must survive restart anyway
(crash recovery), so promotion ends the calling turn and the thread resumes afterwards.
The UI is a built bundle served by the backend, so one promotion covers backend and UI;
the build toolchain must be available to the thread.

**Diagnosis needs nothing new.** Transcripts, logs and events are readable data like
anything else in knowledge.

### 5.10 Configuration and settings
*To be developed.* Everything in §5.6–5.9 that is a knob is a UI-visible setting:
model roles, trigger policies, notification targets, MCP servers and connector setup,
per-tool permission mode, allowed roots and protected paths.

### 5.11 Open questions
- Desk window size and exactly what the "now" board pins.
- Status: in-note section (current lean) vs. separate linked note.
- Connectors: hot-load individually, or always via promotion + restart? Lean: restart
  only, until it hurts.
- The `classify` policy text: what counts as in-scope for a thread vs. the desk?
- Projects vs. tasks: decided 2026-09-21 (see the decision log) — the scope note is
  optional; a thread without one is a task.
- Pre-load token budget and compaction thresholds (tuning, not design).
- Companion doc: `03-technical-design.md` carries implementation-level detail.

## 6. Decision log

Dated, one line each. Records what was decided and the one-line reason, so later
sessions do not re-litigate.

- **2026-09-20** — Project charter created; ethos = robustness, consistency,
  simplicity; Hydra is reference material only.
- **2026-09-20** — Core structure proposed: Desk / Knowledge / Capabilities / Threads;
  memory unit is entities, not sessions; one escalation ladder, no modes.
- **2026-09-20** — Small open-weight local models are the primary tier; proposed ethos
  §2.4 "Frugal by construction".
- **2026-09-20** — Desk and thread are one conversation primitive (scope + lifetime);
  user may enter threads directly.
- **2026-09-20** — Knowledge is an Obsidian-style vault; indexes are caches; no vector DB.
- **2026-09-20** — Proactivity allowed; triggers produce ordinary turns; per-trigger
  interrupt/queue policy, default queue.
- **2026-09-20** — §2.4 corrected: bar is Qwen 3.6 35b-a3b / dense 27b; no crutches for
  small models; 4b–9b are optional utility only.
- **2026-09-20** — Surfaces: web UI first class, CLI second, others via connector
  surface facet; one surface contract in the backend.
- **2026-09-20** — Connector = one concept with tools / events / surface facets.
- **2026-09-20** — Self-modification: ordinary threads + git; code only for isolation,
  promotion/rollback via supervisor, protected paths. Restart is the deploy.
- **2026-09-20** — Consolidation: quick inline write-back + async deep curation.
- **2026-09-20** — Desk: small rolling window + pinned "now" board.
- **2026-09-20** — Integration tiers: knowledge-only → MCP server → native connector;
  use the lowest that works. Secrets in OS keyring; file fallback (encryption later
  dropped — see the `03-technical-design.md` entry below).
- **2026-09-20** — Permissions: global static per-tool allow/ask/classify; filesystem
  guard is the only argument rule; protected paths enforced at promotion. No per-turn
  scope or tool gating.
- **2026-09-20** — Vault is git-tracked. Promotion staging = tests + health probe only.
- **2026-09-20** — Four examples walked through (`01-walkthroughs.md`); no new blocks
  needed. Folded in: pre-load rule, secret injection, classifier input, consolidation
  mechanics, roots grow only by approval, conversation tools + compaction + lifecycle,
  default event policies, knowledge-derived triggers dropped.
- **2026-09-20** — Research sweep (`02-research-takeaways.md`). Adopted: stable prefix,
  model-visible step budget, compaction ladder, calibration, artifact spill, core
  always-in blocks, targeted `knowledge_write` ops, FTS5 transcript recall, SKILL.md
  procedures, static shell pre-filter, persisted approvals, depth and schedule caps,
  status-as-plan, walkthrough on finish. Rejected: per-model prompts, code-as-action,
  bitemporal graphs, memory-write approval, hooks, OS sandbox (v1).
- **2026-09-20** — Verify-on-finish as a mechanism rejected. Verification is judgment in
  the spawning conversation (desk or parent thread), fed by evidence in the finish
  walkthrough. Shell pre-filter may only grant, never deny.
- **2026-09-20** — `03-technical-design.md` written: processes, schema, vault format,
  turn loop, slots, knowledge index/resolution, dispatcher, MCP client, triggers,
  surface API, UI screens, settings inventory, promotion, dependencies, build order.
  Deviation logged: secrets fallback is a permission-restricted plain file, not
  encrypted (no stdlib AEAD; `cryptography` refused under the dependency rule).
- **2026-09-20** — Named **Loom** (provisional). Identifiers: package/CLI `loom`, `~/.loom`, `LOOM_HOME`.
- **2026-09-20** — Hand-off prep: identity fixed (§5.1, `04-soul.md`); reference docs
  copied under `reference/`; `CLAUDE.md` and `README.md` added; first-run flow specified
  (`03` §3.3). Folder `docs/assistant/` becomes `/docs` in the Loom repo.
- **2026-09-20** — Model configuration is providers → presets → roles (Hydra's
  layering); all UI-managed; threads may override with a preset.
- **2026-09-20** — Implementation started. Repo skeleton: schema `0001_init.sql`, migrator,
  settings schema in `config.py`, model-client contract + fake client, tool registry,
  event bus, `App` composition root with per-package `install/start/stop`.
- **2026-09-20** — SQLite access is synchronous on the event-loop thread (sub-ms local
  calls; an async wrapper is a dependency and a codepath for nothing).
- **2026-09-20** — Ollama provider kind uses the native `/api/chat` and `/api/embed`
  endpoints (they honour `num_ctx`, `keep_alive`, `think`); `openai_compatible` uses
  `/v1/chat/completions`. No startup probe: Ollama's OpenAI path is known not to accept
  `options`. Resolves `03` §21 q1.
- **2026-09-20** — Windows shell flavour: `auto` = Git Bash if present, else cmd.exe;
  setting exposed. Resolves `03` §21 q2.
- **2026-09-20** — Now board lists all running threads, one line each, capped by a
  setting (default 10). Resolves `03` §21 q3.
- **2026-09-20** — UI Markdown renderer is `marked` plus a small scratch-built wikilink
  extension and an allowlist sanitiser (no DOMPurify). Resolves `03` §21 q4.
- **2026-09-20** — REST errors use FastAPI's `{"detail": ...}`; rows are returned with
  JSON columns decoded; LAN auth is a bearer token, `?token=` on the WebSocket.
- **2026-09-20** — Path-class policies are `tool_policies` rows keyed `<tool>:<prefix>`
  (e.g. `knowledge_write:core/` = ask); no second table.
- **2026-09-20** — Foundations built: logging + log ring, model client (both kinds,
  streaming, outcomes), per-provider priority queue, role/preset resolution, server core
  (auth, `/ws`, static UI), system/settings/models routers, CLI, supervisor.
- **2026-09-20** — Model outcome mapping completed: 408/409/425/429 are `transient`,
  any other 4xx is `fatal` (a mis-shaped request will not succeed on a retry), and the
  context/length test runs first so a 400 about context is `overflow` whatever else it
  says. Fills `03` §5.5.
- **2026-09-20** — Reasoning replay is carried on the wire as `message.thinking`
  (ollama) / `reasoning_content` (openai_compatible), and tool-call arguments as an
  object for Ollama and a JSON string for OpenAI-compatible endpoints. One
  transformation, in the model client, not at each caller.
- **2026-09-20** — `loom send` follows a turn by polling `/conversations/{id}/events`
  every 0.5s instead of opening the WebSocket: the stdlib has no WebSocket client and
  `03` §19 forbids adding one for the CLI. Exit codes 0/1/2/3/4 documented in `03` §14.
- **2026-09-20** — Supervisor kills the backend tree with `taskkill /T /F` on Windows
  rather than a Job Object (`03` §3.1 said Job Object): ctypes plumbing does not belong
  in the one file whose value is being small and obviously correct. POSIX uses process
  groups as designed.
- **2026-09-20** — Supervisor `promote`/`rollback` are process mechanics only for now
  (stop, switch pointer, start, health watchdog, revert); `snapshot`/`create_release`/
  `probe` are named seams that raise `NotImplementedError` until the self-modification
  package lands. Scope cut visibly, per the charter.
- **2026-09-20** — Added the `models.changed {kind, name}` WS event (`03` §12.2) so the
  Settings screen sees provider/preset/role mutations without polling.
- **2026-09-20** — Tools & permissions landed. Three clarifications recorded in `03` §9.1,
  §9.3 and §15.3: (a) `Tool.read_only_for(args)` makes read-only-ness a property of the
  call, and a read-only call turns a `classify` row into `allow` — a grant-only shortcut
  with the shell pre-filter's justification; it never relaxes an explicit `ask` and
  nothing static denies; (b) path-class rows `<tool>:<prefix>` match any string argument
  of the call, longest prefix wins, and the deciding rule travels in the approval's
  `options["policy_key"]` so `allow_always` widens exactly that rule; (c) removing a
  protected path is an approval on the pseudo-tool `protected_paths_remove` — the
  endpoint returns `202` with the pending row and `decide(allow)` performs the removal.
- **2026-09-20** — Conversation core built (`run_turn`, `assemble`, compaction, threads).
  Additions: `event.appended` WS event for every persisted event; inbox rows are never
  re-rendered after an early turn end; a thread turn ending in plain text →
  `waiting_input` + thread event to the parent; occupancy counts the whole prompt; the
  pre-call brake falls through to the model when nothing can be compacted (the
  `overflow` outcome is the authoritative failure).
- **2026-09-20** — All REST endpoints are `async def` (sync handlers run in a threadpool:
  no event loop to schedule turns, SQLite off the loop thread).
- **2026-09-20** — Knowledge package built. Vault writes are synchronous (index + git on
  the event-loop thread); embeddings are queued and drained in the background, so no
  write waits on a model. `Index.resolve`/`search` are async (they embed the query);
  pre-load, core blocks and the graph stay synchronous.
- **2026-09-20** — Procedures written by Loom itself are approved at write time; the
  approval item exists for skills copied in from outside. Wikilinks resolve a procedure
  by its folder name, not the literal `SKILL` stem.
- **2026-09-20** — Maintenance role built (`03` §8.6): one lock, priority 2, three stages
  (consolidate → curate → housekeep), scheduled by an idle window with work or the
  nightly slot; passes never queue.
- **2026-09-20** — An approval raised *during* a maintenance pass is denied immediately
  and re-posted as a `knowledge` item with the proposed call: a pass has no turn to
  resume, so a pending row would point at nothing. The person still decides; the pass
  never waits. Watermarks advance anyway.
- **2026-09-20** — `knowledge.auto_commit_external_edits` off means a maintenance pass
  commits nothing at all: the safety-net `maintenance: <date>` commit stages the whole
  worktree and would otherwise commit the user's uncommitted edits regardless.
- **2026-09-20** — The nightly slot is tracked in memory as "the date of the last slot a
  pass ran for", seeded at startup with the current slot: no migration, no catch-up pass
  on every restart, and a clock change cannot fire a slot twice.
- **2026-09-20** — Triggers built. One write path for "event → inbox + item"
  (`triggers.router.route`), used by the scheduler and by connector events; the board
  item is written before the inbox row, so both rows exist before `conversation.post`
  wakes the turn. `run_now` fires without shifting `next_run_at`; a schedule whose target
  conversation is gone is disabled, not retried.
- **2026-09-20** — `tzdata` added as a runtime dependency (`03` §19). Windows ships no
  IANA database, so `zoneinfo` raises there and every "local time" in the scheduler and
  the maintenance role would silently fall back to UTC. It is data, not code, and there
  is no stdlib alternative.
- **2026-09-20** — Integrations built: scratch MCP client (stdio + streamable HTTP,
  protocol `2025-06-18`), the `app.mcp` manager, the native-connector loader, and the
  `/mcp-servers` + `/connectors` endpoints. Four decisions recorded in `03` §10: (a) a
  discovered connector is enabled by default — it is in `connectors/` because the user
  put it there — but unset **required** config stops it at the new `needs_config` status
  with a setup item, never half-configured; (b) the streamable-HTTP transport omits the
  optional standalone GET listening stream, so a server pushes notifications only while
  one of our requests is open (which `tools/list_changed` survives, a refresh being a
  request); (c) a server→client `sampling/createMessage` or `elicitation/create` is
  answered with JSON-RPC -32601 rather than ignored, so an unsupported server fails
  clearly instead of hanging; (d) the connector note's tool list is delimited by
  `<!-- tools:begin/end -->` and is the only part a refresh rewrites, so the agent's
  "when to use" prose survives. Setup items are de-duplicated per source while one is
  undismissed, so a 5s→5min reconnect loop cannot bury the now board.
- **2026-09-20** — Context layout: exactly one system message (Qwen 3.6's template
  rejects a second one); summary and the volatile now-board/scope block are user-role
  `<context from="harness">` blocks, the volatile one nearest the query. Found by the
  first live turn against the bar model.
- **2026-09-20** — Self-modification built (`03` §15). Five decisions recorded there:
  (a) **two tools, not one** — `promote` is `ask` by policy, so its handler runs *after*
  the approval and cannot put the protected-file list in the approval's reason; the
  read-only `promote_plan(ref)` (and `GET /system/promote/plan`) produces that list
  first, and `acknowledge_protected=true` asserts the person has seen it while the call
  still goes through the ordinary ask. Nothing static denies; nothing is promoted
  without a human. (b) The probe runs the candidate release with `LOOM_HOME` pointed at
  a scratch directory holding a *copy* of `state.db`, rather than the `serve --probe`
  flag `03` §15.2 once sketched: the CLI has no such flag, and adding a second serve
  mode to express "use this home" would be a special codepath for one caller. (c) The
  supervisor reports progress by appending JSON lines to `APP_HOME/promote.log`, which
  the backend tails from a durable byte offset and republishes as `promote.progress`;
  it cannot be told directly because it is the process being restarted, and the offset
  file means a promotion is reported exactly once even though the backend dies in the
  middle of it. (d) `promote.request_promotion` is the single codepath for steps 1–4,
  shared by the tool and `POST /system/promote`, with the failure class carried as a
  `code` the REST layer maps to one status each. (e) The self-modification *workflow*
  is a vault procedure note (`procedures/self-modification/SKILL.md`, written from
  `prompts/templates/procedure-self-modification.md` on install), not Python — §5.9's
  "no bespoke framework" taken literally; code covers only isolation, promotion with
  rollback, and protected paths.
- **2026-09-20** — Added `wsproto` as a runtime dependency: bare `uvicorn` cannot serve
  the WebSocket the whole surface contract rides on (found on the first real browser
  connection; in-process tests never upgrade). One small pure-Python package.
- **2026-09-20** — `loom serve --fake` uses its own home (`~/.loom-fake` unless `--home`
  is given) so a fake preset can never land in the real state.
- **2026-09-21** — "Clear the desk" is a compaction cut (kind `clear`), not a delete:
  transcripts stay append-only and searchable. Vault folders can be deleted from the UI
  in one revertible commit (`core/` refused); "open in file manager" is bounded to
  APP_HOME and the allowed roots.
- **2026-09-21** — The permission judge has its own bounded timeout
  (`permissions.classifier_timeout_s`, default 45 s, one retry on a busy provider) and
  fails closed to `ask` with the reason on the card. A hosted 9B judge stalled ten
  minutes behind OpenRouter's 429s; a verdict that late is worth nothing.
- **2026-09-21** — `reasoning: off` is sent to OpenAI-compatible providers as
  `{"reasoning": {"effort": "none"}}` rather than omitted: OpenRouter leaves a Qwen
  judge thinking by default, and with `max_tokens: 200` every classification and the
  maintenance consolidation came back "length limit with no output".
- **2026-09-21** — `knowledge_write` folds a frontmatter block found at the top of
  `content` into the note's frontmatter instead of nesting it (the explicit argument
  still wins). The model copies the shape it reads back; two live notes carried two
  blocks. Same rule for every op; no special case for the model.
- **2026-09-21** — Path normalisation reads MSYS/Cygwin drive spellings (`/c/Users/…`)
  as `C:/Users/…` on Windows. The shell is Git Bash, so the model sees and reuses those
  paths; `abspath` had turned one into a real `C:/c/Users/…` tree. File tools that
  miss on a relative path now name the root it was resolved against.
- **2026-09-21** — A thread's default title is the goal's first sentence cut at a word
  boundary (≤ 80 chars, ellipsis), not `goal[:80]`; it names walkthrough notes and
  notifications. The tool prompt now says so, so the desk can pass a better one.
- **2026-09-21** — Queued input is visible: `inbox.posted` carries the posted content,
  `GET /conversations/{id}/inbox` lists rows not yet in the transcript, and the UI shows
  them as *queued* cards until the runner's `user` event replaces them. A clean turn
  that ends with text now re-wakes itself when rows arrived during its last model call
  (they used to wait for the next message or a restart).
- **2026-09-21** — Appearance = theme (light/dark axis) × palette (`general.palette`:
  default, slate, warm, forest, nord, mono), implemented as CSS-variable overrides of
  the colour ramps the components already use — six looks, zero component changes.
- **2026-09-21** — The "also add this root" checkbox on approval cards defaults to
  unchecked and pairs with `allow always` on every surface (the backend only applies it
  there). Widening roots is a deliberate act, not the default click.
- **2026-09-21** — First visual QA pass by screenshot (headless Chromium against a
  `--fake` server, every screen, both themes, all palettes). Behavioural choices it
  settled: inbox rows from threads, schedules and connectors render as grey bubbles
  captioned with their source, not as the person's own words; a tool row whose call is
  waiting on an approval says so instead of spinning; a thread's walkthrough list comes
  from the vault tree by path (`projects/<name>/walkthrough-*`), not from a fuzzy
  search; the Approvals screen orders its buttons like the inline card. Left open: the
  now board's "Queued" group holds unseen items and now shares a word with queued
  messages; the `mono` palette's dark badges and links are faint by construction.
- **2026-09-21** — A thread's scope note is optional; a thread without one is a *task*.
  Status goes through one reader and one writer whose sink the nullable column picks:
  the note's block for a project, `status` events in the thread's own transcript for a
  task (never replayed to the model). A task's walkthrough is appended to the daily
  `log/YYYY-MM-DD.md`. Chosen over a `tasks/` type, scoping to any entity note, or
  retention-by-shape because a task that grows into a project is a field set, not a
  migration — and a one-off investigation no longer leaves a dead project note behind.
