# Tool parity audit: Loom vs Claude Code vs opencode (2026-09-24)

Goal: Loom should be, *from the harness side*, able to do anything these two coding
harnesses can. This compares the model-facing tool surface and the harness mechanics
directly behind it. It says nothing about the models behind them.

Sources:
- **Loom**: `src/loom/tools/**`, `src/loom/conversation/tools.py`, `src/loom/connectors/mcp/*`,
  `bootstrap.py`. Current as of `94a2f38`.
- **Claude Code**: the tool schemas exposed to a live Claude Code session (2026-09),
  plus its documented harness features.
- **opencode**: `github.com/anomalyco/opencode`, the `dev` branch,
  `packages/opencode/src/tool/*` (`sst/opencode` now redirects there). Tools shipped
  behind a flag are marked *(flag)*.

Legend: ✅ parity or better · 🟡 partial · ❌ missing.

## 1. Tool-by-tool

| Capability | Claude Code | opencode | Loom | |
|---|---|---|---|---|
| Read file | `Read`: `cat -n` line numbers, offset/limit, images, PDF (page ranges), `.ipynb` cells | `read`: `N: ` line numbers, 2000-line default, 50 KB cap, images + PDF as attachments, lists directories | `fs_read`: offset/limit, char-capped page + footer, images; **no line numbers**, no PDF or notebook | 🟡 |
| Write file | `Write`: must have Read the file first | `write`: diff for the ask, formatter, LSP diagnostics | `fs_write`: no stale-write check | 🟡 |
| Edit | `Edit`: exact, unique, `replace_all`, must Read first | `edit`: a fuzzy replacer cascade (9 strategies), BOM/CRLF preserved, formatter + LSP diagnostics after the edit | `fs_edit`: exact, unique, `replace_all`, returns a diff; one edit per call, no stale check | 🟡 |
| Multi-file patch | — | `apply_patch` (V4A: add/update/delete/move); GPT-class models only | ❌ | ❌ |
| Glob | `Glob`: sorted by mtime | `glob`: ripgrep, respects `.gitignore`, cap 100 | `fs_list`: `Path.glob`, hides dotfiles; **no `.gitignore`**, no mtime sort | 🟡 |
| Grep | `Grep`: ripgrep; output modes content / files / count; `-A/-B/-C`, multiline, type filter, `head_limit` + `offset` | `grep`: ripgrep, `include` glob, grouped by file | `fs_search`: Python `re`, context, glob; **no `.gitignore`**, no files/count modes, no paging, no multiline | 🟡 |
| Shell | `Bash`: cwd persists across calls, timeout up to 10 min, `run_in_background` | `bash`: tree-sitter parse drives per-command permissions (arity-aware patterns); `workdir`; 2000 lines / 50 KB cap with spill | `shell`: flavour setting, tree kill, stdin, env, `background`; a fresh process each call (`cwd` passed as an argument) | ✅ |
| Background processes | `run_in_background` + `Monitor` (streams lines as events) + `TaskStop` | *(flag)* background subagents only | `process` list/logs/stop + an inbox message on exit; **no watch-for-line** | 🟡 |
| Web fetch | `WebFetch`: HTML→markdown, then a small model extracts against a prompt | `webfetch`: text / markdown / html format, 5 MB cap, retries a Cloudflare challenge | `http`: every method, headers with secret templating, images; **returns raw HTML** | 🟡 |
| Web search | `WebSearch` | `websearch` (Exa / Parallel over MCP; opencode's own providers only) | ❌ | ❌ |
| Subagents | `Agent`: typed agents (`Explore`, `Plan`, custom), foreground or background, `fork`, worktree isolation, resume by message | `task`: typed agents (`general`, `explore`), foreground blocks and returns the result, resume via `task_id`, depth 1 | `conversation_create` / `send` / `read` / `finish`: always asynchronous, full tool set, depth 3; **no blocking call, no restricted-tool agent types** | 🟡 |
| Todo / plan list | `TodoWrite` / task tools | `todowrite` | threads: `status_update` (free-form); desk: none | 🟡 |
| Plan mode | `EnterPlanMode` / `ExitPlanMode` | `plan` agent + *(flag)* `plan_exit` | ❌ (no read-only mode) | ❌ |
| Ask the user | `AskUserQuestion`: options, multi-select, previews | `question` (desktop/app clients; flag) | ❌ (the desk just replies; a thread uses `item_post`) | ❌ |
| Skills | `Skill` | `skill` (SKILL.md + sibling files) | `procedure_load` (vault procedures) | ✅ |
| Deferred tools | `ToolSearch` | — | `tool_search` (keyword scoring, loaded for the current turn only) | ✅ |
| MCP | stdio / HTTP / SSE, OAuth, **resources** | local / remote (SSE fallback), OAuth | stdio + streamable HTTP, **tools only**: no resources, no prompts, no OAuth | 🟡 |
| LSP | through plugins | diagnostics after every edit; *(flag)* `lsp` tool (definition, references, hover, symbols, call hierarchy) | ❌ | ❌ |
| Notebooks | `NotebookEdit` + reading `.ipynb` | — | ❌ | ❌ |
| Scheduling | `CronCreate` / `ScheduleWakeup` | — | `schedule_*` | ✅ |
| Git worktree isolation | `EnterWorktree`, `isolation: worktree` on agents | workspaces *(flag)* | self-mod worktrees only (§15) | 🟡 |
| Screen | — (computer use is separate) | — | `screenshot` | ✅ |
| Secrets | — | — | `secret_set` + `{{secret:NAME}}` + output redaction | ✅ |
| Memory | CLAUDE.md + a memory directory | AGENTS.md | vault: `knowledge_*`, `transcript_search`, pinned notes | ✅ |

## 2. Harness mechanics behind the tools

| Mechanic | Claude Code | opencode | Loom |
|---|---|---|---|
| Permissions | modes (default / acceptEdits / plan / auto / bypass) + allow/ask/deny rules per tool with specifiers | allow/ask/deny, glob patterns per command, per-agent rulesets, unmatched → ask | allow/ask/classify per tool + path classes, pre-filter, LLM classifier, desk auto-approve; never denies (by design) |
| Undo / checkpoints | rewind (restores code and conversation) | shadow-git snapshot per project; `revert` / `unrevert` | ❌ outside the vault (the vault is committed to git) |
| Hooks / plugins | Pre- and PostToolUse hooks, and more | plugin hooks `tool.execute.before/after`, `shell.env`, system-prompt transform, custom tool files | connectors add tools, events and surfaces; **no before/after-tool hooks** |
| Output overflow | truncates and spills to a file | 2000 lines / 50 KB, spilled, points at `explore` | cap, then spill to an artifact, `artifact_recall` pages it ✅ |
| Doom-loop guard | — | the same call 3× → ask | — (deliberately; see §4) |
| Compaction | ✅ | ✅ | ✅ (§7) |

## 3. Gaps, ranked

Agreed items are tracked in `../05-backlog.md`.

Ranked by how much harness capability each unlocks per unit of work. Each item stays
within §19 (no new runtime dependencies) unless it says otherwise.

**P1: coding table stakes**
1. **Line numbers in `fs_read`** (`N\t` prefix; an option to turn them off for prose).
   Every edit, review and citation depends on them. Trivial.
2. **Stale-write guard on `fs_write` and `fs_edit`.** Refuse when the file changed on
   disk since this conversation last read or wrote it (mtime + size recorded per
   conversation). This is not scaffolding for weak models: users edit the vault in
   Obsidian and their own editors at the same time, and `fs_write` / `fs_edit` never check
   whether the file changed since the model last saw it.
3. **Multi-edit.** Add `fs_edit(edits=[{old,new,replace_all}])`, applied atomically in
   order, one diff. Later, an `apply_patch` that takes a unified diff across files
   (built on the same stdlib, `difflib`-level).
4. **`.gitignore`-aware `fs_list` and `fs_search`.** A small in-house gitignore
   matcher (or `git ls-files -co --exclude-standard` when the root is a repository),
   on by default with an `include_ignored` flag. Add to `fs_search` an `output`
   (content / files / count), `offset` paging and `multiline`; add mtime sorting to
   `fs_list`. Without these, searches in a real repository drown in
   `node_modules` / `.venv`.
5. **Readable fetches.** Give `http` a `format` argument (raw / text / markdown),
   converting HTML to markdown with stdlib `html.parser`. Raw HTML burns context
   budget.
6. **Web search.** Add a `web_search(query, k)` tool behind a provider setting
   (SearXNG URL, Brave, Exa, Tavily; key from a secret). Ship it native, with the
   setting in the UI, and have it fail with the defined "not configured" error until
   a provider is set.
7. **Blocking, scoped subagents.** Add `conversation_create(wait=true)`: the call
   returns the thread's `finish` summary, with the step budget as its bound. Add a
   **tool profile** per thread (`full` / `read_only`, the `explore` equivalent),
   enforced in the dispatcher as a kind-like filter. This makes "go look and report"
   one call instead of an asynchronous round-trip through the inbox.
   **Rejected 2026-09-24:** asynchrony is the point of threads; a blocking subagent
   would undo Loom's shape.

**P2: parity on long work**

8. **Plan list for both kinds.** A `todo(items=[{text,state}])` tool whose state is
   rendered in the UI and re-injected by `assemble`, so it survives compaction.
   `status_update` stays for thread reporting.
   **Rejected 2026-09-24:** the thread status (goal / done / doing / next / blockers)
   is already the plan, re-injected every turn (`02`), and the desk hands open-ended
   work to a thread. A per-item list adds only finer ticking-off.
9. **`ask_user(question, options[], multi)`.** It sets `waiting_input` and the UI
   renders the choices, the same flow as an approval. Threads need this most: today
   they can only post an item.
10. **Checkpoints and undo for files under the roots.** Keep a shadow git repository
    per root under `APP_HOME/snapshots/`, snapshot before each writing tool call, and
    add "revert to here" on any transcript event. The persist-before-act principle
    already hands us the hook point.
11. **LSP diagnostics after edits.** Build an LSP client on the existing stdio JSON-RPC
    transport from MCP, with servers configured per extension in Settings, and append
    the diagnostics to `fs_edit` / `fs_write` results. Later, an `lsp` query tool
    (definition, references, symbols).
12. **MCP resources, prompts and OAuth.** Resources become
    `mcp_<server>_resource_read` plus a listing; for remote servers, add OAuth
    (authorization code + PKCE, local callback).
13. **Process watch.** `process(action="watch", id, pattern)` posts an inbox message
    when a log line matches (the `Monitor` equivalent), so "tell me when the server is
    up" needs no polling.

**P3: completeness**

14. Before/after-tool hooks (a connector-declared `on_tool_call`, which can annotate
    or turn an allow into an ask, never deny).
15. Worktree isolation for any thread (`conversation_create(isolation="worktree")`),
    generalising §15.
16. PDF text in `fs_read` (via `pdftotext` when it is on PATH; a new dependency
    otherwise, which needs a §19 entry).
17. `.ipynb` read and edit (plain JSON: cheap).
18. Preserving line endings and the BOM in `fs_edit` / `fs_write`.

## 4. Deliberately not adopted

- **opencode's fuzzy edit cascade.** It absorbs a weak model's whitespace mistakes;
  the charter's capable-model bar rules it out. An exact-match failure already tells
  the model what to do.
- **The doom-loop guard.** Same reasoning. The step budget is the defined bound.
- **Static deny rules.** Loom never denies statically (hard rule). Per-command
  *allow* patterns would fit, though: they are the pre-filter's list, already a setting.
- **Code-mode / `execute`** (scripting MCP tools from a sandbox). Still experimental
  there; revisit once MCP rosters get large.
- **Model-specific tool sets** (opencode swaps `edit` for `apply_patch` on GPT models).
  Loom picks one tool set for every model.

## 5. Where Loom is already ahead

A persistent desk alongside asynchronous threads; the vault as memory, with retrieval,
pinning and links; scheduling; background-process supervision that wakes the
conversation on exit; `http` with every method and secret templating; screenshots;
secret redaction in outputs; an LLM classifier for permissions; self-promotion with
rollback.
