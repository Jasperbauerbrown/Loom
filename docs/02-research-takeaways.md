# Research takeaways: what the field does that we should apply

**2026-09-20.** A sweep of `reference/agentic-harnesses.md` (85 systems),
`reference/harness-study.md` and `reference/context-lifecycle.md` (Hydra's own context
write-ups), filtered through the charter's ethos. Each item: the pattern, who does it, and the effect on our design. **Adopt** =
take as-is; **Adapt** = take the idea, change the mechanism; **Reject** = considered and
declined, with reason, so it is not re-litigated.

## 1. Field-level conclusions that confirm the charter

- **The agent loop is a commodity** (§1.1, §1.4). Every 2026 harness has the same loop;
  several now wrap *competitors'* loops. Differentiation is context assembly, memory,
  permissions, UX. → Keep the loop tiny; spend effort on the other four.
- **Filesystem-as-memory is the quiet consensus** (§1.2): Letta's live product converged
  on git-backed markdown; Claude Code, Basic Memory, Codex, Kortix, Manus all landed on
  files. → Vault design confirmed.
- **The agent writes its own memory with ordinary file tools** (Letta, Claude Code,
  Basic Memory, Agent Zero) is the camp whose systems actually learn cross-session.
  Service-owned extraction (Mem0, Zep) never lets the agent decide. → Confirmed.
- **The big labs all folded standalone agents into one assistant that decides how much
  agency a request needs** (Cowork→Claude, Operator→ChatGPT, Mariner→Gemini). → The
  desk ladder is the same claim.
- **"A session is a resource you connect to"** (Cline hub daemon, opencode server, ACP
  v2 "beyond the turn": updates arrive any time, a prompt response only acknowledges).
  → Surface contract confirmed; add an explicit idle/busy state per conversation.
- **Agent-memory benchmarks are vendor-run and unreplicated** (LoCoMo 84% vs 58% vs 75%
  for the same system). → Do not design against them; design against the four examples.

## 2. Model-facing

| Pattern | Source | Verdict | Effect on design |
|---|---|---|---|
| Byte-stable prompt prefix, volatile content last, no timestamps in the prefix | Manus, browser-use, Hydra slots [1]–[5.5] | **Adopt** | Fixed slot layout; date/time and now-board live *after* the stable prefix. Matters for Ollama's prompt cache. |
| Model-visible step budget so the agent can "land the plane" | Goose `<turn-budget>`, opencode | **Adopt** (threads) | Remaining steps injected as one line; final step runs with tools disabled for a wrap-up. |
| Tool errors returned as results, never crash; malformed args → error result, not execution | Hermes, Hydra P1 | **Adopt** | One error path in the tool dispatcher. |
| Interrupted turn: synthesise a tool result for a dangling call on resume | Hermes recap, Hydra P5 | **Adopt** | Replay builder repairs unpaired calls; model is told work was cut short. |
| Token estimation calibrated from real `prompt_tokens` | Hydra (none of the studied harnesses do this) | **Adopt** | chars/4 × learned factor per conversation; saturated readings only raise it. |
| Tool output bounded at capture, spilled to a file, recallable; "drop the body, keep the reference" | Claude Code, Manus restorable compression, Hydra artifacts | **Adopt** | Large results → artifact file + head/tail preview + `recall` tool. |
| Compaction ladder: reversible first (evict results to stubs), lossy summary second, loud failure last | Manus, Hydra `ensure_fits` | **Adopt** | Also: keep failures in the summary (Manus) so the model updates its beliefs. |
| Generation headroom: `num_ctx` counts prompt + reply | Hydra §5.1 | **Adopt** | Compaction limit = window − max_tokens (capped at half). |
| Reasoning separated from content; replayed within a turn, never across turns | Hydra §4.2 | **Adopt** | Qwen 3.x are thinking models; this is the single largest context consumer. Head-capped replay, knob. |
| Concurrent execution of read-only tool calls | Claude Code, Hydra P4 | **Adopt** | Uses the same `readOnlyHint` we already rely on for permissions. |
| Deferred tools behind a `tool_search` | LibreChat, Claude Code | **Adopt** (already in charter) | Only when the set is large. |
| Empty completion → retry once | Claude Code, Hydra P8 | **Adopt** | Under the transient-retry budget. |
| Plan as *re-injected structured data*, not `todo.md` recitation | browser-use `plan_update`, Manus (abandoned todo.md: ~30% of tokens) | **Adapt** | The thread's status section (goal / done / doing / next / blockers) *is* the plan. Re-injected each turn; user-editable in vault/UI; no separate plan mode. |
| Per-model-family system prompts | opencode, Kilo, Crush, Codex | **Reject** | One bar model; prompt variants are the start of a crutch pile. Prompts are files, so a variant is trivial later. |
| Code-as-action | smolagents, Codex code-mode, Manus | **Reject** (v1) | Needs a real sandbox; JSON tool calls are fine at the bar. Revisit only if round-trip latency dominates. |
| LSP as agent tooling | Crush | **Reject** (v1) | Language-specific; contradicts minimal. |

## 3. Memory

| Pattern | Source | Verdict | Effect on design |
|---|---|---|---|
| Always-in-context core blocks with hard char caps + everything else behind an index / on demand | Letta root `*.md`, Claude Code `MEMORY.md`, Hydra slot [3] | **Adopt** | `core/` notes (about-me, preferences, conventions) always injected, capped by construction; the rest resolved or searched. |
| Harness-stamped `modified` timestamp in frontmatter to make staleness visible | Claude Code | **Adopt** | Zero-cost mitigation for the vault camp's weak spot (contradictions). |
| No relative dates in notes | Letta sleep-time prompt | **Adopt** | Operating rule for `knowledge_write` and the maintenance prompt. |
| Background consolidation as a second agent under a lock, fed only since a watermark | Letta sleep-time (`last_processed_message_id`), Qwen Code `dream`, Codex rollout miner | **Adopt** | Maintenance role: `last_processed` watermark per conversation; one lock; runs when idle. |
| Targeted edits over whole-note rewrites; rewrite only as an explicit act | Letta `memory` tool (`str_replace/insert/…`, `rethink`), ACE's "context collapse" finding | **Adopt** | `knowledge_write` ops: `create / append / replace / rethink`. Guards against telephone-game degradation of notes by repeated LLM rewrites. |
| Full-text search over raw transcripts as the cheapest episodic memory | Kilo `recall`, Hermes FTS5 | **Adopt** | SQLite FTS5 over transcripts. No embeddings needed for episodic; embeddings only for the vault. |
| Git as memory durability | Letta, Codex, Kortix | **Adopt** (already) | Vault is a git repo; maintenance commits its own sweep so it is diffable/revertible. |
| Skills as directories with progressive disclosure (`SKILL.md`: name+description always, body on demand, scripts executed not read) | Agent Skills standard (Anthropic, Codex, Cursor, MAF) | **Adapt** | `procedures/` notes follow the SKILL.md convention. Ecosystem skills drop in; the assistant can author them; third-party ones install via approval (they carry code). |
| Human approval on every inferred memory | Cursor Memories | **Reject** | Friction contradicts simplicity. Take *visibility* instead: a "recent memory changes" view backed by git log, one-click revert. |
| Bitemporal fact graphs, typed supersede edges, decay weights | Graphiti/Zep, Supermemory, cognee | **Reject** | Atomic-fact systems need them; a vault does not. Cost is a graph DB and an extraction pipeline; the payoff is answering "what did we believe when", which git history already gives us. |
| The system prompt as a learned artifact | LangMem, Letta | **Reject** | Soul is not mutable state. Learned behaviour goes in `core/preferences.md`, which is in-context anyway. |
| Service-owned extraction the agent never sees | Mem0, Zep, Khoj | **Reject** | The agent must decide what to remember. |

## 4. Permissions and control

| Pattern | Source | Verdict | Effect on design |
|---|---|---|---|
| LLM judge for actions static rules cannot decide | Claude Code, Gemini CLI conseca, Goose SmartApprove, Codex guardian, Cursor, Qwen Code | **Adopt** (already: `classify`) | Goose's wording: the judge treats tool names and arguments as untrusted data. Goes in the policy prompt. |
| Static shell analysis before the judge: split the pipeline, resolve the most restrictive policy | Continue `terminal-security`, Qwen Code parsed semantics | **Adapt** | Tiny pre-filter that can only *grant*: parse the command into stages; if every stage is on a small read-only allowlist → allow with no model call. Everything else, including parse failures and `&&` chains with a non-listed stage → classify. It never denies (Hydra's static filters did, and models fought them). |
| Approval as durable, resumable state | LangGraph `interrupt()`, OpenAI Agents `RunState` | **Adopt** | A pending approval is persisted with the conversation and survives restart. |
| Delegation depth cap | Claude Agent SDK (default 3) | **Adopt** | Threads may spawn threads via the same conversation tools; one `max_depth` setting. |
| Scheduling caps rather than approvals for autonomy | ChatGPT Scheduled Tasks (≤10 active, ≥1h) | **Adapt** | Soft settings: max active schedules, min interval. Protects a single local GPU from runaway proactivity. |
| Completion adjudicated, not asserted: an independent verifier must accept a "done" claim from evidence | Qwen Code Goal Verifier; Hydra backlog "degraded turns silently satisfy delegations" | **Adapt** (user decision 2026-09-20) | No hardcoded verifier call — an easy failure point. The *spawning* conversation (desk or parent thread) verifies by judgment under operating prose: read the walkthrough and evidence, spot-check, or spawn a verifying thread for large builds. Harness support is limited to routing the finished event to the parent and making evidence (test output, diffs) recallable artifacts. |
| Plan gated before execution; agents emit reviewable artifacts (plan, walkthrough with verification evidence) | Jules, Antigravity | **Adapt** | No plan mode. A thread's first status write *is* the plan and is editable before work proceeds; a thread's finish writes a short walkthrough (what changed, how verified) into the project note. |
| Loop hooks (pre/post tool, shell or LLM) | Claude Code, Hydra P3 | **Reject** (v1) | Our two policy points (`classify`, fs guard) cover the need; hooks are extensibility we do not yet need. |
| OS sandbox (Seatbelt/Landlock/Windows sandbox service) | Codex, Gemini CLI | **Reject** (v1) | Single-user personal machine; permissions + protected paths + git snapshots are the boundary. Heavy on Windows. |
| Container as the entire boundary | Agent Zero, Manus, Devin | **Reject** | The assistant must reach the user's real machine, printer and files. |

## 5. Structure

| Pattern | Source | Verdict | Effect on design |
|---|---|---|---|
| Server + thin typed clients; every surface a client of one engine | opencode, Cline hub, Khoj (Obsidian/Emacs/WhatsApp over one API) | **Adopt** (already) | Confirms §5.2. |
| Subagent = context isolation: empty window in, final message out; child reads parent context for decision quality but writes only its own | Claude Agent SDK, Mastra | **Adopt** (already: spawn payload) | Confirms one conversation primitive with scoped spawn. |
| Git worktree as the cheapest isolation primitive | Conductor, Letta reflection, Cursor racing | **Adopt** (already: self-mod) | Also available to any thread that wants isolation from a live checkout. |
| Prompts, tools, profiles as editable files, nothing hardcoded | Agent Zero, Codex `instructions_template` as config, Hydra `prompts/` | **Adopt** | All model-facing prose in `prompts/*.md`. |
| Same binary is the interactive agent, the scheduled worker and the CI job | Droid | **Adopt** | Triggers produce ordinary turns (already); no separate worker binary. |
| Retrieved content returned as data, never as instruction | Zep, Skyvern `untrusted` filter | **Adopt** | Tool/connector results are fenced as data; the vault is trusted because the agent or user authored it. Provenance noted when a note quotes external content. |
| Harness as swappable backend (ACP, OpenHands `ACPAgent`) | Zed, OpenHands | **Note** | Not a goal, but the surface contract should not preclude speaking ACP later. |

## 6. What this changes in the charter

Folded in as decisions: static shell pre-filter; `knowledge_write` op set; `core/` always-in
blocks; FTS5 transcript recall; SKILL.md-shaped procedures; scheduling caps; depth cap;
persisted approvals; plan = first status write; walkthrough on finish; idle/busy state.
Verify-on-finish resolved as process, not mechanism (see §4).
