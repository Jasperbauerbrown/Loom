> **Reference copy.** Copied from the Hydra repository on 2026-09-20 for Loom's design work. File paths and code references inside refer to Hydra's codebase, not Loom's; they are kept verbatim as evidence.

# Agentic harnesses: a survey of the field

**Compiled 2026-09-17.** Scope: harnesses designed to let an LLM *do arbitrary work on a
computer* — coding, projects, computer use, general assistance. Workflow-graph builders
(Dify, Flowise, n8n) are out of scope and recorded as such; the unit of design there is a
DAG the user authors ahead of time, so there is no open-ended agent loop to compare.

Seven researchers worked in parallel. Open-source systems were shallow-cloned and read at
source — agent loop, prompt templates, tool definitions, context condensation, memory
writer, permission model — and entries cite file paths. Closed products are reconstructed
from primary docs and changelogs and are marked `(docs)` or `(reported)`. `unclear` means
the answer could not be determined and was not guessed.

## How to read an entry

Every entry answers the same eight questions: what it is, what you hand it (**Inputs**),
what it can change (**Effects**), and then the three axes this survey is organised around
— **model-facing** (tools, context assembly, turn-to-turn state), **memory/learning**
(what survives a session and how), and **user-facing** (interaction surface, approval
model). **Unique element** is the distinctive architectural bet in one line.

A note on the dominant answer: for most harnesses the honest memory entry is *"none beyond
the session transcript."* That is stated plainly rather than dressed up. Genuine
cross-session learning is rare, and §2 counts exactly how rare.

---

# 1. Synthesis

## 1.1 Model-facing architecture

**The agent loop has commoditised.** The 2026 loop is the same everywhere: call model,
dispatch tool calls, append results, repeat until a terminal message with no pending calls.
The interesting variation is no longer *the loop* but what bounds it, what the tools look
like, and how the context is rebuilt each turn.

**Turn bounding has not converged**, and the differences are revealing:

| Harness | Bound |
|---|---|
| Codex CLI | token budget + cancellation tokens, **no step counter** |
| OpenHands | 500 iterations **plus a cross-LLM cost budget** |
| Gemini CLI | `MAX_TURNS = 100` |
| Goose | `max_turns`, with a `<turn-budget>` marker **injected into the prompt** |
| opencode | `agent.steps`, final step appends a synthetic max-steps message |
| browser-use / UI-TARS | `max_steps` / `maxLoopCount` (default 100) |

Goose and opencode make the budget *model-visible* so the agent can land the plane; Codex
bounds by economics rather than steps. Most others simply truncate.

**Three action formats now compete.** (a) JSON tool-calling, the default. (b) **Code as
action** — the model writes a program that composes several calls with loops and
conditionals in one model call: smolagents' core bet, and now shipping as an option in
Codex (`code-mode` crates), opencode and Kilo (`code-mode.ts`), Goose (`code_execution`),
Qwen Code (sandboxed JS in a host process with framed IPC and hard byte/time budgets),
LibreChat (Programmatic Tool Calling) and Manus (CodeAct over ~20 atomic tools). It trades a
real sandbox requirement for far fewer round trips. (c) **No tool layer at all** — Aider
parses edits out of raw completion text with a fuzzy search/replace cascade, which is why it
works with models that have no tool-calling API. Continue is the interesting hybrid: for
models without native tool calling it re-renders the *same* tool set as prompted markdown
and intercepts the output, so transport and tool surface are decoupled.

**Per-model-family system prompts are now common** — opencode, Kilo and Crush all select a
prompt by model ID (`anthropic.txt`, `gpt.txt`, `gemini.txt`, `codex.txt`, `kimi.txt`…),
and Codex ships per-model `instructions_template` strings as *config* in
`models-manager/models.json` rather than as checked-in code. The one-prompt-fits-all
assumption is gone.

**Interface design vs. model capability is now an open question, not a settled one.**
SWE-agent's ACI thesis — that the *interface* is the tunable surface, evidenced by a paged
file window, an `edit` command that runs a linter on the proposed result and **refuses to
apply** a syntax-breaking edit, and a hard blocklist of interactive programs — was the
field's founding insight. Its own authors then shipped **mini-swe-agent**: ~100 lines,
bash-only, no tool-calling, no history processing, reportedly >74% on SWE-bench Verified.
Read together they say frontier models have absorbed what the ACI used to supply. **Agentless**
makes the complementary point from the other side: a fixed localize→repair→validate pipeline
with no agency beat contemporary agent loops at a fraction of the cost.

**Context assembly is where the real engineering is**, and two independent teams
(Manus, browser-use) converged on the same playbook:

- **KV-cache stability as the primary production metric** — append-only prefixes, no
  timestamps, deterministic JSON key order, explicit cache breakpoints. Cached tokens are
  ~10× cheaper and the input:output ratio runs ~100:1.
- **Logit-mask the tool set instead of changing it** — tool definitions stay in the stable
  prefix; `browser_*` / `shell_*` name prefixes make masking cheap.
- **Plan as re-injected data, not re-derivation.** Notably Manus *abandoned* `todo.md`
  recitation (~30% of tokens) for a Planner sub-agent emitting a structured Plan object;
  browser-use likewise dropped its separate planner LLM for an in-context `plan_update`
  rendered `[x]/[>]/[ ]/[-]`. Write-ups still describing todo.md recitation are stale.
- **Reversible compaction before lossy summarization** (Manus compacts, then summarizes at
  ~128k, and reports quality degrading ~256k despite a 1M window).
- **Keep failures in context deliberately** so the model updates its beliefs.

Prompt-cache awareness has reached the type system: OpenHands decomposed its Jinja system
prompt into typed `PromptSection` classes carrying `cache_tier`, and SWE-agent has a
`CacheControlHistoryProcessor` whose only job is placing breakpoints.

**Observation format is the whole ballgame for computer use**, and the field has split:

| Format | Systems | Trade |
|---|---|---|
| Pixels only | Anthropic computer tool, OpenAI CUA, UI-TARS | no DOM dependency; grounding must live in the weights |
| Indexed DOM | browser-use, Skyvern | cheap, precise, brittle to exotic UIs |
| Accessibility tree | Stagehand | middle ground, trimmed and cacheable |
| Swappable | Skyvern `RunEngine`, Agent TARS | picks per page |

browser-use's serializer is the canonical indexed-DOM design: ephemeral `selector_index`
per actionable node, `*[` marking elements new since the last step, `|SCROLL|` and
`|SHADOW(open)|` prefixes. Skyvern hashes elements so identity survives re-mounts.

## 1.2 Memory and learning architecture

**The sharpest discriminator is who writes memory.** It predicts nearly everything else:

1. **The agent itself, via ordinary tools** — Letta, Claude Code auto-memory, Basic Memory,
   Agent Zero, Gemini CLI `save_memory`. Memory is a file the model edits with the same
   tools it uses for code. No embeddings, no extraction service.
2. **A service-owned background extraction LLM the agent never sees** — Mem0, Zep, Graphiti,
   Supermemory, Khoj, cognee, Open WebUI. The agent never decides what to remember.
3. **A sidecar proposer gated on human approval** — Cursor Memories (a second model watches
   the conversation and proposes; background writes require explicit approval), and
   Supermemory's inferred-memory review queue.

Camp 3 has exactly two members. **Write approval is the field's least-explored axis:**
Claude Code and ChatGPT let the model write silently and offer a post-hoc audit surface.

**Write trigger is a latency ladder**, and deferral buys coherence at the cost of freshness:

in-turn tool call → end-of-turn → counter-gated background pass (Letta's
`sleeptime_agent_frequency` modulo; `off|step-count|compaction-event`) → fully async batched
ingest (Supermemory's "dreaming" batches by `customId` so facts form from coherent units) →
a separate re-runnable offline pipeline (cognee's `memify`; Codex's two-phase rollout miner).

**Contradiction handling tracks unit atomicity exactly** — and this is the survey's most
actionable finding. Atomic-unit systems get real mechanisms:

- **Bitemporal invalidation** (Graphiti/Zep): `valid_at`/`invalid_at` for event time vs.
  `created_at`/`expired_at` for ingest time. Facts are never deleted, only invalidated — so
  the graph can answer *"what did we believe, and when."*
- **Typed supersede edges** (Supermemory): `updates` / `extends` / `derives` plus `isLatest`.
- **Weight decay** (cognee): EWMA feedback weights, `valid_to` closure.

File- and block-unit systems have **none of this**. Letta relies on an LLM instructed not to
keep "redundant and outdate information"; Claude Code's docs state that across conflicting
files "Claude may pick one arbitrarily"; Basic Memory does nothing at all by design. The
markdown-vault camp buys human legibility and pays for it in contradiction handling.

**Retrieval divides into always-in-context vs. queried**, and serious systems do both.
Letta renders labeled blocks into the system prompt every turn *and* offers archival vector
search. Claude Code keeps a `MEMORY.md` index always loaded and reads topic files on demand.
Zep is the odd bet: the **query is implicit** (the last four thread messages) and the product
sells the *constructor* step — a template-rendered prose block — rather than the search step.

**Genuine cross-session learning is rare.** Across ~85 systems surveyed, the harnesses that
actually learn from their own past runs are: **Codex CLI** (a background two-phase job mines
past session rollouts into a git-versioned memory workspace maintained by a dedicated
consolidation sub-agent — learning that happens *outside any live turn*), **Qwen Code**
(three markdown layers — user, project, and **team memory stored inside the repo so git is
the sync transport** — with a `scan → extract → remember → index → dream → recall` lifecycle,
`dream` being a consolidation/dedup pass under a lock, plus an agent that proposes new skills
from experience subject to review), **Claude Code** (model-written topic notes), **Kilo Code**
(a curated project-memory store with capture/redact/audit, *plus* a `recall` tool that
full-text searches the machine's own past session transcripts across git worktrees),
**Letta** (sleep-time compute + reflection in a git worktree), **Agent Zero** (a `SOLUTIONS`
FAISS area auto-written at `monologue_end`), **Jules** (per-repo memory of user corrections,
replayed on similar tasks), and **Devin** (Knowledge + Playbooks, minable from its own past
sessions). Aider, Goose, opencode, Cline, Continue, Crush, Zed, Amp, Copilot coding agent,
Codex cloud, SWE-agent, Agent S3 and every browser agent surveyed have **none**.

Kilo's `recall` is worth isolating as a design: instead of curating a memory document, it
treats **raw session history as the store** and makes retrieval over it a first-class tool.
That is the cheapest possible cross-session memory and sidesteps every consolidation and
contradiction problem by never consolidating at all.

One widely-cited system does *not* belong on that list: **Cline's "Memory Bank" is not code.**
It is a documented prompt convention — a `memory-bank/` folder of six markdown files driven
by the phrases "initialize memory bank" / "update memory bank" via a `.clinerules` rule. It
works, but it is a prompt pattern any harness can adopt, not a subsystem.

Two things people believe that the code contradicts: **Letta's Python V1 server is archived**
and the live product is a **git-backed markdown filesystem** (root `*.md` are the in-context
blocks, child dirs deferred behind `MEMORY.md` indexes) — the canonical "self-editing memory
blocks" system converged on markdown vaults. And **mem0's shipped write path is ADD-only**
additive extraction, not the ADD/UPDATE/DELETE/NOOP loop its own paper describes; the old
prompt is still in the tree, unused.

**Filesystem-as-memory is the quiet consensus.** Manus (sandbox files as unbounded
externalized context with *restorable* compression — drop the page body, keep the URL),
Letta (MemFS), Claude Code, OpenHands, Gemini CLI, Basic Memory, Agent Zero and Kortix ("a
company is going to be a git repository") all landed there independently. Git specifically
keeps appearing: Codex, Letta, Kortix and Conductor all use it as the durability or
isolation primitive.

## 1.3 User-facing architecture

**LLM-as-judge is the year's clearest convergence.** Six independent teams concluded that
static allow/deny rules are insufficient and added a *second model call* to adjudicate:

| System | Mechanism |
|---|---|
| Claude Code | auto-mode classifier reviewing each action |
| Gemini CLI | `safety/conseca/policy-enforcer.ts` — a small Flash model judging each call against a natural-language security policy |
| Goose | `SmartApprove` (`prompts/permission_judge.md`), prompt explicitly treats tool names and args as untrusted data |
| Codex CLI | `guardian` — an isolated **synchronous** reviewer with its *own* request/token budgets, decoupled from the static policy engine |
| Cursor | Auto-review is a three-stage pipeline: allowlist runs instantly → sandbox where possible → **LLM classifier subagent** returning allow / retry-differently / ask-human |
| Qwen Code | parsed shell semantics + destructive-command rules, with an LLM classifier for the ambiguous remainder and denial tracking |

OpenHands goes further and has the **acting LLM predict a `SecurityRisk` label for its own
action**, which a `ConfirmRisky(threshold=HIGH)` policy then gates on.

**Qwen Code extends the judge pattern past permissions to completion**, which nobody else
here does: a goals runtime accumulates typed evidence records, and a terminal-status claim
must be *accepted* by an independent **Goal Verifier** — its own LLM call, its own system
prompt, a strict accept/reject schema, a 256 KB evidence budget, and rules like "evidence
that is insufficient is a rejection, never an acceptance" and `proofKind:
"delivered_output"` cannot prove external state changed. Completion is adjudicated rather
than asserted. Given that self-reported task success is the standard failure mode of long
autonomous runs, this is the most transferable idea in the survey.

**The best non-LLM command gate found in code is Continue's** `@continuedev/terminal-security`,
which parses a shell command into its pipeline stages and resolves the **most restrictive**
policy across them — approval as static analysis rather than string or prefix matching. It
is strictly better than the allowlist-pattern approach most harnesses ship, and cheaper than
a classifier call.

**Permission architecture otherwise splits three ways:** declarative rule sets (opencode's
last-matching-rule-wins with an `ask` fallback; Claude Code's per-tool specifiers), **OS
sandboxes** (Codex's Seatbelt/Landlock+seccomp+bubblewrap and a dedicated Windows sandbox
service, crossed *orthogonally* with an approval ladder; Gemini CLI ships six macOS seatbelt
profiles and a compiled C# Windows sandbox), and **the container as the entire boundary**
(Agent Zero, Manus, Devin — no per-tool model at all). Home Assistant is the fail-closed
outlier: capability is an entity-exposure allowlist declared by the platform at config time,
never negotiated by the agent.

**Isolation primitives for multi-agent work, cheapest first:** git worktree (Conductor —
turns "N agents on one repo" into a filesystem problem rather than a coordination protocol),
git branch per session (Kortix, with merges as reviewed change requests), container/VM per
agent (Devin's managed Devins, Codex cloud's fan-out), and fresh context window (Claude Agent
SDK subagents).

**The plan is becoming a first-class editable artifact** — Jules gates execution on a plan
the user can review, edit or reject; plan mode ships in Claude Code, Gemini CLI, opencode,
Cline, Continue, Crush and Cursor. This is the main answer to "how do I steer a long
autonomous run without watching it."

**Antigravity pushes that idea furthest:** agents emit *reviewable deliverables* — a Task
List, an Implementation Plan **before** any code changes, and a post-hoc Walkthrough bundling
diffs with verification evidence (test runs, browser screenshots, recordings) — and the human
comments inline on those artifacts. Review is repositioned from reading a tool-call stream to
commenting on documents, and the integrated browser is what generates the evidence. Its
Mission Control board, not a file tree, is the default landing view.

**The session is becoming a detachable resource.** Cline extracted its loop into a layered
SDK behind a **hub daemon** so VS Code, CLI, desktop and Kanban clients attach and detach
from the same *live* run; opencode and Kilo are servers with thin typed clients; Amp
provisions an **Orb** — a cloud machine per thread, preloaded with repo and tools — that
keeps working after the laptop closes. The local-process assumption is dissolving in favour
of "a session is a resource you connect to."

**Out-of-session agency** — the thing that runs when the user isn't there — takes four
shapes: scheduled re-invocation of a stored prompt (ChatGPT Scheduled Tasks, explicitly
rate-limited to ≤10 active tasks and one run/hour; Khoj automations; Goose's scheduler;
Open WebUI automations), background memory consolidation (Codex's rollout miner, Letta's
sleep-time compute, Open WebUI's post-turn memory reviewer), event-triggered runs (Copilot
coding agent on issue assignment, Jules ingesting CI failures and auto-fixing), and
persistent background workers (Factory's Droid Computers).

**The big labs all retreated from standalone agent surfaces in 2026.** Google shut down
Project Mariner (2026-05-04) into Gemini Agent and Chrome auto-browse; OpenAI folded Operator
into agent mode, pulled agent mode, and discontinued Atlas, moving the work into ChatGPT and
Codex, while retiring Pulse into Scheduled Tasks; Anthropic folded Cowork into Claude chat
(2026-09-16). The consolidation *is* the architectural claim: one assistant that decides how
much agency a request needs, rather than a separate product the user must choose.

## 1.4 Harnesses are wrapping other harnesses

The loop having commoditised, several systems now treat *the harness itself* as a swappable
backend — the strongest single signal about where differentiation has moved:

- **OpenHands** `ACPAgent` makes Claude Code, Codex, Gemini CLI or any ACP server an
  interchangeable `AgentBase` subclass, so OpenHands' condenser, memory, skills and security
  stack wrap competitors' agents.
- **Skyvern** `RunEngine` swaps `openai_cua` / `anthropic_cua` / `ui_tars` /
  `yutori_navigator` — DOM harness and pixel models as interchangeable backends.
- **Open Interpreter** (post-rewrite, now a Codex CLI fork) makes `/harness` its headline
  feature, emulating `claude-code`, `kimi-code`, `qwen-code`, `zcode` so a cheap model gets
  the prompt discipline it was trained against.
- **Conductor** wraps Claude Code, Codex and Cursor without touching their tools or context.
- **Kilo Code** abandoned its own Roo/Cline fork lineage and **rebuilt on the opencode server
  codebase**, keeping only its differentiating packages (memory, indexing, sandbox, gateway)
  on top — a harness adopting a rival's engine wholesale.
- **Zed** ships ACP so the editor is a *client* to interchangeable agents rather than shipping
  the one true agent; JetBrains, neovim, Obsidian, Sublime and others now implement it.

If the loop is a commodity, the defensible layers are context assembly, memory, permissions
and UX. Augment bet on exactly this by unbundling its retrieval Context Engine and selling
it as an MCP server to *rival* harnesses.

**ACP's v2 draft is the protocol catching up to this reality.** Its headline change is
"moving beyond the turn": `session/update` may arrive at any time, a prompt response only
*acknowledges receipt* rather than ending the turn, and the agent explicitly signals
idleness — all to support background work, queueing and mid-run steering. The request/response
turn is no longer an adequate model of what these systems do.

## 1.5 Status corrections

Several widely-referenced systems are not what current write-ups describe. Anything built on
a 2025 mental model should re-check these:

| System | Actual state |
|---|---|
| **OpenHands** | Split: `OpenHands` repo is now a thin TS control panel ("Agent Canvas"); the engine is `All-Hands-AI/agent-sdk` |
| **Roo Code** | **Dead** — shutdown announced 2026-04-21, repo archived 2026-05-15 at v3.53.0. Kilo Code is the successor |
| **Windsurf / Cascade** | **Discontinued** — shipped as Devin Desktop OTA 2026-06-02, Cascade hard-EOL 2026-07-01, replaced by Devin Local (Rust rewrite); `windsurf.com/blog` 308-redirects to `devin.ai/blog` |
| **Cline** | 2.0 dropped the XML pseudo-tool protocol for native function calling and extracted a layered SDK + hub daemon |
| **Open Interpreter** | Pivoted — now a Codex CLI fork for low-cost models; the `--os` computer-use mode no longer exists in the tree |
| **Letta** | Python V1 server archived Sep 2026; live product is TypeScript `letta-code` with git-backed markdown memory |
| **Mem0** | Shipped write path is ADD-only additive extraction, not the paper's ADD/UPDATE/DELETE/NOOP loop |
| **AutoGen** | Maintenance mode; last release 2025-09-30. AG2 (v1.0, ground-up rewrite) and MAF are the live lines |
| **Cursor Memories** | Docs page withdrawn (308 → `/docs/rules`); shipped as a Cursor 1.0 beta, current status uncertain |
| **Project Mariner / Operator / Atlas / Pulse / Cowork** | All retired into their parent assistants during 2026 |

Also verified false: a secondary source claiming Google shut down Gemini CLI in June 2026.
The repo has commits dated 2026-09-18 and ships nightly builds.

---

# 2. Unique elements, aggregated

The distinctive bet of each system, grouped by kind. This is the section to mine for ideas.

## Memory and learning bets

| System | The bet |
|---|---|
| **Letta** | The agent's own system prompt *is* the mutable store; file-editor-grade self-edit tools plus a scheduled second self ("sleep-time compute") to keep it tidy |
| **Codex CLI** | Two-phase background pipeline mines past sessions into a git-versioned memory workspace maintained by a consolidation sub-agent — learning outside any live turn |
| **Graphiti/Zep** | Facts are never overwritten, only invalidated on a second time axis — the graph answers "what did we believe, and when" |
| **Supermemory** | "Dreaming": extraction deliberately deferred and batched across related documents, typed supersede/enrich/infer edges, human review queue for inferences |
| **cognee** | "Cognify then memify" — memory *quality* is a pipeline you re-run over the existing graph, not a decision made at write time |
| **Basic Memory** | The knowledge graph is a projection of human-editable file syntax; the system deliberately does no LLM curation of its own |
| **Mem0** | Memory as an extraction-pipeline product, decoupled from the agent loop — the agent never decides what to remember |
| **LangMem** | The only mainstream layer treating **the system prompt as a learned artifact**, with three named optimizer algorithms |
| **Claude Code** | Agent writes its own memory with the same file tools it uses for code; always-loaded index + on-demand topic reads, no embeddings anywhere |
| **ChatGPT** | Memory as an **editable natural-language summary document** the user rewrites by typing corrections — and OpenAI concedes the summary isn't the whole memory |
| **Cursor** | A sidecar model watches the conversation and *proposes* memories the user must approve — the only write-path human-in-the-loop here |
| **Jules** | Per-repo memory of user corrections replayed on similar tasks + frozen environment snapshots: the **repo**, not the session, is the unit of accumulated state |
| **Devin** | Knowledge + Playbooks (a per-task reusable procedure), minable from its own past sessions |
| **Agent Zero** | A dedicated `SOLUTIONS` memory area auto-written at the end of every run |
| **Kortix** | "A company is going to be a git repository" — agents, skills, config and memory are all version-controlled files |
| **Manus** | Filesystem as unbounded externalized context with **restorable** compression (drop the body, keep the URL) |
| **Qwen Code** | Three memory layers where **team memory lives inside the repo so git is the sync transport**, with a `dream` consolidation pass and agent-proposed skills subject to review |
| **Kilo Code** | `recall` over the machine's own past session transcripts — memory as retrieval over raw history rather than a curated document |
| **Continue** | `createRuleBlock` lets the agent author its own rule files; `requestRule` lets it pull in rules by description — agent-requested context instead of always-on prompt bloat |

## Context-engineering bets

| System | The bet |
|---|---|
| **Manus** | KV-cache hit rate as the primary production metric; logit-masked tools over a stable prefix; failures deliberately retained |
| **browser-use** | Ephemeral-index DOM serialization with new-since-last-step markers — the canonical cheap alternative to pixel grounding |
| **SWE-agent** | The ACI thesis: paged file windows, a lint-gated `edit` that refuses broken patches, interactive-program blocklist |
| **mini-swe-agent** | The rebuttal: ~100 lines, bash only, no history processing — and it competes |
| **Agentless** | No agency at all; fixed localize→repair→validate beat contemporary agent loops |
| **AutoCodeRover** | The retrieval unit is a *program entity* (AST), not a file or byte range |
| **Aider** | Edit-format-as-protocol — one coder subclass per textual edit dialect, plus a tree-sitter + personalized-PageRank repo map, no embeddings |
| **Augment** | Unbundled the retrieval engine and sold it as an MCP context provider to rival harnesses |
| **Agent S3** | Deleted its own planner hierarchy *and* memory store, then bought accuracy back with N rollouts and a comparative judge |
| **Flowith** | Context assembly as direct manipulation — the canvas graph *is* the context-selection mechanism |

## Control, permission and isolation bets

| System | The bet |
|---|---|
| **Codex CLI** | Approval × sandbox as orthogonal axes, a declarative `execpolicy` language, and a `guardian` reviewer with its own budgets |
| **Gemini CLI** | Deterministic policy engine *plus* a Flash model judging calls against a natural-language security policy |
| **Goose** | LLM permission judge that treats tool names and arguments as untrusted data; loop itself refactored into testable `Operation`s |
| **OpenHands** | The acting LLM predicts a `SecurityRisk` label for its own action; ensemble of pluggable security analyzers gates on it |
| **Claude Code** | Four stacked layers: declarative rules, LLM classifier, shell hooks, OS sandbox |
| **Conductor** | Git worktree as the isolation primitive — the cheapest credible swarm architecture |
| **Kortix** | Git branch per session; sandbox work is discarded unless committed and reviewed |
| **Copilot coding agent** | The agent's sandbox, permissions, secrets and audit trail *are* GitHub Actions — reuse the governance the repo already has |
| **Home Assistant** | Fail-closed by config: tools contributed by integrations at request time, scoped by an entity-exposure allowlist |
| **Zep** | Tells callers to return retrieved memory as a `tool_result`, never in a system message — retrieved memory as untrusted data |
| **Skyvern** | Page text interpolated through an `\| untrusted` Jinja filter that fences content from instructions |
| **Qwen Code** | **Completion is adjudicated, not asserted** — an independent Goal Verifier with a bounded evidence window must accept the claim that a goal is done |
| **Continue** | Command approval as static analysis — parse the shell pipeline into stages, resolve the most restrictive policy across them |
| **Cursor** | Three-stage auto-review: allowlist → sandbox → LLM classifier returning allow / retry-differently / ask-human |
| **Cline** | Plan mode keeps `run_commands` available but hard-blocks mutating commands via a command-guard hook rather than by prompting alone |

## Structural and orchestration bets

| System | The bet |
|---|---|
| **Agent Zero** | Nothing is hardcoded — every prompt, tool, hook and profile is an editable file; each agent level spawns the next |
| **opencode** | The agent is a *server*; every surface is a typed client of one session engine |
| **LangGraph** | `interrupt()` as a durable exception against the checkpointer — time-travel and branch-from-any-step fall out for free |
| **Mastra** | Subagent delegation with **memory isolation** — reads full history for decision quality, writes only its own delegation |
| **Claude Agent SDK** | Subagent as a context-isolation primitive: empty window in, final message out, scanned for injection |
| **OpenAI Agents SDK** | Handoff-as-tool — delegation *replaces* the running agent rather than nesting it, rewriting history via `input_filter` |
| **AG2** | Multi-agent coordination as a **governance** problem: identity passports, typed channels, rate limits, an auditable write-ahead log |
| **smolagents** | Code-as-action — one Python blob composes several calls with control flow per model call |
| **Pydantic AI** | The loop stays small; durability delegated to real workflow engines, with "durability is not storage" stated outright |
| **Devin** | Devin manages Devins — a coordinator spawns managed agents, each in its own VM, then resolves conflicts |
| **Droid** | The same binary is the interactive agent, the CI job and the scheduled worker — autonomy is a flag, not a product |
| **UI-TARS** | Model-as-harness: a few text-serialized primitives with model-predicted pixel boxes; capability lives in the weights |
| **Open Interpreter** | Harness emulation as a first-class switch — the bet that harness shape, not the model, carries the performance |
| **Stagehand** | Three primitives instead of a loop, with cacheable observations that decay into deterministic selectors |
| **AnythingLLM** | The *workspace*, not the agent, owns knowledge, model and tools |
| **Open WebUI** | Four plugin shapes (Filters/Actions/Pipes/Tools) as live-loaded Python with auto-rendered settings forms; agency added around that substrate |
| **Genspark** | Ensemble-of-models-as-verification substituted for an explicit agent loop — redundancy instead of planning |
| **Antigravity** | Agents emit **reviewable artifacts** (task list, plan-before-code, walkthrough with browser-captured verification evidence); review becomes commenting on documents, not reading a tool log |
| **Zed / ACP** | The editor should be a *client* to interchangeable agents rather than ship the one true agent |
| **Cline** | Loop extracted into a layered SDK behind a **hub daemon** — many clients attach and detach from the same live session |
| **Amp** | **Orbs**: a cloud machine per thread that keeps working after the laptop closes — plus no model picker at all, on the theory the harness should track the frontier |
| **Crush** | LSP as first-class agent tooling — the prompt instructs the model to prefer `lsp_replace_symbol` over text edits and `lsp_definition` over grep, so whitespace-match failures cannot occur |
| **Kilo Code** | Adopted a rival's engine (opencode) wholesale and kept only its differentiating layers on top |
| **Cursor** | Best-of-N agent *racing* in parallel git worktrees with the user picking the winner; plus "Projects" — a persistent coordinator agent that plans and delegates for weeks and writes no code itself |
| **Windsurf** *(historical)* | SWE-grep: a retrieval-specific model issuing ~8 parallel tool calls per turn — a bet on retrieval *latency* over frontier quality |

---

# 3. Terminal / CLI coding agents

### Claude Code — Anthropic, proprietary, active
- **Category:** terminal coding agent (one engine behind terminal, IDE, desktop, web/mobile surfaces)
- **Focus / intended use:** general agentic software work in a real repo — build features, fix bugs, run commands, commit and open PRs — plus headless automation in CI.
- **Inputs:** natural-language prompts in a repo; `CLAUDE.md` / `.claude/rules/*.md` instruction files; @-mentioned files and images; piped stdin for `claude -p`; MCP servers, skills, hooks, plugins.
- **Effects:** arbitrary file edits/writes, shell and PowerShell execution, git commits and PRs, web fetch/search, MCP calls into external systems, scheduled/background runs, desktop and phone notifications, published claude.ai artifacts.
- **Model-facing arch:** Broad flat built-in tool set — `Bash`/`PowerShell`, `Read`/`Write`/`Edit`, `Glob`/`Grep`, `LSP`, `WebFetch`/`WebSearch`, `Agent`+`SendMessage`+`SubagentHandback`, `Task*` tracker, `Skill`, `EnterPlanMode`/`ExitPlanMode`, `EnterWorktree`, `Monitor`, `ToolSearch`, `Workflow`, plus `mcp__server__tool` entries (docs). Each turn is one message list assembled as system prompt → auto-memory `MEMORY.md` (first 200 lines / 25 KB) → environment + git block → deferred MCP tool names → skill index → CLAUDE.md files (injected as a user message, root-down concatenation) → conversation; state carries as the growing transcript, and `/compact` replaces it with a structured summary while re-injecting system prompt, CLAUDE.md, memory, MCP tools, the ~5 most recently modified files and invoked skill bodies capped at 5k tokens each (docs).
- **Memory / learning arch:** Two layers, both plain markdown (docs). User-authored `CLAUDE.md` (managed-policy → `~/.claude/CLAUDE.md` → project → `CLAUDE.local.md`, `@path` imports to depth 4, path-scoped `.claude/rules/*.md` loaded only when matching files are read); and **auto memory** — Claude writes its own `user`/`feedback`/`project`/`reference` notes to `~/.claude/projects/<project>/memory/`, with a `MEMORY.md` index loaded every session and topic files read on demand. Genuine cross-session learned memory, not just a transcript.
- **User-facing arch:** Interactive TUI with slash commands (`/memory`, `/context`, `/permissions`, `/compact`, `/init`, `/sandbox`), plus VS Code/JetBrains, desktop app, web and mobile on the same engine; sessions teleport between surfaces. Permissions are mode-driven — `default`(manual) / `acceptEdits` / `plan` / `auto` (an LLM classifier reviews each action) / `dontAsk` / `bypassPermissions` — over allow/ask/deny rules with per-tool specifiers (`Bash(npm run test:*)`, `WebFetch(domain:…)`, `Read`/`Edit` path globs) saved to `.claude/settings.local.json` at repo root; `PreToolUse` hooks add deterministic enforcement (docs).
- **Unique element:** Model-written persistent memory stacked with a four-layer permission system — declarative rules, an LLM classifier, shell hooks, and an OS sandbox (macOS Seatbelt; bubblewrap + socat network proxy + optional seccomp on Linux/WSL2) — under a single engine shared by terminal, IDE, desktop and cloud.
- **Evidence:** https://code.claude.com/docs/en/overview, https://code.claude.com/docs/en/memory, https://code.claude.com/docs/en/permissions, https://code.claude.com/docs/en/tools-reference, https://code.claude.com/docs/en/context-window, https://code.claude.com/docs/en/sandboxing

### OpenAI Codex CLI — OpenAI, Apache-2.0, active
- **Category:** terminal coding agent (Rust workspace, ~130 crates; thin npm wrapper)
- **Focus / intended use:** agentic coding on a local repo with OS-level sandboxing as the default posture, plus the same engine driving the IDE extension and Codex Web.
- **Inputs:** prompts in the TUI or `codex exec`; `AGENTS.md` (and `AGENTS.override.md`) discovered root-down; `~/.codex/config.toml`; skills; MCP servers; images.
- **Effects:** file edits via `apply_patch`, sandboxed shell execution, network access under an allowlist/proxy, git operations, MCP calls, spawned sub-agents, and background writes to a git-versioned memory workspace.
- **Model-facing arch:** Verified in code — `RegularTask` loops `run_turn` while queued input remains (`codex-rs/core/src/tasks/regular.rs:76`); `run_turn` (`codex-rs/core/src/session/turn.rs:163`) runs pre-sampling compaction, then a `loop` (`turn.rs:423`) over streamed model responses dispatching tool calls until a final message with no pending calls, gated by stop hooks. There is no step counter — bounding is by token budget and cancellation tokens. Shell access is unified: one PTY-backed `exec_command` plus `write_stdin` for resumable sessions (`tools/handlers/shell_spec.rs`; `ConfigShellToolType` only has `Disabled`/`UnifiedExec`), alongside `apply_patch`, `update_plan`, `view_image`, `web_search`, `request_user_input`, `request_permissions`, `tool_search` (model-driven discovery of further tool specs), `get_context_remaining`, `new_context_window`, `sleep`, MCP tools/resources, and a multi-agent set (`spawn_agent`, `wait_agent`, `send_message`, `followup_task`, `list_agents`, `interrupt_agent`, `resume_agent`, `close_agent`). Prompts are data, not code: each model entry in `codex-rs/models-manager/models.json` carries an inline `instructions_template`, with `models-manager/prompt.md` as the `BASE_INSTRUCTIONS` fallback — the per-model `core/*_prompt.md` files are no longer referenced by any `include_str!` in this snapshot.
- **Memory / learning arch:** Verified — a genuine offline learning pipeline, unique among these. `codex-rs/memories/` runs asynchronously at session start: **Phase 1** claims recent session rollouts from a state DB, has a model extract a structured `raw_memory` + `rollout_summary` per rollout (secrets redacted), and stores them back; **Phase 2** takes a global lock, syncs `raw_memories.md` and `rollout_summaries/` into `~/.codex/memories/` (a git-baselined directory), writes a `phase2_workspace_diff.md`, and spawns a **consolidation sub-agent** — no approvals, no network, local write only — to update `MEMORY.md`, `memory_summary.md` and `skills/`. The read path injects memories as developer instructions and parses citations (`codex-rs/memories/read/`). Sessions also persist as resumable rollouts (`codex-rs/rollout/`).
- **User-facing arch:** Rust TUI, non-interactive `codex exec`, an app-server/daemon protocol that backs the IDE extension and remote control, `codex mcp` to expose Codex itself as an MCP server, and Codex Web/cloud (docs). Approval is `AskForApproval { UnlessTrusted, OnRequest (default), Granular{…}, Never }` crossed with `SandboxPolicy { ReadOnly, WorkspaceWrite, ExternalSandbox, DangerFullAccess }` (`codex-rs/protocol/src/protocol.rs:986,1072`), enforced by real OS sandboxes — macOS Seatbelt, Linux Landlock + seccomp + bubblewrap with a network proxy (`codex-rs/linux-sandbox/src/{landlock,bwrap,proxy_routing}.rs`), and a dedicated Windows sandbox service (`codex-rs/windows-sandbox-rs`, `windows-sandbox-service`).
- **Unique element:** The two-phase background memory pipeline that mines past sessions into a git-versioned, agent-maintained memory workspace — learning that happens *outside* any live turn — on top of orthogonal approval × sandbox axes, a declarative `execpolicy` rule language, and a separate "guardian" reviewer subsystem (`codex-rs/core/src/guardian/`, `codex-rs/guardian-context`) with its own request/token budgets that mediates consequential-action decisions independently of the static policy engine.
- **Evidence:** codex-rs/core/src/tasks/regular.rs, codex-rs/core/src/session/turn.rs, codex-rs/core/src/tools/handlers/shell_spec.rs, codex-rs/models-manager/models.json, codex-rs/memories/README.md, codex-rs/protocol/src/protocol.rs, codex-rs/linux-sandbox/src/landlock.rs, codex-rs/core/src/agents_md.rs

### Gemini CLI — Google, Apache-2.0, active
- **Category:** terminal coding agent (TypeScript monorepo)
- **Focus / intended use:** general agentic coding and shell work in a repo, with Gemini models and a large free tier.
- **Inputs:** prompts in a repo; hierarchical `GEMINI.md` context files; `-p` headless prompts; extensions (custom commands, MCP servers, hooks, policies, skills, themes); MCP servers and project-declared discovery commands.
- **Effects:** file reads/writes/edits, shell execution (incl. background), web fetch and Google Search, browser automation via a delegated browser agent, MCP calls, memory writes to `GEMINI.md`.
- **Model-facing arch:** Verified in code — `GeminiClient.sendMessageStream` (`packages/core/src/core/client.ts:93`, `MAX_TURNS = 100` at :79) drives `Turn.run` (`packages/core/src/core/turn.ts:254`), which emits a typed event enum; when no tool calls remain, an LLM-judged `checkNextSpeaker()` decides whether to inject "Please continue." and recurse. Tools live in `packages/core/src/tools/` — `read_file`, `write_file`, `edit`, `read_many_files`, `ls`, `glob`, `grep` (ripgrep), `shell`, `web_fetch`, `web_search`, `save_memory`, `write_todos`, `enter/exit_plan_mode`, `ask_user`, `activate_skill`, `update_topic`, a six-verb `tracker_*` task-dependency graph, and `invoke_agent`; the system prompt is assembled per turn from conditional TypeScript builder functions in `packages/core/src/prompts/snippets.ts` via `promptProvider.ts` (sandbox state, git state, approval mode, skills, hooks, memory all splice in).
- **Memory / learning arch:** Verified — hierarchical `GEMINI.md` discovery in `packages/core/src/utils/memoryDiscovery.ts` (global `~/.gemini/`, upward project walk, extension-contributed, JIT subdirectory memory, `@import` expansion) plus a per-project `MEMORY.md` index; the model writes to it itself via the `save_memory` tool (`packages/core/src/tools/memoryTool.ts`). Sessions persist through a chat-recording service used for resume and compression restarts.
- **User-facing arch:** Ink-based TUI (on a forked `@jrichman/ink`), headless `-p` mode (`packages/cli/src/nonInteractiveCli.ts`), ACP server for IDEs (`packages/cli/src/acp/`) with a VS Code companion, an experimental A2A remote server, and an embeddable SDK. Approval is `ApprovalMode { DEFAULT, AUTO_EDIT, YOLO, PLAN }` (`packages/core/src/policy/types.ts:48`) evaluated by a rule-based `policy-engine.ts` that parses shell commands, with "always allow" grants forcibly narrowed for risky tools (`TOOLS_REQUIRING_NARROWING` in `tool-names.ts`).
- **Unique element:** Two-layer safety — the deterministic policy engine plus "conseca" (`packages/core/src/safety/conseca/policy-enforcer.ts`), a small Flash model judging each tool call against a natural-language security policy — sitting on the broadest native sandbox coverage of any agent here (Docker/Podman, six macOS seatbelt profiles, a Linux manager, and a compiled C# Windows sandbox at `packages/core/src/sandbox/windows/GeminiSandbox.cs`).
- **Evidence:** packages/core/src/core/client.ts, packages/core/src/core/turn.ts, packages/core/src/prompts/snippets.ts, packages/core/src/tools/tool-names.ts, packages/core/src/utils/memoryDiscovery.ts, packages/core/src/policy/policy-engine.ts, packages/core/src/safety/conseca/policy-enforcer.ts, https://github.com/google-gemini/gemini-cli

### Aider — Aider-AI (Paul Gauthier), Apache-2.0, active (slower cadence)
- **Category:** terminal pair-programming agent
- **Focus / intended use:** conversational, git-backed editing of a set of files the user explicitly adds to the chat — not autonomous repo-wide exploration.
- **Inputs:** chat messages plus files added via `/add`, read-only files via `/read` (e.g. `CONVENTIONS.md`), `.aider.conf.yml`, voice input, and `AI!`/`AI?` comments written directly into source files in watch mode.
- **Effects:** file edits applied to disk, automatic git commits per edit (aider-attributed), linter and test-command runs, confirmed shell commands.
- **Model-facing arch:** Verified — no tool-calling. `Coder.run_one` (`aider/coders/base_coder.py:924`) loops `send_message` while `self.reflected_message` is set, capped at `max_reflections = 3`; edits are parsed out of the raw completion text by regex (`aider/coders/editblock_coder.py`, `HEAD = r"^<{5,9} SEARCH>?\s*$"`) and applied through a fuzzy cascade in `aider/coders/search_replace.py` (exact → whitespace-tolerant → `...`-elision → edit-distance). Context per turn is the system prompt from `aider/coders/*_prompts.py`, summarized `done_messages`, live `cur_messages`, the in-chat file contents, and the repo map.
- **Memory / learning arch:** None beyond the session — plainly stated. `.aider.chat.history.md` and `.aider.input.history` are append-only logs never read back; `CONVENTIONS.md` is a static user-authored file loaded as ordinary context. Older history is LLM-summarized within a session by `aider/history.py::ChatSummary`. Git commits are the only durable state.
- **User-facing arch:** CLI chat REPL with slash commands (`aider/commands.py`: `/add`, `/read`, `/undo`, `/clear`, `/diff`, `/architect`, `/ask`, `/run`, `/lint`, `/test`), a Streamlit browser GUI (`aider/gui.py`), voice (`aider/voice.py`), and `--watch-files` (`aider/watch.py`). Approval is coarse: every edit auto-commits by default, `/undo` reverts only aider's own same-session HEAD commit, and `confirm_ask` gates shell/lint/test actions unless `--yes-always`. There is no per-tool ACL because there is no tool layer.
- **Unique element:** Edit-format-as-protocol — one `Coder` subclass per textual edit dialect (editblock, udiff, whole, patch, architect/editor two-model split), auto-selected per model, with a self-healing fuzzy parser and reflection retries instead of provider tool-calling; paired with a tree-sitter + personalized-PageRank repo map (`aider/repomap.py`, `nx.pagerank` at :525) binary-searched to fit a token budget, with no embeddings or vector DB.
- **Evidence:** aider/coders/base_coder.py, aider/coders/editblock_coder.py, aider/coders/search_replace.py, aider/repomap.py, aider/history.py, aider/watch.py, https://github.com/Aider-AI/aider/releases

### OpenHands / Agent Canvas — All-Hands-AI, MIT, active
- **Category:** sandboxed agent platform with CLI, server and web control panel (formerly OpenDevin)
- **Focus / intended use:** autonomous software engineering in an isolated runtime — issue-to-PR work, long tasks, and orchestration of multiple agent backends.
- **Inputs:** task prompts via web canvas, CLI or the agent-server HTTP API; repo contents in a mounted workspace; `.openhands/memory/MEMORY.md`; skills (frontmatter markdown, optionally from a pinned git repo); MCP server configs; agent profiles.
- **Effects:** file edits, persistent shell sessions, browser automation, git/PR operations, MCP calls — all inside a Docker/Apptainer/remote/cloud workspace rather than on the host.
- **Model-facing arch:** Verified — the engine moved out of the old monorepo into `All-Hands-AI/agent-sdk` (v1.49.2); the former `OpenHands/OpenHands` repo is now a thin TS control panel (`@openhands/agent-canvas`) consuming it. One step is `Agent._step()` (`openhands-sdk/openhands/sdk/agent/agent.py`), driven by a `while True` in `LocalConversation.run()` (`.../conversation/impl/local_conversation.py`) that checks, in order, pause/stuck, finished (with deniable stop-hooks), stuck-pattern nudging, confirmation-wait, cost budget, then `max_iteration_per_run` (default 500). Default tools: `TerminalTool`, `FileEditorTool` (`view|create|str_replace|insert|undo_edit`), `TaskTrackerTool`, optional browser and sub-agent task toolsets, plus SDK builtins `finish`, `invoke_skill`, `switch_llm`, `think`, `vision_inspect`. The system prompt was decomposed from one Jinja template into typed `PromptSection` classes with `guard()` and `cache_tier` (`context/prompts/sections/static.py`, assembled by `context/prompts/registry.py`) explicitly for prompt-cache efficiency.
- **Memory / learning arch:** Verified — two-tier markdown memory in `openhands-sdk/openhands/sdk/context/memory.py`: user tier `~/.openhands/memory/MEMORY.md` and project tier `.openhands/memory/MEMORY.md`, loaded under a 6000-char budget (project last, truncated from the top since the agent appends). Separately, a skills subsystem (`sdk/skills/skill.py`) with keyword/path/task triggers, per-skill MCP configs, and an updatable pinned skills git repo. History is condensed by a rolling `LLMSummarizingCondenser` triggered by explicit request, token threshold or event count, emitted as its own turn.
- **User-facing arch:** Agent Canvas web control center, a FastAPI agent-server, and a CLI; confirmation is policy-driven — `AlwaysConfirm` / `NeverConfirm` / `ConfirmRisky(threshold=HIGH)` in `sdk/security/confirmation_policy.py`, fed by a per-action `SecurityRisk` label the LLM itself predicts (`add_security_risk_prediction`) and by pluggable `SecurityAnalyzerBase` implementations (LLM analyzer, Gray Swan, ToolShield, a shell-AST `defense_in_depth` static analyzer, combined via `ensemble.py`).
- **Unique element:** It stopped being a single vertically-integrated agent: `ACPAgent` (`sdk/agent/acp_agent.py`) makes a *foreign* agent — Claude Code, Codex, Gemini CLI, any ACP server — an interchangeable `AgentBase` subclass, so OpenHands' condenser, memory, skills and security stack wrap other vendors' agents, and the Canvas UI treats its own agent as one backend among several.
- **Evidence:** agent-sdk/openhands-sdk/openhands/sdk/agent/agent.py, agent-sdk/openhands-sdk/openhands/sdk/conversation/impl/local_conversation.py, agent-sdk/openhands-sdk/openhands/sdk/context/memory.py, agent-sdk/openhands-sdk/openhands/sdk/context/prompts/registry.py, agent-sdk/openhands-sdk/openhands/sdk/security/confirmation_policy.py, agent-sdk/openhands-sdk/openhands/sdk/agent/acp_agent.py, https://github.com/All-Hands-AI/agent-sdk

### Goose — Block, Apache-2.0, active
- **Category:** extensible terminal + desktop agent (Rust), general computer use rather than coding-only
- **Focus / intended use:** an on-machine agent whose capabilities are supplied by swappable extensions — coding, but also file/OS automation, scheduled and recipe-driven runs.
- **Inputs:** prompts via CLI or desktop app; `.goosehints` files (with `@file` import expansion); recipes (parameterized YAML/JSON task templates rendered with minijinja); MCP extension configs; skills.
- **Effects:** shell execution, file writes/edits, computer control (the `computercontroller` MCP extension), scheduled autonomous runs, subagent spawning, MCP calls.
- **Model-facing arch:** Verified — two loops coexist at v1.51.0: the legacy imperative `Agent::reply_internal` (`crates/goose/src/agents/agent.rs`, ~6k lines) and an opt-in composable state machine gated by `GOOSE_STATE_MACHINE` (`crates/goose/src/agents/state_machine/`) built from discrete `Operation`s (`ops_llm`, `ops_toolcalling`, `ops_tool_approval`, `ops_compaction`, `ops_maxturns`, `ops_steer`, …). Hot-path tools moved in-process as "platform extensions": `write`, `edit`, `shell`, `tree`, `read_image` (`platform_extensions/developer/mod.rs:279`), alongside `analyze`, `todo`, `scheduler`, `orchestrator`, `summarize`, and `manage_extensions`/`search_available_extensions` for enabling MCP extensions at runtime; `computercontroller`, `memory`, `autovisualiser`, `peekaboo`, `tutorial` remain standalone MCP servers in `crates/goose-mcp/`. System prompts are minijinja markdown (`crates/goose/src/prompts/system.md`, `tiny_model_system.md`, `subagent_system.md`).
- **Memory / learning arch:** No automatic learned memory. Sessions persist as transcripts (`crates/goose/src/session/`); cross-session knowledge only exists if the model explicitly calls the **memory MCP extension** (`crates/goose-mcp/src/memory/mod.rs`), which writes categorized markdown notes to a per-OS app-data dir tagged global or per-working-directory. Recipes are saved workflows, not memory.
- **User-facing arch:** CLI plus an Electron desktop app, an ACP server (`crates/goose/src/acp/`) so editors like Zed can drive it, an OIDC proxy for auth, and a scheduler for cron-like runs. Approval is a `GooseMode` — `Auto` (default), `Approve`, `SmartApprove`, `Chat` (`crates/goose-provider-types/src/goose_mode.rs`) — with grants persisted in a `ToolPermissionStore`.
- **Unique element:** "Smart approval" is an LLM-as-classifier: `crates/goose/src/prompts/permission_judge.md` + `permission/permission_judge.rs` ask a model to label each tool call read-only vs sensitive, with the prompt explicitly treating tool names and arguments as untrusted data. Alongside it, Goose is mid-migration to a unit-testable `Operation`-pipeline agent loop — treating the loop itself as a refactorable artifact — and injects a `<turn-budget>` marker into the prompt as the step budget depletes.
- **Evidence:** crates/goose/src/agents/agent.rs, crates/goose/src/agents/state_machine/ops_maxturns.rs, crates/goose/src/agents/platform_extensions/developer/mod.rs, crates/goose/src/prompts/permission_judge.md, crates/goose-provider-types/src/goose_mode.rs, crates/goose-mcp/src/memory/mod.rs, https://github.com/block/goose

### opencode — sst, MIT, active
- **Category:** client/server terminal coding agent (TypeScript/Bun, Effect.ts)
- **Focus / intended use:** provider-agnostic agentic coding with a rich TUI, multi-client attach to one session, and shareable session links.
- **Inputs:** prompts via TUI/desktop/web/CLI/SDK; `AGENTS.md` (also `CLAUDE.md`, legacy `CONTEXT.md`) discovered up the tree and from global config; per-agent config (step budget, tool allowlist, permissions); MCP servers; plugins.
- **Effects:** file reads/writes/edits/patches, shell execution, web fetch and search, LSP queries, background subagents, MCP calls, published share links.
- **Model-facing arch:** Verified — `runLoop` in `packages/opencode/src/session/prompt.ts` (~:1080) is an Effect generator `while(true)` that exits when the last assistant message has a terminal finish reason, no pending tool calls remain, and it directly answers the latest user turn; `agent.steps` bounds it and the final allowed step appends a synthetic `MAX_STEPS_PROMPT` message. Tools in `packages/opencode/src/tool/` each pair a `.ts` with a `.txt` description: `read`, `write`, `edit`, `apply_patch`, `glob`, `grep`, `shell`, `lsp`, `webfetch`, `websearch`, `task` (supports `background: true`), `todo`, `question`, `plan` (`plan_enter`/`plan_exit`), `skill`, `code-mode`. System prompts are model-family-specific files selected by substring-matching the model ID (`session/system.ts:27-46`: `anthropic.txt`, `gpt.txt`, `gemini.txt`, `beast.txt`, `codex.txt`, `kimi.txt`, …).
- **Memory / learning arch:** None beyond the session transcript. Persistence is session/message storage (`session/session.ts`, `message-v2.ts`, `storage/`), static `AGENTS.md`/`CLAUDE.md` instruction files, and shareable hosted transcripts (`share/`). Within a session, `session/compaction.ts` prunes against token constants (`PRUNE_MINIMUM 20_000`, `PRUNE_PROTECT 40_000`, `TOOL_OUTPUT_MAX_CHARS 2_000`) with `overflow.ts` deciding when to fire.
- **User-facing arch:** A from-scratch TypeScript terminal renderer (`packages/tui` on `@opentui`, replacing the earlier Go TUI), plus desktop, web and CLI clients — all thin HTTP/SSE clients of the same server (`packages/opencode/src/server/`), which is what enables multi-client attach and share links. Permissions are `{permission, pattern, action}` rulesets matched by wildcard where **last matching rule wins** and the fallback is `ask` (`packages/opencode/src/permission/index.ts`); plan mode is a separate two-tool mechanism whose exit explicitly asks the user whether to hand off to a build agent.
- **Unique element:** The agent is a server, not a CLI: every surface is a typed client of one session engine, with per-model-family system prompts, per-agent step/tool/permission config and LSP exposed to the model as a first-class tool rather than as editor tooling.
- **Evidence:** packages/opencode/src/session/prompt.ts, packages/opencode/src/session/system.ts, packages/opencode/src/session/compaction.ts, packages/opencode/src/permission/index.ts, packages/opencode/src/tool/lsp.ts, packages/opencode/src/session/instruction.ts, https://github.com/sst/opencode

# 4. IDE-embedded and editor-extension coding agents

### Cline — Cline Bot Inc., Apache-2.0, active (HEAD 8872d81, 2026-09-17)
- **Category:** IDE-embedded coding agent (VS Code + JetBrains + CLI + desktop), now also a reusable agent-runtime SDK
- **Focus / intended use:** Autonomous multi-step coding in the editor, with a Plan/Act split and a bring-your-own-model provider gateway
- **Inputs:** Chat prompt + images + file attachments in the webview; `@`-mentions; `.clinerules/` and `AGENTS.md` rule files; workspace path; MCP servers; skills (`SKILL.md`)
- **Effects:** File writes/patches, shell commands (incl. detachable long-running processes), web fetch, MCP tool calls, git-shadow checkpoints, subagent/team spawning
- **Model-facing arch:** VERIFIED IN CODE — Cline 2.0 dropped the old XML pseudo-tool protocol for native function-calling tools defined in `sdk/packages/core/src/extensions/tools/definitions.ts`: `read_files`, `search_codebase`, `run_commands`, `fetch_web_content`, `apply_patch`, `editor`, `skills`, `ask_question`, `submit_and_exit`, plus `spawn_agent`/team tools (`.../tools/team/`); enabled sets come from `presets.ts` (act / plan / search / minimal / yolo). System prompt is built by `buildClineSystemPrompt` (`sdk/packages/shared/src/prompt/cline.ts`) from one of two templates (`system/act.ts`, `system/yolo.ts`) with `{{PLATFORM_NAME}}/{{CWD}}/{{CURRENT_DATE}}/{{IDE_NAME}}/{{CLINE_RULES}}/{{CLINE_METADATA}}` slots; mode semantics ride in the rules slot and user messages are wrapped in `<user_input mode="...">` with `<mode_notice>` markers so a mid-session Plan→Act switch is visible in the transcript rather than an invisible prompt swap. Turn-to-turn state is a persisted session transcript compacted by a pluggable policy — `basic-compaction.ts` (summarize-and-cut) vs `agentic-compaction.ts` (token-budget projection), both in `sdk/packages/core/src/extensions/context/`, carrying a files-touched section forward into the summary.
- **Memory / learning arch:** No built-in cross-session memory store. The famous "Memory Bank" is **not code** — it is a documented prompt convention only (`docs/best-practices/memory-bank.mdx`: a `memory-bank/` folder of six markdown files — `projectbrief.md`, `productContext.md`, `activeContext.md`, `systemPatterns.md`, `techContext.md`, `progress.md` — installed as a `.clinerules/memory-bank.md` rule, driven by the phrases "initialize memory bank" / "update memory bank" / "follow your custom instructions"). Actual persistence is the rules/skills loader (`core/src/extensions/config/user-instruction-config-loader.ts`, which discovers `.clinerules`, `.cline/`, workspace + global `AGENTS.md`, and `SKILL.md` folders) plus session history under `~/.cline/data/tasks/`.
- **User-facing arch:** Webview chat with a Plan/Act toggle; per-tool approval with auto-approve allowlists; plan mode keeps `run_commands` available but hard-blocks mutating commands via a command-guard hook rather than by prompting alone (`extensions/tools/command-guard.ts`); desktop approvals go over a file-based IPC (`runtime/tools/tool-approval.ts`); checkpoints allow reverting edits.
- **Unique element:** The agent loop was extracted into a layered, separately-consumable runtime (`@cline/shared` → `@cline/llms` → `@cline/agents` (stateless loop) → `@cline/core` (sessions/persistence/hub)) with a **detached hub daemon**: multiple clients (VS Code, CLI, desktop, Kanban) attach and detach from the *same* live session over a WebSocket hub without stopping the run.
- **Evidence:** `cline/sdk/ARCHITECTURE.md`, `cline/sdk/packages/shared/src/prompt/cline.ts`, `cline/sdk/packages/shared/src/prompt/system/act.ts`, `cline/sdk/packages/core/src/extensions/tools/definitions.ts`, `cline/sdk/packages/core/src/extensions/context/agentic-compaction.ts`, `cline/docs/best-practices/memory-bank.mdx`, https://cline.bot/blog/introducing-cline-sdk-the-upgraded-agent-runtime

### Roo Code — Roo Code Inc., Apache-2.0, **STALLED / ARCHIVED**
- **Category:** IDE-embedded coding agent (VS Code), Cline fork
- **Focus / intended use:** Cline's architecture plus user-definable "custom modes" (per-mode prompt + tool allowlist + file-regex restriction)
- **Inputs:** Chat prompt, `.roo/rules/` and `.roomodes` config, MCP servers
- **Effects:** File edits, terminal commands, browser actions, MCP calls — now historical
- **Model-facing arch:** VERIFIED IN CODE (at the archived HEAD b867ec9, 2026-05-15, v3.53.0) — classic Cline-lineage XML-in-markdown tool protocol assembled in `src/core/prompts/system.ts` from `src/core/prompts/sections/` and `src/core/prompts/tools/`; loop and turn state in `src/core/task/`; sliding-window + LLM condensation in `src/core/condense/`.
- **Memory / learning arch:** None beyond the session transcript plus static rule files — say so plainly.
- **User-facing arch:** VS Code webview, per-tool auto-approval toggles (`src/core/auto-approval/`), mode switcher.
- **Unique element:** The custom-modes system — a mode is data (slug, role definition, allowed tool groups, `fileRegex` edit restriction), which is what Kilo inherited; historically significant, no longer maintained.
- **Evidence:** repo `RooCodeInc/Roo-Code` HEAD b867ec9 dated 2026-05-15 (last commit: "Remove roocode.com web app"), `roo/src/core/prompts/system.ts`, `roo/src/core/condense/`, https://kilo.ai/kilo-code/vs/roo-code, https://theaiagentindex.com/compare/roo-code-vs-kilo-code — shutdown announced 2026-04-21, repo archived 2026-05-15; extension still installable but unmaintained.

### Kilo Code — Kilo-Org, Apache-2.0, active (HEAD a728fbf, 2026-09-18)
- **Category:** IDE-embedded coding agent (VS Code + JetBrains + CLI + TUI), successor to Roo Code
- **Focus / intended use:** Multi-surface open-source agent with an open model marketplace (500+ models, zero markup) and mid-task model switching
- **Inputs:** Chat prompt, `AGENTS.md`, skills, MCP servers, per-mode config, workspace/worktree
- **Effects:** File edits/patches, shell, LSP operations, git worktrees, container/sandbox execution, MCP calls, repo cloning
- **Model-facing arch:** VERIFIED IN CODE — Kilo **abandoned the Roo/Cline fork lineage and rebuilt on the OpenCode server codebase** (`packages/opencode/`), keeping Kilo-specific packages alongside (`kilo-memory`, `kilo-indexing`, `kilo-sandbox`, `kilo-gateway`, `kilo-vscode`, `kilo-jetbrains`, `tui`). Tools live as `.ts` + a sibling `.txt` description file in `packages/opencode/src/tool/`: `read`, `write`, `edit`, `apply_patch`, `glob`, `grep`, `lsp`, `diagnostics`, `shell`, `task`, `todo`, `plan`, `question`, `skill`, `recall`, `webfetch`, `websearch`, `repo_clone`, `code-mode`. Distinctively, the system prompt is **selected per model family** — `packages/opencode/src/session/prompt/{anthropic,gpt,gemini,codex,kimi,ling,beast,trinity,meta,default}.txt` — rather than one prompt for all models.
- **Memory / learning arch:** Two separate mechanisms, both verified in code. (1) `recall` tool: full-text search and read over *past session transcripts* in the current project **and its git worktrees**, with ranking and typo tolerance on titles (`packages/opencode/src/tool/recall.txt`). (2) `packages/kilo-memory/`: a curated project-memory store — capture at turn-close or explicit trigger (`capture/`, with `redact.ts` and `reject.ts`), markdown storage with an audit log (`storage/audit.ts`), an indexer and recall path (`recall/`), and LLM prompts for `session-digest`, `typed-consolidation`, `tool-memory-save`, `tool-memory-recall`.
- **User-facing arch:** VS Code/JetBrains panel, CLI and TUI over the same server; per-tool permission service with subagent-scoped permissions (`packages/opencode/src/permission/`, `src/agent/subagent-permissions.ts`); plan mode via `plan-enter`/`plan-exit` tools.
- **Unique element:** Per-model-family system prompts plus `recall` over the machine's own prior session transcripts as a first-class tool — "memory" as retrieval over raw history rather than a curated document.
- **Evidence:** `kilocode/packages/opencode/src/tool/`, `kilocode/packages/opencode/src/session/prompt/`, `kilocode/packages/kilo-memory/src/`, `kilocode/packages/opencode/src/tool/recall.txt`, `kilocode/package.json` (dev script runs `packages/opencode`), https://kilo.ai/articles/roo-to-kilo-migration-guide

### Continue.dev — Continue Dev Inc., Apache-2.0, active but slowing (HEAD 5522c6f, 2026-07-20)
- **Category:** IDE-embedded coding agent + autocomplete (VS Code, JetBrains) with a CLI (`cn`)
- **Focus / intended use:** Configurable, model-agnostic assistant assembled from declarative YAML "assistants" shared via the Continue Hub
- **Inputs:** Chat prompt, `@`-context providers (@codebase, @file, @docs, @terminal, @diff…), `.continue/rules|agents|prompts`, `config.yaml`, MCP servers
- **Effects:** File create/edit, terminal commands, web fetch/search, MCP calls, and **writing new rule files** into the repo
- **Model-facing arch:** VERIFIED IN CODE — tools are declared one-per-file in `core/tools/definitions/`: `readFile`, `readFileRange`, `editFile`, `singleFindAndReplace`, `multiEdit`, `createNewFile`, `ls`, `globSearch`, `grepSearch`, `codebaseTool`, `viewRepoMap`, `viewSubdirectory`, `viewDiff`, `readCurrentlyOpenFile`, `runTerminalCommand`, `fetchUrlContent`, `searchWeb`, `readSkill`, `requestRule`, `createRuleBlock`. For models without native tool calling, `core/tools/systemMessageTools/` re-renders the whole tool set as prompted markdown code-blocks and intercepts the output (`interceptSystemToolCalls.ts`) — the same tool set works on either transport. Per-turn context is the chat history plus resolved context providers plus rules; the codebase index is hybrid (`core/indexing/`: `LanceDbIndex.ts` embeddings + `FullTextSearchCodebaseIndex.ts` + tree-sitter `CodeSnippetsIndex.ts`).
- **Memory / learning arch:** None automatic. Persistence is explicit and file-based: `.continue/rules/*.md` with frontmatter, and the agent can author its own via the `createRuleBlock` tool. The reciprocal `requestRule` tool lets the model pull in rules marked `alwaysApply: false` with no `globs` by name/description — agent-requested rules as a retrieval step rather than always-on prompt bloat.
- **User-facing arch:** Sidebar chat with Chat/Plan/Agent modes; three-valued per-tool policy (`allowedWithoutPermission` / `allowedWithPermission` / `disabled`) declared as `defaultToolPolicy` on each tool definition and overridable per assistant.
- **Unique element:** A dedicated `@continuedev/terminal-security` package that parses a shell command into its constituent pipeline stages and resolves the **most restrictive** policy across them (`packages/terminal-security/src/evaluateTerminalCommandSecurity.ts`) — command approval as static analysis, not string matching.
- **Evidence:** `continue/core/tools/definitions/`, `continue/core/tools/definitions/requestRule.ts`, `continue/core/tools/systemMessageTools/`, `continue/packages/terminal-security/src/evaluateTerminalCommandSecurity.ts`, `continue/core/indexing/LanceDbIndex.ts`, `continue/extensions/cli/src/compaction.ts`

### Cursor — Anysphere, proprietary, active (Cursor 3.6 May 2026; "Projects" Sep 10 2026)
- **Category:** AI-native IDE (VS Code fork) with a cloud agent platform
- **Focus / intended use:** Repo-scale agentic coding, increasingly agent-first rather than file-first (the Agents Window became a primary surface in Cursor 3, 2026-04-02)
- **Inputs:** Prompt plus auto-injected editor state — open files, cursor position, recent files, session edit history, linter diagnostics (reported); `@`-mentions; `.cursor/rules/*.mdc`; `AGENTS.md`; semantic codebase index
- **Effects:** File edits/creates/deletes, notebook cell edits, foreground/background terminal commands, browser automation, git branch pushes and PR creation from Cloud Agents, memory writes
- **Model-facing arch:** (reported, from extracted Agent Prompt 2.0) tools are `codebase_search` (semantic, `target_directories`), `grep` (ripgrep), `glob_file_search`, `read_file`, `list_dir`, `edit_file`, `delete_file`, `edit_notebook`, `run_terminal_cmd`, `read_lints`, `web_search`, `update_memory`, `todo_write`. (docs) Rules are injected at the *start* of model context in precedence order Team → Project → User. Turn-to-turn state is the thread plus repo-resident rules; Cloud Agents carry state as `git.branches[]` (`{repoUrl, branch?, prUrl?}`). Composer is Anysphere's in-house model, trained with codebase-wide semantic search in the loop (docs).
- **Memory / learning arch:** **Memories** — a background process watches agent chats for durable facts (repeated corrections, stated preferences, project conventions) and proposes a memory; persistence **requires explicit user approval** in a settings dashboard, and memories are project-scoped, not global (reported). This is deliberately separate from `.cursor/rules/*.mdc`, which are hand-authored with YAML frontmatter (`description`, `globs`, `alwaysApply`) in four modes: Always Apply, Apply Intelligently (agent-requested via description), Apply to Specific Files (glob-triggered), Apply Manually (@-mention only). Plain `AGENTS.md` is also honored, root + subdirectory with nested override (docs).
- **User-facing arch:** Editor chat + Agents Window; approval is via **Run Modes** (docs): *Auto-review* (default) is a three-stage pipeline — allowlisted commands run instantly, other shell commands are sandboxed where possible, and the remainder go to an LLM classifier subagent returning allow / retry-differently / ask-human; *Allowlist* mode drops the classifier; *Run Everything* is the renamed YOLO. Cloud Agents get a sandboxed VM configured by `.cursor/environment.json` (Dockerfile + setup.sh, or a saved snapshot), and since 2026-09-02 can run on self-hosted machines.
- **Unique element:** Literal best-of-N agent racing — the same task run by multiple agents in parallel git worktrees or remote machines, with the user picking the winner (docs) — plus the Sep 2026 "Projects": a persistent coordinator agent on its own cloud machine that plans and delegates to subagents over weeks and writes no code itself.
- **Evidence:** https://cursor.com/blog/2-0, https://cursor.com/docs/rules, https://cursor.com/docs/cloud-agent, https://cursor.com/docs/agent/security/run-modes, https://cursor.com/changelog, https://github.com/x1xhlol/system-prompts-and-models-of-ai-tools/blob/main/Cursor%20Prompts/Agent%20Prompt%202.0.txt

### Windsurf Cascade — Codeium → Cognition, proprietary, **DISCONTINUED (Cascade EOL 2026-07-01)**
- **Category:** AI-native IDE (VS Code fork) with an in-IDE agent; the agent no longer exists under this name
- **Focus / intended use:** "Flows" — a single surface combining inline copilot and autonomous multi-step agent over a locally indexed codebase
- **Inputs:** Prompt, local codebase index, `.windsurfrules` and workflow files, auto-generated memories, terminal state (reported)
- **Effects:** File edits, terminal commands, browser preview — historical
- **Model-facing arch:** (reported) Cascade ran on Cognition's in-house SWE-1 → SWE-1.5 models, trained on SWE workflows (debugging sessions, refactor sequences, multi-file coordination) rather than being a general model fine-tuned on code; retrieval used SWE-grep, a search-specific model issuing ~8 parallel tool calls per turn over ~4 turns for large-monorepo speed. Exact tool schemas: unclear — no credible extracted Windsurf system prompt was confirmed.
- **Memory / learning arch:** (reported) auto-generated memories distinct from static `.windsurfrules`; scope and approval model unclear.
- **User-facing arch:** (reported) IDE chat panel; "Turbo mode" auto-ran terminal commands without per-command approval, analogous to Cursor's Run Everything.
- **Unique element:** SWE-grep's parallel multi-call retrieval and an in-house model optimized narrowly for SWE-task *latency* rather than general capability — a bet on speed over frontier quality.
- **Evidence (status change is the headline):** Cognition acquired Windsurf ~Dec 2025 after Google DeepMind hired away its CEO and core research staff in 2025; on 2026-06-02 Cognition shipped Windsurf as **Devin Desktop** via OTA update, and **Cascade was deprecated with a hard cutoff on 2026-07-01**, replaced by **Devin Local** (a Rust rewrite, reported ~30% more token-efficient, capable of running subagents) with ACP support added so third-party agents can run in the IDE shell. `windsurf.com/blog` now 308-redirects to `devin.ai/blog` (observed directly). URLs: https://blog.vibecoder.me/windsurf-becomes-devin-desktop-what-changes, https://www.digitalapplied.com/blog/windsurf-becomes-devin-desktop-ide-migration-2026, https://www.ainewsbank.com/posts/windsurf-cascade-agent-end-of-life-july-1-devin-desktop, https://en.wikipedia.org/wiki/Cognition_AI

### Zed agent panel + ACP — Zed Industries, GPL-3.0 (editor) / Apache-2.0 (ACP), active (ACP HEAD a94122b, 2026-09-18)
- **Category:** IDE-embedded coding agent plus an open protocol for hosting *other* vendors' agents
- **Focus / intended use:** A native, fast agent surface in Zed, and a vendor-neutral standard so any agent can render properly in any editor
- **Inputs:** Prompt plus `@`-mentions of files, directories, symbols, previous threads, skills, diagnostics, branch diffs and fetched URLs (docs); MCP servers; for external agents, the agent's own config
- **Effects:** File edits reviewed as diff hunks, terminal commands, MCP calls; for ACP agents, whatever that agent does, mediated through the client
- **Model-facing arch:** The first-party Zed Agent has built-in search/edit/terminal tools gated by **Agent Profiles**, which decide which built-in and MCP tools exist in a given thread (docs). For external agents Zed owns nothing model-facing: the agent subprocess owns its runtime, auth, model selection, tools and prompts, and Zed is purely the renderer. VERIFIED IN SPEC — ACP is JSON-RPC over the agent subprocess's stdin/stdout, deliberately reusing MCP types; v1 methods include `session/new`, `session/prompt`, `session/update`, `session/cancel`, `session/resume`, `session/list`, `session/close`, `session/delete`, `session/set_config_option`, `session/request_permission`, `auth/login`, `auth/logout`, `elicitation/create`, `elicitation/complete`. Several concurrent sessions ride one connection.
- **Memory / learning arch:** None beyond the session transcript and thread history; threads persist in a Threads Sidebar sorted chronologically, and "New From Summary" seeds a fresh thread from a summary of an old one — manual carry-forward, not automatic memory (docs).
- **User-facing arch:** Agent Panel with multiple parallel threads across projects (Parallel Agents shipped 2026-04-22); per-tool permissions set to allow / deny / confirm; a "Restore Checkpoint" button after every model edit; edits land in a multi-buffer tab where each hunk is accepted or rejected individually (docs).
- **Unique element:** ACP itself — the bet that the editor should be a *client* to interchangeable agents rather than shipping the one true agent. It has since become a governed standard with an RFD process (15+ RFDs on v1), 1.0 SDKs, a stabilized **agent registry** with a shared manifest format for discovery/install (2026-03-09), and a **v2 draft** (2026-07-20) whose central change is "moving beyond the turn": `session/update` may arrive at any time, a prompt response only acknowledges receipt rather than ending the turn, and the agent signals idleness — explicitly to support background work, queueing and steering. v2 also replaces `oldText`/`newText` diffs with structured file changes plus an optional `git_patch`, and decouples permission-request `title`/`description` from the tool call. Dozens of third-party clients now implement it (JetBrains, neovim, Obsidian, Sublime, Qt Creator, Pulsar, Unity, Chrome).
- **Evidence:** https://zed.dev/docs/ai/agent-panel, https://zed.dev/docs/ai/external-agents, `acp/docs/get-started/architecture.mdx`, `acp/docs/announcements/acp-v2-draft.mdx`, `acp/docs/announcements/acp-agent-registry-stabilized.mdx`, `acp/schema/v2/schema.json`, `acp/docs/get-started/clients.mdx`

### Amp — Amp Inc. (spun out of Sourcegraph ~Dec 2025), proprietary, active (changelog entries through 2026-09-17)
- **Category:** IDE-embedded coding agent + CLI + web + native macOS/iOS, with a cloud execution layer
- **Focus / intended use:** Deliberately opinionated: **no model picker** — Amp routes to whatever it considers frontier-class, on the theory that the harness should track the model frontier rather than expose it as a knob
- **Inputs:** Prompt in CLI / VS Code / web / mobile (voice via "Puck"); `AGENTS.md`, falling back to `CLAUDE.md` when absent (reported); MCP servers; custom executables as tools via toolboxes
- **Effects:** File edits, shell commands, MCP tool calls, and cloud-side execution that continues after the client disconnects
- **Model-facing arch:** Main agentic loop plus an **Oracle** — a read-only reasoning subagent for review, debugging and "what should I do next" analysis. Notably, Amp deliberately does *not* push Oracle in the system prompt, to avoid inflating cost and latency; it is invoked explicitly (docs/reported). Exact tool schemas: unclear — the manual body did not render reliably, and no credible extracted Amp system prompt was confirmed. Specific model IDs in circulation are perishable; treat only the mechanism as solid.
- **Memory / learning arch:** No documented automatic memory. Persistence is the **thread** — Amp's durable, shareable unit of work, visible across CLI/IDE/web/mobile, with "multiplayer threads" letting teammates collaborate on a shared orb. Team-visible thread history is the learning surface (docs).
- **User-facing arch:** Chat in any of four clients against one thread; approval/autonomy model not confirmed in retrievable docs — unclear. Pricing pivoted 2026-09-13 to free when bringing your own compute/model subscription (docs).
- **Unique element:** **Orbs** — a cloud machine provisioned per thread, preloaded with the repo, tools and plugins, that keeps working after the laptop closes; later extended to accept MCP server connections (2026-08-19) and live-testing "Portals" (2026-08-06). Combined with the no-model-picker stance, Amp's bet is that the *session* is a cloud resource, not a local process.
- **Evidence:** https://ampcode.com/manual, https://ampcode.com/news, https://ampcode.com/news/oracle, https://ampcode.com/news/amp-frontier-corporation, https://sourcegraph.com/blog/why-sourcegraph-and-amp-are-becoming-independent-companies, https://www.npmjs.com/package/@sourcegraph/amp

### Crush — Charm, MIT (FSL-ish per repo LICENSE.md), active (HEAD ef7bae1, 2026-09-17)
- **Category:** Terminal-native coding agent (Go TUI), editor-adjacent rather than editor-embedded
- **Focus / intended use:** A glamorous, model-agnostic coding agent in the terminal with real language-server intelligence
- **Inputs:** Prompt in the TUI, `CRUSH.md`/`AGENTS.md`-style context files (project and global), `crush.json` config, LSP server config, skills, MCP servers
- **Effects:** File writes/edits, shell commands including tracked background jobs, LSP-driven semantic refactors, web fetch/search, Sourcegraph code search, MCP calls
- **Model-facing arch:** VERIFIED IN CODE — each tool is a Go file plus a sibling `.md`/`.md.tpl` description in `internal/agent/tools/`: `view`, `ls`, `glob`, `grep`, `edit`, `multiedit`, `write`, `bash`, `job_output`, `job_kill`, `fetch`, `web_fetch`, `web_search`, `download`, `sourcegraph`, `todos`, `question`, `diagnostics`, `references`, MCP resource tools, and the LSP family `lsp_symbols`, `lsp_definition`, `lsp_rename`, `lsp_replace_symbol`, `lsp_call_hierarchy`, `lsp_restart`. Four Go-templated system prompts are embedded at build time (`internal/agent/templates/{coder,plan,task,initialize}.md.tpl`) and rendered per turn with working dir, platform, date, git status/branch/recent commits, configured LSP list, available-skills XML, and the contents of project + global context files.
- **Memory / learning arch:** None automatic. State persists in a SQLite database (sqlc-generated, migrations in `internal/db/migrations/`) holding sessions, messages, per-session `todos`, and a `read_files` table; auto-summarization fires when remaining context drops below a threshold, writing a summary message that becomes the new conversation root (`internal/agent/agent.go`, `templates/summary.md`).
- **User-facing arch:** Bubble Tea TUI; a permission service with per-request grant, persistent grant, session-wide auto-approve, a CLI allowlist, and a global skip flag (`internal/permission/permission.go`); plan mode via a separate prompt; an `agent` tool spawns parallel task subagents with their own prompt and restricted toolset.
- **Unique element:** LSP as first-class agent tooling — the coder prompt explicitly instructs the model to *prefer* `lsp_replace_symbol` over text-matching edits and `lsp_definition` over `grep`, because symbol boundaries come from the language server and so whitespace-matching edit failures cannot occur.
- **Evidence:** `crush/internal/agent/tools/`, `crush/internal/agent/templates/coder.md.tpl` (lines ~140-157, the "Prefer LSP tools" section), `crush/internal/agent/prompt/prompt.go`, `crush/internal/permission/permission.go`, `crush/internal/agent/agent_tool.go`, `crush/internal/db/migrations/`

### Qwen Code — Alibaba / Qwen Team, Apache-2.0, active (HEAD 83d5971, 2026-09-18, v0.24.0)
- **Category:** Originally a Gemini-CLI fork; now a multi-surface agent platform (CLI, TUI, web shell, desktop shell, mobile shell, VS Code companion, Zed extension, ACP bridge)
- **Focus / intended use:** Long-horizon agentic work with explicit goals, verified completion, durable memory and team coordination — well beyond a coding CLI
- **Inputs:** Prompt across any surface, `QWEN.md`-style context files, skills, MCP servers, cron schedules, git worktrees, screen/browser/mobile devices
- **Effects:** File edits, shell, LSP operations, image generation, browser and computer-use automation (`packages/browser-use`, `packages/cua-driver`), mobile device control (`packages/mobile-mcp`), scheduled jobs (`cron-create`/`cron-list`/`cron-delete`), worktree enter/exit, sub-session spawning
- **Model-facing arch:** VERIFIED IN CODE — a large tool surface in `packages/core/src/tools/` including `edit`, `glob`, `grep`, `ls`, `lsp`, `exec`, `askUserQuestion`, `enterPlanMode`/`exitPlanMode`, `enter-worktree`/`exit-worktree`, `create-sub-session`, `list-agents`, `cron-*`, `display-image`, `image-gen`, `code-mode`. **Code mode** (`packages/core/src/code-mode/`) is a distinct execution path: instead of emitting one tool call at a time, the model writes JavaScript source that calls tools as functions inside a sandboxed host process with framed IPC and hard byte/time budgets (`CODE_MODE_MAX_SOURCE_CHARS`, `CODE_MODE_TIMEOUT_MS`, media limits). Turn state is further structured by a **goals runtime** (`packages/core/src/goals/`): goals persist, accumulate typed evidence records, and a terminal-status proposal is judged by an independent **Goal Verifier** — a separate LLM call with its own system prompt, a strict accept/reject JSON schema, a 256 KB evidence budget, and rules such as "evidence that is insufficient is a rejection, never an acceptance" and `proofKind: "delivered_output"` cannot prove external state changed.
- **Memory / learning arch:** The most elaborate in this cluster, all verified in code (`packages/core/src/memory/`). Three layers of markdown auto-memory: **user** (`memories/`, cross-project facts about the user), **project** (scoped to git-root or workspace), and **team** (`.qwen/team-memory/`, stored *inside the repo* so git is the sync transport, guarded by `team-memory-secret-guard.ts` and `secret-scanner.ts`). Each layer has a `MEMORY.md` index, a `pinned/` dir, `meta.json`, and an `extract-cursor.json` so extraction resumes incrementally. The lifecycle is `scan` → `extract` (LLM-planned) → `remember`/`forget` → `indexer` → **`dream`** (a consolidation/dedup pass run by a planner agent under a consolidation lock) → `recall` (relevance-selected injection). There is also `learn-skill-agent.ts` / `pending-skills.ts` — the agent proposes new skills from experience, subject to review.
- **User-facing arch:** Any of the shells; a permission manager with parsed shell semantics, dangerous/destructive command rules, an LLM classifier for ambiguous commands, denial tracking and an auto mode (`packages/core/src/permissions/`); a **team board** (`board.ts` → `agents/team/board-tasks.ts`, `asks.ts`) where agents claim tasks and raise "asks" that a human or another agent answers.
- **Unique element:** Completion is adjudicated, not asserted — an independent Goal Verifier with a bounded evidence window must accept the agent's claim that a goal is done, and treats all evidence as untrusted data rather than instructions.
- **Evidence:** `qwen-code/packages/core/src/goals/goal-verifier.ts`, `qwen-code/packages/core/src/memory/paths.ts`, `qwen-code/packages/core/src/memory/dream.ts`, `qwen-code/packages/core/src/code-mode/protocol.ts`, `qwen-code/packages/core/src/permissions/`, `qwen-code/packages/core/src/board.ts`, `qwen-code/packages/` (acp-bridge, browser-use, cua-driver, mobile-shell, zed-extension)

### Google Antigravity — Google / DeepMind, proprietary (free in preview), active (v2.13.0 per changelog tracker, Sep 2026)
- **Category:** Agent-first IDE (VS Code fork, reported Windsurf-derived) plus extensions, CLI and SDK
- **Focus / intended use:** Orchestrating several agents at once and reviewing their work as structured deliverables rather than as tool-call logs
- **Inputs:** Prompt in the editor or the agent manager, multi-folder/multi-repo workspaces, audio attachments, interactive image comments, MCP servers
- **Effects:** File edits, terminal commands, and **browser control** via an integrated Chrome (optionally a separate profile) so the agent can exercise and verify its own output
- **Model-facing arch:** Agents get editor, terminal and browser access (docs). No credible extracted system prompt or full tool schema was found — tool names and per-turn context assembly are **unclear**. Model roster spans Gemini 3.1 Pro and Gemini 3 Flash variants, Claude Sonnet 4.6 / Opus 4.6 (with Thinking variants), and GPT-OSS-120B for local/private deployment (docs + reported).
- **Memory / learning arch:** Weakly evidenced and probably overstated in secondary coverage. Each new conversation starts without memory of prior sessions (reported, third-party); no first-party cross-session memory subsystem could be confirmed. What exists is a "Documents" section added in v2.13.0 (purpose unclear) and community-built persistent-memory plugins that bolt on a project-instruction-file-plus-handover scheme. Treat "Antigravity learns across tasks" as **unclear/unconfirmed**.
- **User-facing arch:** Two surfaces — a conventional Editor view, and **Mission Control / Agent Manager**, which is the default landing view instead of a file tree: a board of all running agents with status, produced artifacts and pending approvals, orchestrating parallel and scheduled work across workspaces. Two review policies gate execution: **Request Review** (recommended default — the agent halts after producing a plan or diff, the user leaves inline comments, then approves) and **Always Proceed**. No agent-to-agent messaging primitive was found; coordination is user-mediated through the manager.
- **Unique element:** The **artifact** model — agents emit reviewable deliverables (Task List, Implementation Plan *before* code changes, and a post-hoc Walkthrough bundling diffs with verification evidence: test runs, browser screenshots, recordings) and the human reviews those artifacts with inline comments. Review is repositioned from reading a tool-call stream to commenting on documents, and browser self-verification is what generates the evidence.
- **Evidence:** https://antigravity.google/docs/artifact-review/, https://antigravity.google/docs/getting-started, https://antigravity.google/changelog/, https://en.wikipedia.org/wiki/Google_Antigravity, https://releasebot.io/updates/google/antigravity, https://discuss.ai.google.dev/t/persistent-memory-plugin-give-your-agent-long-term-memory-across-all-projects/178110

# 5. Autonomous / asynchronous SWE agents

### SWE-agent — Princeton NLP / SWE-agent org, MIT, active
- **Category:** open-source research harness for autonomous issue-resolution (batch/headless, not chat)
- **Focus / intended use:** resolve a GitHub issue in a containerized repo checkout and emit a patch; also the reference platform for ACI experiments (cybersecurity variant EnIGMA, multimodal variant share the same core).
- **Inputs:** a problem statement (GitHub issue URL, text, or SWE-bench instance), a repo (local dir, GitHub URL, or preserved image), and a YAML config selecting model, tool bundles, prompt templates, and history processors.
- **Effects:** arbitrary shell execution inside a Docker/Modal/Singularity sandbox; file edits in the checkout; the deliverable is a `git diff` produced by the `submit` tool. With the `web_browser` bundle it also drives a headless browser (`tools/web_browser/bin/*`: `open_site`, `click_mouse`, `screenshot_site`, `execute_script_on_page`).
- **Model-facing arch:** tools are *bundles* of executables on PATH plus a `config.yaml` declaring signature/docstring/arguments (`tools/search/config.yaml`, `tools/windowed/config.yaml`), rendered either into the system prompt (`{{command_docs}}`) or into native function-calling schemas (`parse_function: function_calling` in `config/default.yaml`); each turn appends one observation to a linear history, and a bundle's `state_command` (e.g. `tools/windowed` -> `_state`) injects live environment state (open file, cursor window, cwd) into the prompt templates.
- **Memory / learning arch:** none across tasks. Within a task, context is managed by composable `history_processors` (`sweagent/agent/history_processors.py`): `LastNObservations` elides all but the last n observations, `ClosedWindowHistoryProcessor` replaces stale file-window dumps with "Outdated window with N lines omitted...", `CacheControlHistoryProcessor` places prompt-cache breakpoints. Optional `RetryAgent` + `sweagent/agent/reviewer.py` (`Reviewer`, `Preselector`, `Chooser`, `ScoreRetryLoop`) does best-of-n over whole trajectories.
- **User-facing arch:** CLI `sweagent run` / `run-batch`, plus a local web GUI and a trajectory inspector (`sweagent/inspector/`); fully autonomous by default — no approval gate. Steering happens by editing YAML configs, not mid-run.
- **Unique element:** the ACI thesis made concrete — the interface, not the model, is the tunable surface: a paged file *window* rather than `cat`, an `edit <start>:<end>` command that runs flake8 on the *proposed* result and **refuses to apply** a syntactically broken edit (`tools/windowed_edit_linting/bin/edit`, `_LINT_ERROR_TEMPLATE` shows applied-vs-original windows and says "DO NOT re-run the same failed edit command"), concise fixed-format search results, and a hard blocklist of interactive programs (`sweagent/tools/tools.py` blocks `vim`, `emacs`, `nano`, `less`, `gdb`, `tail -f`, and bare `python`/`bash`/`sh`). Paper reports 12.5% vs a 3.8% RAG baseline on SWE-bench at publication, attributed to interface design alone.
- **Evidence:** https://arxiv.org/abs/2405.15793, repos/SWE-agent/config/default.yaml, repos/SWE-agent/config/sweagent_0_7/07.yaml, repos/SWE-agent/tools/windowed_edit_linting/bin/edit, repos/SWE-agent/sweagent/agent/history_processors.py, repos/SWE-agent/sweagent/tools/tools.py

### mini-swe-agent — SWE-agent org, MIT, active
- **Category:** minimal autonomous SWE agent (deliberate anti-ACI control)
- **Focus / intended use:** show how much of SWE-agent's result survives with zero custom tooling; recommended as the default over SWE-agent unless you are experimenting with tools (docs).
- **Inputs:** problem statement + repo; a single model name.
- **Effects:** shell commands in a sandbox; patch output.
- **Model-facing arch:** ~100-line agent class; the only tool is bash, emitted as a fenced code block (no tool-calling API required, so any model works); each action runs via a fresh `subprocess.run` rather than a persistent shell.
- **Memory / learning arch:** none; strictly linear append-only history with no processors — "what you see is what the LM receives" (docs).
- **User-facing arch:** CLI plus batch mode; no approval gate, no environment-state injection.
- **Unique element:** the counter-argument to SWE-agent's own thesis — >74% on SWE-bench Verified (docs) with no windowed editor, no search commands and no history compaction, implying frontier models now supply what the ACI used to.
- **Evidence:** https://github.com/SWE-agent/mini-swe-agent, https://mini-swe-agent.com/latest/, https://arxiv.org/abs/2405.15793

### Agentless — Xia/Deng/Dunn/Zhang et al. (UIUC), MIT, active reference implementation (widely forked)
- **Category:** fixed-pipeline (non-agentic) issue resolver
- **Focus / intended use:** SWE-bench issue resolution without letting the LLM choose its own actions; the architectural control condition for every agentic harness.
- **Inputs:** issue text + repo snapshot.
- **Effects:** produces a patch; runs generated reproduction tests and the existing suite for validation. No open-ended shell.
- **Model-facing arch:** three hardcoded phases — (1) hierarchical localization (repo skeleton -> suspect files -> classes/functions -> edit lines), (2) repair by sampling N candidate patches in a simple search/replace diff format, (3) validation via LLM-generated reproduction tests plus regression runs, with ranking/majority selection. The LLM never picks a tool; each phase is a templated prompt with a fixed output schema.
- **Memory / learning arch:** none. State between phases is structured data (file lists, line ranges, candidate patches), not conversation.
- **User-facing arch:** a batch script over a dataset; not an interactive product.
- **Unique element:** showed that retrieve-then-patch-then-validate beat contemporary agent loops on SWE-bench Lite (32.0%, ~$0.70/instance at publication) — the strongest evidence that "agency" often pays for search a harness could do deterministically.
- **Evidence:** https://arxiv.org/abs/2407.01489, https://github.com/OpenAutoCoder/Agentless, https://dl.acm.org/doi/full/10.1145/3715754

### AutoCodeRover — National University of Singapore / AutoCodeRoverSG, GPL-3.0, active
- **Category:** structure-aware autonomous program-improvement agent
- **Focus / intended use:** issue fixing, and with a test suite, fault-localization-guided repair.
- **Inputs:** issue text + repo; optionally tests / a reproducer.
- **Effects:** patches; runs tests for statistical fault localization.
- **Model-facing arch:** the tool surface is a small set of *program-structure* search APIs over the AST (`search_class`, `search_method_in_file`, `search_code`, ...) rather than grep/cat; the agent iterates context retrieval until it can name concrete class/method locations, then a separate patch-writing stage edits those locations.
- **Memory / learning arch:** none across tasks; within a task the accumulated "code context" set is the carried state.
- **User-facing arch:** CLI / Docker, batch over SWE-bench or a single GitHub issue; no interactive approval.
- **Unique element:** the retrieval unit is a program entity, not a file or byte range — code search as specification inference from program structure (paper). Later releases report ~37.3% SWE-bench Lite / ~46.2% Verified pass@1 (repo README).
- **Evidence:** https://arxiv.org/pdf/2404.05427, https://github.com/AutoCodeRoverSG/auto-code-rover, https://zhiyufan.github.io/files/ISSTA2024a.pdf

### Devin — Cognition, proprietary, active
- **Category:** async autonomous SWE agent with a full virtual workstation
- **Focus / intended use:** ticket-sized backlog work (~3h envelope per session, docs): Linear/Jira tickets, migrations, bug repro, PR review, integrations.
- **Inputs:** a prompt or ticket (web app, Slack/Teams, Devin CLI, API v3, Devin MCP server), a repo plus machine snapshot, optionally a Playbook and the org's Knowledge.
- **Effects:** full command execution in a per-session cloud VM; file edits; branches and PRs; web browsing and web-app testing; scheduled recurring sessions; writes back into its own Knowledge base and DeepWiki/Devin Wiki docs.
- **Model-facing arch:** the tool surface is a virtual workstation rather than a tool list — shell, embedded IDE/editor, and browser, each with a human-visible pane (docs); Devin plans first and works the plan, and session state persists as a VM snapshot so a session can be resumed or forked.
- **Memory / learning arch:** the most explicit of any product here — **Knowledge** (user-authored or Devin-proposed operational facts, with triggers, repo pinning, enable/disable), **Playbooks** (reusable procedure + success criteria for a repeated task, effectively a per-task system prompt), in-repo `AGENTS.md`/SKILL.md rules, and **DeepWiki** auto-generated repo documentation that Ask Devin queries. Devin can be asked to mine its own past sessions into new Playbooks or to dedupe Knowledge (docs).
- **User-facing arch:** chat alongside a live workspace — you watch the shell/editor/browser, interrupt, message mid-session, or take the machine over; billing is in ACUs (~15 min of autonomous work, ~$2.00–2.25/ACU, reported).
- **Unique element:** Devin manages Devins — a coordinator session decomposes work and spawns **managed Devins**, each a full agent in its own isolated VM with its own terminal and browser, then monitors, resolves conflicts and compiles results (parallel sessions Feb 2026, orchestrated managed Devins Mar 2026, reported).
- **Evidence:** https://docs.devin.ai/get-started/devin-intro, https://docs.devin.ai/work-with-devin/advanced-capabilities, https://cognition.ai/blog/devin-can-now-manage-devins, https://cognition.com/blog/how-cognition-uses-devin-to-build-devin, https://devin.ai/pricing

### Jules — Google (Labs / DeepMind), proprietary, active
- **Category:** async autonomous SWE agent, plan-approval flavored
- **Focus / intended use:** repo-scoped tasks (bug fixes, tests, docs, small features) that end in a PR; the free tier is a real product tier (reported).
- **Inputs:** a repo + branch and a prose task, or a GitHub issue assignment; `AGENTS.md` at repo root; an optional setup script.
- **Effects:** clones into an isolated Google Cloud VM, edits, runs builds/tests, pushes commits and opens PRs; a 2026 addition ingests CI failures and auto-fixes/re-pushes without a human in the loop (reported).
- **Model-facing arch:** plan -> execute -> review loop on Gemini 3.x (3.1 Pro paid / 3 Flash free, reported); the VM is a general dev box (Node/Python/Go/Java/Rust, Maven/Gradle/Docker preinstalled) so the tool surface is essentially shell + repo, with the plan as the structure carried between steps.
- **Memory / learning arch:** **Memory for Repositories** — Jules records the user's corrections, nudges and preferences during a task and replays them on similar tasks in that repo; plus **environment snapshots**, where a validated setup script is frozen into an image reused by later tasks in the same repo. Google describes the underlying technique as actor–critic style critiquing (reported).
- **User-facing arch:** web app (jules.google), GitHub issue assignment, a CLI with parallel runs and a diff viewer, and a REST API (repoless tasks, file outputs, activity filters); the distinguishing gate is that the *plan* can be reviewed, edited or rejected before execution, and steered mid-run.
- **Unique element:** the plan is a first-class editable artifact, and per-repo memory + snapshots make the *repo*, not the session, the unit of accumulated state.
- **Evidence:** https://jules.google/docs, https://jules.google/docs/environment/, https://jules.google/docs/changelog/, https://developers.google.com/jules/api, https://blog.google/innovation-and-ai/models-and-research/google-labs/jules/

### GitHub Copilot coding agent — GitHub/Microsoft, proprietary, active (GA; Copilot Workspace sunset 30 May 2025 and folded into this)
- **Category:** async autonomous SWE agent bound to the GitHub platform
- **Focus / intended use:** assign an issue (or `@copilot` a PR comment) and get a draft PR back.
- **Inputs:** a GitHub issue or a prompt from the agents panel / VS Code / PR comment; repo custom-instruction files and `AGENTS.md` (nested per-directory files supported); repo-level MCP settings.
- **Effects:** runs in an ephemeral dev environment **powered by GitHub Actions** — explores code, edits, runs tests and linters, pushes commits, opens/updates a PR, resolves merge conflicts. Hard scope limits: one repository per task and a **59-minute** session cap that "cannot be extended or bypassed" (docs).
- **Model-facing arch:** shell + repo inside the Actions runner, plus MCP servers — the GitHub MCP server and Playwright MCP server are enabled by default (docs); every step lands as a commit, so the git history is effectively the turn-to-turn state.
- **Memory / learning arch:** none of its own; persistence is repo-side — custom instruction files and `AGENTS.md` (load-bearing here precisely because its multi-file edits get no inline review), plus the PR thread as the carried conversation.
- **User-facing arch:** review happens entirely in the GitHub PR UI — comment on the draft PR to steer, with per-step session logs; branch protections and Actions approval gates are the enforcement surface rather than a bespoke approval model.
- **Unique element:** it inherited Workspace's issue-to-PR and natural-language planning ideas but reimplemented them as *CI* — the agent's sandbox, permissions, secrets and audit trail are the same GitHub Actions machinery the repo already governs.
- **Evidence:** https://docs.github.com/copilot/concepts/agents/coding-agent/about-coding-agent, https://docs.github.com/en/copilot/responsible-use/agents, https://en.wikipedia.org/wiki/GitHub_Copilot

### Codex cloud — OpenAI, proprietary service (the Codex CLI is Apache-2.0), active
- **Category:** async autonomous SWE agent, parallel-task oriented
- **Focus / intended use:** delegate background work from ChatGPT/CLI/IDE/desktop and collect diffs or PRs; also automated code review.
- **Inputs:** a task description plus a configured cloud *environment* per repo (setup steps, deps, env vars); `AGENTS.md` files in-repo; tasks can also originate from GitHub, GitLab (beta), Linear or Slack (docs).
- **Effects:** clones the repo into a fresh isolated container/microVM, runs commands and tests, produces a diff and can open a PR. Two-phase network model: the **setup phase has network access** for dependency install, the **agent phase is offline by default** unless internet is explicitly enabled for that environment (docs).
- **Model-facing arch:** the model receives a stacked context — working directory and shell, every applicable `AGENTS.md`, sandbox permission rules, developer config, model-specific instructions, and the tool definitions (docs); the tool surface itself is shell + file edits inside the container.
- **Memory / learning arch:** none that persists reasoning across tasks; what persists is the *environment definition* (reproducible container config) and repo `AGENTS.md`. Environment config is the memory.
- **User-facing arch:** a task list showing many concurrent runs, each finishing with a diff, logs and test output to accept or turn into a PR; the same agent runs locally in the Codex CLI/IDE behind an explicit approval ladder (read-only -> auto-edit -> full-access), and work hands off between local and cloud.
- **Unique element:** fan-out as the default interaction — run several containers on the same task and pick the best result, with the offline agent phase as the safety story.
- **Evidence:** https://learn.chatgpt.com/docs/cloud, https://developers.openai.com/codex/agent-approvals-security, https://openai.com/index/introducing-codex/, https://en.wikipedia.org/wiki/Codex_(AI_agent)

### Droid — Factory AI, proprietary, active
- **Category:** model-agnostic coding agent spanning terminal, CI and remote background execution
- **Focus / intended use:** one harness for interactive work, headless automation and delegated cloud tasks across the SDLC (code, review, QA, docs).
- **Inputs:** prompts in the TUI; `droid exec` for non-interactive/CI invocation; **Missions** (a markdown file describing a multi-agent workflow, runnable as `droid exec --mission -f mission.md`); MCP connectors, subagents and plugins; ticketing/alerting integrations.
- **Effects:** shell, file edits, git/PRs, browser automation; runs inside CI pipelines; **Droid Computers** are remote cloud machines that run tasks in the background on a schedule or trigger — alert watching, customer-issue triage, deep research (docs).
- **Model-facing arch:** a customizable harness (connectors, MCP servers, subagents, plugins) with mid-session model routing across Anthropic/OpenAI/Google/open-weight backends plus BYOK (Ollama, OpenRouter, Fireworks, DeepInfra) (docs).
- **Memory / learning arch:** repo-level agent instruction files plus reusable Skills and Missions; no claimed cross-task learned memory — Missions are the reuse unit.
- **User-facing arch:** four surfaces over the same agent — desktop app, CLI TUI, headless `droid exec`, and web/mobile for delegating from anywhere; autonomy-level controls rather than per-call approval.
- **Unique element:** the same binary is the interactive agent, the CI job and the scheduled background worker — autonomy is a flag, not a separate product.
- **Evidence:** https://docs.factory.ai/, https://factory.ai/product/droids, https://factory.com/news/droid-computers, https://factory.ai/product/web, https://agent-safehouse.dev/docs/agent-investigations/droid

### Augment Code (Remote Agents / Intent) — Augment Computing, proprietary, active but repositioned (IDE extensions sunset mid-2026)
- **Category:** cloud parallel SWE agents built on a proprietary retrieval index
- **Focus / intended use:** large-codebase tasks where retrieval quality, not the loop, is the bottleneck; now pitched as multi-agent orchestration (Intent/Cosmos) after the IDE-assistant line was retired (reported).
- **Inputs:** a task plus a connected repo set; agents launched from the web, VS Code, or the Auggie CLI.
- **Effects:** each Remote Agent runs in its own cloud environment on its own branch with full toolchain access; several run concurrently; output is branches/PRs.
- **Model-facing arch:** the differentiator is context assembly rather than tools — the **Context Engine** crawls connected repos into a semantic index plus dependency graph with commit history and serves retrieval per turn; since Feb 2026 it is exposed as a standalone MCP server that Cursor, Claude Code and Codex can call (docs).
- **Memory / learning arch:** the continuously re-crawled index (cross-repo in hosted mode) is the only persistent artifact; no per-session learned memory claimed.
- **User-facing arch:** a dashboard of concurrent remote agents, one branch each, reviewed as diffs/PRs; with the IDE extension path retiring, CLI + web are the surviving surfaces (reported).
- **Unique element:** unbundling — the retrieval layer was separated from the agent and sold as an MCP context provider for *other* harnesses, a bet that context engineering is the defensible component.
- **Evidence:** https://www.augmentcode.com/product/remote-agents, https://www.augmentcode.com/blog/context-engine-mcp-now-live, https://www.augmentcode.com/blog/introducing-remote-agent, https://blog.kilo.ai/p/is-augment-sunsetting-its-ide-extensions

### Conductor — Melty Labs, proprietary (Mac app), active (v0.86.x as of Sep 2026)
- **Category:** local multi-agent orchestrator / swarm shell around other harnesses
- **Focus / intended use:** run several Claude Code, Codex and Cursor agents in parallel on one machine without them colliding.
- **Inputs:** a repo plus one prompt per workspace; the underlying agent CLI and its own auth.
- **Effects:** whatever the wrapped agent can do, confined to a per-agent **git worktree**; merges are user-driven.
- **Model-facing arch:** none of its own — it does not alter the wrapped agent's tools or context assembly; it supplies process and filesystem isolation (one worktree per agent) plus a supervising UI.
- **Memory / learning arch:** none; the wrapped agent's own project files (`CLAUDE.md`/`AGENTS.md`) carry any persistence.
- **User-facing arch:** Mac app with a dashboard of who is working on what, live per-agent output, and per-workspace diff review and merge.
- **Unique element:** the isolation primitive is the git worktree, which turns "N agents on one repo" into a filesystem problem rather than a coordination-protocol problem — the cheapest credible swarm architecture. (Contrast: Terragon, the hosted equivalent, shut down in 2026 — reported.)
- **Evidence:** https://conductor.build/, https://www.oreilly.com/radar/conductors-to-orchestrators-the-future-of-agentic-coding/, https://nimbalyst.com/blog/best-multi-agent-coding-tools-2026/

# 6. Computer-use, browser and OS-level agents

### Anthropic computer use tool + computer-use-demo — Anthropic, proprietary API / MIT reference impl, active
- **Category:** OS-level computer-use tool spec + reference harness
- **Focus / intended use:** Let a Claude model drive an arbitrary GUI desktop (VM/container) via screenshots and synthetic input.
- **Inputs:** A task prompt plus a `tools: [{"type": "computer_toolset_20260801"}]` declaration; the caller supplies the machine (the demo ships a Docker image with Xvfb + Mutter + Tint2 + Firefox/LibreOffice).
- **Effects:** Anything a local user can do on that desktop — clicks, keystrokes, file and app manipulation; the demo container also has bash and a text editor tool.
- **Model-facing arch:** Current toolset `computer_toolset_20260801` is 17 member tools (`screenshot`, `zoom`, five click variants, `left_click_drag`, `mouse_move`, `left_mouse_down/up`, `cursor_position`, `scroll`, `type`, `key`, `hold_key`, `wait`); observation is a base64 PNG of the full display in absolute pixel coordinates (zoom crops still answer in full-screenshot coordinates), non-screenshot actions return the literal string "OK". The loop is plain Messages-API tool use — assistant may emit a *batch* of `tool_use` blocks executed in order, aborting at first failure with later ones returned as `"Not executed: an earlier computer action in this turn failed."`; turn-to-turn state is the raw append-only `messages` array.
- **Memory / learning arch:** None. Persistence is the conversation array plus whatever the model writes to the VM's filesystem; docs recommend pruning old screenshots in batches every ~25 turns (not every turn) to preserve the prompt cache, or capping at ~20 retained images.
- **User-facing arch:** The demo is a Streamlit web UI showing the VM via VNC next to the chat; no built-in approval gate — docs push the user to a low-privilege VM, domain allowlists, and human confirmation for consequential actions. A server-side classifier scans tool_result screenshots for prompt injection and steers the model to re-confirm with the user; it can be opted out via support.
- **Unique element:** Pure pixel-space action API with no DOM/a11y channel at all — the harness is deliberately thin, and screenshot-count/cache management is pushed onto the integrator.
- **Evidence:** https://platform.claude.com/docs/en/agents-and-tools/tool-use/computer-use-tool, https://github.com/anthropics/anthropic-quickstarts/tree/main/computer-use-demo, https://simonwillison.net/2024/Oct/22/computer-use/

### Claude in Chrome — Anthropic, proprietary, active
- **Category:** browser-extension agent (in-session, user's own profile)
- **Focus / intended use:** Run Claude inside the user's logged-in Chrome to read and act on real pages.
- **Inputs:** A natural-language task in a side panel, on whatever tabs the user has granted.
- **Effects:** Navigates, clicks, types, fills forms, moves between tabs, downloads, in the user's authenticated sessions.
- **Model-facing arch:** Extension-mediated page access (manifest permissions include `debugger`, `scripting`, `tabs`/`tabGroups`, `downloads`, `webNavigation`, `nativeMessaging`) — i.e. CDP-level control plus injected scripts, rather than the pixel-only computer tool; exact observation serialization is not published (docs/third-party analysis only). (docs)
- **Memory / learning arch:** unclear — sessions are described as session-bound; no published cross-session skill store.
- **User-facing arch:** Side panel in Chrome, **site-level permission model**: the user approves each site, sessions are per-site scoped, Team/Enterprise admins can allowlist/blocklist; `nativeMessaging` lets it bridge to Claude Desktop/Code. Anthropic gates high-risk categories and relies on injection classifiers; researchers have repeatedly demonstrated injection chains through 2026.
- **Unique element:** Reuses the human's existing logged-in browser rather than a clean cloud VM, and makes the trust boundary a per-site permission grant.
- **Evidence:** https://labs.zenity.io/post/claude-in-chrome-a-threat-analysis, https://chromewebstore.google.com/publisher/anthropic, https://www.explainx.ai/blog/claude-in-chrome-browser-extension-guide-safety-2026

### OpenAI computer use (CUA) / Operator → ChatGPT agent → Atlas — OpenAI, proprietary, API tool active / consumer agent surfaces retired
- **Category:** cloud-VM browser agent + computer-use API tool
- **Focus / intended use:** A model trained to operate a GUI (originally a cloud-hosted browser) end-to-end from screenshots.
- **Inputs:** A task in ChatGPT, or via the API a `computer` tool declaration plus screenshots the caller captures.
- **Effects:** Full browsing and form submission in a sandboxed browser/VM; the 2025 ChatGPT-agent build added a terminal, a text browser, and connectors.
- **Model-facing arch:** The API tool exposes 8 action types (`click`, `double_click`, `drag`, `move`, `scroll`, `keypress`, `type`, `wait`, plus `screenshot`); model emits a `computer_call` with an ordered `actions` array, the app executes and returns a `computer_call_output` carrying a base64 PNG at `"detail": "original"`; cross-turn state is carried by `previous_response_id` (docs note a 20-turn session cap). Observation is screenshots only — no DOM/a11y channel.
- **Memory / learning arch:** None at the tool level. Atlas added "browser memories" (site-derived facts fed back into chat) before retirement.
- **User-facing arch:** Historically: live cursor view, "takeover mode" for the user to type credentials, confirmation prompts before consequential actions, watch mode on sensitive sites, isolated logged-out agent sessions discarded at end. **Current state (2026-09):** standalone Operator was folded into ChatGPT agent mode (2025), agent mode was pulled in Aug 2026, and ChatGPT Atlas was discontinued 2026-08-09 with browser-agentic work moved into ChatGPT and Codex; the API `computer` tool survives but OpenAI's docs now steer GPT-6-class models toward code execution instead. (docs/secondary — openai.com blocks fetching)
- **Unique element:** Betting on one RL-trained end-to-end model (no scaffolded planner) over a scripted harness — and, in 2026, retreating from GUI pixels toward code execution.
- **Evidence:** https://developers.openai.com/api/docs/guides/tools-computer-use, https://help.openai.com/en/articles/20001371-evolving-atlas-into-chatgpt-for-browser-based-agentic-work, https://openai.com/index/computer-using-agent/, https://openai.com/index/building-chatgpt-atlas/, https://presenc.ai/research/openai-operator-update-tracker-2026

### browser-use — browser-use Inc., MIT, active (v0.13.10, pushed 2026-09-15)
- **Category:** browser-automation agent library (loop-owning)
- **Focus / intended use:** Point any LLM at a Chromium instance over CDP and have it complete a web task.
- **Inputs:** A task string, an LLM handle, optional sensitive-data map, allowed file paths, and skills.
- **Effects:** Full browsing; also writes/reads files in an agent-owned FileSystem, downloads, PDFs, and arbitrary in-page JS via `evaluate`.
- **Model-facing arch:** Observation is an **indexed, serialized DOM**: `browser_use/dom/serializer/serializer.py` walks the CDP tree, filters to visible/interactive/scrollable nodes with bounding-box containment and paint-order checks, allocates an ephemeral `selector_index` per actionable node, and renders `[33]<div />` / `\t[35]<input type=text placeholder=... />` tab-indented lines; `*[` marks nodes that are **new since the last step**, `|SCROLL|` and `|SHADOW(open)|` prefix scroll containers and shadow roots. Screenshots are optional (`use_vision` true/auto/false) and bounding-box-annotated. Action space is a registry (`browser_use/tools/service.py`): `search, navigate, go_back, wait, click, input, upload_file, switch, close, extract, search_page, find_elements, scroll, send_keys, find_text, screenshot, save_as_pdf, dropdown_options, select_dropdown, write_file, replace_file, read_file, evaluate, done`; up to 5 actions per step. Every step is **one system message + one rebuilt user message** (`message_manager/service.py::create_state_messages`) containing `<user_request> <agent_history> <agent_state>(file_system + todo_contents) <browser_state> <read_state>`; the model returns `{thinking, evaluation_previous_goal, memory, next_goal, action[]}`.
- **Memory / learning arch:** Three layers, no cross-run learning: (a) the per-step `memory` string that becomes the durable `<agent_history>` line; (b) an agent-owned filesystem seeded with `todo.md` that the prompt tells it to tick off via `replace_file` as its *first* action — Manus-style recitation, plus result files; (c) LLM **compaction** every N steps into a `<compacted_memory>` block (`maybe_compact_messages`). There is no separate planner LLM any more — planning is an in-context structured plan the agent emits via `plan_update`, rendered as `[x]/[>]/[ ]/[-] i: step` (`agent/service.py::_render_plan_description`), with scripted nudges for replan-on-stall, plan-after-N-exploration-steps, and a loop detector that hashes normalized actions over a 20-step window and warns on repetition/page stagnation.
- **User-facing arch:** Python/CLI library (`Agent(task=..., llm=...).run()`) plus a hosted cloud; no interactive approval gate in the core loop — control is via allowed domains, sensitive-data placeholders, `max_steps`, and a `judge` for eval.
- **Unique element:** The ephemeral-index DOM serialization (indices regenerated each observation, new elements starred) is the canonical cheap alternative to pixel grounding — and the harness has converged on Manus's playbook: todo file, structured plan object, compaction, keep-failures-in-context nudges.
- **Evidence:** repos/bu/browser_use/dom/serializer/serializer.py, repos/bu/browser_use/agent/system_prompts/system_prompt.md, repos/bu/browser_use/agent/message_manager/service.py, repos/bu/browser_use/tools/service.py, repos/bu/browser_use/agent/service.py (_render_plan_description, _inject_replan_nudge), https://github.com/browser-use/browser-use

### UI-TARS / UI-TARS-desktop — ByteDance Seed, Apache-2.0 (model weights + app), active
- **Category:** native GUI agent model + desktop/remote operator harness
- **Focus / intended use:** One end-to-end VLM that perceives a screenshot and emits a grounded action — no DOM, no scaffold.
- **Inputs:** A natural-language instruction in the Electron app; the user picks a target (local computer, local browser, remote cloud computer, remote browser).
- **Effects:** Mouse/keyboard on the local desktop via NutJS, or on a ByteDance-hosted sandbox VM/browser.
- **Model-facing arch:** Strict `Thought: ... / Action: ...` text format over a compact action space — `click|left_double|right_single|drag(start_box='<|box_start|>(x,y)<|box_end|>')`, `hotkey(key='ctrl c')`, `type(content=)`, `scroll(start_box, direction)`, `wait()`, `finished()`, `call_user()` (prompt variants per model generation; Doubao/UI-TARS-2 variants use `[x1,y1,x2,y2]` boxes). Observation is the raw screenshot only (coordinates are predicted directly by the model, not selected from a candidate list); turn state is the rolling screenshot + action history, capped by `maxLoopCount` (default 100, settable 25–200).
- **Memory / learning arch:** Pushed into training rather than the harness: UI-TARS-2 (arXiv 2509.02544) is trained with multi-turn RL and a *hierarchical memory* (working memory for the recent trace, episodic memory as condensed long-term notes), in a hybrid environment that also exposes a filesystem and terminal. The desktop app itself keeps only a session trace.
- **User-facing arch:** Electron app with a thought-chain panel, screenshot gallery, pause/stop, and `call_user()` as the model's escape hatch back to the human; remote operators create a cloud sandbox per session.
- **Unique element:** Model-as-harness — the action space is a few text-serialized primitives with model-predicted pixel boxes, so almost all capability lives in the weights.
- **Evidence:** repos/tars/apps/ui-tars/src/main/agent/prompts.ts, repos/tars/apps/ui-tars/src/main/remote/operators.ts, repos/tars/apps/ui-tars/src/main/store/setting.ts, https://arxiv.org/abs/2509.02544, https://arxiv.org/abs/2501.12326

### Agent TARS — ByteDance Seed, Apache-2.0, beta / slowing
- **Category:** general multimodal agent stack (CLI + Web UI) around GUI + MCP tools
- **Focus / intended use:** A Manus-like general agent that combines GUI grounding with ordinary tool calls in terminal/browser.
- **Inputs:** A prompt via `agent-tars` CLI or its Web UI, plus MCP server config.
- **Effects:** Browser control, shell, filesystem, and any wired MCP tool.
- **Model-facing arch:** Event-stream core — the agent builds context from an internal Agent Event Stream, with state-management events expressed as RFC-6902 JSON Patches for incremental updates; browser interaction is pluggable via "browser control strategies" (DOM-based, visual-grounding, and hybrid), so it can fall back from DOM indices to UI-TARS-style coordinates on the same page. (repo layout + docs)
- **Memory / learning arch:** Event stream is the session record; no documented cross-session memory.
- **User-facing arch:** CLI and a replayable Web UI showing the event stream; no approval gating documented.
- **Unique element:** Explicit DOM-vs-visual-grounding strategy swap behind one browser interface.
- **Evidence:** https://agent-tars.com/blog/2025-06-25-introducing-agent-tars-beta, https://github.com/bytedance/UI-TARS-desktop/discussions/873, repos/tars/README.md, repos/tars/multimodal/agent-tars/core/src/environments/local/browser/browser-control-strategies/ (path listing)

### Open Interpreter — Open Interpreter / Killian Lucas, Apache-2.0, active but repurposed
- **Category:** local code-execution agent (formerly a computer-use agent)
- **Focus / intended use:** As of the July 2026 Rust rewrite it is a **coding agent tuned for low-cost/open models**, not a GUI agent.
- **Inputs:** A prompt in a terminal TUI (`i` / `interpreter`), plus a chosen *harness*.
- **Effects:** Runs code and shell commands on the local machine under native sandboxing; edits files.
- **Model-facing arch:** Fork of OpenAI's Codex CLI (`codex-rs/`), speaking the Codex exec protocol; the distinctive feature is `/harness` — it re-implements other vendors' agent harnesses (`native`, `claude-code`, `claude-code-bare`, `zcode`, `kimi-code`, `kimi-cli`, `qwen-code`, …) so a cheap model gets the prompt/tool discipline it was trained against.
- **Memory / learning arch:** Inherits Codex's session/rollout files and memory-doc conventions; no computer-use memory.
- **User-facing arch:** Terminal UI with Codex-style approval modes and sandboxing; `/harness` and `/model` switch the emulated scaffold.
- **Unique element:** Harness emulation as a first-class switch — the bet that harness/prompt shape, not the model, carries most of the performance.
- **Evidence:** repos/oi/README.md, repos/oi/FORK_BRANDING.md, repos/oi/codex-rs/ (tree), https://www.openinterpreter.com/blog/open-interpreter, https://github.com/openinterpreter/openinterpreter
- **Note:** The original Python Open Interpreter's `--os` mode (screenshot + OCR + pyautogui, "computer" API) is gone; a grep of the current tree finds no screenshot/GUI-control code. Historical only.

### Skyvern — Skyvern AI, AGPL-3.0, active (pushed 2026-09-18)
- **Category:** browser-automation platform (agent + workflow engine)
- **Focus / intended use:** Production RPA replacement — repeatable web workflows (forms, procurement, invoicing) that survive layout changes.
- **Inputs:** A task prompt + target URL + typed data schema, or a no-code workflow graph of blocks; also a Playwright-compatible SDK.
- **Effects:** Browsing, form fill, file upload/download, 2FA/TOTP handling, data extraction into structured JSON, persistent browser sessions with cookies.
- **Model-facing arch:** Three-role loop (Planner decomposes → Actor acts → Validator checks and triggers replan). Observation is a scraped element list: `webeye/scraper/scraper.py` injects `unique_id`s into DOM nodes, builds `id_to_element/css/frame` maps plus element **hashes** (so an element keeps identity across re-mounts), and renders a pruned element tree, optionally alongside screenshots. The model returns a JSON array of actions with `{id, action_type, reasoning, confidence, context}` over a ~30-verb action space (`click, input_text, paste_text, upload_file, download_file, select_option, checkbox, wait, hover, solve_captcha, verification_code, goto_url, go_back/forward, scroll, keypress, move, drag, left_mouse, execute_js, extract, complete, terminate, …`). Page text is interpolated through an `| untrusted` Jinja filter that fences page content from instructions.
- **Memory / learning arch:** Per-run step/action history in Postgres with artifacts (screenshots, HAR, video); cached action plans keyed to the workflow so repeat runs skip LLM planning; persistent browser sessions carry cookies across runs. No cross-task learned memory.
- **User-facing arch:** FastAPI service + React workflow builder + live browser streaming; runs are observable and replayable, with human-in-the-loop blocks (e.g. TOTP/verification-code prompts) rather than per-action approval.
- **Unique element:** Engine pluralism — a single run can be driven by Skyvern's own planner or delegated to `openai_cua`, `anthropic_cua`, `ui_tars`, or `yutori_navigator` via `RunEngine`, so the DOM harness and the pixel models are interchangeable backends.
- **Evidence:** repos/sky/skyvern/webeye/actions/action_types.py, repos/sky/skyvern/webeye/scraper/scraper.py, repos/sky/skyvern/forge/prompts/skyvern/extract-action.j2, repos/sky/skyvern/forge/agent.py (RunEngine mapping ~L486), https://github.com/Skyvern-AI/skyvern

### Manus — Butterfly Effect / Manus AI, proprietary, active
- **Category:** general autonomous agent on a cloud VM
- **Focus / intended use:** Hand off a whole multi-step knowledge-work task (research, deck, site, data pull) and get artifacts back.
- **Inputs:** A prompt and files in a web/mobile chat; optional scheduled or long-running tasks.
- **Effects:** Everything inside a per-task Ubuntu VM — shell, Python, filesystem, Chromium, deployed sites/apps; outbound web actions.
- **Model-facing arch:** CodeAct-style — the model's primary action is executable code/shell in the sandbox on top of a small core toolset (~20 atomic tools; general-purpose bash/CLI wrappers rather than tool proliferation), with a Planner sub-agent emitting a structured Plan object injected only when needed. Context discipline is the published core: KV-cache-stable append-only prefixes (no timestamps, deterministic JSON key order, explicit cache breakpoints; cached tokens ~10× cheaper), **logit masking** of tool availability instead of removing tools (with `browser_*`/`shell_*` name prefixes to make masking cheap), filesystem as unbounded externalized context with **restorable** compression (drop the page body, keep the URL/path), and deliberately varied serialization to avoid few-shot ruts.
- **Memory / learning arch:** The sandbox filesystem is the memory: files persist across the task and sleep/wake cycles (recycled after 7 days free / 21 days Pro, artifacts restored, temp files not). Recitation via `todo.md` rewriting was the original attention mechanism but cost ~30% of actions/tokens and was replaced by the structured Plan object. Long contexts are handled by reversible **compaction** first, lossy **summarization** at ~128k while keeping recent tool calls raw; they report quality degrading well before the 1M window (~256k). Failed actions and stack traces are deliberately left in context so the model updates its beliefs.
- **User-facing arch:** Chat with a live "Manus's Computer" pane (browser/terminal/editor replay), artifact files browsable and shareable, collaborators can access the sandbox; async — the agent runs for many minutes and reports back. Wide Research spawns N parallel sub-agents, each a fresh minimal-context instance, to dodge the context window.
- **Unique element:** The clearest public statement that the agent's product is context engineering, not the model — KV-cache hit rate treated as the primary production metric (~100:1 input:output token ratio, ~50 tool calls/task).
- **Evidence:** https://manus.im/blog/Context-Engineering-for-AI-Agents-Lessons-from-Building-Manus, https://www.philschmid.de/context-engineering-part-2, https://manus.im/blog/manus-sandbox, https://manus.im/blog/introducing-wide-research, https://manus.im/blog/manus-1.5-release

### Agent S3 — Simular AI, Apache-2.0, active (pushed 2026-09-05)
- **Category:** OS-level computer-use agent framework (OSWorld-derived research harness)
- **Focus / intended use:** Generalist desktop agent across Linux/Mac/Windows; reference implementation for OSWorld-style benchmarks.
- **Inputs:** CLI task prompt plus a main model and a separate **grounding** endpoint (`--ground_provider/--ground_url/--ground_model/--grounding_width/--grounding_height`, e.g. UI-TARS-1.5-7B at 1920×1080).
- **Effects:** Mouse/keyboard on the host or VM, plus sandboxed Python/Bash execution.
- **Model-facing arch:** A **flat policy** that replans at every step from the current screenshot plus a compact history (default `max_trajectory_length` 8 image turns) — the Agent S2 manager/worker hierarchy was deliberately removed because subgoals go stale. A two-model split persists: a reasoning model proposes the action in natural language, a small grounding model converts the description to coordinates. A coding agent is *inside* the GUI action space, so the policy can choose a GUI action or hand off bulk/file/parsing work to code, receiving status/return-code/stdout/stderr back. Reported vs S2: +13.8% success, −52.3% LLM calls, −62.4% wall time.
- **Memory / learning arch:** None at run time in S3 — the earlier generations' experience/narrative memory was dropped; gains come from test-time scaling, not learning. Behavior Best-of-N runs N stochastic rollouts, compresses each into a *behavior narrative* (per-transition VLM facts, with markers overlaid pre-action and zoomed crops post-action), and has a judge pick one in a single multiple-choice comparison — 66% → 72.6% on OSWorld at 100 steps.
- **User-facing arch:** Python CLI / library with an optional reflection agent; no approval surface — it is a benchmark-and-research harness.
- **Unique element:** Deleting the planner hierarchy and the memory store, then buying back accuracy with N parallel rollouts and a comparative judge over summarized behavior.
- **Evidence:** https://arxiv.org/abs/2510.02250, https://arxiv.org/html/2510.02250v1, https://www.simular.ai/articles/agent-s3, https://github.com/simular-ai/Agent-S, https://arxiv.org/pdf/2504.00906

### Self-Operating Computer — OthersideAI, MIT, stalled (last push 2025-09-19)
- **Category:** minimal multimodal computer-use harness
- **Focus / intended use:** Historical reference — one of the first open full-screen computer-use loops (Nov 2023).
- **Inputs:** `operate` CLI objective, optional `-m` model flag and voice input.
- **Effects:** Mouse/keyboard on the local desktop.
- **Model-facing arch:** Screenshot → multimodal model → JSON action (`click` with a percentage-based x/y, `write`, `press`, `done`), with an optional Set-of-Mark / OCR mode that labels clickable elements so the model names a label instead of guessing coordinates; history is the accumulated message list. Model-agnostic (GPT-4o/4.1/o1, Gemini, Claude 3, Qwen-VL, LLaVA).
- **Memory / learning arch:** None.
- **User-facing arch:** Terminal only; no approval gate, no takeover.
- **Unique element:** Percent-of-screen coordinates plus optional OCR/SoM labelling — the earliest widely-copied answer to "how does a VLM click".
- **Evidence:** https://github.com/OthersideAI/self-operating-computer, https://github.com/OthersideAI/self-operating-computer/blob/main/operate/main.py, https://api.github.com/repos/OthersideAI/self-operating-computer (pushed_at 2025-09-19)

### Stagehand — Browserbase, MIT, active (pushed 2026-09-18) [close neighbor]
- **Category:** browser-agent SDK (not loop-owning by default)
- **Focus / intended use:** Let deterministic Playwright code drop into natural language only where the page is unpredictable.
- **Inputs:** TypeScript/Python code calling `act()`, `extract(schema)`, `observe()` on a Playwright page; optional `agent()` for a full loop.
- **Effects:** Anything Playwright can do, local or on Browserbase cloud browsers.
- **Model-facing arch:** Observation is a **trimmed hybrid accessibility tree** (not raw DOM, not pixels), optionally plus a viewport screenshot (`extract(screenshot: true)`, v3.5+); `observe()` returns candidate actions/selectors the caller can inspect before executing, so the LLM proposal and the execution are separable steps. Runs as a browser extension next to the page to cut round-trip latency.
- **Memory / learning arch:** Action caching — `observe()` results (resolved selectors) can be persisted and replayed without an LLM call, and v4 (Aug 2026) caches `act`/`observe`/`extract` server-side with a hit-count threshold before serving from cache.
- **User-facing arch:** SDK; the developer owns the loop and the approvals. Browserbase adds session recordings and live view.
- **Unique element:** Three primitives instead of an agent loop, with cacheable observations that decay an LLM step into a deterministic selector over repeated runs.
- **Evidence:** https://github.com/browserbase/stagehand, https://docs.stagehand.dev/v3/best-practices/caching, https://www.browserbase.com/blog/ai-web-agent-sdk, https://www.browserbase.com/changelog

# 7. General assistants and local-first agent runtimes

### Agent Zero — Agent Zero s.r.o. (agent0ai, orig. frdel), MIT, active
- **Category:** local-first general agent runtime (Dockerized Linux desktop + web UI)
- **Focus / intended use:** a general personal agent that runs real software in a container — shell, browser, LibreOffice, files — rather than a chat wrapper.
- **Inputs:** free-text chat + attachments in the Alpine.js WebUI; the user also edits prompt markdown, `agent.yaml` profiles, plugins, and skills as ordinary files under `usr/`.
- **Effects:** arbitrary code/shell execution in its container, browser automation with DOM annotation, file and document edits, scheduled jobs, MCP client/server calls, and host-machine file access via the A0 CLI connector.
- **Model-facing arch:** One `Agent.monologue()` loop (`agent.py`) rebuilds the system prompt each iteration from concatenated markdown sections (`prompts/agent.system.main.*.md`) plus dynamic blocks; the model must emit a single JSON object (`thoughts`/`headline`/`tool_name`/`tool_args`) and the loop only terminates when the `response` tool sets `break_loop`. Malformed or repeated output is fed back through dedicated repair prompts (`prompts/fw.msg_misformat.md`, `fw.msg_repeat.md`).
- **Memory / learning arch:** FAISS vector store with three areas — `MAIN`, `FRAGMENTS`, `SOLUTIONS` (`plugins/_memory/helpers/memory.py:56`); `monologue_end` extensions auto-write fragments and "solutions" from each completed run (`plugins/_memory/extensions/python/monologue_end/_50_memorize_fragments.py`, `_51_memorize_solutions.py`) and a `message_loop_prompts_after` extension auto-recalls them into the next prompt. A separate `behaviour_adjustment` tool mutates a persistent behaviour-rules file, and a Claude-style `skills/` folder tree (`SKILL.md` + frontmatter `triggers`) holds procedural knowledge loaded on demand via `skills_tool`.
- **User-facing arch:** Self-hosted web UI with live streaming, intervention (you can interrupt mid-loop), chat branching, time-travel workspace snapshots, a memory dashboard, project isolation, and a plugin hub; approval is largely absent — the container is the safety boundary, not a per-tool permission model.
- **Unique element:** Nothing is hardcoded: every prompt, tool, lifecycle hook and agent profile is a file on disk the user (or the agent) can edit, and each agent level spawns the next via `call_subordinate` — A0 creates A1, A1 creates A2 — each subordinate getting its own `AgentContext`, profile and clean history, returning only its summary to the parent.
- **Evidence:** https://github.com/agent0ai/agent-zero, agent-zero:`agent.py` (`monologue`, `prepare_prompt`, `get_system_prompt`), agent-zero:`tools/call_subordinate.py` (`get_or_create_subordinate`, `_is_child_context`, slot/`context_id` continuation), agent-zero:`prompts/agent.system.tool.call_sub.md`, agent-zero:`plugins/_memory/` (AGENTS.md, `helpers/memory.py`, `prompts/agent.system.solutions.md`), agent-zero:`extensions/python/` (29 lifecycle hook points), https://github.com/agent0ai/agent-zero/blob/main/docs/developer/architecture.md

### Open WebUI — Open WebUI, Open WebUI License (BSD-3 derived, branding-preservation clause), active
- **Category:** self-hosted LLM front-end that has grown a full agentic backend
- **Focus / intended use:** a "home for AI" over Ollama/OpenAI-compatible endpoints; the 2026 codebase is no longer chat-only — it ships subagents, tool approval, terminals, automations and skills.
- **Inputs:** chat messages, file/knowledge uploads, per-model system prompts ("Models" = agent presets), Python plugin source pasted into the admin UI (Tools/Functions), MCP/OpenAPI server URLs, and cron-style automation prompts.
- **Effects:** HTTP/MCP tool calls, Jupyter-kernel code execution, shell terminals, file and knowledge-base writes, calendar/notification writes, scheduled recurring runs, and whatever user-authored Python plugins do.
- **Model-facing arch:** Middleware assembles per-turn context (system prompt + RAG citations + memory injection + tool specs) and drives an OpenAI-Responses-shaped loop where each `function_call` item carries a `status` of `pending`/`queued`/`requires_approval`/`rejected` and is resolved out-of-band (`backend/open_webui/utils/tool_approval.py`). Tool specs come from three sources: in-process Python Tools loaded by `utils/plugin.py`, MCP servers (`utils/mcp/`), and OpenAPI servers.
- **Memory / learning arch:** A `memory` table plus vector search, with an LLM post-turn reviewer that proposes `add_memory`/`update_memory`/`replace_memory_content`/`delete_memory` operations against path-scoped memory rows (`utils/memory.py:409 review_memory_after_turn`, `:521 _generate_memory_operations`); separately a `skill` table (`models/skills.py`) holds user-authored markdown skills, and `utils/context_compaction.py` compacts long chats.
- **User-facing arch:** Multi-user web app with RBAC/groups/access grants; the human sits in the loop through `ask_user` (a tool that blocks the run for an answer) and explicit approve/reject on tool calls, plus chat forking, time-stamped automations, and an admin surface for Tools/Functions/Pipelines valves.
- **Unique element:** Four distinct plugin shapes — Filters (pre/post hooks), Actions (message buttons), Pipes (custom "models"), and Tools — all loaded as live Python modules from database rows via `importlib`, with pydantic "Valves" auto-rendering a settings form; agency was added around this plugin substrate rather than replacing it.
- **Evidence:** https://github.com/open-webui/open-webui, open-webui:`backend/open_webui/utils/subagents.py` (`DEFAULT_SUBAGENT_SYSTEM_PROMPT`, background/foreground semaphores, subagent JWT `typ`), open-webui:`backend/open_webui/utils/tool_approval.py`, open-webui:`backend/open_webui/utils/plugin.py` (`resolve_valves_schema_options`, dynamic module load), open-webui:`backend/open_webui/utils/memory.py`, open-webui:`backend/open_webui/utils/code_interpreter.py` (`JupyterCodeExecuter`), open-webui:`backend/open_webui/models/skills.py`

### LibreChat — Danny Avila / LibreChat-AI, MIT, active
- **Category:** multi-user hosted chat platform with a first-class agent builder
- **Focus / intended use:** an enhanced ChatGPT clone where "Agents" are shareable, ACL-governed assistant definitions across many providers.
- **Inputs:** agent config forms (name, instructions with `{{current_date}}`-style server-resolved variables, model + params, capability toggles), uploaded files, OpenAPI specs for Actions, MCP server configs, and `librechat.yaml`.
- **Effects:** sandboxed code execution in ~10 languages, RAG over uploaded files, arbitrary HTTP calls through OpenAPI Actions and MCP tools, artifact rendering, and writes to a shared memory store.
- **Model-facing arch:** Per-agent tool set assembled from capability toggles + MCP servers, with "deferred tools" kept out of the initial context and discovered at runtime via a Tool Search tool; orchestration primitives are Agent Chain (up to 10 agents in sequence, each seeing prior outputs), Subagents (isolated child runs returning compact results), and Handoffs (context transfer to a specialist) (docs).
- **Memory / learning arch:** An explicit Memory tool the agent calls to save/update/delete structured user memories, either a shared personal pool or per-agent isolated partitions with their own access control; no automatic learning beyond that plus conversation storage (docs).
- **User-facing arch:** Web chat with an agent builder panel; three-level ACL (Viewer/Editor/Owner) sharing to users, groups or roles; steering via `Ask User` and explicit Tool Approval pauses, plus Activity Groups that collapse long reasoning runs.
- **Unique element:** Programmatic Tool Calling — the agent writes code inside the Code Interpreter sandbox that invokes MCP tools in loops/conditionals, so tool orchestration happens in the sandbox rather than in the model's turn loop (docs).
- **Evidence:** https://www.librechat.ai/docs/features/agents, https://www.librechat.ai/docs/features/code_interpreter, https://github.com/LibreChat-AI/code-interpreter, https://www.librechat.ai/docs/configuration/librechat_yaml/object_structure/agents, https://www.librechat.ai/blog/2026-02-18_2026_roadmap

### AnythingLLM — Mintplex Labs, MIT, active
- **Category:** workspace-scoped local/desktop RAG app with a bolt-on agent mode
- **Focus / intended use:** document-centric private assistant; agent mode extends a workspace's RAG chat with tool use.
- **Inputs:** documents into a workspace, chat prompts (optionally prefixed `@agent`), Agent Skill toggles, MCP server configs, JS plugin folders (`plugin.json` + `handler.js`), and visually-built Agent Flows.
- **Effects:** web search/scrape, SQL queries against connected databases, file save/list/summarize, chart generation, scheduled jobs, MCP tool calls, and whatever custom JS skills do (desktop build runs on the user's machine).
- **Model-facing arch:** Workspace is the unit of context — embeddings, chat history, model and skill set all scope to it; an agent session is entered per-invocation (`@agent`, or automatically when the provider supports native tool calling) and the tool list is filtered per turn by "Intelligent Tool Selection" so only plausibly-relevant tools/MCPs enter the prompt (docs).
- **Memory / learning arch:** Workspace vector store over ingested documents plus per-workspace chat history and a personalization/memory feature; no autonomous self-learning (docs).
- **User-facing arch:** Desktop app or Docker web app, multi-user with workspace permissions; steering is coarse — you toggle skills globally in Settings > Agent Skills and start/end an agent session inside an otherwise normal chat. No per-call approval surface found (docs).
- **Unique element:** Agent capability is a mode layered onto a RAG workspace rather than the primary abstraction — the workspace, not the agent, owns knowledge, model and tools.
- **Evidence:** https://docs.anythingllm.com/agent/usage/overview, https://docs.anythingllm.com/agent/setup, https://docs.anythingllm.com/features/chat-modes, https://github.com/Mintplex-Labs/anything-llm

### Kortix (formerly Suna) — Kortix AI, source-available (Elastic License 2.0 on the Suna lineage; repo now presents as open-source self-hostable), active
- **Category:** self-hostable general "AI management system" / agent fleet
- **Focus / intended use:** explicitly positioned as the open alternative to Claude Cowork and ChatGPT Work — agents that deliver finished work products for an organization.
- **Inputs:** natural-language assignments via web/CLI/desktop/mobile clients, plus agent, skill and machine definitions committed as files in a git repo.
- **Effects:** full Linux sandbox per session (shell, browser, files), 3,000+ app connectors plus MCP/OpenAPI/GraphQL/HTTP, website deploys; work reaches the real repo only through human-reviewed change requests (docs).
- **Model-facing arch:** Built on OpenCode agents with scoped tool access, one disposable cloud computer per session, each session on its own git branch; agents can run in parallel across many sessions and are described as able to rewrite themselves (docs).
- **Memory / learning arch:** The "company brain" is plain-text files in a git repository — organizational knowledge and learnings accumulate as commits rather than in a vector store (docs).
- **User-facing arch:** Web, CLI, desktop (Electron) and mobile clients; the approval model is a pull-request analogue — sandbox work is discarded unless committed and reviewed. Credentials are brokered server-side and never exposed to sandboxes (docs).
- **Unique element:** "A company is going to be a git repository" — agents, skills, config, machine definitions and memory are all version-controlled files, and the sandbox/branch pairing makes every agent run reviewable and revertible.
- **Evidence:** https://github.com/kortix-ai/suna, https://github.com/kortix-ai/suna/blob/main/README.md, https://suna.so, https://apidog.com/blog/suna-ai-open-source-general-ai-agent/

### AutoGPT — Significant Gravitas, dual (MIT for `classic/`, Polyform Shield for `autogpt_platform/`), active but pivoted
- **Category:** historically an autonomous general agent; now a hosted/self-hosted block-graph agent platform
- **Focus / intended use:** 2023's "give it a goal and walk away" loop is now `classic/`, effectively frozen reference code; the live product is a visual builder for agents wired to SaaS connectors.
- **Inputs:** plain-English outcome descriptions (AutoPilot generates the graph), or hand-wired blocks on a canvas; runs are triggered on demand, on schedule, or by webhook.
- **Effects:** actions across 45+ integrated platforms (Gmail, Calendar, GitHub, Slack, Notion, Jira, Salesforce, Stripe…), LLM calls, and marketplace-published agents.
- **Model-facing arch:** The current platform is block-graph execution, not a free-running agent loop — the model fills nodes rather than choosing the next step; `classic/` retains the original plan→execute→critique→re-plan loop with a file-backed workspace and the Forge/agbenchmark harness.
- **Memory / learning arch:** Classic used a task list plus vector-store "memory" of past steps (the lineage's signature idea); the platform persists run history and agent definitions, not learned behaviour.
- **User-facing arch:** Web dashboards for runs and costs, a marketplace of prebuilt agents, and a drag-drop builder; hosted tier is paid and managed, self-hosting is the free path.
- **Unique element:** The clearest case of the lineage's lesson learned in public — unsupervised recursive goal decomposition was abandoned for bounded, user-authored graphs with explicit triggers.
- **Evidence:** https://github.com/Significant-Gravitas/AutoGPT, https://github.com/Significant-Gravitas/AutoGPT/releases, https://en.wikipedia.org/wiki/AutoGPT, https://newreleases.io/project/github/Significant-Gravitas/AutoGPT/release/autogpt-platform-beta-v0.6.59

### BabyAGI — Yohei Nakajima, MIT, stalled (maintained as experiment, not a product)
- **Category:** minimal autonomous task-loop reference implementation
- **Focus / intended use:** the 2023 ~140-line demonstration of task creation → prioritization → execution against a vector store; later rewritten as an experimental function-framework ("babyagi 2o" and successors).
- **Inputs:** a single objective string plus an initial task.
- **Effects:** none by default beyond LLM calls and vector-store writes — no filesystem or shell tools in the original; later experiments add self-registering Python functions.
- **Model-facing arch:** Three prompted roles over a shared task queue — execution agent (does the top task with retrieved context), task-creation agent (invents follow-ups from the result), prioritization agent (reorders the queue) — looping until tasks run out, which in practice they never do.
- **Memory / learning arch:** Task results embedded into a vector index and retrieved as context for later tasks; no other persistence.
- **User-facing arch:** A script. No approval, no steering surface, no stop condition beyond killing the process.
- **Unique element:** Historically load-bearing: it made "the task list is the agent's state" legible, and its failure modes (unbounded task inflation, no grounding, no termination) are why later harnesses bound depth, budget and tool effects.
- **Evidence:** https://github.com/yoheinakajima/babyagi, https://yoheinakajima.com/, https://pinokio.co/apps/github-com-yoheinakajima-babyagi

### Khoj — Khoj AI, AGPL-3.0, active
- **Category:** self-hostable personal "second brain" with custom agents and automations
- **Focus / intended use:** search and reason over the user's own documents and the web, from wherever the user already is.
- **Inputs:** synced documents (pdf/markdown/org-mode/word/notion/images), chat from browser, Obsidian, Emacs, desktop, phone or WhatsApp; agent definitions (persona, knowledge set, chat model, tools).
- **Effects:** web search and page reads, document retrieval, image generation, TTS, and scheduled automations that run a prompt on a cadence and deliver results (e.g. by email).
- **Model-facing arch:** Python/Django service that routes a query through tool selection (notes search, web search, webpage read, code run) and composes retrieved chunks into the prompt; "research mode" runs an iterative multi-step retrieval loop before answering (docs).
- **Memory / learning arch:** Semantic index over the user's synced corpus is the durable store; conversation history persists per agent. No learned-behaviour store (docs).
- **User-facing arch:** Web/desktop/editor-plugin clients; agents are configured through forms; automations are the scheduling surface. No per-tool approval — effects are read-mostly, which is why it needs none (docs).
- **Unique element:** Architecturally distinct in the client dimension: the same agent runtime is exposed through Obsidian/Emacs/WhatsApp clients over a common API, making the assistant ambient rather than a destination app.
- **Evidence:** https://github.com/khoj-ai/khoj, https://docs.khoj.dev, https://community.obsidian.md/plugins/khoj

### Home Assistant LLM conversation agents — Open Home Foundation, Apache-2.0, active
- **Category:** embedded assistant runtime inside a home-automation platform
- **Focus / intended use:** natural-language control of a home; the LLM is a conversation agent behind the Assist pipeline, not a general agent.
- **Inputs:** voice (Wyoming protocol: faster-whisper → agent → Piper) or typed text; the user configures which entities are exposed and writes a persona prompt.
- **Effects:** whatever the exposed Assist API intents allow — turning devices on/off, setting climate, shopping-list writes, scripts and scenes; effects are bounded by entity exposure, not by model judgement.
- **Model-facing arch:** The `llm` integration assembles an API per request: a base Assist API of intents plus tools contributed by other integrations through an `llm.py` platform exposing `async_get_tools(LLMContext, api_id)`, evaluated lazily per request and returning tools plus an optional prompt fragment. Agents are pluggable (Ollama, OpenAI, Anthropic).
- **Memory / learning arch:** None beyond the conversation session, plus recent-command context sharing between the deterministic Assist agent and the LLM agent.
- **User-facing arch:** Voice satellites and the HA dashboard; steering is configuration-time (entity exposure lists, per-agent prompt), and there is no runtime approval step — the permission model is the exposure allowlist.
- **Unique element:** Tools are contributed by third-party integrations at request time and scoped by an explicit entity-exposure allowlist, so capability is declared by the platform rather than negotiated by the agent — a fail-closed, config-time permission model.
- **Evidence:** https://developers.home-assistant.io/docs/core/llm/, https://www.home-assistant.io/blog/2025/09/11/ai-in-home-assistant/, https://www.home-assistant.io/voice_control/assist_create_open_ai_personality/

### Claude (Cowork folded in) — Anthropic, proprietary, active
- **Category:** big-lab general assistant with an agentic execution mode
- **Focus / intended use:** Cowork shipped Jan 2026 as a file-and-task agent for non-developers; as of 2026-09-16 it is being merged into Claude chat so one surface routes between quick answers and long agentic work.
- **Inputs:** natural-language requests, attached/connected files, and connectors (Google Drive, Gmail, Docusign, FactSet); enterprise "plugins" scope it to domains like financial analysis or HR.
- **Effects:** produces and edits real artifacts — documents, spreadsheets, presentations, research reports — and acts through connectors; Claude Docs and Claude Slides launched in beta alongside the merge.
- **Model-facing arch:** Not published. Externally it presents as a Claude Code-derived agent loop with file-system and connector tools, with the merged product performing routing between a short answer and a long agentic run (docs).
- **Memory / learning arch:** Not published in architectural terms; Claude's consumer memory and project files are the user-visible persistence (docs).
- **User-facing arch:** Desktop (macOS/Windows), web and mobile; the deliverable is a set of editable files handed back, and Claude Docs adds real-time multi-person collaboration with Claude drafting and commenting alongside human editors.
- **Unique element:** The 2026 consolidation itself is the architectural bet — "agent mode" as a separate product was retired in favour of one assistant that decides how much agency a request needs.
- **Evidence:** https://fortune.com/2026/09/16/anthropic-merges-its-claude-chat-and-agentic-cowork-products-into-a-single-ai-assistant-as-part-of-a-push-to-build-an-ai-superapp/, https://venturebeat.com/technology/anthropic-is-killing-off-cowork-and-folding-it-into-claude-launching-claude-docs-and-claude-slides, https://www.unite.ai/anthropic-folds-cowork-into-a-single-claude-experience-across-plans/, https://en.wikipedia.org/wiki/Claude_(language_model)

### Gemini Agent / Chrome Auto Browse (ex-Project Mariner) — Google DeepMind, proprietary, active (Mariner dead)
- **Category:** big-lab general assistant with absorbed browser agency
- **Focus / intended use:** Project Mariner, the standalone browser agent, was shut down 2026-05-04 after 17 months; its algorithms were folded into Gemini Agent (task automation in the Gemini app), Chrome's Auto Browse, and AI Mode in Search.
- **Inputs:** natural-language task descriptions in the Gemini app or Chrome; account-linked context (Gmail, Calendar).
- **Effects:** multi-step web navigation and form filling, email triage, reservations — operating inside the user's authenticated browser session.
- **Model-facing arch:** Not published. Externally: a Gemini model driving a browser-control tool surface, now integrated into the browser itself rather than an extension or a separate cloud VM (docs).
- **Memory / learning arch:** Not published; Gemini's account-level personalization is the user-visible persistence (docs).
- **User-facing arch:** Gemini app and Chrome, gated to AI Pro/Ultra subscribers in the US; steering is per-task with user takeover of the browser.
- **Unique element:** The negative result worth recording: a dedicated visual web agent was not durable as its own product, and the capability survived only by being absorbed into the browser and assistant surfaces users already had.
- **Evidence:** https://en.wikipedia.org/wiki/Project_Mariner, https://gigazine.net/gsc_news/en/20260507-google-shuts-down-project-mariner/, https://nerova.ai/news/google-shuts-down-project-mariner-gemini-agent-browser-2026

### ChatGPT Work / Scheduled Tasks — OpenAI, proprietary, active (Pulse retired)
- **Category:** big-lab general assistant with a long-running work agent and a scheduling layer
- **Focus / intended use:** ChatGPT Work (announced 2026-07-09, GPT-5.6) runs multi-hour tasks producing documents, spreadsheets, presentations and web apps for business users; Scheduled Tasks is the recurring-automation surface.
- **Inputs:** natural-language task briefs; for tasks, a stored prompt plus a schedule or a web-monitoring trigger.
- **Effects:** file artifacts, connector actions, and web monitoring with notifications; Pulse's proactive daily briefing was discontinued and its function absorbed into scheduled tasks.
- **Model-facing arch:** Not published. Observable contract: a persistent agent run with a tool surface over files, code and connectors, and a scheduler that re-invokes a stored prompt (docs).
- **Memory / learning arch:** ChatGPT's account memory plus per-task stored prompts; no published learning loop (docs).
- **User-facing arch:** ChatGPT web/desktop with a sidebar hub for managing tasks; hard limits are the steering mechanism — max 10 active tasks, at most one run per hour, idle tasks auto-pause.
- **Unique element:** Scheduling as a rate-limited, quota-bounded product primitive rather than a general cron — the harness constrains autonomy with caps rather than with approvals.
- **Evidence:** https://www.bloomberg.com/news/articles/2026-07-09/openai-unveils-chatgpt-work-agent-to-field-tasks-for-hours, https://itbrief.com.au/story/openai-expands-chatgpt-scheduled-tasks-with-new-hub, https://prowlo.com/blog/chatgpt-pulse-shut-down, https://help.openai.com/en/articles/6825453-chatgpt-release-notes

### Genspark Super Agent — Genspark (MainFunc), proprietary, active
- **Category:** hosted general assistant with multi-model routing
- **Focus / intended use:** one prompt in, a finished research artifact out — reports, slide decks, sheets, media.
- **Inputs:** natural-language requests; no self-hosting, no config files.
- **Effects:** web search and browsing, document/deck/media generation, some phone-call and booking actions; all server-side.
- **Model-facing arch:** "Mixture-of-Agents" — a router fans a request across multiple models and dozens of tools, with models cross-checking each other before a single answer is returned (docs; internals unpublished).
- **Memory / learning arch:** unclear — no published persistence model beyond session/project history.
- **User-facing arch:** Web/mobile chat with a credit meter; the dominant user-facing mechanic in 2026 reporting is credit burn, and steering is limited to re-prompting.
- **Unique element:** Ensemble-of-models-as-verification substituted for an explicit agent loop — redundancy instead of planning.
- **Evidence:** https://www.eesel.ai/blog/genspark-ai-review, https://www.lindy.ai/blog/genspark-ai-features, https://www.genspark.ai

### Flowith — Flowith, proprietary, active
- **Category:** hosted general assistant on a canvas interaction model
- **Focus / intended use:** multi-branch AI work where the conversation is a graph on an infinite canvas rather than a linear thread.
- **Inputs:** prompts placed as nodes on a canvas; uploaded knowledge; "Oracle"/agent mode tasks.
- **Effects:** web research, document and media generation, deployed artifacts; hosted only.
- **Model-facing arch:** Each canvas node is its own context branch with explicit parent edges, so the user — not a summarizer — decides what context a given generation inherits (docs; internals unpublished).
- **Memory / learning arch:** A knowledge-base ("Knowledge Garden") the user curates; no published autonomous memory.
- **User-facing arch:** Infinite canvas web app with credit-metered runs; steering is spatial — you fork, prune and re-parent nodes.
- **Unique element:** Context assembly is made a direct-manipulation UI problem: the canvas graph *is* the context-selection mechanism.
- **Evidence:** https://flowith.io, https://flowith.io/blog/flowith-vs-manus-vs-genspark/, https://storyflow.so/blog/best-manus-ai-alternatives-2026

### Proxy — Convergence AI (acquired by Salesforce, 2025), proprietary, stalled as an independent product
- **Category:** hosted web-task agent (general-assistant framing, browser-agent substance)
- **Focus / intended use:** delegate web tasks — research, form filling, data gathering — to an agent that drives a real browser.
- **Inputs:** natural-language task descriptions; recorded/named reusable tasks.
- **Effects:** browser actions (click, type, navigate) against live sites, in cloud browser sessions.
- **Model-facing arch:** Not published; externally a plan-then-act browser-control loop over DOM/screenshot observations (docs).
- **Memory / learning arch:** Marketed as "learning new skills" from user demonstrations — saved task recipes replayed later; no published memory architecture. Treat the learning claim as marketing until evidenced.
- **User-facing arch:** Web app with live session view and takeover; steering is interrupt-and-correct.
- **Unique element:** Little architecturally distinctive; included because it is frequently listed as a Manus-like general assistant when it is really a browser agent. Post-acquisition its independent trajectory is unclear.
- **Evidence:** https://aiagentstore.ai/ai-agent/convergence-proxy, https://www.aiapps.com/items/proxy-by-convergence-ai/, https://aiagentstore.ai/compare-ai-agents/convergence-proxy-ai-vs-genspark-super-agent

### Dify — LangGenius, Apache-2.0 with a competing-cloud-service restriction, active — OUT OF SCOPE
- **Category:** visual LLM-app / workflow platform
- **Out of scope because:** the unit of design is a DAG the *user* authors (nodes for LLM calls, tools, branches, loops) plus RAG knowledge bases; its "agent" is one node type inside that graph. Control flow is authored ahead of time, so there is no open-ended agent loop for a harness survey to compare. Published output is an API endpoint, not an assistant.
- **Evidence:** https://github.com/langgenius/dify, https://www.sandbase.ai/blog/dify-ai-platform-explained-2026/, https://toolhalla.ai/blog/dify-vs-flowise-vs-langflow-2026

### Flowise — FlowiseAI, Apache-2.0, active — OUT OF SCOPE
- **Category:** visual LangChain/LangGraph builder
- **Out of scope because:** it is a Node.js canvas that renders LangChain/LlamaIndex components as drag-and-drop nodes; Flowise 2.0 added multi-agent orchestration but still as a visually wired LangGraph, i.e. the topology is user-authored. It is a harness *builder*, not a harness with its own agent contract.
- **Evidence:** https://github.com/FlowiseAI/Flowise, https://use-apify.com/blog/dify-vs-flowise-2026, https://rapidclaw.dev/blog/low-code-ai-agent-platforms-compared-2026

### OpenHands — All Hands AI, MIT, active — NOT COVERED HERE
- **Category:** coding-first agent runtime
- **Note:** deliberately skipped per cluster scoping; its general (non-coding) usage is covered by another researcher. No findings recorded in this file.
- **Evidence:** https://github.com/All-Hands-AI/OpenHands

# 8. Memory systems and memory-centric runtimes

> This section covers both standalone memory layers and harnesses whose defining bet
> is memory. It ends with the research lineage and a note on why published
> agent-memory benchmark numbers should not be trusted at face value.

### Letta (f.k.a. MemGPT) — Letta AI, Apache-2.0, active (Python V1 server archived Sep 2026; active source is `letta-code`, TypeScript)
- **Category:** memory-centric agent runtime (harness + server + hosted cloud)
- **Focus / intended use:** stateful agents whose context window is explicitly managed as a tiered memory hierarchy; now shipped primarily as a coding/general CLI agent (`letta`).
- **Inputs:** conversation turns; explicit `/remember` teaching; `/init` project bootstrap; files under the memory repo; skills under `$MEMORY_DIR/skills`.
- **Effects:** rewrites the agent's own system prompt contents (memory blocks), commits/pushes a git memory repository, spawns background subagents, pages messages in/out of context.
- **Model-facing arch:** Memory reaches the model two ways at once — a set of labeled **blocks rendered into the system prompt every turn** (bounded by `CORE_MEMORY_BLOCK_CHAR_LIMIT`, each with `label`, `description`, `value`, `read_only`), and **tool calls** for everything outside it. In the current MemFS design, root-level `*.md` files in the memory repo are the in-context blocks; child directories are deferred and only the rendered file tree is shown as a signpost (`memory-format.ts::isCoreMemoryPath`, `memory-filesystem.ts::renderMemoryFilesystemTree`).
- **Memory / learning arch:** Unit = a **memory block** = one markdown file with `name`/`description` YAML frontmatter (`memory-markdown.ts`); frontmatter-free `MEMORY.md` files act as indexes and a child directory only counts as memory if it contains one (`memory-format.ts::isProjectedMemoryPath`). The **writer is the agent itself**, via line-precise self-edit tools — V1 `core_memory_append/replace`, `memory_replace`, `memory_insert`, `memory_apply_patch`, `memory_rethink`, `memory_finish_edits` (`letta/functions/function_sets/base.py:246-520`); current runtime exposes one `memory` tool with `str_replace|insert|delete|rename|update_description|create`, each edit auto-committed to git and pushed after the turn (`src/tools/descriptions/MemoryV2.md`). Write triggers are layered: in-turn tool calls; **sleep-time compute**, a separate background agent run after the foreground turn, gated by a modulo counter (`group.sleeptime_agent_frequency`, `bump_turns_counter_async`, `sleeptime_multi_agent_v4.py:132-160`) and fed only messages since `last_processed_message_id`; and in `letta-code` a **reflection/"dreaming"** trigger of `off | step-count | compaction-event` (`src/reflection-settings.ts`) running the subagent in a git worktree. Retrieval outside the blocks is vector search over archival passages (`archival_memory_search`) and full-history `conversation_search`; MemFS semantic search is an optional mod. Consolidation is explicitly LLM-driven, not scored: the sleep-time system prompt instructs "do not contain redundant and outdate information", forbids relative dates, and offers `rethink` for whole-block rewrites (`letta/prompts/system_prompts/sleeptime_v2.py`); plus a **defrag** pass that splits/merges files and a `/doctor` audit of duplication and system-prompt token usage (docs).
- **User-facing arch:** fully visible and editable — memory is a real git checkout of markdown the user can open, diff, revert and share between agents; `/memory`-style CLI views, and dreaming can be configured to require review before updates are applied (docs).
- **Unique element:** the agent's own system prompt *is* the mutable memory store, and the model is given file-editor-grade tools plus a scheduled second self to keep it tidy.
- **Evidence:** repos/letta-code/src/agent/memory-format.ts, repos/letta-code/src/agent/memory-markdown.ts, repos/letta-code/src/tools/descriptions/MemoryV2.md, repos/letta-archive/letta/functions/function_sets/base.py, repos/letta-archive/letta/groups/sleeptime_multi_agent_v4.py, repos/letta-archive/letta/prompts/system_prompts/sleeptime_v2.py, https://docs.letta.com/letta-code/memfs, https://arxiv.org/abs/2310.08560

### Mem0 — Mem0 (mem0ai), Apache-2.0, very active (commits Sep 2026)
- **Category:** memory layer / SDK + hosted platform (drop-in, host-agent-agnostic)
- **Focus / intended use:** extract durable facts from chat traffic and re-inject them, scoped per `user_id` / `agent_id` / `run_id`.
- **Inputs:** `Memory.add(messages, user_id=..., metadata=..., infer=True|False, expiration_date=...)`; `search(query, filters)`.
- **Effects:** writes fact rows into a pluggable vector store (30 providers) plus a SQLite history table and an entity store; returns ranked fact strings the host must splice into its own prompt.
- **Model-facing arch:** Not in-context by default — the host application calls `search()` and injects the returned fact list itself (or uses the OpenAI-compatible proxy). Mem0 never owns the prompt.
- **Memory / learning arch:** Unit = a **self-contained natural-language fact string** ("User went to Paris the week of May 15, 2023"), plus a normalized entity row linked to it. The **writer is a background/inline extraction LLM**, not the agent: `_add_to_vector_store` runs a phased pipeline — fetch last 10 session messages, vector-search the top-10 existing memories, one LLM call under `ADDITIVE_EXTRACTION_PROMPT`, batch-embed, md5-hash dedup, batch insert, batch entity link (`mem0/memory/main.py:879-1207`). Notably the current OSS pipeline is **ADD-only**: the older `DEFAULT_UPDATE_MEMORY_PROMPT` ADD/UPDATE/DELETE/NONE tool loop still exists in `configs/prompts.py` but the live path replaces it with additive extraction plus dedup and `linked_memory_ids`; existing memory IDs are mapped to integers before being shown to the LLM as an anti-hallucination measure. Retrieval is **hybrid**: over-fetch 4×top-k by cosine, add a BM25 keyword pass over a lemmatized field, add an entity-store boost, fuse via `score_and_rank` (`main.py:1628-1812`). Decay is crude — a per-memory `expiration_date` filtered at read time (`_payload_is_expired`); the OSS `decay` flag raises a "platform feature" error. There is a separate `_create_procedural_memory` that summarizes an agent's full step history verbatim (`PROCEDURAL_MEMORY_SYSTEM_PROMPT`).
- **User-facing arch:** memories are readable/editable strings through `get_all`/`update`/`delete`/`history` (full per-memory audit trail in SQLite) and a hosted dashboard; no human-editable file form.
- **Unique element:** memory as an *extraction pipeline product*, deliberately decoupled from the agent loop — the agent never decides what to remember.
- **Evidence:** repos/mem0/mem0/memory/main.py:879-1207 (write path), :1628-1812 (hybrid retrieval), repos/mem0/mem0/configs/prompts.py (ADDITIVE_EXTRACTION_PROMPT L468, DEFAULT_UPDATE_MEMORY_PROMPT L176, PROCEDURAL_MEMORY_SYSTEM_PROMPT L326), https://arxiv.org/abs/2504.19413, https://mem0.ai/openmemory
- **Note:** OpenMemory (the local Docker/MCP self-host path) was removed from the monorepo in Jul 2026 and the documented MCP entry point is now hosted (`mcp.mem0.ai`); a self-host server on Postgres+pgvector remains in `server/` (docs).

### Graphiti — Zep (getzep), Apache-2.0, very active (commits Sep 2026)
- **Category:** temporal knowledge-graph memory library (the engine under Zep)
- **Focus / intended use:** incrementally build a bi-temporal entity graph from a stream of episodes without batch re-computation.
- **Inputs:** `add_episode(name, episode_body, source=text|message|json, reference_time, group_id, saga=...)`; optional custom entity/edge Pydantic types and an ontology.
- **Effects:** writes `EpisodicNode`, `EntityNode`, `EntityEdge`, `CommunityNode`, `SagaNode` and their edges into Neo4j/FalkorDB/Kuzu/Neptune, with embeddings and full-text indexes.
- **Model-facing arch:** tool/API retrieval — `search()` returns ranked facts (edge `fact` strings) and node summaries that the caller injects; Zep's product layer pre-assembles these into a context block.
- **Memory / learning arch:** Three-layer unit: the raw **episode** (retained verbatim unless `store_raw_episode_content=False`), the **entity node**, and the **entity edge = a fact triple with a natural-language `fact` string**. The writer is an **LLM extraction pipeline at ingest time** (`extract_nodes` → dedupe → `extract_edges` → resolve), triggered synchronously per `add_episode`. Everything is **bi-temporal**: `created_at`/`expired_at` are ingestion time, `valid_at`/`invalid_at` are event time (`graphiti_core/edges.py:263-290`). Contradiction handling is first-class: `resolve_extracted_edges` searches for *invalidation candidates* separately from duplicate candidates and `resolve_edge_contradictions` sets `expired_at`/`invalid_at` on superseded edges rather than deleting them (`utils/maintenance/edge_operations.py:325-575`). Node dedup runs a cheap non-LLM prefilter first — normalized exact match, then 3-gram shingles → MinHash signature → LSH bands → Jaccard, with a name-entropy gate to skip unreliable short names (`utils/maintenance/dedup_helpers.py`) — falling back to an LLM only for the ambiguous remainder. Consolidation: **communities** built by label propagation over the entity graph and summarized pairwise (`community_operations.py:93-243`), and **sagas** — an episode chain (`NEXT_EPISODE` / `HAS_EPISODE`) with an incrementally re-summarized `SagaNode` watermarked by `last_summarized_at`. Retrieval is hybrid by construction: cosine + BM25 full-text + BFS graph traversal, fused by one of RRF / MMR / node-distance / episode-mentions / cross-encoder rerankers, packaged as `*_HYBRID_SEARCH_*` recipes (`search/search_config_recipes.py`).
- **User-facing arch:** no human-editable text form; inspection via the graph DB, Zep's web graph explorer, or `search_`. `remove_episode` unwinds an episode's contributions.
- **Unique element:** facts are never overwritten or deleted — they are *invalidated on a second time axis*, so the graph can answer "what did we believe, and when".
- **Evidence:** repos/graphiti/graphiti_core/edges.py:263-360, repos/graphiti/graphiti_core/utils/maintenance/edge_operations.py:325-575, repos/graphiti/graphiti_core/utils/maintenance/dedup_helpers.py:88-210, repos/graphiti/graphiti_core/utils/maintenance/community_operations.py:30-243, repos/graphiti/graphiti_core/search/search_config_recipes.py, repos/graphiti/graphiti_core/graphiti.py:700-812 (saga/episode wiring), https://arxiv.org/abs/2501.13956

### cognee — topoteretes, Apache-2.0, very active (v1.5.4, Sep 2026)
- **Category:** graph+vector knowledge-memory framework (Python SDK + FastAPI service + MCP server)
- **Focus / intended use:** turn ingested documents and agent traces into a queryable semantic layer that apps query instead of raw chunks.
- **Inputs:** `add(data, dataset_name, node_set, importance_weight)` then `cognify()`; optional `graph_model` Pydantic schema, ontology, `custom_prompt`, `temporal_cognify=True`. Over MCP: `remember` / `recall` / `forget` / `cognify_status`.
- **Effects:** writes to three stores at once — relational (SQLAlchemy bookkeeping), a graph DB (default `ladybug`; kuzu/neo4j/postgres/turso), and a vector DB (LanceDB default).
- **Model-facing arch:** tool-call retrieval, and most `SearchType`s run their own LLM over the retrieved subgraph and return prose rather than raw hits. An `@agent_memory(...)` decorator wraps an agent entrypoint to auto-inject a retrieved memory block (`memory_top_k`, `memory_system_prompt`, `session_memory_last_n`) and persist the trace afterwards.
- **Memory / learning arch:** Unit = a typed **`DataPoint`** node: a Pydantic model with `metadata.index_fields` (which fields get embedded) and optional `metadata.identity_fields` (id = uuid5 over those fields → idempotent merge across runs; uuid4 otherwise → never dedupes), plus `ontology_uri`, bi-temporal `valid_to`, `version`, `feedback_weight`, `importance_weight`, provenance. Writer = the cognify task graph's LLM steps (`classify_documents` → chunk → `extract_graph_and_summarize` → `record_provenance` → `detect_contradictions` → `add_data_points`); trigger is an **explicit host call**, never passive observation. Retrieval is a registry of ~25 retrievers — default `brute_force_triplet_search` (vector search over node *and edge* embeddings, then graph expansion into a ranked "memory fragment"), alongside `cypher_search`, `natural_language_retriever` (LLM→Cypher), BM25/lexical, temporal, hybrid, agentic. Consolidation is a **separate re-runnable pipeline (`memify`)**: `consolidate_entities` (embedding similarity ≥0.85, `dry_run`, `protect_node_types`), `consolidate_entity_descriptions`, `cross_connect_entities`, `apply_frequency_weights`, `apply_feedback_weights` (streaming EWMA from QA feedback). Decay is weight-based, not TTL; contradictions close nodes via `valid_to`.
- **User-facing arch:** not human-editable; inspection via `search()`, REST API, the `cognee-frontend` graph UI and CLI. `forget` deletes across all three stores.
- **Unique element:** the "cognify then memify" split — memory *quality* is a pipeline you re-run over the existing graph, not a decision made at write time.
- **Evidence:** repos/cognee/cognee/infrastructure/engine/models/DataPoint.py, repos/cognee/cognee/api/v1/cognify/cognify.py:495-650, repos/cognee/cognee/memify_pipelines/ (consolidate_entities.py, apply_feedback_weights.py), repos/cognee/cognee/modules/retrieval/, repos/cognee/cognee-mcp/src/server.py, repos/cognee/cognee/modules/agent_memory/decorator.py

### Basic Memory — Basic Machines, AGPL-3.0-or-later, very active (v0.23.2 Aug 2026)
- **Category:** markdown-vault memory layer exposed over MCP
- **Focus / intended use:** conversations write durable, human-owned markdown notes that form an emergent knowledge graph.
- **Inputs:** MCP tool calls from the host agent (note title/folder/markdown, `memory://` URIs, search queries, project names) — and the user's own edits to files on disk.
- **Effects:** writes `.md` files in a project directory (files are the system of record) and maintains a rebuildable SQLite/Postgres index: `entity`, `observation`, `relation`, an FTS5/tsvector `search_index`, and a sqlite-vec embedding table.
- **Model-facing arch:** pure tool-call, nothing auto-injected. ~25 MCP tools: `write_note`, `read_note`, `edit_note`, `move_note`, `search_notes`, `build_context`, `recent_activity`, POSIX-style `cat/find/grep/ls/tail`, schema tools. `build_context(url="memory://folder/note", depth, timeframe)` is the "resume the thread" call.
- **Memory / learning arch:** Unit = a **markdown note = one entity**, decomposed by *syntax* into **observations** (`- [category] content #tag (context)`) and **relations** (`- relation_type [[Target]] (context)`; bare `[[link]]` → `relates_to`, prose wikilink → `links_to`). The **writer is the agent or the human, verbatim** — no server-side LLM is ever called, so nothing is paraphrased or invented. Write trigger is an explicit tool call, or a filesystem change picked up by `watchfiles.awatch` with an `index_delay` debounce that re-parses and reconciles. Retrieval is `fts | vector | hybrid` (FTS5/tsvector + local `fastembed` `bge-small-en-v1.5`, optional cross-encoder rerank) plus graph traversal over relations and `recent_activity`. **No consolidation, summarization, dedup or decay exists**; the substitute is link maintenance — forward references to not-yet-existing notes are stored unresolved and filled in when the target appears, and `[[X]]` resolves by permalink → title → path → fuzzy.
- **User-facing arch:** maximally visible — memory *is* a folder of markdown the user reads, greps, edits and git-commits; the DB is a cache. Optional Picoschema frontmatter schemas with `warn`/`strict` validation and `bm schema infer` / `diff` drift detection.
- **Unique element:** the knowledge graph is a *projection of human-editable file syntax*, not a separate store — and the system deliberately does no LLM curation of its own.
- **Evidence:** repos/basic-memory/NOTE-FORMAT.md, repos/basic-memory/src/basic_memory/mcp/tools/__init__.py, repos/basic-memory/src/basic_memory/models/search.py, repos/basic-memory/src/basic_memory/schemas/search.py:26, repos/basic-memory/src/basic_memory/index/watch_service.py, repos/basic-memory/src/basic_memory/indexing/forward_reference_resolution.py, repos/basic-memory/src/basic_memory/hooks/inbox.py (append-only WAL capture of Claude Code / Codex SessionStart+PreCompact hooks, no structure at capture time)

### Khoj — khoj-ai, AGPL-3.0-or-later, slowing (last tag 2.0.0-beta.28 Mar 2026; last commit Aug 2026)
- **Category:** personal-assistant application with a document index plus a separate chat-memory store
- **Focus / intended use:** chat over your own files with configured personas and scheduled research automations.
- **Inputs:** files pushed by sync clients (`PUT/PATCH /api/content`, full vs incremental), Notion/GitHub connectors, chat turns.
- **Effects:** writes `Entry` chunk+pgvector rows and `FileObject` raw text, `Conversation` logs, `UserMemory` fact rows, and APScheduler cron jobs.
- **Model-facing arch:** **injected retrieval, not tools** — each chat turn pulls `pull_memories(...)` (last 10, 7-day window) plus `search_memories(q)` (vector) and folds them into the prompt alongside document hits (`routers/api_chat.py:980`).
- **Memory / learning arch:** Two disjoint stores. (a) Document index: unit = `Entry`, a heading-prefixed chunk (recursive splitter, `chunk_overlap=0`, md5 per chunk for incremental re-index), written by the indexer on client sync, retrieved by pgvector cosine above a 0.18 confidence threshold then cross-encoder rerank (`gte-small` bi-encoder, `mxbai-rerank-xsmall-v1`). (b) Chat memory: unit = `UserMemory`, a **single free-text fact + embedding**; writer = an LLM fact-extractor ("Muninn") via `extract_facts_from_query` → `MemoryUpdates(create=[], delete=[])` → `ai_update_memories`; trigger = automatically after **every saved conversation turn**, explicitly skipped for automation-triggered turns to avoid noise; retrieval = cosine top-10; consolidation = the same LLM call emits deletes of superseded facts — no scoring, merging, decay or summarization pass. Gated per user by `is_memory_enabled`.
- **User-facing arch:** `GET/PUT/DELETE /api/memories` lets the user list, edit and delete individual memory strings from the web UI; documents stay on disk, the index is opaque Postgres.
- **Unique element:** an LLM-curated flat fact list held deliberately separate from the document index, with "agents" being configured personas + cron automations rather than tool-using loops.
- **Evidence:** repos/khoj/src/khoj/database/models/__init__.py:544-582 (SearchModelConfig), :768 (Entry), :855 (UserMemory), repos/khoj/src/khoj/database/adapters/__init__.py:2292-2360, repos/khoj/src/khoj/routers/helpers.py:995-1068, repos/khoj/src/khoj/processor/conversation/utils.py:550-620, repos/khoj/src/khoj/processor/content/text_to_entries.py

### LangMem — LangChain, MIT, active (commits Sep 2026, ~1.7k stars)
- **Category:** memory layer SDK bound to the LangGraph store (framework-coupled)
- **Focus / intended use:** give a LangGraph agent semantic, episodic and procedural memory over a namespaced key-value+vector store.
- **Inputs:** message lists / trajectories, an optional `existing` set of prior memories to merge against, a namespace template (typically `user_id`-scoped), and for the optimizer a trajectory plus a performance signal.
- **Effects:** writes memory records into the LangGraph `BaseStore`; the prompt optimizer instead rewrites the agent's own system prompt text.
- **Model-facing arch:** two modes offered side by side — **hot-path tools** (`create_manage_memory_tool` / `create_search_memory_tool`, so the agent decides in-turn) and a **background manager** (`create_memory_store_manager`) whose output is later retrieved by similarity and injected.
- **Memory / learning arch:** LangMem's organizing claim is the cognitive-science triad: **semantic** = fact/profile records (Pydantic-typed or free text), **episodic** = summarized past interactions kept as few-shot examples, **procedural** = the agent's *own system prompt*. Writer is an LLM memory manager that enrolls new, updates existing, and can mark records obsolete; trigger is either the agent's tool call (in-turn) or a deferred background pass. Retrieval is vector similarity within a namespace, except procedural memory which is data-independent and always in the prompt. Consolidation is the manager's update/obsolete decision plus `create_prompt_optimizer` with three named algorithms — `metaprompt` (reflect then meta-prompt), `gradient` (separate critique and proposal steps), `prompt_memory` (single pass) — which folds graded trajectories back into the prompt.
- **User-facing arch:** records are readable/writable through the store API; the optimized prompt is plain text the developer can inspect and version. No end-user surface of its own.
- **Unique element:** the only mainstream memory layer that treats **the system prompt as a learned artifact** with an explicit optimizer, making procedural memory a first-class store rather than a side effect.
- **Evidence:** https://www.langchain.com/blog/langmem-sdk-launch, https://github.com/langchain-ai/langmem, https://docs.langchain.com/oss/python/concepts/memory

### Zep — Zep AI (getzep), proprietary SaaS (Zep Community Edition deprecated; the Graphiti engine is Apache-2.0), active
- **Category:** hosted agent-memory / context-assembly service over a temporal knowledge graph
- **Focus / intended use:** return one pre-assembled, personalized context string per user/thread in <200ms, built from chat history plus structured business data.
- **Inputs:** `thread.add_messages()` (chat turns) and `graph.add()` (episodes of type `message` | `text` | `json`), scoped by `user_id` or `graph_id`; optional `created_at` (feeds fact invalidation), <=10 metadata keys, custom entity/edge types.
- **Effects:** episodes are queued and processed asynchronously and serialized per graph; entities/edges are extracted, facts get `t_valid`/`t_invalid`, and user/entity/thread summaries plus "observations" are derived. Billing meters ingestion only.
- **Model-facing arch:** the app calls `thread.get_user_context(thread_id)` and receives `.context`, a single formatted **Context Block string**. Zep's docs tell callers to keep it out of system/developer messages and, on the Anthropic API, to return it as a `tool_result` — i.e. treat retrieved memory as untrusted data, not instruction (docs). v3 default returns an LLM-summarized block; `mode="basic"` returns raw facts+entities.
- **Memory / learning arch:** Unit = a time-scoped fact on a graph edge (plus entity nodes with narrative summaries, episodes as raw source, communities) — Graphiti's model. Writer = Zep's own background extraction LLM; the agent never authors memories, it only submits episodes. Retrieval is semantic + full-text + breadth-first graph search with reranking, and the notable design choice is that **the query is implicit — the four most recent thread messages** — after which "Smart Context Assembly" selects which context types to include. Contradiction handling is bitemporal invalidation: a new fact marks the prior edge `t_invalid` and the date range is shown to the model rather than the fact being deleted.
- **User-facing arch:** developer console and graph explorer; plus a hosted end-user **Memory MCP server** (`search_graph`, `get_user_summary`, `get_subgraph`, `list_episodes`, `add_memory`). **Context templates** are project-level configs with placeholder syntax (`%{user_summary}`, `%{edges limit=10}`, `%{entities limit=5 types=[person,organization]}`) passed as `template_id`.
- **Unique element:** Zep sells the *constructor* step, not the search step — an implicit query and a template-rendered prose block instead of a result set.
- **Evidence:** https://arxiv.org/abs/2501.13956, https://help.getzep.com/retrieving-context, https://help.getzep.com/context-templates, https://help.getzep.com/memory-mcp-server, https://blog.getzep.com/announcing-a-new-direction-for-zeps-open-source-strategy/, https://github.com/getzep/zep-papers/issues/5
- **Benchmarks:** paper reports DMR 94.8% vs MemGPT 93.4%, and LongMemEval 63.8% vs 55.4% full-context (gpt-4o-mini) at 1.6k vs 115k context tokens — the headline "90% latency reduction" is really a ~70x token-reduction claim. The separate LoCoMo 84% claim is contested: Mem0 recomputed 58.44%, Zep countered 75.14%.

### Supermemory — supermemoryai, MIT on the monorepo but the memory engine ships as prebuilt binaries (open periphery, binary core), very active (~30k stars, Sep 2026)
- **Category:** hosted memory/context API + managed RAG, plus a single-binary self-hosted server and a remote MCP
- **Focus / intended use:** one API giving any app durable per-user/per-project memory and document retrieval without running a vector DB or extraction pipeline.
- **Inputs:** `POST /v3/documents` with `content` (text, PDF, images, A/V, code, URLs), a required `containerTag`, optional `customId` (stable identity, enables dedupe), `metadata`, `taskType` (`memory` | `superrag`), `dreaming` (`dynamic` | `instant`); hosted connectors for Drive/Gmail/Notion/S3/GitHub.
- **Effects:** extract -> type-aware chunk (AST-aware for code) -> embed -> index -> "dream" into a temporal fact graph; the per-container profile is updated.
- **Model-facing arch:** either the app calls `POST /v4/search` / `/v4/profile` and pastes results in, or `withSupermemory(model, {containerTag, mode})` wraps a Vercel AI SDK model and injects profile and/or search results into every call, writing the conversation back by default. Agent-callable tools and a remote MCP expose `search_memory` / `get_profile` / `add_memory`.
- **Memory / learning arch:** Two units in one container — document **chunks** for RAG grounding, and **memories** = atomic fact statements as nodes in a temporal vector-graph carrying `isLatest`, `isInference`, `isForgotten`, `forgetAfter`, `version`. Writer = a background extraction model (proprietary hosted; your own LLM when self-hosted), not the agent. Trigger = async on ingest, and `dreaming: "dynamic"` deliberately **batches related documents by `customId`** so facts form from coherent multi-turn units, with extraction continuing after the document reports done. Retrieval is hybrid vector + full-text + graph in one engine (`searchMode` in `memories|documents|hybrid`, cross-encoder rerank, query rewriting, recency bias, `include.relatedMemories` edge walking); `containerTag` is hashed to a dedicated vector namespace, so isolation is real rather than post-filtering. Consolidation uses three typed edges — `updates` (supersedes for retrieval, history kept), `extends` (enriches), `derives` (inferred, flagged and down-weighted until reviewed) — plus `forgetAfter` expiry and decay for episodic memories while repeated preferences strengthen.
- **User-facing arch:** console with memory-graph visualization, forget/update endpoints, `include.forgottenMemories` recovery, and a **review queue for inferred memories** (`/v3/container-tags/{tag}/inferred`, `approve|decline|undo`).
- **Unique element:** "dreaming" — extraction is deliberately deferred and batched across related documents, with typed supersede/enrich/infer edges and a human review queue for anything merely inferred.
- **Evidence:** https://github.com/supermemoryai/supermemory, apps/docs/concepts/how-it-works.mdx, apps/docs/concepts/graph-memory.mdx, apps/docs/recall/search.mdx, apps/docs/recall/memory-review.mdx, apps/docs/self-hosting/overview.mdx
- **Note:** the "infinite chat" transparent proxy (point a client `baseURL` at `api.supermemory.ai/v3/<upstream base url>`; it trimmed old turns into memory and injected context with zero app changes) has been retired; its docs now redirect to the explicit `withSupermemory` middleware. Treat the proxy design as historical (docs).

### Claude Code memory + Agent Skills — Anthropic, proprietary product (the formats are plain markdown), active
- **Category:** in-product agent memory — human-authored instruction files, a model-authored note directory, and on-demand procedural skills
- **Focus / intended use:** carry knowledge across sessions that each begin with a fresh context window (docs).
- **Inputs:** markdown files the user writes; corrections and preferences stated in chat; `/init` codebase analysis.
- **Effects:** files are concatenated into session context; the agent writes and edits memory files during the session.
- **Model-facing arch (docs):** CLAUDE.md content is delivered **as a user message after the system prompt**, not in the system prompt — it is context, not enforced configuration. Load order is broadest-to-most-specific and purely additive (managed policy -> user `~/.claude/CLAUDE.md` -> project `./CLAUDE.md` -> `./CLAUDE.local.md`), with subdirectory files loaded lazily when the agent reads files there.
- **Memory / learning arch (docs):** Two systems. (1) **CLAUDE.md**: unit = a markdown file/section; writer = the user; trigger = manual; retrieval = **always-in-context, no search**; `@path` imports expand at launch to a maximum depth of 4 hops; `.claude/rules/*.md` with `paths:` glob frontmatter are the only conditional-load mechanism. (2) **Auto memory**: unit = one topic markdown file typed in frontmatter as `user | feedback | project | reference`, plus a `MEMORY.md` index; **writer = the agent itself using ordinary file tools**, not a separate extraction model; trigger = in-turn whenever the agent judges something durable (explicitly skipping anything derivable from the codebase); retrieval = the **index is always in context (first 200 lines or 25KB), topic files are read on demand**. Consolidation is prompt pressure rather than algorithm — after a write the harness measures the index against the limit and instructs the agent to merge, shorten or drop stale entries, erroring if over, because overflow is silently dropped at next load. Contradiction handling is documented as a non-feature: across conflicting files "Claude may pick one arbitrarily"; a harness-stamped `modified` ISO-8601 frontmatter field makes staleness visible. Storage is keyed to the git repo so all worktrees share one directory; subagents do not inherit the main conversation's auto memory.
- **User-facing arch:** everything is plain markdown the user owns; `/memory` lists and toggles, `/context` shows what actually loaded, `claudeMdExcludes` skips unwanted ancestor files, and the model-written layer can be disabled outright.
- **Unique element:** the agent writes its own memory with the same file tools it uses for code — no memory service, no embeddings, no extraction model; retrieval is an always-loaded index plus ordinary file reads. **Agent Skills** supply the procedural half by **three-level progressive disclosure**: L1 `name`+`description` (~100 tokens) always in the system prompt, L2 the `SKILL.md` body (<5k tokens) read from disk on trigger and persisting across turns, L3 bundled reference files read on demand — with scripts executed via bash so only their *output* costs tokens. Frontmatter controls invocation direction (`disable-model-invocation`, `user-invocable: false`), tool pre-approval (`allowed-tools`, that turn only) and subagent forking (`context: fork`).
- **Evidence:** https://code.claude.com/docs/en/memory, https://code.claude.com/docs/en/skills, https://code.claude.com/docs/en/sub-agents, https://code.claude.com/docs/en/context-window

### ChatGPT memory — OpenAI, proprietary, active
- **Category:** consumer assistant personalization memory
- **Focus / intended use:** carry user facts and preferences across conversations without being re-told (docs).
- **Inputs:** conversation turns; explicit "remember that ..."; the Manage Memory editor.
- **Effects:** entries are written to a per-user store and used in later responses; deleting a *chat* does not delete memories derived from it (docs).
- **Model-facing arch (docs):** memory is part of the context the model generates from. Legacy "saved memories" are discrete notepad entries described as always considered in future responses; the current "improved memory" supplies a **continually updated synthesis** of past-chat context, of which the user-visible summary is only a high-level view. How that synthesis reaches the model (verbatim block vs retrieved chunks) is **undocumented — unclear**.
- **Memory / learning arch (docs):** Unit = legacy, a discrete saved-memory string; current, a continuously maintained memory **summary/synthesis** broader than the listed items. Writer = the model itself, saving, updating, combining and removing details it judges useful without being asked (no separate extractor is documented; the classic `bio` tool is not named in official docs). Trigger = in-turn and model-decided, plus explicit user requests; the summary auto-updates as you chat and carries a last-updated timestamp. Retrieval = always-in-context for saved memories; **mechanism for reference chat history / improved memory is undisclosed — unclear** (Free gets "short term continuity", Plus/Pro "longer term understanding"), and the only detailed accounts are third-party reverse-engineering. Contradiction handling was the **stated motivation for the rework** — the old system "became stale" and memories "could contradict one another" — but the new mechanism is not described; decay is undocumented, deleted saved memories are logged up to 30 days.
- **User-facing arch:** Settings -> Personalization -> Memory lets the user view the memory summary and **edit it by typing what should change or highlighting text to correct**, refresh it, delete-and-disable, or revert to the legacy saved-memories list. Per-response provenance: a book icon shows which sources personalized the answer (custom instructions, past chats, files, memories) with a correction path; sources are stripped from shared chats. Temporary Chat bypasses read and write; Project-only memory confines referencing to one project.
- **Unique element:** memory is presented as an **editable natural-language summary document** the user rewrites by typing corrections, rather than a list of rows — and OpenAI concedes the summary is not the whole memory.
- **Evidence:** https://help.openai.com/en/articles/8590148-memory-faq (Cloudflare-blocked to fetchers; readable via web.archive.org), https://openai.com/index/memory-and-new-controls-for-chatgpt/, https://help.openai.com/en/collections/8471548-memory

### Cursor Memories — Anysphere (Cursor), proprietary, status uncertain (shipped beta in Cursor 1.0; the dedicated docs page has since been withdrawn)
- **Category:** automatic, project-scoped rule generation from conversations (memories *are* auto-generated rules)
- **Focus / intended use:** "automatically generated rules based on your conversations in Chat... scoped to your project and maintain context across sessions" (docs).
- **Inputs:** agent chat conversations; explicit user requests to remember something.
- **Effects:** a saved memory behaves as a project rule — its text lands in the model context for Agent/Chat; it does not affect Tab or Inline Edit.
- **Model-facing arch (docs):** memories become rules, and rules are inserted at the start of the model context. Whether all project memories are always included or selected by relevance is **not documented — unclear**; the four inclusion modes are documented for `.mdc` rule files, not for memories.
- **Memory / learning arch:** Unit = one short generated rule, project-scoped. Two writer paths — (a) a **sidecar model that observes conversations passively in the background** and extracts candidate memories, and (b) the agent creating one via tool call when asked or when it notices something important. Trigger is background for the sidecar and in-turn for the tool call; toggled at Settings -> Rules -> "Generate Memories". **Approval is mandatory for background-generated memories** — dismiss the prompt and nothing is saved. Retrieval is rule-style context injection with **no index of any kind documented — unclear**; consolidation, decay and contradiction handling are undocumented, and review/edit/delete is manual.
- **User-facing arch:** approval prompt at creation, then review/edit/delete in Cursor Settings -> Rules. Stored client/account-side; the on-disk location is **undocumented — unclear**. The deliberate contrast is `.cursor/rules/*.mdc` — user-authored, version-controlled, code-reviewed, attaching by alwaysApply / description / glob / manual, with Team Rules optionally *enforced* so users cannot disable them.
- **Unique element:** a second model runs as a sidecar watching the conversation and proposes memories the user must approve — the only system here with write-path human-in-the-loop.
- **Evidence:** https://web.archive.org/web/20250814052731/https://docs.cursor.com/en/context/memories (canonical text; live page withdrawn), https://cursor.com/docs/rules.md, https://cursor.com/docs/llms.txt (current index has no memories page), https://cursor.com/changelog/1-0, https://forum.cursor.com/t/rules-vs-memories-and-global-vs-project/137149

---

## Research: agent self-improvement and experience reuse

**Foundational**
- **Reflexion** (2023, arXiv:2303.11366) — converts a scalar or binary environment reward into a written self-critique; reflections accumulate in a sliding episodic buffer prepended verbatim on the next attempt at the *same* task. No cross-task learning and no weight updates — the reflection acts as a "semantic gradient".
- **Voyager** (2023, arXiv:2305.16291) — skills are *executable JavaScript functions* that enter the library only after passing self-verification, keyed by an embedding of an LLM-written description; retrieval is a vector lookup returning code the agent calls and composes. Append-only, so the library never degrades.
- **Generative Agents** (2023, arXiv:2304.03442) — a memory stream of timestamped natural-language observations scored by recency (exponential decay) x importance (an LLM poignancy score assigned at insertion) x relevance (cosine); periodically the agent queries its own stream and writes higher-level **reflections** back as new records citing their sources, forming a recursive reflection tree.
- **MemGPT** (2023, arXiv:2310.08560) — the OS analogy: fixed main context (system instructions + a writable core-memory block + FIFO queue) vs external context (recall + archival storage), with the LLM paging across the boundary via function calls and interrupts firing on context pressure. Introduced the DMR eval.
- **ExpeL** (2023, arXiv:2308.10144) — collects Reflexion-style success and failure trajectories into a pool, then cross-compares them to distil a compact list of natural-language **insights** that the agent itself ADDs / UPVOTEs / DOWNVOTEs / EDITs; inference prepends the insight list plus the k nearest successful trajectories as demos.
- **Self-Refine** (2023, arXiv:2303.17651) — generate -> self-critique -> refine, iterated; purely intra-episode with nothing persisted. Contrast **Self-Rewarding LMs** (arXiv:2401.10020), which distils the same judging into weights via iterative DPO.

**2025**
- **A-MEM** (arXiv:2502.12110) — Zettelkasten-style atomic notes with LLM-generated description, keywords and tags; inserting a note retrieves near neighbours and proposes bidirectional links, and **memory evolution** lets a new note rewrite the context and tags of the older notes it links to, so the network reorganizes instead of merely appending.
- **Mem0 / Mem0^g** (arXiv:2504.19413) — the published two-phase extract-then-update loop (ADD / UPDATE / DELETE / NOOP against the top-s similar memories); `Mem0^g` stores entity-relation triplets, merging nodes semantically and marking superseded edges invalid rather than deleting them.
- **ACE — Agentic Context Engineering** (Stanford + SambaNova, arXiv:2510.04618) — the context *is* the learned artifact: an itemised playbook of strategies and pitfalls, each bullet carrying helpful/harmful counters, produced by a Generator / Reflector / **Curator** triad that emits structured **deltas** merged non-destructively and deduplicated by embedding ("grow-and-refine"). It explicitly targets **brevity bias** and **context collapse** — the telephone-game degradation of monolithic LLM rewrites. +10.6% on agent benchmarks; the closest prior art for any self-editing markdown-vault design.

**2026 — procedural memory, skill induction, weight-free continual learning**
- **When Continual Learning Moves to Memory** (arXiv:2604.27003) — the best survey-grade framing: separates *what* the reusable unit is (lessons, executable skills, full trajectories, persistent abstractions) from *how* it is organized for retrieval. Finds abstract procedural memories transfer more reliably than detailed trajectories, and that memory organizations with strong forward transfer suffer severe forgetting — external memory **relocates** the stability-plasticity bottleneck rather than dissolving it.
- **Managing Procedural Memory in LLM Agents** (arXiv:2606.23127) — the AFTER benchmark (382 enterprise tasks, 22 procedural skills, controlled cross-task / cross-role / cross-model splits); skills are induced from execution traces, one refinement round buys 3.7-6.7 points, and skills evolved from *multi-model* traces reach 73.1% cross-model accuracy, i.e. induced procedures are portable across backbones.
- **FORGE** (arXiv:2605.16233) — a Reflexion-style inner loop producing Rule / Example / Mixed artifacts, wrapped in an evolutionary outer loop that broadcasts the best instance's memory to the whole population and freezes converged instances.
- **Just-In-Time RL** (arXiv:2601.18510) — a non-parametric trajectory memory whose retrieved neighbours estimate action advantages that additively modulate output logits; the additive rule is the closed-form solution of the KL-constrained policy-optimization objective, i.e. RL with a replay buffer standing in for the gradient.
- **Neural Procedural Memory** (arXiv:2606.29824) — distils procedural skills into **steering vectors in activation space** rather than text; the notable counterpoint to text-as-memory. See also Skill-Pro (arXiv:2602.01869), Forget to Improve (budget-curated memory and active forgetting, arXiv:2606.25115), and the Externalization in LLM Agents review (arXiv:2604.08224): "memory preserves experience, while skill induction extracts the reusable structure within it".

**Benchmarks and their criticisms**
- **DMR** (from MemGPT) — single-fact recall; saturated in the mid-90s and no longer discriminative.
- **LoCoMo** (arXiv:2402.17753) — multi-session QA, but the conversations are only ~16k-26k tokens (inside modern context windows), naive full-context baselines reach ~74%, an independent audit found ~6.4% corrupted answer keys, and the standard GPT-4o-mini judge accepted ~63% of deliberately-wrong-but-adjacent answers.
- **LongMemEval** (arXiv:2410.10813) — 500 questions across five abilities, with commercial assistants dropping ~30% accuracy. But it scores answer quality rather than retrieval precision (a system can dump a noisy candidate set and let the reader recover), per-category n is small, and filler sessions borrowed from other benchmarks may be stylistically detectable.
- **Cross-cutting:** every published number here is harness-dependent, and the Zep/Mem0 LoCoMo dispute (84% vs 58.44% vs 75.14% for the same system) is the clearest evidence that agent-memory benchmarks are currently vendor-run and unreplicated. Treat any single-benchmark SOTA claim in this space as unverified.

# 9. Frameworks and SDKs (building blocks, not harnesses)

> These are libraries for *building* a harness. They are separated because their
> "user" is a developer, and because the axes that separate them — who owns the loop,
> durability vs. storage, and what a subagent is — differ from the product axes above.

### Claude Agent SDK — Anthropic, proprietary (Anthropic Commercial Terms; Python/TS wrapper packages MIT), active
- **Category:** harness-as-a-library (packaged coding-agent loop)
- **Focus / intended use:** run the Claude Code agent loop, tools and context management from your own Python/TypeScript program.
- **Inputs:** a prompt plus an options object (`ClaudeAgentOptions` / `Options`): allowed/disallowed tools, `agents` map of `AgentDefinition`, hooks, MCP servers, permission mode, `systemPrompt` preset or override, `settingSources`; optionally `.claude/` on disk (agents, skills, commands, CLAUDE.md, plugins).
- **Effects:** spawns the Claude Code CLI as a subprocess and streams typed messages back; the agent reads/writes real files, runs shell commands, searches the web, and writes a JSONL transcript per session.
- **Model-facing arch:** single autonomous ReAct-style loop owned by the harness — you do not write the loop, only bound it (`max_turns`, `max_budget_usd`). Tools are the Claude Code built-ins (Read/Edit/Write/Bash/Glob/Grep/WebSearch/Agent/Skill/Workflow) plus MCP servers and in-process SDK MCP tools; context is assembled by the harness from system-prompt preset + CLAUDE.md + preloaded skill frontmatter + transcript, with automatic compaction.
- **Memory / learning arch:** sessions persist as `~/.claude/projects/<encoded-cwd>/<session-id>.jsonl`; `continue` (most recent in cwd), `resume` (by id) and `fork_session` (copy-on-branch, new id) are the resume primitives, with `listSessions`/`getSessionMessages`/`renameSession`/`tagSession` for management and a `SessionStore` adapter to mirror transcripts off-host. Separate `file-checkpointing` snapshots filesystem changes; skills + CLAUDE.md are the durable "learned" layer, edited by hand or by the agent.
- **User-facing arch:** developer writes async iteration over a message stream; no shipped GUI (the CLI and its terminal UI are the sibling product, and branding rules forbid presenting your product as Claude Code).
- **Unique element:** the subagent is a first-class *context-isolation* primitive — a non-fork subagent starts with an empty window, receives only the Agent tool's prompt string, and returns only its final message (scanned for instruction-shaped injection before the parent reads it), with depth/concurrency/spend caps (`CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH` default 3, `CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS` default 20) and a `Workflow` tool when orchestration outgrows the conversation.
- **Evidence:** https://code.claude.com/docs/en/agent-sdk/overview, https://code.claude.com/docs/en/agent-sdk/subagents, https://code.claude.com/docs/en/agent-sdk/sessions, https://code.claude.com/docs/en/agent-sdk/hooks, https://github.com/anthropics/claude-agent-sdk-python (PyPI `claude-agent-sdk` 0.2.156; npm `@anthropic-ai/claude-agent-sdk` 0.3.276)

### OpenAI Agents SDK — OpenAI, MIT, active
- **Category:** minimal multi-agent runtime (agents + handoffs + guardrails)
- **Focus / intended use:** production successor to Swarm; orchestrate LLM agents in plain Python/TypeScript with very few abstractions.
- **Inputs:** `Agent(instructions=..., tools=[...], handoffs=[...], output_type=..., guardrails=...)`; tools from the `@function_tool` decorator (signature + docstring → JSON schema via `function_schema.py`), hosted tools (`WebSearchTool`, `FileSearchTool`, `CodeInterpreterTool`, `ComputerTool`, `ApplyPatchTool`, shell), MCP servers, or `agent.as_tool()`.
- **Effects:** `Runner.run()` drives the loop against the Responses API (or Chat Completions / LiteLLM), emits traces to the OpenAI dashboard, and persists conversation items to a `Session`.
- **Model-facing arch:** one imperative loop in `src/agents/run.py` that repeatedly calls the model and classifies the result into `NextStepRunAgain` / `NextStepHandoff` / `NextStepFinalOutput` (`src/agents/run_internal/`), bounded by `max_turns`. No graph, no DSL — control flow is ordinary Python around `Runner.run`.
- **Memory / learning arch:** two independent layers. (1) `Session` protocol (`src/agents/memory/session.py`: `get_items`/`add_items`/`pop_item`/`clear_session`) with SQLite, async SQLite, SQLAlchemy, Redis, MongoDB, Dapr, encrypted and OpenAI-Conversations backends in `src/agents/extensions/memory/`, plus a compaction-aware session variant. (2) `RunState` (`src/agents/run_state.py`) — a versioned JSON snapshot (`CURRENT_SCHEMA_VERSION = "1.17"`) of an interrupted run including pending tool approvals, so human-in-the-loop `approve()`/`reject()` can resume across processes.
- **User-facing arch:** developer-facing only; built-in tracing UI on platform.openai.com, `src/agents/extensions/visualization.py` renders the agent graph, `repl.py` gives a local chat loop. Realtime and voice pipelines ship in-tree.
- **Unique element:** handoff-as-tool — `handoff()` (`src/agents/handoffs/__init__.py`) compiles a target agent into a `transfer_to_<name>` tool with an `input_filter` that rewrites the history handed over, so delegation *replaces* the running agent rather than nesting it (v0.22.3 adds a local/Docker `sandbox/` package and `apply_patch`/shell tools, pulling the SDK toward coding-harness territory).
- **Evidence:** https://openai.github.io/openai-agents-python/, repos/oa/src/agents/run.py, repos/oa/src/agents/handoffs/__init__.py, repos/oa/src/agents/run_state.py, repos/oa/src/agents/memory/session.py, repos/oa/src/agents/sandbox/

### OpenAI Responses API + Agents API — OpenAI, proprietary service, active
- **Category:** server-side agentic surface (not a library)
- **Focus / intended use:** Responses = one API call that runs a multi-tool loop server-side; Agents API (public beta 2026-09-10) = fully managed harness running OpenAI's Codex harness and sandbox.
- **Inputs:** a `POST /v1/responses` body with `input`, `tools` (hosted: `web_search`, `file_search`, `code_interpreter`, `computer_use`, shell/container, remote MCP; plus your function schemas) and either manual response chaining or a `Conversations` object; for the Agents API, an agent definition plus skills.
- **Effects:** the server executes tool calls, hosted containers and the loop, returning items; state lives in OpenAI's store.
- **Model-facing arch:** the Responses API is an agentic loop at the request level — the model may call several tools within one request — but OpenAI's own docs position it as "build an agent from scratch," with the built-in execution loop, hosted container workspace, context compaction and reusable skills belonging to the Agents API (docs; conflicting third-party reports place shell/container/compaction in Responses itself — treat the exact boundary as unclear).
- **Memory / learning arch:** server-managed: `Conversations` objects or `previous_response_id` chaining replace client-side history; the Agents API adds automatic context compaction and long-running sessions (docs).
- **User-facing arch:** REST + dashboard; ChatKit ships an embeddable chat UI. Agent Builder (visual canvas) and the Evals platform are being wound down — read-only 2026-10-31, removed 2026-11-30.
- **Unique element:** moves the harness itself behind the API boundary — the same Codex loop OpenAI runs for its own product, rented rather than implemented.
- **Evidence:** https://developers.openai.com/api/docs/guides/agents, https://openai.com/index/new-tools-and-features-in-the-responses-api/, https://openai.com/index/introducing-agentkit/, https://openai.com/index/the-next-evolution-of-the-agents-sdk/

### LangGraph — LangChain Inc., MIT, active (1.2.11, commits daily)
- **Category:** durable-execution graph runtime for agents
- **Focus / intended use:** express an agent as an explicit state machine whose every superstep is checkpointed, so runs can pause, resume, fork and be edited.
- **Inputs:** a `StateGraph` over a typed state schema with reducer-annotated channels (`Annotated[list, add_messages]`), node functions, conditional edges, plus a `checkpointer` and optional `store`; or the functional API (`@entrypoint`/`@task`).
- **Effects:** a `CompiledStateGraph` you `invoke`/`stream`; each superstep writes a checkpoint row to SQLite/Postgres/memory and emits stream events.
- **Model-facing arch:** Pregel-style bulk-synchronous execution (`libs/langgraph/langgraph/pregel/_loop.py`, `_algo.py`): nodes subscribe to channels, all nodes whose channels changed run in parallel each superstep, writes are applied by reducers. There is no built-in agent loop — the ReAct loop is just the two-node `agent ⇄ ToolNode` cycle in `libs/prebuilt/langgraph/prebuilt/chat_agent_executor.py`, and that prebuilt is now deprecated in favour of `langchain.agents.create_agent`.
- **Memory / learning arch:** short-term = `Checkpoint` TypedDict (`libs/checkpoint/langgraph/checkpoint/base/__init__.py`: `channel_values`, `channel_versions`, `versions_seen`, `updated_channels`) keyed by thread id, with `source: input|loop|update|fork` metadata, pending writes for crash recovery, and a `Durability = Literal["sync","async","exit"]` knob. Long-term = `BaseStore` (`libs/checkpoint/langgraph/store/base/`), a hierarchical-namespace key-value store with optional vector search, injected into nodes.
- **User-facing arch:** the developer writes graph topology, not prose; LangGraph Studio v2 (browser-based since early 2026, wired to LangSmith) renders the graph, rewinds to any checkpoint, edits state and forks a new path; `langgraph-cli` (`langgraph dev`) and LangGraph Platform/Server deploy the same graph behind an API with a JS/Python SDK.
- **Unique element:** `interrupt()` + `Command(resume=...)` — human-in-the-loop is implemented as a durable exception against the checkpointer, which is also what makes time-travel and branch-from-any-step fall out for free.
- **Evidence:** repos/lg/libs/checkpoint/langgraph/checkpoint/base/__init__.py, repos/lg/libs/langgraph/langgraph/pregel/_loop.py, repos/lg/libs/langgraph/langgraph/types.py (Interrupt, Command, Durability), repos/lg/libs/prebuilt/langgraph/prebuilt/chat_agent_executor.py:275 (deprecation), https://docs.langchain.com/oss/python/langgraph/overview

### deepagents — LangChain Inc., MIT, active (0.7.15, ~29.5k stars)
- **Category:** opinionated agent harness built on `langchain.agents.create_agent` (middleware stack)
- **Focus / intended use:** a batteries-included Claude-Code-shaped harness: planning, virtual filesystem, subagents, skills — without writing the graph.
- **Inputs:** `create_deep_agent(tools=..., instructions=..., subagents=[...], middleware=[...])`.
- **Effects:** returns a compiled LangGraph agent with the default middleware attached; runs on any LangGraph checkpointer/store.
- **Model-facing arch:** every capability is a middleware that injects tools + prompt text into a single `create_agent` loop — todo middleware adds `write_todos`, filesystem middleware adds `ls/read_file/write_file/edit_file`, subagent middleware adds the `task` tool. Context-management middlewares compress history, offload large tool results and set prompt-cache breakpoints.
- **Memory / learning arch:** inherits LangGraph checkpointing and `BaseStore`; the virtual filesystem lives in graph state (or a backing store), so scratch files are checkpointed like any other channel.
- **User-facing arch:** developer-facing Python/TS API; same LangGraph Studio/Platform tooling; a CLI and reference UI exist.
- **Unique element:** treats "harness features" as composable middleware over one loop rather than as graph nodes — the clearest statement of the layered stack LangGraph (runtime) → create_agent (loop) → deepagents (harness).
- **Evidence:** https://github.com/langchain-ai/deepagents, https://docs.langchain.com/oss/python/deepagents/overview, https://reference.langchain.com/python/deepagents/graph/create_deep_agent, https://www.langchain.com/blog/deep-agents-vs-langchain-vs-langgraph

### smolagents — Hugging Face, Apache-2.0, active but slower (1.26.0; last push 2026-08-25)
- **Category:** minimal code-as-action agent library
- **Focus / intended use:** agents whose action format is executable Python rather than JSON tool calls; ~1k-line core, model-agnostic.
- **Inputs:** `CodeAgent(tools=[...], model=..., managed_agents=[...], additional_authorized_imports=[...], executor_type=...)`; tools are `@tool`-decorated functions or `Tool` subclasses whose type hints/docstrings become the description injected into the prompt, plus MCP via `mcp_client.py`.
- **Effects:** runs a step loop that generates a code blob, executes it in a sandbox, and feeds observations back until `final_answer(...)` is called.
- **Model-facing arch:** `MultiStepAgent.run` → `_run_stream` loops `step()` until `is_final_answer` or `max_steps` (`src/smolagents/agents.py:544`). `CodeAgent` parses a Python snippet from the completion and executes it; `ToolCallingAgent` is the conventional JSON-tool-call variant. Tools, managed agents and authorized imports are rendered into the system prompt from Jinja templates in `src/smolagents/prompts/`; an optional periodic planning step is a separate prompt template.
- **Memory / learning arch:** `AgentMemory` (`src/smolagents/memory.py`) is an in-process list of typed steps (`SystemPromptStep`, `TaskStep`, `PlanningStep`, `ActionStep`, `FinalAnswerStep`), each with `to_messages(summary_mode=...)` — context is re-rendered from the step list every turn. No checkpointing; persistence is `agent.save()/push_to_hub()` of the *configuration*, not the run.
- **User-facing arch:** Python API plus a shipped `GradioUI` (`src/smolagents/gradio_ui.py`), a CLI, and Hub push/pull of agents and tools.
- **Unique element:** code-as-action — one Python blob can compose several tool calls, loops and conditionals per step, which collapses multi-step tool chains into one model call at the cost of needing a real sandbox (`LocalPythonExecutor` with an import allowlist, or E2B/Docker/Modal/Blaxel remote executors).
- **Evidence:** repos/sa/src/smolagents/agents.py (MultiStepAgent.run, CodeAgent, executor_type), repos/sa/src/smolagents/memory.py, repos/sa/src/smolagents/local_python_executor.py, repos/sa/src/smolagents/remote_executors.py, https://huggingface.co/docs/smolagents

### Google ADK (Agent Development Kit) — Google, Apache-2.0, active (python 2.9.1, 2026-09-15)
- **Category:** code-first agent framework + graph orchestration runtime, with a deployment path to Vertex AI / Cloud Run
- **Focus / intended use:** build agents in Python/Java/Go/TypeScript, run them locally, deploy them to Google Cloud.
- **Inputs:** `LlmAgent(instruction=, model=, tools=, sub_agents=)`; in 2.0, `WorkflowAgent(nodes=[...], edges=[("start","Classifier"), ("Classifier", handler, "urgent")])` with `JoinNode` for fan-in; tools are plain functions (schema inferred from type hints + docstrings), OpenAPI specs, MCP servers, or 100+ prebuilt integrations.
- **Effects:** an Event stream (events now carry `node_info`/`output` graph metadata); runs under `adk run`/`adk web` locally or Vertex AI Agent Engine in production.
- **Model-facing arch:** 2.0 replaced the 1.x agent hierarchy with a **Workflow Runtime** — agents, tools and plain functions are graph nodes with conditional edges, fan-out/fan-in, loops, retry (`RetryConfig`) and nested workflows; the old modes survive as "collaborative workflows" (LLM-driven `transfer_to_agent` delegation to `sub_agents`) and "template workflows" (`SequentialAgent`/`ParallelAgent`/`LoopAgent`). Context comes from `SessionService`'s event list plus instruction/state templating plus `MemoryService` retrieval. Note the 2.0 line is Python/Go/TS only — **adk-java is still on 1.x** (1.10.0, 2026-09-16).
- **Memory / learning arch:** `Session` (events + `state` dict with `app:`/`user:`/`temp:` key scoping) behind a pluggable `SessionService` (InMemory default — lost on restart — database, or Vertex AI); `MemoryService` is a separate searchable cross-session store (InMemory or Vertex AI Memory Bank). Java 1.10.0 adds `EventActions.agentState` for session-resumability checkpoints; full durable-execution semantics of the 2.0 graph runtime are unclear from public docs.
- **User-facing arch:** `adk web` dev UI (separate repo `google/adk-web`, :8000) with chat, event/trace inspection and eval; a Visual Builder at `/dev-ui/` that writes real project files; `adk run` CLI and a built-in eval framework.
- **Unique element:** deliberately moves orchestration out of the model — deterministic node/edge control flow for the routing steps, LLM only where judgment is needed.
- **Evidence:** https://github.com/google/adk-python, https://adk.dev/2.0/, https://adk.dev/workflows/, https://adk.dev/visual-builder/, https://github.com/google/adk-java/releases/tag/v1.10.0

### AWS Strands Agents — AWS, Apache-2.0, very active (python 1.56.0 / typescript 1.18.0, 2026-09-15)
- **Category:** in-process model-driven agent loop, explicitly a library rather than a platform
- **Focus / intended use:** add an agent to an existing service (FastAPI/Express/Next.js) with no control plane and no required database. Repo repositioned in 2026 from `strands-agents/sdk-python` to **`strands-agents/harness-sdk`** ("build an agent harness and control it end-to-end"), a Python+TS monorepo.
- **Inputs:** `Agent(system_prompt=, model=, tools=[...], session_manager=, conversation_manager=, plugins=[...])`; tools via the `@tool` decorator (spec generated from docstrings + type hints), class-based or module-based `TOOL_SPEC` tools, MCP clients; TypeScript uses `tool()` with Zod schemas.
- **Effects:** a recursive in-process loop against Bedrock/Anthropic/OpenAI/Gemini/Ollama/SageMaker/custom providers; sessions snapshotted to file/S3/AgentCore Memory; can expose or consume A2A.
- **Model-facing arch:** a single ReAct-style event loop — invoke model, validate and execute requested tools (async, concurrent), append results, invoke again, terminate on `end_turn`. No graph in the base case. Extension points are lifecycle **hooks** (before/after invocation, model call, tool execution) and a newer **Plugin protocol**; a pluggable `ConversationManager` (sliding window, summarization, stash/offload) does the context-window management.
- **Memory / learning arch:** `SnapshotSessionManager` writes point-in-time snapshots of messages, agent key-value state, conversation-manager state, system prompt and model config, auto-restoring from `snapshot_latest`; `FileSessionManager`/`S3SessionManager` keep a record per message (required for Graph/Swarm); `AgentCoreMemorySessionManager` wraps Bedrock AgentCore Memory. Documented sharp edge: no distributed locks, so concurrent writers to one session id are undetected.
- **User-facing arch:** developer-facing only — no first-party GUI playground (unclear whether one now exists). Ships `strands-agents/tools`, a Rust sandboxed `shell`, `evals`, `agent-sop` (natural-language SOPs) and OpenTelemetry tracing; 2026 work adds live transcript rendering, background tasks and context strategies.
- **Unique element:** four composable, nestable multi-agent topologies over the same loop — agents-as-tools (`.as_tool()`), **Swarm** (auto-injected handoff tool + shared memory), **Graph** (deterministic DAG whose nodes may be nested swarms/graphs) and **Workflow** (task dependencies) — with orchestration as a library pattern, never deployed infrastructure.
- **Evidence:** https://github.com/strands-agents/harness-sdk, https://strandsagents.com/docs/user-guide/concepts/agents/agent-loop/, https://strandsagents.com/docs/user-guide/concepts/agents/session-management/, https://strandsagents.com/docs/user-guide/concepts/multi-agent/swarm/, https://strandsagents.com/docs/user-guide/concepts/multi-agent/graph/

### Mastra — Mastra AI (YC W25), Apache-2.0 core + proprietary Enterprise License for `ee/` dirs, very active (monorepo tag @1.67.0, 2026-09-15)
- **Category:** full-stack TypeScript agent framework with durable graph workflows
- **Focus / intended use:** give Node/Next.js developers agents, workflows, memory, RAG, evals, a local server and a GUI in one dependency.
- **Inputs:** `new Agent({instructions, model, tools, agents})`, `createTool()` with explicit Zod input/output schemas, `createWorkflow().then().parallel().branch().dowhile().foreach().commit()`; also **file-based agents** where a directory of subagent directories becomes delegation tools.
- **Effects:** `mastra dev` serves a REST API + Swagger UI + Studio on :4111; deploys to the Mastra server or serverless targets; also runs as an MCP server.
- **Model-facing arch:** two explicit layers — the nondeterministic **agent** loop (tools, memory, subagent delegation) and the deterministic **workflow** graph, with typed steps, nested workflows and a discriminated result union (`success | failed | suspended | paused | tripwire`). Context = instructions + message history + working memory (as system messages) + semantic-recall hits + observational-memory reflections, trimmed by Memory Processors.
- **Memory / learning arch:** the strongest persistence story here. `suspend`/`resume` is a first-class workflow primitive (`.resumeStream()`, `.restart()` from the last active step), active runs auto-recover on server restart, and stored agents can enable durability at runtime without a redeploy. Memory is scoped `resourceId × threadId` with four kinds: message history, structured **working memory**, **observational memory** (a background agent compresses old messages into reflections), and **semantic recall** (embedding retrieval, optionally cross-thread), over LibSQL/Postgres/Upstash.
- **User-facing arch:** **Studio** (formerly Playground) at :4111 for interactive agent/workflow/tool testing and trace inspection, gaining a persistent Workflow Builder backed by an editor-owned agent in 1.67.0; plus CLI, auto-generated REST API and observability adapters.
- **Unique element:** subagent delegation with **memory isolation** — the subagent reads the full conversation for decision quality but only its own delegation prompt+response is written to its memory, each delegation getting a fresh `threadId` and deterministic `resourceId`, with `onDelegationStart`/`onDelegationComplete` hooks able to reject or rewrite a delegation. (The older `.network()` Agent Networks API is deprecated in favour of this supervisor pattern.)
- **Evidence:** https://github.com/mastra-ai/mastra, https://mastra.ai/docs/workflows/overview, https://mastra.ai/docs/subagents, https://mastra.ai/docs/agents/agent-memory, https://mastra.ai/docs/harness/agent-controller

### AutoGen — Microsoft, MIT (docs CC-BY-4.0), **stalled / maintenance mode**
- **Category:** event-driven actor runtime with conversational team presets
- **Focus / intended use:** multi-agent applications as message-passing systems; historically the research reference implementation. README now carries an explicit maintenance-mode banner and points new users at Microsoft Agent Framework; last release `python-v0.7.5`, 2025-09-30, nearly a year stale.
- **Inputs:** Python/.NET agent classes with typed `@message_handler` methods (core), or `AssistantAgent(tools=[plain python fns])` plus a team preset (agentchat).
- **Effects:** a running agent runtime that routes messages between agents, in-process or distributed.
- **Model-facing arch:** three layers — `autogen-core` is a genuine **actor model** (agents addressed directly or via topics/subscriptions; `SingleThreadedAgentRuntime` and a distributed host/worker/gateway runtime share one API, so topology is swappable without touching agent code); `autogen-agentchat` adds team presets (`RoundRobinGroupChat`, `SelectorGroupChat` where an LLM picks the next speaker, `Swarm` using `HandoffMessage`, `MagenticOneGroupChat`) with composable termination conditions; `autogen-ext` holds model clients.
- **Memory / learning arch:** teams are stateful and keep conversation history across runs until `reset()`; `save_state()`/`load_state()` serialize to a dict you persist yourself. No durable-execution engine.
- **User-facing arch:** AutoGen Studio (no-code GUI, FastAPI + Gatsby) and AutoGen Bench ship in-repo, both frozen with it.
- **Unique element:** the only mainstream framework that made the runtime a distributed actor system first and conversation patterns a thin preset layer on top — a bet that has now been carried into MAF rather than continued here.
- **Evidence:** https://github.com/microsoft/autogen (maintenance-mode banner), https://microsoft.github.io/autogen/stable/user-guide/core-user-guide/core-concepts/architecture.html, https://microsoft.github.io/autogen/stable/user-guide/agentchat-user-guide/tutorial/teams.html, https://pypi.org/project/autogen-agentchat/

### AG2 — ag2ai (AutoGen's original creators), Apache-2.0, very active (v1.0.5, 2026-09-11)
- **Category:** governed agent-coordination protocol runtime
- **Focus / intended use:** the live fork of AutoGen. **v1.0 (2026-07-27) is a ground-up rewrite** — the classic AutoGen-0.2-style API moved to a separate `ag2ai/ag2-classic` package, so "AG2 preserves the old API" is now only true of that package.
- **Inputs:** agents plus, optionally, a **Network**: a `Hub` and typed channels with a chosen adapter; agents also run standalone (the Network is opt-in).
- **Effects:** an async hub-and-spoke runtime with a write-ahead log, audit trail and replayable channel transcripts; transport swaps from `LocalLink` (same process) to `WsLink` (separate processes/hosts) without changing agent code.
- **Model-facing arch:** coordination is a bounded, typed **channel** with a lifecycle (`INVITED → ACTIVE → CLOSED`) governed by an adapter that declares allowed sends, view policy and termination rules. Four built-in adapters replace the old `GroupChat`: Conversation (2-party free-form), Consulting (2-party strict Q/R), Discussion (N-party round-robin), Workflow (declarative `TransitionGraph`). Agents carry a `Passport` (identity) and a `Resume` (capability claims + observed track record).
- **Memory / learning arch:** durable messaging via the hub's write-ahead log with replayable transcripts plus a hub-side audit log; session resume shipped in recent releases. Per-run conversational memory otherwise conventional.
- **User-facing arch:** a browser-based **AG2 Playground** and a "Build with AG2" examples repo. An "AG2 Studio" is referenced only by third-party blogs — no package, no official docs page; treat as nonexistent.
- **Unique element:** treats multi-agent coordination as a governance problem — identity, typed channels, per-agent access/rate/inbox limits, prompt-injection policies, and an auditable WAL — rather than as prompt orchestration; heavy 2026 protocol-interop work (MCP 2.0 client *and* server, MCP Apps, A2A with signed agent cards, ACP in both directions).
- **Evidence:** https://github.com/ag2ai/ag2, https://docs.ag2.ai/docs/user-guide/network/overview/, https://github.com/ag2ai/ag2-classic, https://api.github.com/repos/ag2ai/ag2/releases

### Microsoft Agent Framework (MAF) — Microsoft, MIT, active (1.0 GA 2026-04-03; dotnet-1.21.0 / python-1.18.0, Sept 2026)
- **Category:** typed, checkpointed workflow-graph framework with agents as nodes
- **Focus / intended use:** the declared successor to *both* AutoGen and Semantic Kernel — enterprise multi-agent orchestration for .NET, Python and (preview) Go.
- **Inputs:** `AIAgent` definitions (model, tools, hosted MCP servers) and a `WorkflowBuilder` graph of typed **executors** joined by conditional edges; Python also has an experimental functional API (`@workflow`/`@step`) using native control flow.
- **Effects:** a workflow run emitting per-executor events, with checkpoints at superstep boundaries; deployable against Microsoft Foundry, Azure OpenAI, OpenAI, Anthropic, Ollama and others.
- **Model-facing arch:** Pregel-style **superstep execution** over a typed graph, with fan-out/fan-in, `RequestInfoExecutor` for human-in-the-loop, and shipped orchestration patterns (sequential, concurrent, handoff, group collaboration, magentic). Graphs and agents are mutually nestable: sub-workflows are executors, and `.as_agent()` / `AsAIAgent()` wraps a whole workflow back into the agent interface.
- **Memory / learning arch:** a session object for agent state, context providers for memory, middleware for intercepting agent actions, and workflow state that is either run-scoped or durable, with checkpointing at superstep boundaries and time-travel debugging (docs).
- **User-facing arch:** **DevUI**, an interactive developer UI for building, testing and debugging workflows; OpenTelemetry telemetry inherited from Semantic Kernel.
- **Unique element:** the **Harness Agent** — an opinionated batteries-included agent for long multi-step tasks with planning/todo tracking, context compaction, file access and memory, persistent ("don't ask again") tool approval and observability; Microsoft shipping the Claude-Code-shaped harness as a first-class SDK object.
- **Evidence:** https://learn.microsoft.com/en-us/agent-framework/overview/, https://learn.microsoft.com/en-us/agent-framework/concepts/workflows/, https://devblogs.microsoft.com/agent-framework/microsoft-agent-framework-version-1-0/, https://github.com/microsoft/agent-framework

### Semantic Kernel — Microsoft, MIT, maintained but superseded (dotnet-1.80.1 2026-09-03, python-1.44.1 2026-08-06)
- **Category:** previous-generation kernel/plugins/connectors SDK
- **Focus / intended use:** still distinct from MAF as a codebase and package line, and still shipping feature-bearing releases — but Microsoft's documented position is that MAF is the successor, that "the majority of new features will be built for Microsoft Agent Framework", and that SK v1.x is supported for at least one year past MAF GA (i.e. through at least 2027-04).
- **Inputs:** a `Kernel` with plugins (native functions + prompt templates), planners and connectors.
- **Effects:** function/plugin invocation with telemetry; agent abstractions layered on later.
- **Model-facing arch:** plugin-and-planner model rather than an agent loop or a graph — the kernel resolves and invokes functions, with prompt templates as first-class assets. Migration guides exist from SK to MAF.
- **Memory / learning arch:** memory connectors and vector-store abstractions; no checkpointing or durable execution comparable to MAF workflows.
- **User-facing arch:** SDK only (.NET/Python/Java); its OpenTelemetry instrumentation is what MAF inherited.
- **Unique element:** included here only to record that it is *not* dead — but there is no reason to start new work on it rather than MAF.
- **Evidence:** https://devblogs.microsoft.com/agent-framework/semantic-kernel-and-microsoft-agent-framework/, https://learn.microsoft.com/en-us/agent-framework/migration-guide/from-semantic-kernel/, https://github.com/microsoft/semantic-kernel, https://api.github.com/repos/microsoft/semantic-kernel/releases

### CrewAI — CrewAI Inc., MIT (framework; AMP platform commercial), very active (1.15.22, 2026-09-16)
- **Category:** role-playing agent crews plus event-driven flows
- **Focus / intended use:** model a team — agents with role/goal/backstory, tasks with expected outputs — and let an LLM manager coordinate. Standalone since the LangChain dependency was dropped. (The README's `v0.102.0` install snippet is stale; the live line is 1.x.)
- **Inputs:** `agents.yaml` / `tasks.yaml` plus a `@CrewBase` Python class; tools are `BaseTool` subclasses or `@tool`-decorated functions; Flows are written as decorated methods.
- **Effects:** `crew.kickoff(inputs=...)` returns a `CrewOutput` with aggregated usage metrics; Flows drive Crews and persist state to SQLite.
- **Model-facing arch:** two layers. **Crews** run tasks under a `sequential` process (outputs chain as context) or a `hierarchical` one, where CrewAI auto-assigns a manager agent (`manager_llm` or a custom `manager_agent`) and injects `Delegate work to coworker` / `Ask question to coworker` tools into agents with `allow_delegation=True`. **Flows** are the deterministic outer layer: `@start()`, `@listen()`, `@router()` with `or_()`/`and_()` combinators, calling Crews inside flow methods.
- **Memory / learning arch:** `@persist` on a Flow gives crash/restart recovery over `SQLiteFlowPersistence`, with **resume** (`id=<uuid>`) and **fork** (`restore_from_state_id=<uuid>`) hydration modes over dict or Pydantic-typed state. Memory was consolidated into a single `Memory` class replacing the old short-term/long-term/entity/external taxonomy — default store **LanceDB under `./.crewai/memory`**, pluggable via a `StorageBackend` protocol, with `self.remember()`/`self.recall()`/`self.extract_memories()` available on every Flow. (Most third-party write-ups still describe the retired taxonomy.)
- **User-facing arch:** `crewai create crew` / `install` / `run` CLI; **CrewAI AMP** (formerly CrewAI Enterprise) is the commercial control plane with Crew Studio visual editor, managed deployment, observability and governance — its feature claims are vendor marketing, unverified.
- **Unique element:** anthropomorphic team modeling as the primary abstraction — you write the org chart, not the graph — with Flows added as the escape hatch when that autonomy proves too loose.
- **Evidence:** https://github.com/crewAIInc/crewAI, https://docs.crewai.com/en/concepts/flows, https://docs.crewai.com/en/concepts/memory, https://api.github.com/repos/crewAIInc/crewAI/releases

### Pydantic AI — Pydantic Services Inc., MIT, very active (v2.45.0, 2026-09-18; v1.107.x still patched)
- **Category:** type-safe single-loop agent SDK
- **Focus / intended use:** "FastAPI feeling" for GenAI — typed dependencies, typed outputs, validated tool schemas, model-agnostic.
- **Inputs:** `Agent(model, deps_type=, output_type=, instructions=)` plus `@agent.tool` functions whose signatures and docstrings become JSON schemas; v2 adds **capabilities** as the composition unit.
- **Effects:** `run()` / `run_sync()` / `run_stream()` returning a typed validated result; the same agent can be served as CLI, web chat, realtime voice, AG-UI, or headless.
- **Model-facing arch:** one typed agent loop (model call → tool calls → validate output → repeat), deliberately not graph-first; internally it is a `pydantic-graph` state machine you can step through with `agent.iter()`. v2's **capability** primitive bundles instructions, tools, lifecycle hooks and model settings into one composable unit that can rewrite what the model sees mid-run — built on the same public hooks API that Code Mode, Tool Search, MCP and Logfire use, so the core stays small. v2 also defaults OpenAI model names to the Responses API.
- **Memory / learning arch:** conversation state is an explicit `message_history` of Pydantic models that you store — no hidden session store. Durability is delegated to real workflow engines: **Temporal, DBOS, Prefect, Restate and AWS Lambda durable functions** are co-maintained, turning model calls and tool calls into engine activities. The docs draw the sharpest line in this survey: *"durability is not storage"* — keeping one run alive across crashes is a different problem from persisting threads across sessions.
- **User-facing arch:** **Pydantic Logfire** (OpenTelemetry-native instrumentation, commercial SaaS but OTel-compatible), **Pydantic Evals** for behavior testing, a `clai` CLI, MCP client+server and AG-UI support.
- **Unique element:** the loop stays small and everything else is a capability or an external engine — including a separate **Pydantic AI Harness** library (memory, guardrails, context management, filesystem, Code Mode, `SubAgents` delegation, prebuilt Coder/Researcher agents) versioned apart from the core so the core can stay stable.
- **Evidence:** https://github.com/pydantic/pydantic-ai, https://pydantic.dev/articles/pydantic-ai-v2, https://pydantic.dev/docs/ai/integrations/durable_execution/overview/, https://pydantic.dev/docs/ai/guides/multi-agent-applications/, https://pydantic.dev/docs/ai/harness/subagents/

## Protocols

**MCP (Model Context Protocol)** — Anthropic-originated, now multi-vendor; current revision **2026-07-28**. Standardizes how a host/agent discovers and calls *tools, resources and prompts* exposed by an external server over JSON-RPC (stdio or HTTP), plus sampling and elicitation callbacks the other way. The 2026-07-28 revision removes protocol-level session tracking (identity/capabilities move into a per-request `_meta`), adds multi-round-trip requests, header-based routing, cacheable list results and a formal extensions framework — architecturally it pushes MCP toward a stateless, cacheable, load-balancer-friendly web protocol, which means harnesses can no longer assume a sticky server connection. MCP Apps (SEP-1865) lets a server ship sandboxed HTML UI for a tool. Evidence: https://modelcontextprotocol.io/specification/2026-07-28, https://blog.modelcontextprotocol.io/posts/2026-07-28/

**A2A (Agent2Agent)** — Google-originated, donated to the Linux Foundation June 2025 and a hosted project of the Agentic AI Foundation alongside MCP since 2026-08-17; **v1.0.0 spec 2026-03-12**, v1.0.1 2026-05-28; 150+ participating orgs. Standardizes agent-to-agent delegation: a signed `AgentCard` at `/.well-known/agent-card.json` advertising skills, endpoints, transports and auth schemes; a `Task` with an eight-state lifecycle (`submitted`, `working`, `input_required`, `auth_required`, `completed`, `failed`, `canceled`, `rejected`); and `Message`/`Part`/`Artifact` payloads over three functionally equivalent bindings (JSON-RPC 2.0, gRPC, HTTP+JSON) with polling, SSE streaming or webhook push for long jobs. Architecturally it is the peer-to-peer complement to MCP's client-server tool axis: remote agents are opaque services, so delegation is a task with a lifecycle you poll or subscribe to rather than a function you call — which also means multi-agent topology *inside* a framework and agent federation *across* frameworks become separate concerns. Evidence: https://a2a-protocol.org/latest/specification/, https://github.com/a2aproject/A2A/releases, https://a2a-protocol.org/latest/blog/2026/08/27/a-new-chapter-for-a2a-joining-the-agentic-ai-foundation/

**ACP (Agent Client Protocol)** — Zed Industries, Apache-2.0, adopted by JetBrains, Google and GitHub; registry launched 2026-01-28. Standardizes the *editor ⇄ agent* boundary the way LSP standardized editor ⇄ language server: the editor spawns the agent as a subprocess and speaks JSON-RPC 2.0 over stdio, covering session setup with workspace context, streamed assistant output, tool-call display, permission requests and file edits routed back through the editor's buffers. Architecturally it separates the harness (agent process) from the UI (editor), turning an N×M integration problem into N+M and making "which agent" a runtime choice. Evidence: https://agentclientprotocol.com/, https://github.com/agentclientprotocol/agent-client-protocol, https://zed.dev/blog/acp-registry

**AGENTS.md / CLAUDE.md** — community convention, stewarded by the Agentic AI Foundation (Linux Foundation) since 2025-12-09; 60k+ repos. Standardizes *where* repo-specific operating instructions live: a plain Markdown file at the repo root (and nested, closest-file-wins) giving build/test/style/convention guidance, with no required schema — the agent just reads the prose. Read natively by Claude Code, Codex CLI, Cursor, Aider, Copilot, Gemini CLI, Windsurf and Amazon Q; CLAUDE.md is Anthropic's parallel file, and most harnesses now read both. Architecturally it makes project context a versioned repo artifact rather than harness configuration, so it survives tool switches and code review. Evidence: https://agents.md/, https://www.morphllm.com/agents-md-guide, https://developers.redhat.com/articles/2026/07/27/standardize-project-context-agentsmd-and-agent-skills

**Agent Skills (SKILL.md)** — Anthropic, released December 2025, since adopted by Codex CLI, Microsoft Agent Framework, Cursor and Copilot. Standardizes a capability as a *directory*: `SKILL.md` with YAML frontmatter (`name`, `description`) plus optional scripts, reference docs and assets. The bet is progressive disclosure — only name+description (~100 tokens each) sit in context at startup; the body (recommended <5k tokens) is loaded on match via a skill tool; referenced files and bundled scripts are read or executed only if the instructions call for them. Architecturally it makes capability breadth independent of context length and turns procedural knowledge into a filesystem artifact an agent can author and version — at the cost of a real supply-chain surface, since a skill can carry executable code and the registry layer is largely ungoverned. Evidence: https://code.claude.com/docs/en/agent-sdk/skills, https://www.firecrawl.dev/blog/agent-skills, https://learn.microsoft.com/en-us/agent-framework/agents/skills, https://anomity.ai/blog/agent-skills-open-standard-adoption-governance/
