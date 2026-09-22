# Walkthroughs: the four examples through the design

**2026-09-20.** Each example from `00-charter.md` §5.1 traced step by step against the
design as written. Every step names the mechanism it relies on; a step that relied on
nothing written down is marked **GAP** and resolved in §5 below (and folded back into the
charter).

Common prelude for every user message: surface → surface API → target conversation's
inbox → a turn starts. Context is assembled once per turn: soul/operating rules, current
date/time/timezone, the "now" board, the rolling window, and the pre-loaded entities.

---

## 1. "Heat up the Ender 3."

**Earlier, once:** "Familiarise yourself with the 3D printer on the network." The desk
acted inline (or in a short thread): probed the host, identified the API (say Moonraker),
read its docs, made one read-only call to confirm, then wrote
`devices/ender-3.md` (aliases: *ender 3, the printer, 3d printer*; host, API kind,
credential reference) and `procedures/ender-3-control.md` (heat / cool / status
commands). Integration tier 1: knowledge only.

**Now:**

| Step | Mechanism | Note |
|---|---|---|
| Resolve "ender 3" → `devices/ender-3.md` | alias index (§5.4) | ms, no model call |
| Pre-load the entity **and its procedure** | **GAP → pre-load rule** | without the procedure every device action costs an extra round trip |
| Model picks rung *act inline*, calls `shell`/`http` with the heat command | ladder (§5.3) | |
| Permission: shell is `classify` | §5.5 | classifier sees the *user's request* + the call → in-scope physical action the user asked for → run. **GAP → classifier input must include the request** |
| Command needs the API key | **GAP → secret injection** | model must never see the secret; args carry a reference the harness resolves |
| Result → reply "heating to 200/60" | | |
| Consolidation | **GAP → cheap when nothing durable happened** | forcing a write-back step here is waste |

Failure paths: host unreachable → tool error → model reports, may note it in the entity.
"the printer" matches two devices → both pre-loaded, model disambiguates or asks.

Cost: resolve + 1 model call + 1 tool + 1 model call.

## 2. "Add a play-random feature to the music bot."

**Earlier, once:** `projects/music-bot.md` exists (path, stack, run/test commands,
conventions) from a familiarisation, or the desk searched the configured project roots
when first asked and created it.

| Step | Mechanism | Note |
|---|---|---|
| Resolve → project entity pre-loaded | alias index | |
| Model picks *spawn thread* (multi-step coding, user may want to continue past it) | ladder | judgment, not a rule |
| Desk creates the thread: scope = project entity, goal + the relevant excerpt of the desk exchange | **GAP → desk needs conversation tools** (create / send / read) and a defined spawn payload | the thread does *not* receive the desk window |
| Desk replies "started; I'll say when it's ready", stays free | | |
| Thread (worker role) reads code, implements, runs tests, commits | tools, shell `classify` | `git push` is external → ask |
| Thread edits files under the project path | filesystem guard | **GAP → how does a project path become an allowed root?** knowledge notes must not be able to widen access |
| Thread rewrites the status section of `music-bot.md` at milestones | §5.4 | |
| Thread needs a decision ("random from queue or whole library?") | thread event *stopped without finishing* → desk turn | **GAP → default policy per event class**; user answers on the desk and the desk forwards into the thread inbox, or opens the thread |
| Thread finishes → event → desk turn → notification | §5.7 | |
| Consolidation: thread writes the new feature + learned conventions into the entity | | |
| Later: "make it skip recently played" | desk routes to the same thread | **GAP → finished threads stay addressable**; archived by maintenance after inactivity |

Concurrency: desk and thread share one model → priority queue, desk first (§5.6).

## 3. "I need a program to visualise coordinates and datapoints on a 3D map."

| Step | Mechanism | Note |
|---|---|---|
| Resolve → nothing matches | | new project, by judgment |
| Desk asks only what is costly to guess (web or desktop? data source?) or starts on stated assumptions | simplicity ethos | the thread can ask later via events |
| Desk creates `projects/3d-map-viz.md` **before** spawning (aliases: *3d map, map data project*; root = `<projects root>/3d-map-viz`) | state written before acting on it (§4) | |
| Spawns thread scoped to it | as in #2 | |
| Thread architects, scaffolds, implements; rewrites status at each milestone; records decisions in a linked note | §5.4 | **GAP → status safety net**: if a whole thread turn passes without a status write, the turn-end write-back does it |
| User, meanwhile: "what's the state of the 3D map project?" | resolve → entity pre-loaded incl. status → desk answers | no thread transcript read; if status looks stale the desk may `conversation.read` a summary |
| Thread transcript grows over days | **GAP → compaction inside a conversation** | may be lossy: durable facts are already in the vault |
| Restart (promotion / crash) mid-thread | conversation state persisted per step; resumes | already required by §5.9 |
| Preemption: a thread step cannot be interrupted mid-stream | | desk waits for the current generation; **bound it** with a per-step max-tokens knob for background priority |

## 4. "What homework do I have this week?"

**Earlier, once:** Canvas configured as an MCP server (tier 2; token in keyring);
school mail as an MCP server (tier 2). Familiarisation produced `connectors/canvas.md`,
`connectors/school-mail.md`, `school.md` (aliases: *homework, school, classes*; current
term; links to `courses/*.md`), and one note per course holding the syllabus facts —
including the recurring items that only exist in a PDF.

| Step | Mechanism | Note |
|---|---|---|
| Resolve "homework" → `school.md` + first-hop course notes | alias index, pre-load rule | |
| Model picks *act inline* | ladder | several tool calls, no code |
| Canvas `list_upcoming` (read-only → allow); mail `search` (read-only → allow) | §5.5 defaults via MCP annotations | |
| Syllabus items come from the course notes already in context | | |
| Synthesises one table, replies | | needs *today's date* in context — trivially always present |
| Finds an assignment that exists only in an email → writes it into the course note | inline `knowledge_write` | the consolidation the invariant is actually for |
| Follow-up: "remind me the day before each is due" | model calls a `schedule` tool → schedule triggers → desk turns with event input, policy *interrupt* | **SIMPLIFICATION**: this removes the need for "knowledge-derived" triggers; the model schedules explicitly |
| Variant: "every Sunday evening show me the week" | same, recurring | |

---

## 5. Gaps found and how they are resolved

Each is folded into `00-charter.md` at the cited section.

1. **Pre-load rule** (§5.4): pre-load the resolved entity note plus its first-hop linked
   notes, procedures first, within a fixed token budget. One rule, no per-entity config.
2. **Secret injection** (§5.5, §5.8): tool arguments may carry `{{secret:NAME}}`; the
   harness substitutes just before execution and redacts in transcripts. The model never
   sees secret values.
3. **Classifier input** (§5.5): the classifier sees the user's request / thread goal, the
   proposed call, and the policy text. In-scope actions the user asked for pass.
4. **Consolidation mechanics** (§5.3): inline write-back is the model calling
   `knowledge_write` when warranted — no forced trailing step. The async maintenance
   pass sweeps recent transcripts for durable facts that were missed. For threads, a
   status safety net: a thread turn that wrote no status gets one at turn end.
5. **Allowed roots grow only by approval** (§5.5): a path outside the roots produces an
   approval with "always allow this root"; accepting writes the root to settings.
   Knowledge notes never widen access.
6. **Conversation tools** (§5.3): the desk (and any conversation) has `conversation.create /
   send / read / list` — the surface contract exposed as tools. Spawn payload = goal +
   scope + a model-selected excerpt; never the whole window.
7. **Default event policies** (§5.7): *stopped without finishing* → interrupt; *finished* → interrupt;
   *blocked/failed* → interrupt; schedule and connector events → queue unless the
   creator set otherwise. All UI-editable.
8. **Thread lifecycle** (§5.3): done threads remain addressable and can be resumed;
   maintenance archives after configurable inactivity.
9. **Compaction** is part of the conversation primitive (§5.3): rolling window + summary;
   may be lossy because durable facts live in the vault.
10. **Knowledge-derived triggers dropped** (§5.7): the model creates schedules explicitly.
11. **Preemption granularity** (§5.6): at model-call boundaries; background steps carry a
    max-tokens bound.
12. **Always-present context**: current date/time/timezone in every turn.

Nothing in the four traces required a new block or a new codepath. The design held;
the gaps were unstated rules inside existing blocks.
