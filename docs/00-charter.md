# Loom — charter

**Name:** Loom (provisional, chosen 2026-09-20; subject to change).

**Status:** brainstorm, 2026-09-20. This document is the living design for a new agentic
assistant. It was drafted inside the Hydra repo so Hydra and the harness survey could serve as
reference material; the reference documents travel with these docs under
`reference/`. Loom is *not* a Hydra feature; it is a separate project.

Companions (reading order in `README.md`): `01-walkthroughs.md` traces the four example
requests through this design; `02-research-takeaways.md` records what was adopted,
adapted and rejected from the harness survey; `03-technical-design.md` is the
implementation-level design; `04-soul.md` states the constraints on the system prompt (identity and
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
effect. The full identity and operating rules live in `prompts/system.md`; `04-soul.md` states
the constraints they keep, chief among them no mutable state.

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
  decisions) into its own transcript as it works and pins the notes it needs. The desk
  answers "how is it going?" from that status, not from the thread transcript. Threads
  are where the session model is *correct*: bounded, focused, disposable; their durable
  output is knowledge and files. What a project is, Loom does not decide: it is whatever
  the person calls one, known through a note the model writes.

**One conversation primitive.** Desk and thread are the same thing configured
differently: a *conversation* = scope + lifetime + transcript. Desk: scope
"everything", lifetime infinite. Thread: scope "one goal", lifetime until done.
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
  procedures and decisions. A thread's status lives in its own transcript, never in a
  note (decided 2026-09-22; until then a project note carried a status block its thread
  owned).
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
- Status: decided 2026-09-22 — in the thread's transcript, never in a note.
- Connectors: hot-load individually, or always via promotion + restart? Lean: restart
  only, until it hurts.
- The `classify` policy text: what counts as in-scope for a thread vs. the desk?
- Threads vs. projects: decided 2026-09-22 (see the decision log) — a thread is Loom's
  shape; a project is knowledge the model writes.
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
- **2026-09-21** - After a hosted Qwen 3.6 desk step spent 68k characters repeating
  five paragraphs verbatim: sampling penalties (repetition/presence/frequency) are
  ordinary preset params on both wire shapes; in-turn reasoning replay keeps the *tail*
  of a long step (what led to its calls) behind an elision marker instead of the head
  (the plan, and where a loop's seed sits); the desk prompt says to send a failed
  verification back to the thread rather than debugging it itself. A stream-level
  repetition brake was proposed and left for the person to decide.
- **2026-09-21** - The volatile harness block lists the conversation's allowed roots
  (first = where relative paths resolve) and protected paths every step. The model had
  been learning them only by failing against them; roots are mutable state, so they
  ride in the context block, not the soul.
- **2026-09-21** - The test suite wrote secrets to the real Windows Credential Manager
  under Loom's service; the keyring library's move-to-compound step then landed the
  person's fresh OpenRouter key on a stale lowercase twin (targets are case-insensitive)
  once per test run, and every read fell back to the revoked key. Tests now get a
  dict-backed keyring from the `app` fixture (a hard rule in CLAUDE.md), and secret
  names are case-insensitive in Loom, existing spelling first.
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
- **2026-09-21** — Image input, Hydra's design carried over: image bytes are artifact
  files (`img_<ulid>`), transcripts hold only `{id, mime}` refs and data URLs exist in
  memory at assembly time; a preset says `multimodal` (else every image is a placeholder
  line, so text-only models never see a parts array) and `image_tokens` (a fixed
  per-image estimate for occupancy — honest at chars/4 precision); a tool result's
  images ride a user-role follower after the step's tool messages because tool messages
  cannot carry them; `fs_read` and `http` return images when they meet one; a
  `screenshot` tool captures through the OS (PowerShell/System.Drawing, `screencapture`,
  `grim`…) rather than a new dependency; uploads are raw bodies, not multipart, for the
  same reason.
- **2026-09-22** — A capability test (a desk-driven game build across seven threads)
  exposed a harness bug: `artifact_recall` pages and full `fs_read` pages were exactly
  the spill cap plus a footer, so the dispatcher spilled every page into a fresh artifact
  and a model could never read past the first page (one thread made ~50 identical
  recalls, 63 artifacts, until cancelled). Decision: a tool that pages its own output
  declares `paged`; the dispatcher never spills it; its page is `tool_output_cap` and
  the footer rides on top. One mechanism, derived from the one setting. The
  repeated-identical-call brake stays deferred; the step budget remains the only stop.
- **2026-09-22** — A thread turn that ends in plain text is reported to its parent and
  on the board as *stopped without finishing*, not *needs input*. The status value
  `waiting_input` is unchanged (the thread does resume on a message); the words were
  wrong: three of four finished threads in the test ended with a report instead of
  `conversation_finish`, and every surface said they were waiting on the person. The
  harness cannot tell a question from a report from a yield and does not try; the
  thread prompt now says what a text ending means and what to do instead.
- **2026-09-22** — Tools declare the conversation kinds that see them; `conversation_finish`
  and `status_update` are thread-only. The desk had called finish on itself twice and
  been told "the desk never finishes".
- **2026-09-22** — Sub-threading stays (a thread may split a larger project into
  sub-threads within `max_thread_depth`), but a sub-thread never shares its parent's
  scope note: `conversation_create` refuses it. Two threads writing one status block
  produced duplicate blocks and a confused desk. The thread prompt now says the goal is
  the thread's own to do, and when a sub-thread is the right split.
- **2026-09-22** — The status block has one writer, enforced: `knowledge_write` refuses
  any edit that would change or introduce a status block and names `status_update`.
  Models had been hand-editing blocks, copying an artifact id into `owner=`.
- **2026-09-22** — The model's text on a tool-calling step (its mid-turn narration) was
  persisted as `tool_call.meta.text` but the UI dropped it once the step landed; it
  survived only in reasoning traces. The transcript now renders it as an assistant
  bubble above the step's tool rows. No new tool: the channel already existed, and
  `conversation_send` to the parent already carries a thread's mid-turn message to its
  caller.
- **2026-09-22** — The harness block names the projects root next to the allowed roots.
  `general.projects_root` had pointed at a folder that did not exist, so every new
  project note carried a bad root, threads improvised locations (one inside this repo)
  and the person redirected them three times. Roots are mutable state; they ride in the
  context block, not in a core note.
- **2026-09-22** — The soul says verified means executed, and that an unexecutable claim
  is labelled unverified; and that a missing program is looked for and its location
  recorded before giving up. In the test the desk reported "39 tests passing" from a
  file listing, and no thread or desk turn ever launched Godot, whose binary sat in
  Downloads; a one-character scene-header typo went unnoticed through two rewrites.
- **2026-09-22** — Threads and projects are separate things. A thread is a scoped tangent
  Loom manages: goal, roots, status in its own transcript, pinned notes, a walkthrough
  filed in the daily log, temporary by design. A project is a shapeless thing on disk —
  a codebase, a folder of images, an ongoing effort — that the person calls a project;
  Loom does not maintain a shape for it. It exists as an ordinary `projects/` note the
  model writes when it recognises the work will persist and be returned to (the soul
  rule; the thread as it works, the desk as it verifies, maintenance sweeping
  transcripts), with `root` a recorded fact. Removed: the harness-created project note,
  the scope note and its status block with `owner=`, the `knowledge_write` block guard,
  the sub-thread scope rule, the `projects_root` setting and wizard step, and the
  project walkthrough folder. Added: pinned notes — any vault paths a conversation keeps
  in context every turn, pinned by the thread through `knowledge_read(pin=true)`, by the
  spawning conversation at spawn, or by the person. This supersedes the 2026-09-21
  tasks decision by making it universal: every thread is what a task was, and the word
  goes.
- **2026-09-22** — Background processes. A program that must keep running (a dev server,
  a bot) is started with `shell(background=true)`: the `processes` row is written before
  the spawn, output goes to a log file beside the conversation's artifacts, a watcher
  marks the exit and posts it to the owning conversation, and one `process` tool lists,
  reads and stops. Before this a long-running command either blocked its thread until
  the timeout killed the tree, or escaped with `nohup … &` into a process Loom knew
  nothing about. Carried over from Hydra's supervised processes minus the restart
  policy, the Job Object containment (needs `pywin32`) and log rotation: all three are
  knobs for when something needs them. Children outlive the backend on purpose — a
  pipe would kill them on the first write after a restart, a file does not — so a
  restart reconciles the rows (alive → re-attached and polled, dead → `lost`) instead
  of restarting anything.
- **2026-09-22** — Three fixes from the first live use of background processes, when
  the desk tried to start the Music-Bridge bot: it could not tell Loom's own port from
  the bot's, Git Bash rewrote its `taskkill /F` as a path and `2>nul` wrote a file, an
  orphaned `node … &` from an earlier timed-out call kept the bot's port for an hour,
  and `cat ~/.loom/secrets.json` sailed through the pre-filter. Decisions: the harness
  block names Loom itself (address, pid, home) and the shell flavour with its quirks —
  mutable facts, so the context block and not the soul; every foreground shell call runs
  in a kill-on-close Job Object on Windows (through `ctypes`, no new dependency; a
  process group elsewhere), so everything a call started ends with the call, and a
  background process's job lets `stop` reach reparented descendants while the backend
  that started it runs; and the pre-filter no longer grants a command that names a
  protected path (it still never denies: the classifier judges it).
- **2026-09-22** — The harness block is a per-turn snapshot, and the only per-step text is
  the step counter, moved to the end. It had been rebuilt on every step and carried
  `[steps remaining: N]`, so each step invalidated the prompt cache immediately before the
  turn's own messages and re-prefilled everything the turn had accumulated: quadratic on a
  long thread turn. Now `assemble` builds the fenced block once, `messages()` reuses that
  exact string, and a thread's step counter rides a small trailing block after the turn's
  messages (the desk has none). Within a turn everything up to the turn's messages is
  byte-stable and the turn is append-only. The cost is deliberate: the now board, the
  clock, the status and the pinned notes are as of turn start, so a thread that pins a
  note or writes its status mid-turn sees it in the block next turn, with the content in
  its own transcript meanwhile. The three things that still break the cache mid-turn are
  known and accepted: a roots change, a forced compaction, and `tool_search` widening the
  tool list (schemas sit at the very front, so that one invalidates everything).
- **2026-09-22** — The shell flavour's quirks live in `prompts/context/shell-<flavour>.md`
  and are composed into the `shell` tool's description at registration, not injected into
  the harness block every turn. Model-facing prose belongs in `prompts/` (a hard rule that
  a hardcoded `FLAVOUR_HINTS` dict had just broken), the tool's own description is where
  the model is already looking when it writes a command, and tool schemas are cached. The
  rule "never stop Loom, kill its process tree, or bind over its address" moved there too:
  it is a rule, not a fact, and the block had been repeating it every turn. Because the
  description is composed once at registration, `permissions.shell_flavour` is now
  restart-required, so the setting and `resolve_shell` can never disagree.
- **2026-09-22** — The first-run Setup screen is gone. It was the one screen that owned
  the app: the UI asked `GET /api/system/setup` before rendering anything and showed the
  wizard while no desk role was set, so a person who wanted to look around, read the
  vault or change a setting first had to satisfy it — and a blank model list (no Ollama
  on the machine) left nothing to satisfy it with. It also duplicated Settings › Models:
  a second way to write a provider, a preset and three roles, with its own endpoints and
  its own preset defaults that Settings did not know about. Two ways to do one thing, one
  of them modal. Now a missing model is what it always was — an unconfigured setting —
  and is said the way this charter already says such things are said: one *setup* item on
  the now board, de-duplicated per source, dismissible. `GET`/`POST /api/system/setup`,
  `screens/Setup.tsx` and the `#/setup` route are deleted; the browser opens on the Desk;
  the terminal line and the 409 from a send both name Settings › Models. What is lost is
  named: the wizard's one-click "preset with sensible defaults wired to desk, worker and
  maintenance", so a first model now takes a preset and a role assignment in Settings.
  That is the cost of having one place where models are configured rather than two.
- **2026-09-22** — The desk hands over open-ended work before its first step. Asked to
  "get familiar with this machine", the desk surveyed it inline: 11 model steps and 18
  tool calls across four approval segments, no note written, and reasoning that never
  once weighed a thread. The prose had sized effort by the request ("short requests are
  yours"), named thread work by its products (many steps, files, a later conversation),
  which a survey has none of up front, and "check knowledge before you spawn" had licensed
  the first tool calls. Now the soul sizes effort by the work; the ladder's test is
  whether the end can be named from here; a result that raises a new question instead of
  answering one is the sign of exploring; `kind-desk.md` names finding-out as thread work
  and bounds the pre-spawn lookup to what is already written; `conversation_create`'s goal
  names what an investigation must write. No step counter for the desk: the fix is
  judgment, not a budget. Two rules the tool descriptions already carry word for word
  (background processes: `shell`, `process`; waiting for approvals: the soul itself) left
  the rules section, paying for the two new lines.
- **2026-09-22** — Git Bash is found from where Git is installed, never from a bare
  `bash.exe` on PATH. In the same survey turn the harness block said the shell was Git
  Bash and the process was WSL Ubuntu: Git lives in a non-standard folder here, the
  standard-folder candidates missed, and the fallback `which("bash")` returned the WSL
  launcher in System32. Five of the turn's eleven steps went to that discrepancy, and the
  device note it left behind records WSL, and a "the shell eats `$var`" gotcha that was
  really WSL's launcher, as facts about the machine. `find_git_bash` now asks the
  registry (`SOFTWARE\GitForWindows\InstallPath`), then the standard folders, then
  derives the root from the `git` on PATH; a PATH `bash.exe` under System32 or
  WindowsApps is refused. Restart-required, as the flavour already was: the harness block
  and the process can no longer disagree about which shell this is.
- **2026-09-22** — The web UI blanked whenever a tool asked for approval: the desk's
  `conversation.updated` carried the raw conversation row, so `pinned_notes` arrived as
  the json string `"[]"`, the header called `.map` on it, and React unmounted the whole
  tree — a white page until a reload. Two rules, one per class of failure. One shape on
  the bus: `events.announce` is the only emitter of `conversation.updated`, always the
  public row (the same shape `GET /conversations/{id}` serves), and every status
  transition goes through `events.set_status`, which announces; the three sites that had
  hand-rolled an `UPDATE` plus an emit (approval requested, an internal approval applied,
  an archived conversation reopened) lost their copy. And one defined behaviour for a
  render error: an `ErrorBoundary` around the routed screen (keyed on the route, so
  navigating away clears it) and a second around the shell, so a bad row costs the screen
  it broke, never the app.
- **2026-09-24** — Desk auto-approve. One setting, `permissions.desk_auto_approve`
  (default off), toggled from the desk header and Settings → Permissions. Under it, a
  desk call that would ask or classify runs instead (`policy: "auto"` on the event), so
  there is no classifier generation either. Grant-only, the same principle as the
  pre-filter. Protected paths keep asking: the fs guard's protected ask, and a shell
  command that names a protected path or cannot be parsed to tell, go down the ordinary
  path. Threads are unaffected; they are where long unattended work lives, and a
  desk-wide switch should not silently widen what they may do.
- **2026-09-24** — Tool parity with Claude Code and opencode
  (`reference/tool-parity-audit.md`). Agreed: audit P1 #1–6 and P3, tracked in
  `05-backlog.md`. Rejected: blocking subagents (asynchrony is what threads are for)
  and a separate todo tool (the thread status is the plan). The rest of P2 is undecided.
- **2026-09-24** — The backlog build ran as five parallel streams partitioned by file
  ownership (fs, web, machine, permissions, threads), each a manager with workers, after
  the overseer laid down the shared contracts first: migrations 0004–0006, the
  dispatcher's step 4½ (`hooks.before`/`after`), `permissions.conversation_grant`
  consulted before classify/ask, `timeout_for(app, tool, args)`, `ToolContext.intent`,
  and the `tools` settings group. Entries below are the streams' own, in stream order.
  Two follow-ups the streams could not make in files they did not own were made by the
  overseer: `dispatch` uses `classify.command_cwd` (one cwd rule, not two copies), and
  standby detection is two settings (`conversation.suspend_drift_s`, `suspend_retries`)
  rather than module constants, so it is UI-visible like every other knob.
- **2026-09-24** — `tools/native/fs.py` became the package `tools/native/fs/`: `read`
  (text, PDF, notebooks, images), `search` (fs_list, fs_search), `write` (fs_write,
  fs_edit), `patch`, `ledger`, `notebook`, `pdf`, `common`. The tool schemas stay in one
  place, the package's `__init__`, so the model-facing surface is still read in a single
  file. `from . import fs` and `fs.tools()` are unchanged.
- **2026-09-24** — `fs_read` numbers its lines `N\t` by default (`line_numbers=false`
  turns it off for prose you will quote). The numbers are absolute, so page two
  continues the count, and the page cap applies to the *rendered* text, prefixes
  included; the footer's next offset stays exact. The prompts for `fs_edit` and
  `fs_patch` say the prefix is not part of the file's text, and the "not found" error
  repeats it, because that is the one mistake numbering invites.
- **2026-09-24** — A stale-write guard, kept in the `file_reads` table (migration 0004:
  conversation, normalised path, mtime_ns, size, seen_at, and `wrote`). `fs_read`
  records; `fs_write`, `fs_edit` and `fs_patch` check then record. Four classes, one
  behaviour each: record matches → proceed; record differs → refuse and say to read it
  again; no record and the file exists → refuse, read it first; no file → create it.
  This is not scaffolding for a weak model — the person edits the same notes and files
  in Obsidian and their own editor while a thread is working. There is no opt-out
  setting: a second read is one cheap call, and an override would be a way to lose work.
  `wrote=1` is how another part of the harness (the permission classifier's script
  lookup) learns which files this conversation wrote.
- **2026-09-24** — `fs_edit` takes `edits=[{old,new,replace_all}]` beside the single
  `old`/`new` form (never both in one call: that is an error naming the choice). The
  list is applied in order to the text in memory and written once, so a failing edit
  leaves the file exactly as it was, and the result carries one diff of the whole
  change. Each edit keeps the existing rule — exact text, unique unless `replace_all` —
  and the error names which edit of how many failed.
- **2026-09-24** — The unified-diff tool is `fs_patch`, not the backlog's `apply_patch`:
  the fs family is named by what it acts on, and a tool called `apply_patch` sorts away
  from its five siblings in the registry, the permissions table and the model's tool
  list. Arguments: `patch`, `cwd`, `strip` (default: drop a leading `a/`/`b/`, git's
  spelling), `dry_run`. `/dev/null` on the left creates, on the right deletes; differing
  paths rename.
- **2026-09-24** — A patch hunk is applied by exactly the rule `fs_edit` uses: its
  context-plus-deletions block must match the file exactly, and unambiguously. The `@@`
  line number is a hint tried first, never a fuzz factor; a block that matches nowhere,
  or in two places while the stated line matches neither, is refused with what to do
  next. No fuzzy-replacer cascade (tool-parity audit §4). Parsing is hand-written —
  `difflib` writes diffs and cannot read them — and is tolerant of what surrounds a
  diff: `diff --git`, `index`, mode lines and prose are skipped.
- **2026-09-24** — `Tool.path_params` cannot express "the paths inside a diff", and the
  registry is not the place to learn about diffs. `fs_guard.register_paths(tool_name,
  extractor)` is the smallest general mechanism: one function per tool name, whose
  result the guard judges in the same loop as every `path_params` value, so protection
  and root checks are one codepath. A patch that cannot be parsed yields no paths — and
  is refused by the handler with the parse error before anything is written.
- **2026-09-24** — PDFs in `fs_read` go through the `pdftotext` binary when it is on
  PATH (`layout`, default true), and a missing binary is a clear error naming it. No
  Python PDF dependency: §19 stays as it is.
- **2026-09-24** — `.ipynb` is JSON, so it needs no dependency either: `fs_read` renders
  the cells (a header per cell, the source, one summarised line per output — outputs are
  never dumped), `fs_edit` matches `old`/`new` against the cell *sources* and rewrites
  the JSON preserving outputs, metadata, execution counts, the file's own indentation
  and its `source` list shape, and `fs_write` writes raw JSON like any other file.
- **2026-09-24** — `fs_write`, `fs_edit` and `fs_patch` keep a file's line endings and
  its UTF-8 BOM. A file whose endings are all CRLF is written back as CRLF; a file with
  mixed endings is left exactly as it is, because picking a winner would rewrite lines
  nobody touched. The BOM is stripped from what `fs_read` shows, so what the model
  copies is what `old` matches, and put back on the write. Writes are atomic: a temp
  file beside the target, then a replace.
- **2026-09-24** — A relative path that names an existing vault note is refused by
  `fs_write` and `fs_edit` with a pointer to `knowledge_read`/`knowledge_write` (TD
  audit F9), instead of creating a stray file under the first allowed root. The
  "relative paths resolve against …" hint stays for everything else.
- **2026-09-24** — `http` gains `format` (`raw` / `text` / `markdown`, default
  `markdown`). HTML is converted by an in-house `html.parser` walker
  (`tools/native/html_text.py`) — no dependency, per §19 — which drops
  script/style/noscript/template/svg and the navigational furniture
  (nav/header/footer/aside/form/iframe) and renders headings, paragraphs, lists,
  links, images, code and `pre`, emphasis, blockquotes, rules and best-effort tables.
  The default is `markdown` rather than `raw` because raw HTML is the expensive answer
  to almost every fetch: a two-thousand-word article arrives as two hundred thousand
  characters of markup the model pays for and does not need. `raw` stays one argument
  away for the times the markup *is* the question.
- **2026-09-24** — A body that is not HTML passes through unchanged whatever `format`
  says (JSON still pretty-printed, source files untouched). One rule, no per-type
  special cases: the tool converts what it can read as a page and never quietly mangles
  anything else. HTML is recognised by the declared content-type, or — only when the
  server declared none at all — by the body's own first bytes, because a page served
  with no content-type is common enough that believing the server costs the whole
  context budget.
- **2026-09-24** — `web_search(query, k)` ships native and read-only behind one
  provider setting: SearXNG (self-hosted, `tools.web_search_url`, no key), Brave, Exa
  or Tavily (key fetched from the secret *named* in `tools.web_search_secret`, never
  pasted into a setting). A provider is only a request branch and a parse branch over
  one `Hit`; the key lookup, timeout, failure classes and numbered output are shared,
  because none of that has any business differing between indexes.
- **2026-09-24** — The tool exists even when nothing is configured, and answers with a
  defined "not configured" note (`prompts/context/web-search-unconfigured.md`) naming
  Settings → Tools, the four providers and `secret_set`. A tool that disappears when it
  is unconfigured teaches the model nothing and leaves the person un-prompted. Five
  defined failures in all: unset provider, SearXNG with no URL, missing secret,
  provider HTTP failure (status + first body line), non-JSON body. No hits is an
  answer, not a failure.
- **2026-09-24** — `web_search` carries `read_only=True` and gets **no**
  `DEFAULT_TOOL_POLICIES` row: §9.3 rule 3 already resolves a rowless read-only tool to
  `allow`. Adding a row would state the default twice and let the two drift.
- **2026-09-24** — The Settings "Tools" tab has no hand-written screen. The Settings
  shell already renders any declared group it has no tab for straight from the schema,
  so a group of scalars costs one line in `TAB_ORDER` — which is only about *where* the
  tab sits — and nothing else. A dedicated tab would be a second place for the same
  four fields to be wrong.
- **2026-09-24** — `screenshot(window=…)` captures the window, not the screen in front
  of it. The Windows capture now renders the window's own pixels with
  `PrintWindow(hwnd, hdc, PW_RENDERFULLCONTENT)` and falls back to the old rect copy
  only when that call fails or the bitmap comes back all black (an 8×8 grid of sampled
  pixels decides). `region=` and `monitor=` are unchanged and keep capturing whatever is
  visible at that spot — that is what they are for, and the prompt now says so in both
  directions. In the tower-defense run 0 of 6 desk captures showed the game, because
  every one of them was a rect copy of whatever was on top.
- **2026-09-24** — A screenshot failure is a sentence, not a serialisation format. The
  PowerShell capture catches its own errors, writes the message as plain text on stdout
  and exits non-zero, so the model reads `screen capture failed: no window whose title
  contains "tower"`. Belt and braces, Loom decodes a `#< CLIXML` stream (stdlib XML plus
  the `_xNNNN_` unescape) before it ever looks at the first line, because PowerShell
  serialises its error stream whenever stderr is a pipe and any future `throw` outside
  the script's own try would leak it again. In the audited run that lost message would
  have told the desk its process was already dead, 40 minutes early.
- **2026-09-24** — A foreground `shell` result names what the containment killed. The
  survivors are enumerated *before* the kill — the Job Object's pid list on Windows
  (`QueryInformationJobObject` + `QueryFullProcessImageNameW`, ctypes, no dependency),
  `ps -g` or `/proc` on POSIX — and the trailer goes on its own line after `[exit N]`:
  `[ended 1 process the command left running: game.exe pid 5732]`, at most eight names
  then `, and N more`, and `meta["ended"]` beside it. Nothing left running means no
  line at all. Best effort throughout: a platform that refuses the call logs at debug
  and the call behaves exactly as before. `prompts/tools/shell.md` already stated the
  rule and the desk still spent forty minutes on it in the tower-defense run (F3); a
  rule the model is told once is weaker than the same rule in the result it is reading.
  The same change makes the rule true off Windows, where the process group was only
  killed on a timeout and a normal exit left orphans behind.
- **2026-09-24** — The call's `timeout_s` is honoured, and the tool's own deadline is
  the one that fires. `timeout_for` gives a tool that declares a `timeout_s` argument
  (shell, http) the number the model asked for, clamped by the new
  `conversation.tool_timeout_max_s` (default 900) — the declared argument outranks the
  tool's own `timeout_s`, and junk, zero or a negative value falls back to the per-kind
  default rather than erroring. The dispatcher's `wait_for` becomes a grace margin above
  that (`timeout + min(30, max(1, 0.1 × timeout))`), so it is a backstop for a tool that
  does not time itself out and never the thing that cancels a tool mid-flight. That is
  what makes partial output survivable, and the shell handler now reads stdout into its
  own buffer through a shielded reader: a cancelled `communicate()` discards everything
  it had already read, which is why a timed-out build used to come back empty. A
  timed-out call returns the text produced before the kill plus `[timed out after Ns;
  process tree killed]`. `http` clamps its own `timeout_s` the same way.
- **2026-09-24** — `process(logs)` follows a growing log without bookkeeping. `tail=true`
  reads the last page from the log's current end, `since=<offset>` reads forward from
  there, `offset` stays as an alias so nothing that works breaks, and the tail is still
  the default. The footer is printed on every read — including a log that fits in one
  page and an empty one — and always names the offsets and the exact next call, so the
  loop is read, then hand the footer's number back as `since`. A `since` past the end
  returns an empty page naming the real end: that is the "nothing new yet" answer, not
  an error. Watch-for-line (blocking until a pattern appears) is a different question
  and stays undecided.
- **2026-09-24** — The permission judge sees what the call is actually about. Three
  inputs were added to `classify.render_inputs` after the tower-defense run (F1), where
  seven `powershell -File <script Loom had just written>` calls were asked because "the
  script's contents are unknown" and "the 'Desk' goal does not clearly authorize". (1)
  The turn's **stated intent** (`ToolContext.intent`, what the assistant said it was
  about to do) renders as its own fenced untrusted block. (2) A conversation's goal is
  its `goal` column and nothing else — the `title` fallback is gone, so the desk never
  renders a bare "Desk"; with no goal the line says the stated intent below is this
  turn's scope, and the intent stays inside its fence, because untrusted text never
  rides on a bare label line. (3) The **contents of files the call names, when this
  conversation owns them** — under its artifacts folder, or a row of its `file_reads`
  ledger — are shown as untrusted data: at most two files, 4000 chars each, files this
  conversation wrote first. Candidate paths come from the shell pre-filter's word
  splitter plus every string argument, normalised against the cwd the shell tool will
  use. A file the conversation has never touched is never read, so the judge is never a
  way to see more than the conversation already saw.
- **2026-09-24** — Connectors may hook tool calls, and a hook may only ask. Two optional
  fields on `Connector` — `before_tool(api, config, ctx_summary, tool_name, args)` and
  `after_tool(..., result)` — make hooks the fourth connector facet (audit #14).
  `before_tool` runs at dispatcher step 4½, once the call is already allowed, and may
  annotate it or turn the allow into an ask; `after_tool` annotates the result. Neither
  may deny: a hook that returns `deny` is logged and counted as an ask, because nothing
  static ever denies (the hard rule), and an ask is already the strongest thing a
  connector can say. A hook reads copies and a `ctx_summary` dict, never the live
  `ToolContext`, and sees `{{secret:NAME}}` placeholders rather than secrets, so it can
  observe a call but never change what executes. Every failure class is one behaviour —
  raises, times out past `HOOK_TIMEOUT_S`, or answers the wrong type: log a warning and
  ignore that hook. The notes ride on the result's text through
  `prompts/context/tool-hook-note.md`, so the model reads them like any other trailer.
- **2026-09-24** — "Allow this call for the conversation", a third allowing answer. The
  tower-defense run asked the same `powershell -File` command five times because the
  only way to stop being asked was `allow_always`, which widens the whole tool — never
  a sane answer for one script. `approvals.DECISIONS` is now
  `("allow", "allow_conversation", "deny", "allow_always")`: the three allowing answers
  widen once, this conversation, always. `allow_conversation` writes a `call_grants` row
  keyed `(conversation_id, tool, sha256(db.dumps(args)))` (migration 0005) and nothing
  else — no policy row, no root. The dispatcher consults it only when the mode is not
  already `allow` and only after the fs guard, so a protected path or a path outside the
  roots keeps asking: a grant is the person allowing one call, never a way around the
  guard. The key is the conversation id alone, so a thread's grant never reaches its
  parent or the desk.
- **2026-09-24** — A pending approval is a notification, not just a card. `approvals.
  create` posts one `notification` item with `notify=True` — the existing interrupt path
  (web toast plus every surface in `triggers.notification_targets`) — naming the
  conversation, the tool and the reason, sourced `approval:<id>`. Answering it dismisses
  that item, so the now board never keeps a question that has been answered. A card
  waiting in a screen nobody is looking at is a question nobody hears (TD audit F1),
  and no new mechanism was invented to fix it: items and the interrupt policy already
  existed.
- **2026-09-24** — **A finished thread hands over its evidence, not a pointer to it.**
  `conversation_finish` now delivers to the spawning conversation: the summary first,
  then the walkthrough section it just filed (path and text, capped at 8,000 chars with
  a marker naming `knowledge_read`), then every artifact and image the thread produced
  as an id with a caption. The now-board item stays the bare summary. The 2026-09-20
  decision — no verify-on-finish, judgment in the spawning conversation "fed by evidence
  in the finish walkthrough" — was never actually fed: the walkthrough was filed in the
  daily log and the desk got ~590 characters. It then spent 43 minutes re-deriving a
  thread's work it could have read (audit §3, F4). The mechanism is unchanged; only what
  the message carries is. Built in `conversation/tools.py` (`evidence_block`, `produced`)
  and delivered by the runner, so there is still one notify path.
- **2026-09-24** — **Artifacts and images are readable down the spawn tree, and only
  down it.** `artifact_recall` takes an id belonging to the calling conversation or to
  any conversation below it in the spawn tree (its threads, their sub-threads), never a
  sibling and never an ancestor; an image id returns the image itself. Until now the tool
  read text only and was scoped to nothing at all — any conversation's artifact, but no
  image, so a cited screenshot was unopenable (audit §3). Evidence flows up to the
  conversation that commissioned the work, which is the same direction responsibility
  flows; one helper in `artifacts.py` decides it for both kinds.
- **2026-09-24** — **A turn resumed after an approval keeps its step count.** The count
  is read back from the dangling `tool_call` event's `meta.step`, so it needs no column
  and survives a restart. A turn whose results were lost to a backend restart is not a
  resumed turn and starts over — the distinction the repair branches already draw. A
  thread 65 steps into a 100-step budget woke from an approval with a fresh 100 and said
  so to itself (audit F6).
- **2026-09-24** — **Standby is a named event.** Every model call is measured with both
  the monotonic and the wall clock; when a failed call was spanned by a wall-clock jump
  of a minute or more, the runner writes the system event `system resumed after N min;
  retrying` and retries without charging the model-error budget, at most twice per call.
  ~60 of one 136-minute run went into Modern Standby, and the only trace was a
  stream-idle watchdog line blaming the provider (audit F7). Holding a power request
  while a turn runs stays undecided — that is the person's battery.
- **2026-09-24** — **The turn's stated intent travels with every tool call.**
  `ToolContext.intent` is the step's own assistant text, else the turn's last, else the
  transcript's last; on a repair it is the persisted `tool_call` event's `meta.text`. The
  classifier had only `request_text`, which at the desk is the last user message — "go
  ahead" — and refused seven identical commands for lack of a stated purpose (audit F1).
  The runner fills it; what reads it is the permissions stream's.
- **2026-09-24** — **The desk opens what a thread cites; the thread stops when the
  evidence exists.** `kind-desk.md` now says to open the ids a finish carries before
  reproducing anything, to say so and stop when something cannot be opened, and never to
  rebuild a thread's verification harness; `kind-thread.md` says verification is done
  once the cited evidence exists and a documentation-only edit needs no rebuild (audit
  F4, F10). Both files stay at their 12-line budget: the traded rule is the desk's "when
  asked about a thread, answer from its status and its walkthrough in the daily log, not
  by re-doing its work", which the new rule states more precisely and enforceably.
- **2026-09-24** — **`knowledge_write(op="replace")` accepts `new` as the name for
  `content`.** old→new is how the op reads and how a model sends it (audit F9). Because
  the dispatcher enforces the schema's `required` list before a handler sees the call,
  `content` moved out of it and is checked in the handler — the one place that knows the
  two names mean one field. The prompt names the fields beside each op.
- **2026-09-24** — **Worktree isolation is a property of any thread, not a
  self-modification special case.** `conversation_create(isolation="worktree")` gives a
  thread its own `git worktree` checkout under `APP_HOME/worktrees/<id>` on branch
  `loom/<id>`; the checkout replaces the source root as the thread's first root, so the
  thread cannot touch the tree it was branched from, and the harness block tells it where
  it works and on which branch. A root that is not a git repository is a defined refusal,
  not a silent downgrade. On archive a clean worktree is removed and a dirty one is kept
  with a system event saying why. 03 §15's release worktree becomes one use of this
  mechanism (audit #15 in the backlog).
- **2026-09-25** — **The user's own desk request authorises reversible external
  effects.** On the desk the person typed "please start the bot again"; the judge asked
  about `npm start` because a bot that connects to Discord is an external side effect, and
  the prompt said scope never widens the test. The person's own typed instruction is not
  injected data, so the judge now sees who wrote the request: `Request that led here
  (typed by the user on the desk)` when every claimed row is `source="user"` on the desk,
  `(untrusted data)` for everything else (a thread's goal or handoff is the desk model's
  text; connector, schedule and thread-report rows are someone else's). A specific user
  request allows a reversible external effect — starting, stopping or restarting a
  service, bot or server they named. The boundary stays where harm is hard to undo:
  outbound communication on the person's behalf, payments, pushes, publishing, deleting
  or overwriting work and credential access still ask, as does anything broader than the
  request; vague requests count only when the stated intent makes them concrete, and
  uncertainty is still an ask.
- **2026-09-25** — **The intent fallback stays inside the turn.** `ToolContext.intent`
  falls back to this turn's earlier text only, never the transcript: the judge in the
  incident above was handed a project-ideas reply from 22 hours earlier as the "stated
  intent". A bare first call now renders `(none stated)`; a resumed call takes its own
  preamble, else what its turn said before it. `sleep` joined the default read-only shell
  commands in the same change.
- **2026-09-25** — **Paths are read the way the shell reads them.** A thread ran
  `curl … -o /tmp/centauri/…` then `cd /tmp/centauri && grep …`; Git Bash put the file in
  the user's temp dir, while `fs_guard.normalise` read `/tmp/centauri` as
  `C:/tmp/centauri` — a different, real directory — so the guard, the classifier's
  owned-file lookup and the shell disagreed about which file a path named. Allowing both
  spellings as roots cannot fix that. On Windows `normalise` now reads every POSIX
  absolute path through Git Bash's own mount table (`/tmp` → `%TEMP%`, `/bin` →
  `<git>/usr/bin`, `/` and any unmounted `/foo` → the Git install root), asked once per
  process by running `mount` under the same bash the shell tool finds, derived from the
  bash path and `%TEMP%` if that fails, empty without Git Bash. One function, so every
  consumer (guard, shell cwd, pre-filter, classifier) agrees without edits.
- **2026-09-25** — **Scratch work goes in the conversation's artifacts folder.** The
  harness block's Filesystem section names it (`scratch: this conversation's own folder
  is …`) as the place for downloads and intermediate files instead of `/tmp`, which is
  outside the roots, asks for approval, and is shared with every program on the machine;
  the shell's `TMPDIR` points there too, so `mktemp` and `tempfile` follow. `TEMP`/`TMP`
  are deliberately untouched: Git Bash mounts `/tmp` at `TEMP`, and moving it per
  conversation would make the per-process mount table — and so the guard — disagree
  with the shell about `/tmp` again.
- **2026-09-28** — **Four palettes added** (`general.palette`: grape, ocean, ember,
  moss), approved by the person on 2026-09-28 from the colour-themes proposal. Same
  mechanism as the 2026-09-21 decision — CSS-variable override blocks in
  `ui/src/index.css`, one `choices` entry, no component changes. `moss` is a new
  green-on-dark design meeting the proposal's contrast gates (white on accent-600 ≥ 4:1,
  accent-400 on neutral-900 ≥ 4:1, body text ≥ 12:1, dark muted ≥ 4.5:1).
- **2026-09-28** — **The root checkbox says which button applies it.** A self-mod
  thread's every file call asked, although the person had ticked "also allow this root"
  on each card: the tick only applies with `allow always` (2026-09-21, unchanged), and
  they had answered `allow`. The checkbox keeps that pairing, but ticking it now
  relabels the buttons — "Allow always and add the root" (primary) against "Allow,
  without adding the root" — on both surfaces from one helper.
- **2026-09-28** — **A self-mod thread is spawned isolated.** The procedure note still
  told the thread to `git worktree add releases/dev-<id>` itself, wording from before
  isolation became a property of any thread (2026-09-24); that checkout was outside
  every root, so every read and edit into it asked. Step 1 is now `conversation_create`
  with `isolation: "worktree"`, which makes the checkout the thread's first root by
  construction; the scratch `LOOM_HOME` moves into the thread's own scratch folder
  (2026-09-25) for the same reason. §15.1 says the same thing in one voice now.
- **2026-09-28** — **Presets can be renamed and duplicated.** A preset's name was
  immutable, so changing it or making a near-copy meant retyping every parameter.
  `PATCH /presets/{name}` now accepts `name`: one transaction with deferred foreign-key
  checks updates the row and repoints `model_roles` and every conversation (live or
  archived) that names it; a taken name is a 409 before anything moves, and the
  `models.changed` event carries `renamed_from`. *Duplicate* is a UI affordance, not an
  endpoint: it opens the add dialog seeded from the source (name `<source>-copy`, then
  `-copy-2`…) minus the machine-written `token_factor_seed`, so the person tweaks before
  saving and the ordinary create path does the rest.
- **2026-09-28** — **A promotion planned from a release fetches from the checkout.**
  The first self-promotion moved the backend from the repo checkout into a release
  worktree of the supervisor's bare mirror; the second could not resolve a commit made
  in the checkout, because the mirror learns of commits only when a release is built.
  `plan` now fetches an unresolvable ref from the mirror's origin the way
  `create_release` does, and retries once. The suite runner finds `uv` on PATH before
  `python -m uv`: a project or release virtualenv has no `uv` module, and the desk had
  worked around the "No module named uv" failure by installing one into `.venv`. And
  the runner sets `PYTHONPATH=<worktree>/src` for the child: the supervisor pins a
  release's backend to its own `src` the same way, and a child that inherited that
  tested the live code against the candidate's tests (the third promotion failed on an
  `ImportError` for a name only the candidate had). The supervisor's `uv sync` step
  resolves `uv` the same way; it had the same failure once the hand-installed module
  was pruned. A supervisor change is live only after `loom serve` is started again.
- **2026-09-28** — **What a promotion feeds back, and where its suite runs.** The
  first self-promotion showed four gaps between what the desk was told and what
  happened. (1) The tool prompt promised "the outcome is waiting when you come back";
  the tail only appended a `system` event, nothing woke the desk, and it never
  reported. The outcome is now a message in the asking conversation's inbox — the one
  mechanism that starts a turn — and the tail runs on the event loop so the post can
  wake. (2) The suite ran inside the `promote` call, blocking the desk for nine minutes
  with no visible progress, and the desk then told the person the supervisor was
  "running the suite". It is now a supervisor stage, `tests`, run inside the release
  just built, bounded by `system.promotion_test_timeout_s`, logged like every other
  stage; the backend's runner, throwaway worktree and 600 s cap are gone. (3) A runner
  that could not start was reported as "the test suite fails", which sent the desk on
  a thirty-minute detour re-running the suite by hand; the stage message now
  distinguishes the two by whether pytest's own summary line is present. (4) A step
  with no preamble took the turn's earlier text as its intent (kept: 2026-09-25), and
  the judge read "promoting now — this restarts me" as the intent of five later
  read-only calls; the block is now labelled *carried from an earlier step*, and the
  judge prompt says what that means.
- **2026-09-28** — **Self-review is a procedure, not code.** Loom's ability to find
  its own failures — a thread looping, a tool that keeps breaking, approvals that keep
  asking, a result ignored, a false finish — is a built-in procedure note
  (`procedures/self-review/SKILL.md`, from `prompts/templates/procedure-self-review.md`)
  over the observability that already exists: `conversation_read`, `transcript_search`,
  the daily log, artifacts, the backend log. It attributes each finding by one question
  — would a capable model with these tools have failed the same way? — so a recurring
  "model" failure is filed as harness and a weak model's mistake earns no scaffolding
  (§2.1), names the smallest change, writes to the daily log, and changes nothing
  itself. Two small things in code, both general: the built-in procedures are one table
  (`vault.BUILTIN_PROCEDURES`) ensured by `knowledge.install`, replacing the one-off in
  `promote.install`; and a rendered tool result carries the harness's verdict when the
  call was not simply allowed (`ask → allow: <reason>`), which is what permission
  friction looks like from inside a transcript.
- **2026-09-29** — **The judge's asks, read back over a week.** 339 shell calls reached
  the judge, 69 were asked, and about twenty of those asks were wrong by the prompt's
  own rules while every allow held up — so the fix is not a stricter or looser judge but
  four specific things. The static pre-filter now grants `cd` (87 of the 339 began `cd
  <dir> &&`, seven of them consecutive greps the judge then asked about) and drops
  redirections that write nowhere (`2>&1`, `/dev/null`) instead of failing the parse.
  The prompt says running code is not by itself irreversible and names what is: tests,
  builds, linters, a commit on a branch, a localhost server the goal calls for, a
  package installed into the project's own environment — five pytest asks in a row on
  the desk, a commit and a dev server had each been called "destructive". A read inside
  the roots is an allow whatever the assistant's reason for wanting it. A program the
  user asked to start may talk to a service on its own. And the reason is a verdict —
  rule plus trigger — never a question back to the user. The judge stays on GLM 5.3 for
  now (the user's call, to revisit); "allow for this conversation" stays keyed on the
  exact arguments, which is why a run of thirteen printer-write asks felt long.
- **2026-09-29** — **An edit that misses is told where it missed.** Of 26 `fs_edit`
  failures in one week, 2 sent CRLF against an LF file, 4 differed in whitespace or a
  moved line break, and 18 were text the model had reconstructed — one of them retried
  six times on an error that only said "copy the text exactly". Two changes, both
  general. The matcher no longer compares what a transcript cannot show: when nothing
  matches exactly, line endings and trailing whitespace are ignored, the span still
  indexes the original text, `new` is written with the file's own endings, and one
  exact occurrence beats loose ones; indentation and wording still have to match, since
  the model can see them (no indent-agnostic or rewrapped matching — that would absorb
  a weaker model's mistakes, §2.1). And the miss itself is precise: the error shows the
  closest text as a diff of the file (-) against `old` (+), and an ambiguous `old` names
  the lines it is on, so the next attempt is a correction rather than another guess.
  The notebook's cells match through the same function.
- **2026-09-29** — **One matcher for notes too, and the daily log may say "today".**
  The vault's `replace` had its own exact matcher and had missed 25 times with the
  same "copy the text exactly" that `fs_edit` used to give; the rule now lives once, in
  `loom/textmatch.py`, imported by the tools and the knowledge packages alike, so notes
  get the same forgiveness of line endings and trailing whitespace, the same closest-
  text diff on a miss, and the same line numbers on an ambiguous `old`. And the finish
  handler writes the daily log with the relative-date refusal off: that note's name is
  its date, so "today" inside it is absolute by construction, and four finishes in one
  night had each cost a retry over the word. Every other note keeps the refusal, and the
  refusal's message stays as it is (the user held off on adding the date to it).
- **2026-09-29** — **Ignore-aware search dropped from the backlog.** `fs_list` and
  `fs_search` keep ignoring nothing; `.gitignore` awareness (tool-parity audit #4) has
  not been felt as friction, so it is not built. Revisit only if it bites.
- **2026-09-29** — **Token metrics: persisted per step, totals derived.** Every
  assistant/tool_call event gets `meta.usage` (`prompt_tokens`/`completion_tokens`,
  whichever the provider reported for that step) when the runner persists it — no
  migration, since it rides in the existing `meta` json. Nothing new is stored: the
  conversation's totals and its last-20-turns breakdown are summed from that json on
  every `GET …/usage` call, the same way occupancy is computed, not calibrated or
  cached.
- **2026-09-29** — **Notification toasts fade, and items resolve themselves.** The web
  toast for an interrupt item now fades after `general.notification_fade_seconds`
  (default 6; 0 keeps it until dismissed), pausable by hovering. And the writer that
  posted an item now resolves it through `by_source` when its cause is gone (an
  approval decided, a thread running again, a model configured, an MCP server
  connected, the next promotion) rather than leaving every item for a person to click;
  opening an item's target conversation marks it seen.
