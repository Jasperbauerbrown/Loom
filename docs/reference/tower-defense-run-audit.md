# Tower-defense run audit (2026-09-24)

An after-action review of one real task: the desk (Qwen 3.8 Flash via OpenRouter)
spawned a thread to build an ASCII C++ tower-defense game, then a second thread to
port it to raylib, then verified the result itself. The task succeeded. This document
records where the 136 minutes went and what Loom should change. The backlog items it
produced are tagged "TD audit §N" in `05-backlog.md`.

Sources: the `events`, `approvals` and `artifacts` tables in `~/.loom/state.db`;
`~/.loom/logs/backend.log`; the Windows System event log (Kernel-Power 506/507); the
vault note `projects/tower-defense.md` and `log/2026-09-24.md`. Event ids below are
`events.id`. Threads: ASCII build `01M3A8EA6HZ0VDV55WT0E6YDJ5` (events 347–450), raylib
port `01M3A9K6A2Q7PVNW04Q779F8GM` (467–754); desk `01M34XFXYM7J92V067ADTW3HG2`
(310–842 for this task).

## 1. Time budget

Wall clock 17:42Z → 20:03Z, 136 minutes.

| Bucket | Minutes | Evidence |
|---|---|---|
| Laptop in Modern Standby | ~60 | Kernel-Power 506/507: 12:15:48–12:47:23, 13:02:51–13:11:56, and five shorter sleeps 13:24–14:00 local |
| Awake, waiting for an approval click | ~18 | `approvals.created_at`/`decided_at`: 7 asks, 46 min end to end, 28 of them overlapping standby |
| Agent working | ~58 | ASCII thread 16; raylib code and docs ~25; raylib visual verification ~11; desk re-verification 6 |

Every approval was answered within seconds of a wake-up: the person was away, the
machine idled into standby, and the turn waited. Each wait outlived the game's
`--autoclose`, so each approval produced a dead window and another request.

Loom's own behaviour across the sleeps was correct: the 120 s stream-idle watchdog fired
on wake (`backend.log` 12:47:24, "no data for 120s"), the retry succeeded, nothing was
lost. The log line is misleading, though: the cause was suspend, not the provider.

## 2. Findings

Ranked by cost. Each names its fix location.

**F1. Approval loop (permissions design).** Seven `shell` calls were classified `ask`,
all of them `powershell -File <script Loom had just written>`. Classifier reasons
(`approvals.reason`): "script's contents are unknown", "the 'Desk' goal does not
clearly authorize". The classifier sees only the command line, and the desk has no
goal, so its request text was the last user message ("go ahead", "apologies, i closed
it by accident"). The same command was asked five times (desk 774, 777, 792, 804, 810).
`allow_always` widens the whole tool, so it was never a sane option. No notification
reaches the person; the pending card sits in the UI.

**F2. Screenshot tool (`tools/native/screen.py`).** `window=` and `region=` copy the
screen rectangle with `CopyFromScreen`, so an occluded window returns whatever covers
it. Desk: 0 of 6 captures showed the game (762, 765, 795, 798, 813, 828). Thread: 1 of
5 (578 wrong content, 581 success by full monitor, 628, 643, 691 the browser). When the
window was already gone the error surfaced as `screen capture failed: #< CLIXML`
(763, 796): the real message, "no window whose title contains …", was inside
PowerShell's CLIXML error stream. That message would have told the desk the process
was dead and short-circuited F3.

**F3. Kill-on-close, documented but unlearned.** `prompts/tools/shell.md` says
everything a foreground call starts ends when the call ends. The desk launched the game
in foreground calls four times (759, 768, 777, 810), chased three wrong hypotheses
(title mismatch 780, frames-vs-seconds 819) and found the cause at 822, minute 42 of
43. The thread used `background=true` from the start (574) and never hit it. The shell
result says nothing about what it killed.

**F4. Desk over-verification.** `kind-desk.md`: "read its walkthrough, spot-check one
thing". The desk made 28 calls over 43 minutes, never read the walkthrough, never
opened the cited screenshot or the two PNGs, re-derived the thread's
SetWindowPos/FindWindow scripts twice (768 then 786 after 771 had fixed it), launched a
second game instance (825), and reported "mousetest 9/9" (838) when the output was
`PASS (0 failures)` with no count. It ended (838) at the trust decision available at
759. See §3 for why the handoff made this likely.

**F5. Dispatcher timeout ignores the call's `timeout_s`** (`tools/dispatch.py`
`timeout_for`). The handler is cancelled at the tool default (120 s) and partial output
is discarded. ASCII thread step 9 (event 391) asked for 180 s and got "shell timed out
after 120s".

**F6. Approval resume resets the thread step budget.** `runner.py` re-enters at step 1
after an approval pause; the raylib thread went 65 steps, paused, then had a fresh 100
(682: "I have limited steps" → 685: "I have plenty of steps").

**F7. Standby is invisible to Loom.** No power request is held while a turn runs or an
approval is pending; on resume the only trace is a misleading idle-timeout log line.

**F8. `process(logs)` has no tail mode.** The ASCII thread read fixed offsets of a
495 KB log growing at 60 frames/s and briefly chased a phantom freeze (420–426).

**F9. Small tool-surface papercuts.** `fs_edit` on a vault-relative path resolves
against `C:/Projects` and reports "no such file" (ASCII 442). `knowledge_write` with
`op=replace` wants `old` + `content`; the thread sent `old` + `new` (735).

**F10. Model discipline (Qwen 3.8 Flash).** Hallucinated tool `fs_shell_placeholder`
(621); repeated a quoting mistake six steps after fixing it (desk 786); shipped drafts
with self-acknowledged bugs (532, 555); one 22 KB file written from a 34 K-char
reasoning pass then patched with 11 edits before the first compile (ASCII 355–370); two
redundant full rebuilds after doc-only edits (742, 748). About 8 minutes in total. The
code itself was good: 13/13 on the first selftest run, and it found real bugs (padding
`memcmp` 517, one-frame synthetic click 706).

**F11. Person-side.** The game window was closed by hand at 18:52 (590), forcing a
relaunch and a "WINDOW NOT FOUND" cascade (600–627).

## 3. What the threads handed back

`conversation_finish` delivers only `summary` to the spawning conversation; the
`walkthrough` goes to `log/<date>.md`. Both summaries were ~590 characters.

- ASCII (451) named what was and was not verified ("interactive mode smoke-tested by
  reading rendered frames"). The desk checked it in one cheap step (452) and moved on.
- raylib (755) said "Window rendering and real click-to-build were confirmed by
  screenshot" and nothing else about evidence. The walkthrough had everything the desk
  needed: the screenshot artifact id, the PNG paths, and three gotchas (window-matched
  capture returns occluded content; `SetForegroundWindow` is refused, `SetWindowPos`
  works; hold a synthetic click ~150 ms). None of it reached the desk's context.

Even with the pointer, the desk could not have opened the cited image: image artifacts
are stored per conversation and `artifact_recall` reads text only. The decision log
(2026-09-20) rejected verify-on-finish as a mechanism in favour of "judgment in the
spawning conversation, fed by evidence in the finish walkthrough", but the walkthrough
is not fed; it is filed. Three layers failed together: the summary did not cite, the
mechanism did not carry, and the desk did not go and read.

## 4. Numbers

| | ASCII thread | raylib thread | desk verify |
|---|---|---|---|
| Steps (model calls) | 25 | 88 | 29 |
| Tool calls | 48 | 104 | 28 |
| Compile/link cycles | 7 | 8 | 0 |
| Screenshot calls | 0 | 5 (1 usable) | 6 (0 usable) |
| Estimated prompt tokens | ~0.5 M | ~3.3 M | ~0.2 M |
| Wall clock | 15.7 min | 72.5 min | 43.7 min |
