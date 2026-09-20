> **Reference copy.** Copied from the Hydra repository on 2026-09-20 for Loom's design work. File paths and code references inside refer to Hydra's codebase, not Loom's; they are kept verbatim as evidence.

# Everything Context: how a Hydra agent's window is built, spent, and reclaimed

A reader's guide to the full context lifecycle — what enters the model's prompt
each turn, in what order, what it costs, what gets cut, and every knob that
changes the behavior. Written against the code as of 2026-07-14; source
references point at the files that implement each piece. The formal specs are
`docs/implementation/05-sessions-context.md` (context/compaction), `01` (turn
loop), `02` (tools), and `07` (presets/budgets); this document is the connected
walkthrough.

The one-sentence version: **Hydra never keeps a running chat buffer — every
turn, the prompt is rebuilt from scratch out of durable state** (the transcript
file, the session row, the vaults, the preset), and everything context-related
follows from that.

---

## 1. The durable state everything is built from

| Store | What lives there | Context role |
|---|---|---|
| `HYDRA_HOME/sessions/<id>.jsonl` | The transcript: one JSON event per line, append-only, fsync'd | The system-of-record for conversation history. **Nothing here is ever mutated or deleted** — compaction and truncation only change what *re-enters the prompt*, never what's on disk. |
| `sessions` row (SQLite) | `summary` (the recursive compaction summary), `token_factor` (occupancy calibration), preset override | Slot [7] + the occupancy math |
| `agents` row | `system_prompt` (identity) | Slot [2] |
| Markdown vaults (`HYDRA_HOME/vaults/`) | Long-term memory notes | Slots [3], [6], [9] |
| `presets` row | `params` (max_tokens, temperature, …) + `profile` (effective_ctx, compact_threshold, …) | The budget every decision is made against |

Transcript event kinds (`sessions/transcript.py`): `user`, `assistant`,
`tool_call`, `tool_result`, `thinking`, `system_event`, `compaction`. Only the
first four are "conversational" — the last three **never** re-enter a prompt
and **never** count toward occupancy.

Crash safety is a context property too: the runtime appends to the transcript
*before* acting on anything (persist-before-act), so a crash loses at most one
in-flight step, and a torn final line from a mid-write crash is skipped on read
rather than corrupting the session.

---

## 2. Turn-to-turn: the full walk of `run_turn`

Everything below is `runtime/turn.py::run_turn` — the *only* agent-loop
entrypoint (chat, agent threads and message wake-ups, and scheduled wake-ups
all funnel here).

### 2.1 Before the first model call

1. **Load + resolve.** Session row, agent row, and the layered preset
   (`agent default < session override < task < call` — most specific wins,
   `params`/`profile` merge key-wise).
2. **Tools.** Layer-1 grants decide which tools the agent can *see*. Their
   JSON schemas go to the model through the API's `tools` parameter (that costs
   window space too, even though Hydra doesn't render it as text), and a
   human-readable listing goes into the system prompt (slot [4]).
3. **Skills.** Discovered skill folders contribute one metadata line each
   (slot [5]). Skill *bodies* are not in context — the agent pulls one in by
   calling `skill_load`, and the body arrives as an ordinary `tool_result`
   (so it's subject to everything in §4).
4. **Inbox messages** (comms redesign 2026-08-22 — this replaced the old
   `agent_notify` mailbox and its slot [9.5]). The session's undelivered
   `session_messages` rows — agent thread messages, thread replies, queued
   operator messages — are *claimed* and enter as ordinary user-role input
   (agent-sourced ones with a one-line sender header); the claim repeats
   mid-turn between tool steps, and rows are marked delivered only on turn
   success (a crashed turn's messages redeliver).
5. **Context assembly** (§3) happens *before* the new user input is appended
   to the transcript — the input is passed separately and placed last, so it
   isn't duplicated. Then the `user` event is persisted.

### 2.2 The tool loop

Each iteration ("step") is one model call:

1. **Stream.** The gateway streams two channels — visible `content` and
   `reasoning`. Reasoning arrives either as the provider's structured field or
   as inline `<think>…</think>` that the gateway's splitter re-routes
   (`models/litellm_gateway.py`), so downstream code always sees clean
   separation.
2. **Persist reasoning first.** The step's reasoning is written as a
   `thinking` event *before* the step's own event, so the transcript reads
   "thought, then acted."
3. **No tool calls?** Persist the `assistant` event, turn over.
4. **Tool calls?** Persist a `tool_call` event; run each call through the
   permission gate (missing grant = deny; `approval` pauses the turn on a
   human/ancestor decision); persist each `tool_result` event; feed the results
   back; go to 1.

Per-step context growth *within* the turn (the in-flight message list):

- the step's visible text + its tool-call JSON,
- **reasoning replay**: the step's reasoning is fed back into *this turn's*
  subsequent steps, head-capped at `reasoning_replay_max_chars` (default
  4,000 — the plan lives at the front of a reasoning block; the tail is
  execution chatter). Without this, thinking models re-derive their plan from
  tool results at every step. `profile.reasoning_replay: false` disables it.
  litellm has no request field for reasoning, so it's rendered back as a
  `<think>` block inside the message content.
- every tool result, as truncated at capture (§4).

Brakes on the loop, in the order they can fire:

- **Model-error retry** (`model_error_retries`, default 1): a stochastic
  failure (malformed tool-call XML, connection blip) rerolls the identical
  request; the failed call doesn't consume a step.
- **Empty-`length` recovery**: a `finish_reason="length"` with *zero* text and
  no tool calls means the prompt filled the whole window and generation had no
  room — Ollama truncates input silently, so this is the only observable
  symptom. Hydra force-compacts (§5.3), rebuilds the prompt, and retries.
  If the forced-compaction budget is spent, the turn ends with a *visible*
  "context window exhausted" error — never a silent empty message.
- **Proactive in-flight brake**: after each step's tool results land, the
  runtime estimates the in-flight prompt (chars/4 × calibration factor, over
  content + replayed reasoning + tool-call JSON). If it crosses the compaction
  limit, it force-compacts and rebuilds *before* the next model call. This is
  what stops a 17-step tool turn from outrunning between-turn compaction.
- **Step cap** (`max_tool_steps`, default 25): one final model call with tools
  disabled to let the agent summarize, then the turn returns.

Both compaction-flavored brakes share one budget:
`forced_compactions_per_turn` (default 2).

### 2.3 After the turn

Only on clean turns (not cancelled, no error):

1. **Token calibration** (§6): if the provider reported `prompt_tokens`,
   recalibrate the session's `token_factor` and write the seed back to the
   preset.
2. **Between-turns compaction** (§5): `maybe_compact` checks occupancy against
   the budget and folds if needed. The response has already streamed — this
   never blocks anything the user is waiting on.

---

## 3. What's actually in the prompt: the ten-slot layout

`sessions/context.py::assemble_context` is the *sole* context builder. The
layout (numbers are the doc-05 slot names):

```
[1] SYSTEM PROMPT      Hydra conventions + how the memory system works
[2] IDENTITY           <agent_identity> agents.system_prompt </agent_identity>
[3] CORE MEMORY        <memory_block label="agent|global|task"> — pinned vault
                       notes, hard char caps (1500/1200/1000) so overflow is
                       impossible by construction
[4] TOOLS              <available_tools> name+description listing, [approval]
                       markers (schemas travel via the tools API param)
[5] SKILLS             <available_skills> name + description per skill
[5.5] WORKSPACES       <workspaces> paths + read/write modes
      —— end of the stable prefix ——
[6] MEMORY METADATA    one line: vault sizes + "use memory_search for more"
[7] SESSION SUMMARY    <session_summary> the recursive compaction summary
[8] RECENT TURNS       verbatim replay of the live buffer (see below)
[9] RETRIEVED MEMORIES ephemeral auto-retrieval for this turn only
[10] CURRENT INPUT     always last — operator input and/or claimed inbox
                       messages, all as ordinary user-role messages
                       (slot [9.5] <agent_messages> was deleted by the
                       2026-08-22 comms redesign)
```

Slots [1]–[5.5] are one big system message that is byte-stable turn-to-turn —
deliberately cache-friendly for local models' KV/prompt caches. Everything
volatile sits after it, nearest the query.

**Slot [8], precisely:** the "live buffer" is every *conversational* event
(`user`/`assistant`/`tool_call`/`tool_result`) since the last compaction
cutoff, replayed verbatim in transcript order — `tool_call` events become
assistant messages carrying their tool-call JSON, `tool_result` events become
`tool` role messages. Since the kept-tail fix, the buffer is: the
`kept_events` recorded on the newest compaction marker (the events immediately
*before* the marker) plus everything appended after it. Two things are
conspicuously absent from replay: `thinking` events (§4.2) and `system_event`
markers (cancellations, errors, compaction notes — operator-facing bookkeeping
only).

**Slot [9] is ephemeral by design:** each user turn runs a fused memory search
with the input text; top `retrieval_k` hits above a relative threshold are
injected as short excerpts with a "may be irrelevant — ignore freely"
disclaimer, and *never persisted* — next turn re-retrieves fresh. So retrieval
can't accumulate stale context, and the transcript stays clean. Auto-retrieval
runs once per turn (on the user input), never inside the tool loop.

---

## 4. Tools and thoughts: the two big context consumers

### 4.1 Tool results — bounded at capture, discarded at fold

Tool output is truncated **at capture time** — what's persisted to the
transcript is already bounded, and that's what replays while the event lives
in the buffer:

- **`exec_run`**: 8,000-char cap by default, with a marker suggesting
  `full: true` or filtering. `full: true` lifts it to a 200k-char hard ceiling
  (a transcript guard against runaway commands). One command per call, no
  `&&` chaining; on Windows it's cmd.exe (the tool description tells the
  agent all of this).
- **`fs_read`**: ~8,000-char default; `offset` (1-based start line) + `limit`
  (line count) slice by line; `full: true` lifts to the same 200k ceiling.
  Any capped/sliced read ends with a footer like
  `[fs_read: lines 1-79 of 200 — continue with offset=80 …]` so the agent
  pages instead of re-reading whole files. The cap-or-full decision is the
  agent's: slice when only part matters, `full` when whole-file fidelity is
  worth the context.
- Everything else returns what it returns — but tools designed for Hydra keep
  their outputs modest (memory excerpts, skill listings, etc.).

Lifecycle of one tool result: captured (bounded) → persisted verbatim →
replayed verbatim every turn while in the live buffer → at compaction, if it
falls in the fold, **discarded entirely** — the summarizer never sees raw tool
output (the summary prompt keeps decisions/paths/plans instead). It still
exists in full on disk, forever.

### 4.2 Thoughts — five different fates for one reasoning block

Model reasoning is the single most expensive thing a thinking model produces
(the cut-out forensics found a 31k-char block for one step), and Hydra treats
it differently at every stage:

| Stage | What happens to the reasoning |
|---|---|
| Streaming | Separated from visible text (structured field or `<think>` splitter), streamed to the UI as its own channel |
| Persistence | Written **in full** as a `thinking` event, *before* the step it narrates |
| Within the same turn | Replayed into subsequent steps, head-capped at 4,000 chars/step (`reasoning_replay_max_chars`) |
| Across turns | **Never replayed.** `thinking` events are skipped by the live-buffer builder |
| At compaction | A ≤500-char excerpt is attached to the folded step it narrated, so the summarizer can preserve *intent* for steps whose visible text is empty (on thinking models, that's most tool steps) |
| Occupancy meter | **Counted at zero** — thinking never re-enters a prompt across turns, so it doesn't count. But within-turn replay (up to 4k/step) *is* real prompt weight; the transcript-based meter can't see it, which is one reason the in-flight brake estimates from the live message list instead |

So: a long thought is fully preserved for you to read in the transcript,
partially available to the model while it finishes the turn that produced it,
and reduced to a one-line intent excerpt in long-term (summary) memory.

---

## 5. Compaction: how history gets folded

`sessions/compaction.py`. Runs **between turns** normally; mid-turn only via
the emergency path.

### 5.1 The trigger math

```
occupancy  = (len(summary) + Σ len(live buffer event contents)) / 4 × token_factor
limit      = min(effective_ctx × compact_threshold,
                 effective_ctx − min(max_tokens, effective_ctx ÷ 2))
compact when occupancy ≥ limit  (and the buffer has ≥ 4 events)
```

The second term of the `min` is **generation headroom**: prompt and reply
share one window (Ollama's `num_ctx` counts both), so the preset's
`max_tokens` is reserved out of the prompt budget — capped at half the window
so a preset with `max_tokens == effective_ctx` can't starve the prompt to
nothing. Example (the Coding Agent preset): `32768 × 0.8 = 26214` but
`32768 − min(32768, 16384) = 16384` → compaction fires at 16,384 estimated
tokens, leaving 16k for a long thinking reply.

Honest caveat: occupancy counts **only** summary + live buffer. The stable
prefix (system prompt, tool listing + schemas, memory blocks, skills) is
unmeasured — the calibration factor absorbs it (§6), which is one reason
calibrated factors land well above a pure tokenizer ratio.

### 5.2 The fold

- Fold the oldest ~70% of the buffer; keep the newest ~30% verbatim.
- The boundary nudges forward past `tool_result` events so the kept tail never
  opens with a result whose `tool_call` was folded (an invalid chat-API
  sequence). The buffer builder also drops such orphans defensively, which
  heals markers written before this fix existed.
- The folded events are rendered for the summarizer as `User:`/`Assistant:`
  lines plus `[assistant called tools — reasoning: <excerpt>]` markers;
  **raw tool output is dropped**. A utility model call updates
  `sessions.summary` (recursive: old summary + new turns → new summary),
  preserving decisions, open problems, explicit user instructions,
  identifiers/file paths, and plan state.
- A `compaction` event is appended with `folded_events` / `kept_events` /
  `summary_len` in its meta. **The marker lands *after* the kept tail** in the
  append-only transcript; replay retains those trailing `kept_events` plus
  everything after the marker. (The pre-fix bug: replay dropped everything
  before the marker, so every compaction was secretly a full wipe to
  summary-only — the direct cause of the "agent forgot the second half of the
  session" symptom.)

Net effect on the next prompt: `[7]` grows a little, `[8]` shrinks a lot,
disk keeps everything.

### 5.3 The emergency (mid-turn/forced) path

`maybe_compact(..., force=True)` skips the occupancy check (needs only ≥2
buffered events). The turn engine uses it in the two brake situations from
§2.2, then **rebuilds the in-flight message list from scratch**: fresh
assembly over the now-compacted transcript (the current turn's user input and
tool steps are already persisted, so they come back via the kept tail or the
summary), with an ephemeral continuation note as slot [10] ("context was
compacted mid-turn … continue, don't start over"). This deliberately trades
the prompt cache for survival, and it's bounded per turn.

---

## 6. The occupancy meter and token calibration

- **The estimate** is chars/4 — cheap and wrong by a model-dependent constant.
  `token_factor` is that constant, learned per session: after each clean turn
  with reported usage, `factor = prompt_tokens / heuristic` (clamped to
  [0.5, 4.0], stored on the session row). Local Qwen-family models on
  code-heavy content calibrate to ~2–3.3×.
- **Saturated readings are floors.** If `prompt_tokens ≥ ~98%` of
  `effective_ctx`, the provider clipped the prompt to the window; the reading
  can only *raise* the factor, never lower a real calibration.
- **Seeding.** Each calibration writes `token_factor_seed` into the preset's
  profile JSON. A fresh session with no calibration of its own uses the seed,
  so first turns aren't 2–3× optimistic. Meter labels: `usage-calibrated`
  (session's own factor) → `preset-seeded` → `heuristic` (raw 1.0).
- **The chat-header meter** (`GET /api/sessions/{id}/occupancy`) reports
  `used_tokens`, `effective_ctx`, `limit_tokens` (the headroom-aware trigger),
  `pct`, factor, and estimator. The UI refetches it on session switch, at turn
  end, and mid-turn at every step boundary (new tool row / assistant burst) —
  the exact moments the transcript gains events. Its color ramp keys off
  `limit_tokens`, i.e. red means "compaction imminent," not a raw percentage.
- What the meter cannot see: the stable prefix, this turn's in-flight
  reasoning replay, and the streaming-but-not-yet-persisted assistant text.
  The first is absorbed by calibration; the second is covered by the in-flight
  brake; the third resolves at the next step boundary.

---

## 7. The window itself

`profile.effective_ctx` is the *reliable* window you choose per preset (not
the model's advertised max). On Ollama it is forwarded as `num_ctx` — so the
window compaction budgets against and the window the server actually allocates
are the same number. `params.max_tokens` (→ `num_predict`) shares that window
with the prompt, which is exactly why headroom is reserved (§5.1).
`openai_compatible` providers never receive `num_ctx`.

---

## 8. Quick reference: every context knob

**Preset `params`:** `max_tokens` (generation budget; also reserved out of the
compaction limit), `num_ctx` (explicit Ollama override; else derived from
`effective_ctx`).

**Preset `profile`:**

| Key | Default | Effect |
|---|---|---|
| `effective_ctx` | 8192 | The window everything budgets against; → Ollama `num_ctx` |
| `compact_threshold` | 0.75 | Fraction of the window that triggers folding (before headroom) |
| `max_tool_steps` | 25 | Tool-loop step cap per turn |
| `model_error_retries` | 1 | Reroll budget for stochastic model failures |
| `forced_compactions_per_turn` | 2 | Shared budget: in-flight brake + empty-`length` recovery |
| `reasoning_replay` | true | Feed each step's reasoning back within the turn |
| `reasoning_replay_max_chars` | 4000 | Head-cap per step for that replay |
| `retrieval_k` | 4 | Auto-retrieved memory hits per user turn |
| `retrieval_rel_threshold` | config | Drop hits below this fraction of the top hit's score |
| `token_factor_seed` | — | Machine-written by calibration; seeds new sessions' occupancy factor |

**Tool-call arguments:** `exec_run(full=…)`, `fs_read(offset=…, limit=…,
full=…)` — the agent's own cap-or-page-or-dump choice, 8k default / 200k hard
ceiling.

---

## 9. FAQ-style summary

- **Is anything ever lost?** From disk, no — transcripts are append-only and
  truncation happens before persistence (so what's on disk *is* the captured
  result). From the model's working memory, yes, in three ways: tool results
  are bounded at capture, folded history collapses to the summary, and
  reasoning never crosses a turn boundary.
- **Why did my agent used to go silent, and what happens now?** The prompt
  filled the whole Ollama window; generation had zero room; the model
  "finished" with `length` and no text. Now that's detected, force-compacted,
  and retried — and if recovery is impossible, it's a visible error.
- **What does the model remember from 40 turns ago?** Whatever survived into
  the recursive summary: decisions, instructions, file paths, plan state.
  Not tool output, not reasoning, not verbatim phrasing.
- **What's the cheapest way for an agent to read a big file?** A slice
  (`offset`/`limit`), then `fs_search`/`exec_run` filters, then `full: true`
  as the deliberate last resort. The tool descriptions steer agents the same
  way.
