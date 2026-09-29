# Backlog

Concrete, agreed engineering work that is not yet built. Design questions belong in the
charter (§5.11 open questions); the build order is `03` §20. When an item ships, delete
it here and record it where it now lives (`03`, the decision log).

Each item names its source. "Audit" means `reference/tool-parity-audit.md` §3.

## Tools: coding parity (audit P1)
- **Ignore-aware search.** `fs_list` and `fs_search` honour `.gitignore` by default
  (`include_ignored` to opt out). `fs_search` gains `output` (content / files / count),
  `offset` and `multiline`; `fs_list` gains mtime sorting. (Audit #4.)

## Undecided (audit P2, not yet agreed)
- `ask_user` with options, rendered like an approval (#9).
- Checkpoints and undo for files under the roots (#10).
- LSP diagnostics after edits (#11).
- MCP resources, prompts and OAuth (#12).
- `process` watch-for-line (#13).

## Appearance (Jasper, 2026-09-28)
- **Notification lifecycle.** Notifications disappear / get marked seen when their source
  is acknowledged — e.g. when an approval request is finalized, clear the
  "approval required" notification automatically.
- **Popup auto-fade.** Notification popups (the ones not on the desk) should show for a
  few seconds, then fade.
- **Multi-thread / desk view.** A view showing all threads (and the desk) at once,
  beyond the current single-conversation focus.
- **Graph view.** Graph visualization of vault links / conversation relationships.
- **Token metrics.** Per tool call / turn / thread token totals (in and out), for
  visibility into where context goes. Explore later.
- **New color palettes** — approved 2026-09-28: grape, ocean, ember, moss (green-on-dark).
  Implemented in worktree `dev-01M3N9P0KV51GQ3M7865CXHWDQ`, awaiting promotion sign-off.

## Undecided (TD audit, not yet agreed)
- Hold a system-required power request while a turn runs or an approval is pending, so
  the machine does not sleep mid-task. Battery policy is the person's call. (F7)
- `conversation_finish` requires an artifact or image id in the summary when the goal
  asked for visual proof. Leans on the model; the finish delivery (shipped 2026-09-24)
  already hands the spawner every artifact and image id. (§3)
