# Backlog

Concrete, agreed engineering work that is not yet built. Design questions belong in the
charter (§5.11 open questions); the build order is `03` §20. When an item ships, delete
it here and record it where it now lives (`03`, the decision log).

Each item names its source. "Audit" means `reference/tool-parity-audit.md` §3.

## Tools: coding parity (audit P1)
- **fs_read line numbers.** `N\t` prefix by default, with an option to turn them off. (Audit #1.)
- **Stale-write guard.** `fs_write` and `fs_edit` refuse when the file changed on disk
  since this conversation last read or wrote it (mtime + size per conversation). (Audit #2.)
- **Multi-edit, then apply_patch.** `fs_edit(edits=[…])`, applied atomically in order
  with one diff. Later, a unified-diff `apply_patch` that works across files. (Audit #3.)
- **Ignore-aware search.** `fs_list` and `fs_search` honour `.gitignore` by default
  (`include_ignored` to opt out). `fs_search` gains `output` (content / files / count),
  `offset` and `multiline`; `fs_list` gains mtime sorting. (Audit #4.)
- **Readable fetches.** An `http` `format` argument (raw / text / markdown), with HTML
  converted to markdown through stdlib `html.parser`. (Audit #5.)
- **Web search.** A native `web_search(query, k)` behind a provider setting (SearXNG /
  Brave / Exa / Tavily, key from a secret), failing with a "not configured" error until
  a provider is set. (Audit #6.)

## Tools: completeness (audit P3)
- Before/after-tool hooks declared by connectors (may annotate, or turn an allow into
  an ask; never deny). (Audit #14.)
- Worktree isolation for any thread, generalising `03` §15. (Audit #15.)
- PDF text in `fs_read` (`pdftotext` when it is on PATH; any new dependency needs a §19
  entry). (Audit #16.)
- `.ipynb` read and edit. (Audit #17.)
- `fs_edit` / `fs_write` keep the file's line endings and BOM. (Audit #18.)

## Undecided (audit P2, not yet agreed)
- `ask_user` with options, rendered like an approval (#9).
- Checkpoints and undo for files under the roots (#10).
- LSP diagnostics after edits (#11).
- MCP resources, prompts and OAuth (#12).
- `process` watch-for-line (#13).

## Tools: from the tower-defense run (TD audit)
"TD audit" means `reference/tower-defense-run-audit.md` §2.
- **Screenshot of an occluded window.** `screenshot(window=…)` captures the window's
  own pixels (`PrintWindow` with `PW_RENDERFULLCONTENT`, falling back to the rect copy
  when it returns black); `region=` and `monitor=` stay as they are and the prompt says
  they capture whatever is visible. (F2)
- **Screenshot errors readable.** The PowerShell capture reports its failure on stdout
  in plain text (or Loom decodes the CLIXML stream); the model must see "no window
  whose title contains …", never `#< CLIXML`. (F2)
- **Shell result names what it killed.** When the kill-on-close job ends processes the
  command left running, the result's trailer lists them: `[ended 1 process the command
  left running: tower-defense-graphical.exe pid 5732]`. One line teaches the rule the
  prompt already states. (F3)
- **Dispatcher honours the call's `timeout_s`.** `timeout_for` takes the argument when
  the tool declares one (shell, http), clamped by a maximum setting; the tool's own
  timeout is then the only one, so partial output survives. (F5)
- **`process(logs)` tail mode.** `since` (the offset footer of the last read) or
  `tail=true`, so a fast-growing log is read from its end without offset bookkeeping.
  Distinct from watch-for-line (#13 below). (F8)
- **Vault-shaped paths in `fs_edit`/`fs_write`.** When a relative path matches an
  existing vault note, the error says so and names `knowledge_write`. (F9)
- **`knowledge_write` replace accepts `new`.** Alias for `content` under `op=replace`,
  or the prompt states the field names next to the op. (F9)

## Threads and the desk (TD audit)
Priority within this section: the first two items. The handoff failed at three layers
(the summary did not cite, the mechanism did not carry, the desk did not read); the
delivery and artifact-access changes fix the two that do not depend on the model. The
prompt changes are cheap and worth doing but would not alone have let the desk open the
cited screenshot. (§3)
- **Finish delivery carries the evidence.** The inbox message to the spawner includes
  the walkthrough section (path and text, it is a few KB) and the artifacts and images
  the thread produced, as ids with captions. The desk's judgment is fed, not pointed at.
  (§3)
- **Spawner reads the thread's artifacts.** `artifact_recall` (and image recall) accept
  an id from any conversation in the spawn tree below the caller. (§3)
- **Approval resume keeps the step count.** A turn resumed after an approval pause
  continues at step N+1 with the remaining budget, not at step 1. (F6)
- **Prompts.** `conversation_finish` summary: name what was verified, what was not, and
  how the spawner can check it in one step. `kind-desk`: open the evidence a thread
  cites before reproducing anything; when it cannot be opened, say so and stop; never
  rebuild a thread's verification harness. Thread: verification stops when the cited
  evidence exists; no rebuild after doc-only edits. (F4, F10)

## Permissions and approvals (TD audit)
- **Classifier sees the script.** When the command runs a file under the artifacts
  folder or one written earlier in this conversation, its contents (bounded) are part
  of the classifier input. (F1)
- **Classifier sees the intent.** For the desk, the request text is the turn's stated
  intent (the step's preamble or last assistant text), not a bare "Desk" goal and the
  last user message. (F1)
- **"Allow this call for the conversation."** A third card option keyed on the tool and
  an argument hash, between allow-once and `allow_always`. (F1)
- **Pending approval notifies.** An approval posts a `notification` item with
  `notify=True` (the existing interrupt path) so a person away from the screen is told.
  (F1)

## Runtime (TD audit)
- **Suspend is a named event.** The runner detects a wall-clock jump across a model
  call and logs "system resumed after N min; retrying" instead of an idle timeout. (F7)

## Undecided (TD audit, not yet agreed)
- Hold a system-required power request while a turn runs or an approval is pending, so
  the machine does not sleep mid-task. Battery policy is the person's call. (F7)
- `conversation_finish` requires an artifact or image id in the summary when the goal
  asked for visual proof. Leans on the model; may be better served by the delivery
  change above. (§3)
