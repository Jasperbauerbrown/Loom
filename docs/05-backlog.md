# Backlog

Concrete, agreed engineering work that is not yet built. Design questions belong in the
charter (§5.11 open questions); the build order is `03` §20. When an item ships, delete
it here and record it where it now lives (`03`, the decision log).

Each item names its source. "Audit" means `reference/tool-parity-audit.md` §3.

## LAN access (Jasper, 2026-09-29)
- **Token prompt in the web UI.** With `general.lan_token` set, every `/api` call and
  `/ws` need the token, but nothing in the UI ever writes `loom_token` to browser
  storage — a LAN browser (and loopback, once the token is set) gets 401 with no way
  in short of devtools. On a 401, prompt for the token, store it, retry; a Settings
  entry to clear it. Mostly `ui/src/lib/api.ts` plus one dialog.

## Cancel (Jasper, 2026-09-29)
- **Cancel must stop everything the turn is doing.** Today `POST …/cancel` sets a flag
  that the runner reads between steps and on each streamed delta (`runner.py`
  `_call_model`), then closes the HTTP stream. Gaps: a model call still waiting in the
  priority queue or in prompt processing (no delta yet) is not interrupted; a running
  tool call (`shell` up to its timeout, `http`, MCP) runs to completion because the
  dispatcher's `wait_for` watches only the timeout; background processes the turn
  started keep running; sub-threads the turn spawned keep running. Wanted: cancel
  wakes the queue wait and the tool wait (one cancellation signal both await), kills
  the in-flight process of a foreground `shell`, stops processes this turn started,
  cancels the turn's own sub-threads, and the UI says what was stopped.

## Permissions (Jasper, 2026-09-29)
- **Desk auto-approve must actually work.** With `permissions.desk_auto_approve` on, the
  desk still asks in practice. The switch lives in `dispatch.py` `_auto_approved`, which
  is consulted twice (a non-protected fs-guard ask; a policy ask/classify) and grants
  only when the context's conversation row says `kind == "desk"`, the tool is not
  `shell`, or the shell command splits cleanly and names no protected path. Suspects:
  a `ToolContext` built without `kind` (resumed calls via `_repair`, calls dispatched
  from a sub-step), a shell command the pre-filter cannot split (quotes, heredocs,
  `&&` chains) falling to the ordinary path, MCP tool names taking a path that skips
  the check. Reproduce with a test per suspect, fix, and make the tool row say why a
  desk call asked despite the switch.

## Promotion (Jasper, 2026-09-29)
- **Promote must find `uv` on a machine where it is not on PATH.** The 2026-09-29
  21:02 promotion of `f0ff96e` failed at `uv sync`: `supervisor/__main__.py`
  `uv_command()` tries `shutil.which("uv")`, then `sys.executable -m uv` — and under
  `uv run` that interpreter is the project venv, which has no `uv` module (the same
  failure as 2026-09-28; the retry at 21:41 got past it, cause unknown). On this box uv
  is a pip user package under system Python 3.11, reachable only as `python -m uv`.
  Wanted: a resolution chain that ends in something that works — PATH, the venv, the
  user-site `Scripts/uv.exe`, every `py -0p` interpreter's `-m uv` — remembered once it
  succeeds, and a `system.uv_path` setting as the override, UI-visible like every
  other knob. The failure message names what was tried.

## Dropped
- **Ignore-aware search** (audit #4) — dropped 2026-09-29: not felt as friction yet;
  revisit only if it bites.

## Undecided (audit P2, not yet agreed)
- `ask_user` with options, rendered like an approval (#9). Paused 2026-09-29: it opens
  four design questions — whether a thread's question goes to the person or, as today,
  to the parent by judgment; which status it sets (`waiting_input` now means "stopped");
  whether the answer resumes the paused call (approvals shape) or arrives as an inbox
  message; and whether it interrupts like an approval or queues on the board.
- Checkpoints and undo for files under the roots (#10).
- LSP diagnostics after edits (#11).
- MCP resources, prompts and OAuth (#12).
- `process` watch-for-line (#13).

## Undecided (TD audit, not yet agreed)
- Hold a system-required power request while a turn runs or an approval is pending, so
  the machine does not sleep mid-task. Battery policy is the person's call. (F7)
- `conversation_finish` requires an artifact or image id in the summary when the goal
  asked for visual proof. Leans on the model; the finish delivery (shipped 2026-09-24)
  already hands the spawner every artifact and image id. (§3)
