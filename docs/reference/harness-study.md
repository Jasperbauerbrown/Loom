> **Reference copy.** Copied from the Hydra repository on 2026-09-20 for Loom's design work. File paths and code references inside refer to Hydra's codebase, not Loom's; they are kept verbatim as evidence.

# Harness Study — What Other Agent Harnesses Do, and What Hydra Should Take

*2026-08. A comparison of four successful agent harnesses against Hydra's own
(runtime/turn.py, sessions/context.py + compaction.py, tools/, scheduler/),
looking for reliability, context-management, long-horizon, and general
functionality gaps. Companion to docs/implementation/09-context-lifecycle.md.*

Sources: [VILA-Lab/Dive-into-Claude-Code](https://github.com/VILA-Lab/Dive-into-Claude-Code),
[shareAI-lab/learn-claude-code](https://github.com/shareAI-lab/learn-claude-code),
[opencode docs](https://opencode.ai/docs/agents/),
[OpenClaw heartbeat docs](https://docs.openclaw.ai/gateway/heartbeat),
[OpenClaw architecture write-up](https://bibek-poudel.medium.com/how-openclaw-works-understanding-ai-agents-through-a-real-architecture-5d59cc7a4764),
[Hermes Agent deep dive](https://dev.to/truongpx396/hermes-agent-deep-dive-build-your-own-guide-1pcc).

---

## 1. The harnesses

### Claude Code (Anthropic; analyzed via VILA-Lab + shareAI-lab studies)

- **Loop**: single ReAct while-loop; ~9-step per-turn pipeline (settings →
  state → context assembly → pre-model shapers → model → permission gate →
  tool execution → stop check). Analysis finding: only ~1.6% of the codebase
  is "AI decision logic"; 98.4% is deterministic infrastructure.
- **Reliability**: max-output-token escalation with retries; prompt-too-long
  fallback model; streaming fallback; reactive compaction once per turn; tools
  classified *concurrent-safe vs exclusive* and safe ones executed in parallel.
- **Context**: five compaction shapers ordered by cost (budget reduction →
  snip → microcompact → non-destructive read-time collapse → full model
  summarization). Large tool outputs offload to the filesystem. Append-only
  JSONL transcripts with read-time chain patching — nothing destructively
  edited.
- **Long-horizon**: TodoWrite plan tracking ("doubles completion rates" per
  the shareAI study); file-backed task graphs with `blockedBy` dependencies;
  background task execution with an async notification queue; scheduled
  automation; goal-driven continuation (an independent evaluator gates loop
  termination).
- **Functionality**: 7 permission modes, deny-first rules, hook system (27
  events: PreToolUse/PostToolUse/etc., shell or LLM-evaluated); subagents with
  isolated contexts returning only summaries; skills with progressive
  disclosure; MCP.

### opencode (sst)

- Primary agents (Build/Plan) vs subagents (General/Explore/Scout) — subagents
  inherit the invoking agent's model unless overridden; child sessions are
  first-class and navigable from the UI (parent ↔ children keybinds).
- Per-agent permission keys with three states (allow/ask/deny) and
  **per-argument pattern rules** (`"bash": {"git push": "ask", "grep *": "allow"}`).
- Hidden system "Compaction agent" summarizes on overflow; tool-output pruning;
  a revert/undo system for messages.

### OpenClaw (ex-Clawdbot/Moltbot)

- Central gateway process; strict **one-command-at-a-time serialization per
  session** (a command queue) to prevent tool conflicts.
- **Heartbeat**: a scheduled main-session agent turn (default 30 min) driven by
  a persistent "scratch" checklist; the agent replies `HEARTBEAT_OK` when
  nothing needs attention (suppressed from delivery). Options: fresh isolated
  session, or `lightContext` skipping bootstrap files to cut token cost.
- Memory = plain markdown (`MEMORY.md`, `SOUL.md`, append-only daily logs),
  retrieved on demand; optional sqlite-vec embedding search; compaction
  summarizes older turns.

### Hermes Agent (Nous Research)

- **Sessions as infrastructure**: SQLite WAL + FTS5 full-text over all prior
  turns; resumable with automatic recap.
- **Frozen-snapshot pattern**: `MEMORY.md`/`USER.md` embedded immutably in the
  system prompt at session start so mid-session writes never invalidate the
  prompt cache.
- **Iteration budgeting shared across parent and subagents**, with *refunds*
  for cheap operations; exhaustion → one warning injection → one grace call →
  forced summarization.
- Tool **registration vs exposure** separated: one self-registering registry;
  availability predicates filter what each context sees; toolsets
  enable/disable as groups; tool errors always wrapped as tool results.
- Provider abstraction routes chat-completions / Anthropic messages / Codex
  responses / Bedrock converse with no provider branches in the core loop.

---

## 2. Where Hydra already has parity (or better)

Grounded in the current code — these need no action:

| Mechanism | Hydra's version |
|---|---|
| Cost-ordered compaction ladder | Reducer ladder (`sessions/compaction.py::ensure_fits`): evict-to-stubs (no LLM) → summarize-fold → fail loudly, with anti-thrash blocking after two ineffective folds |
| Token estimation that self-corrects | Per-call real-usage calibration (`record_prompt_reading`), overflow-driven upward bump, preset-level seed for new sessions — none of the four studied harnesses calibrate from real usage |
| Tool-output offloading | Spill-to-artifact with head+tail preview + `tool_recall` (`sessions/artifacts.py`, doc 09) — same shape as Claude Code's filesystem offload, plus recallability after eviction |
| Append-only replayable transcript | JSONL with eviction/compaction events applied at read time (`live_events`) — same philosophy as Claude Code's chain patching |
| Plan/todo continuity | `todo` transcript events, open items re-injected every assembly (slot 7.5) — equivalent to TodoWrite's reminder loop |
| Prompt-cache-friendly prefix | Byte-stable slots [1]–[5] — same instinct as Hermes' frozen snapshot |
| Typed model outcomes + retry | OK/transient/overflow/fatal classification in the gateway; bounded transient retries; overflow → recalibrate + reduce + retry (`runtime/turn.py`) |
| Heartbeat / scheduled cognition | `agent_turn` cron + event-triggered jobs → `wake_agent` → the one `run_turn` entrypoint (`scheduler/jobs.py`) — OpenClaw's heartbeat is a special case of this |
| Permission spectrum | Two-layer fail-closed (missing row = deny) + per-argument layer 2 + ownership-chain escalation — deny-first like Claude Code |
| Subagent isolation | `agent_request` runs the task in a fresh session on the target and returns only the final text; async mode notifies on completion *(superseded by the 2026-08-22 comms redesign: `agent_send` messages a persistent thread session; the isolation-and-final-text shape is unchanged)* |
| Memory flush before fold | `_memory_flush` (doc 09) — none of the studied harnesses distill to durable memory *before* discarding detail; this is ahead of the field |

## 3. Gaps and recommendations (prioritized)

### P1 — Malformed tool-call arguments executed silently ✅ implemented

`runtime/turn.py::_run_tool_call` coerced unparseable/non-dict JSON arguments
to `{}` and **executed the tool anyway**. Every studied harness returns tool
errors *to the model* so it can self-correct (Hermes: "errors wrapped as tool
results, never crash the loop"). Executing `memory_write`/`fs_write` with
empty arguments on a local model's malformed JSON is a silent-corruption path.
**Change**: return an error tool result naming the parse failure, without
executing. Minimal, model-agnostic (a frontier model gets the same contract).

*Landed*: `runtime/turn.py::_run_tool_call` returns "invalid arguments for
&lt;tool&gt;: … resend the call…" as an error result; test in
`tests/test_tools_phase2.py::test_malformed_tool_arguments_return_error_result`.

### P2 — No shared budget across a delegation tree (effort M, impact M-H) *(superseded by the 2026-08-22 comms redesign: delegations no longer exist, and the user explicitly declined depth/budget machinery for the thread/message model)*

Hermes shares one iteration budget across parent + subagents (with refunds).
Hydra's `agent_request` chains are bounded only by per-turn step caps and the
300 s timeout — an agent delegating to an agent delegating onward multiplies
work with no aggregate brake, and a cycle of mutual delegation is bounded only
by timeouts. **Proposal**: carry a `depth`/`budget` field on delegations
(`core/coordination.py`); refuse past a configurable depth (default ~3) with a
clear error. Small schema addition, no new codepath in the loop.

### P3 — Loop-level hooks (effort M, impact M)

Claude Code's biggest extensibility lever is hooks around the loop (PreToolUse
/ PostToolUse / etc.); Hydra's event bus is observe-only. A minimal version —
an optional pre-tool-execution hook point that can veto or annotate a call,
configured per-deployment — would let users add policy (audit, redaction,
site-specific guards) without forking the loop. Should reuse the existing
approvals broker shape rather than a new mechanism.

### P4 — Parallel execution of concurrent-safe tool calls ✅ implemented

`turn.py` executes multi-tool steps strictly sequentially. Claude Code
classifies tools concurrent-safe vs exclusive and gathers the safe ones.
On local models latency per step is the dominant cost, so this matters less
than for API models — but read-heavy steps (fs_read ×3) would still halve.
**Proposal**: a `concurrency_safe: bool` on tool registration; in the loop,
`asyncio.gather` maximal safe runs, preserving result order. Care: approval
elicitation inside parallel calls must serialize.

### P5 — Interrupted-turn recap on next turn ✅ implemented

Persist-before-act means a crash loses at most one in-flight step, but the
next turn silently resumes with a dangling `tool_call` (no paired result) in
the buffer — some providers reject that sequence, and the model gets no signal
that work was cut short. Hermes recaps on resume; Claude Code chain-patches.
**Proposal**: `live_events` (or assembly) detects a trailing unpaired
tool_call and appends a synthetic error tool_result ("interrupted — result
lost"), which both fixes the API sequence and tells the model what happened.

*Landed*: slot-[8] replay in `sessions/context.py` now synthesizes a tool
result (`prompts/context/interrupted-tool-result.md`) for each unpaired call;
test in `tests/test_turn.py::test_dangling_tool_call_repaired_at_replay`.

### P6 — Per-command exec permission patterns (effort M, impact M)

opencode's `"bash": {"git push": "ask", "grep *": "allow"}` is finer than
Hydra's layer-2 rules for `exec_run`. If layer 2 for exec is currently
workspace-scoped only, command-pattern rules (glob on the command line,
allow/approval per pattern) would fit the existing two-layer model cleanly and
reduce approval fatigue without loosening the gate.

### P7 — Preset fallback chain (effort S, impact S-M) ✅ implemented

Claude Code falls back to a secondary model on prompt-too-long and stream
failures; LiteLLM natively supports fallbacks. Hydra already has
`summarizer_preset`; a symmetric optional `fallback_preset` profile knob —
tried once when the primary preset's call ends FATAL — would make single-box
Ollama setups resilient to one model being unloaded/broken. (Not a small-model
crutch: it's provider redundancy, orthogonal to capability.)

### P8 — Minor reliability polish (report-only)

- **Empty completions**: an OK outcome with empty text and no tool calls
  persists an empty assistant event silently; Claude Code retries once.
  Cheap to add under the existing transient-retry budget if it shows up in
  real testing.
- **No-progress detection**: Claude Code stalls out identical repeated tool
  calls. Deliberately *not* recommended for Hydra yet — it borders on a
  small-model crutch; the step cap already bounds the damage.

### UI notes (for the graph/session work, not executed here)

- opencode makes **child sessions navigable from the parent** (leader+↓/←/→).
  Hydra records delegations and draws live delegation edges; the Chat view
  could surface a delegation's child session as a clickable link in the
  transcript, and the session sidebar could nest delegation sessions under
  their initiating session. *(Largely landed via the 2026-08-22 comms
  redesign: `agent_send` results carry a thread deep-link, and the graph
  draws live thread edges.)*
- The graph's "start focused, expand on demand" already matches the research
  consensus; the main mess is density — consider collapsing category hubs
  whose child count is zero and a "recent activity only" filter as the default
  lens.

## 4. Suggested sequencing

1. **P1**, **P5** — landed with this study (see commits).
2. **P2** — small, pure-reliability, testable with the existing FakeGateway
   harness.
3. **P7** — one profile knob + one gateway retry branch.
4. **P4/P3/P6** — worth doing when there's real-usage evidence (Thread-1
   testing) showing latency pain, policy need, or approval fatigue
   respectively.
