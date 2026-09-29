---
name: self-review
description: Examine a thread, or a stretch of the desk, for failures of the harness or the model — loops, tools that keep breaking, approvals that keep asking, results ignored — and turn each into evidence, a class and the smallest fix.
type: procedure
---
# Self-review

## When to use
When something went badly and nobody yet knows why: a thread finished `failed` or was
stopped; a walkthrough reads like a fight with the tooling; the person asks "what
happened there?" or "why did that take forty steps?"; or you notice, mid-task, that a
tool has failed the same way twice — finish the task first, then review. Also on a
schedule, if the person wants one: `schedule_create` with a payload that names this
procedure and a window ("review every thread that finished since the previous review").

Not for judging whether the *work* was good — that is the walkthrough's job — and not
for a conversation that is still running: wait for it to stop, or read it only up to
where it stands and say so.

## What you can see
- `conversation_list` — every conversation with its status and goal. The `failed` ones
  and the long ones are where to start.
- `conversation_read` — the transcript as text: `USER`, `ASSISTANT`, `TOOL CALL
  name(args)`, `TOOL RESULT (ok|ERROR; verdict): preview` and `SYSTEM` lines. The
  verdict appears whenever the harness did not simply allow a call: `ask → allow` means
  the person was asked and said yes, `ask → deny` that they refused, `classify → allow`
  that the classifier or the static pre-filter judged it, `auto → allow` that the desk
  auto-approved it. The footer names the earliest event shown; page backwards with
  `before` until you reach the goal or the message that opened the stretch. Ask for
  30–60 events at a time so nothing spills to an artifact. A compacted conversation
  shows a `summary:` line for what was folded — evidence from before the fold is
  coarser; say so.
- `transcript_search` with `conversation` set — jump to the exact spot: search
  `ERROR`, `outside allowed roots`, `did not answer`, `denied`, `unknown tool`, a tool's
  name, or a phrase the person used. `neighbours: true` shows the exchange around it.
- The daily log (`log/YYYY-MM-DD.md`) — the thread's walkthrough and what it claims it
  verified; the vault's project note for the work, if there is one.
- Artifacts — long tool outputs are spilled to the conversation's artifacts folder;
  `artifact_recall` fetches one when the 160-character preview is not enough.
- The backend log at `<APP_HOME>/logs/backend.log` has the harness's own view: tool
  exceptions with tracebacks, model-call errors, guard decisions. It is outside the
  roots, so reading it asks; do that only when the transcript does not explain a failure.

## Steps
1. **Fix the scope.** One conversation id, or the desk between two moments (the
   messages that opened and closed the stretch). Write it into the review so the person
   knows what was and was not looked at.
2. **Read it whole before judging anything.** Page back to the start. Note the goal,
   the number of steps, how it ended (`done`, `failed`, stopped, still running) and what
   the walkthrough claims. A review that starts from the last page blames the wrong step.
3. **Mark every place the transcript stumbles** — the signatures below. For each,
   record the evidence as it appears: the exact `TOOL CALL` or `TOOL RESULT` line, how
   many times, and the event ids around it (the paging footer gives them).
4. **Attribute each one** to a class — harness, model, environment, or the ask — with
   the rule further down. When in doubt, say what would settle it.
5. **Name the smallest change** that would have prevented it, and how the person would
   know it worked. One change per finding. A finding with no change is an observation,
   and observations do not go in the review.
6. **Write it down** (below) and tell the person the short version: how many findings,
   which class dominates, the one change you would make first. Then stop — what to do
   with it is their call: fix it now (the self-modification procedure), record it in the
   repository's backlog (`docs/05-backlog.md`), or accept it.

## Signatures, and what each usually means
- **Loop.** The same `TOOL CALL` (name and arguments) three or more times with nothing
  new in between; two calls alternating; an `ASSISTANT` line repeated nearly verbatim.
  If the results were identical too, the harness kept returning the same answer and the
  model kept asking — look at *why the result did not help* before blaming the model.
  If the results differed and the model still repeated itself, it is the model.
- **A tool that keeps breaking.** `TOOL RESULT (ERROR)` with the same message across
  different steps or arguments, especially when the model visibly changes arguments to
  get around it (quoting, path spellings, smaller pieces). Harness until shown
  otherwise: the tool, its description, or the guard is not doing what the model was
  told it does. Copy the error text exactly — it is the bug report.
- **Reaching for something that is not there.** `unknown tool`; `tool_search` that
  finds nothing; `shell` used for what a tool exists for; a file read refused where the
  model plainly needed it. Harness when the capability is missing or hidden; model when
  it was in the tool list and the model did not look. Recurring across conversations, it
  is a description or harness-block gap either way.
- **Permission friction.** The same verdict (`ask → allow`) many times for the same
  root, command shape or path class; `classify` verdicts whose reason is a timeout or a
  provider error ("did not answer") instead of a judgement; a `deny` followed by
  retries of the same call. Harness or settings: name the one root, policy row or
  pre-filter change that would have removed the whole series. The person answering yes
  twelve times is a finding, not a workflow.
- **Ignored results.** A result says X and the next `ASSISTANT` line asserts not-X; a
  retry straight after an explicit "not found"; a verification step the goal asked for
  that never appears. Model.
- **Stalls and aborts.** `SYSTEM` lines — `model call failed … trying fallback`, `turn
  crashed`, `context near the limit`, `compaction failed`, `system resumed after N
  min`, `cancelled by user` — or a thread ending `failed` with the reason in its last
  events. Environment (provider, network, the machine slept) unless the same line
  recurs without an outside cause; then harness.
- **Scope drift.** Work on paths, threads or topics the goal did not name; a sub-thread
  spawned to do what the thread itself was asked to do. Model — or the ask, when the
  goal was vague enough that the drift was a fair reading. Quote the goal alongside.
- **A false finish.** `done` without the verification the goal asked for; a walkthrough
  that describes checks the transcript does not contain; "evidence" that is a
  restatement. Model.

## Attributing: harness or model
Ask one question: *would a capable model with these tools and this context have failed
the same way?* If yes, it is the harness — a tool, a guard, a description, the harness
block, a setting. If the same tooling works in other conversations and this one simply
did not use it, it is the model. Two corrections:
- A model failure that recurs across conversations and models is a harness failure by
  another name: the tool description, the kind prompt or the context did not make the
  right move findable. Record it as harness.
- A failure specific to one weak model is not a reason to build scaffolding (the
  capable-model bar in `CLAUDE.md`). Record it, name the model it happened on, and
  recommend nothing but a better model — unless the harness also had a part.
Never file a tool error as a model failure because the model "should have coped".

## What to write
- One section in the daily log for the day of the review, `## Self-review: <title>
  (<conversation id>)`, with
  the scope, how it ended, each finding as evidence → class → smallest change → how to
  verify, and the one change you would make first. `knowledge_write` with
  `op: append` (it creates the day's note when there is none).
- When a finding is the third of its kind, or the fix is not immediate, a topic note for
  that failure class (`topics/loom-<failure>.md`) that links the reviews it appears in,
  so the next review starts from history and the person sees the pattern in one place.
  Link the project note for Loom itself if there is one.
- Nothing else. A review changes no setting, no root, no policy and no code, and does
  not post into the conversation it reviewed.

## Checks
- Every finding has a quoted transcript line, a count and an event-id range.
- Every finding has exactly one class and one smallest change; observations were left
  out.
- The review names what it did not read: a folded summary, a log it did not open.
- `transcript_search` for `Self-review:` with the conversation id finds nothing older —
  the conversation had not already been reviewed; if it had, extend that entry instead.
- The person got the short version, and the decision stayed theirs.
