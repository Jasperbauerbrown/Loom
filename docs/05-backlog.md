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
