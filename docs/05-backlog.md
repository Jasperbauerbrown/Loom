# Backlog

Concrete, agreed engineering work that is not yet built. Design questions belong in the
charter (§5.11 open questions); the build order is `03` §20. When an item ships, delete
it here and record it where it now lives (`03`, the decision log).

Each item names its source. "Audit" means `reference/tool-parity-audit.md` §3.

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
