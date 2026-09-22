# CLAUDE.md — Loom

Loom is a local-first personal assistant with a technical focus: one always-on
conversation (the **desk**), parallel **threads** for long work, an Obsidian-style
markdown **vault** as memory, and **capabilities** (tools, MCP servers, connectors).
Backend Python 3.12 (FastAPI + stdlib SQLite), web UI React/Vite/TS, models via
OpenAI-compatible HTTP (Ollama local, OpenRouter remote). Name provisional.

**The docs are authoritative.** Read in this order before changing anything:
`docs/README.md` → `docs/00-charter.md` (ethos, rules, decisions) →
`docs/03-technical-design.md` (what to build) → `docs/04-soul.md` (constraints on the soul; the prompts themselves are `prompts/*.md`) →
`docs/01-walkthroughs.md` and `docs/02-research-takeaways.md` as needed.
When code and docs disagree, update the docs in the same change. Record design
decisions in the charter's decision log with a date.

## Ethos (from the charter; the short form)

- **Robustness.** The widest range of situations through the simplest mechanism. Few
  general codepaths; no special cases; failure is an expected input with one defined
  behaviour per class.
- **Consistency.** UI and backend never disagree. Every setting is UI-visible; no config
  file is the only interface. Same behaviour on every surface and machine.
- **Simplicity.** Sensible defaults; configuration is an override. No ten-minute setup.
- **Capable-model bar.** Design for Qwen 3.6 35b-a3b / dense 27b. No scaffolding whose
  only job is to absorb a weaker model's mistakes. Context is a budget for efficiency,
  not because the model cannot cope.
- **System prompts are soul only.** Identity, temperament, method. Never mutable state
  (models, paths, rosters); that lives in the vault.
- **Fully realised.** Whatever is built is built completely: every relevant setting,
  every obvious argument, UI parity for every knob. Scope is cut visibly, never silently.

## Hard rules

- **Dependency-light.** Runtime deps are exactly those listed in `03` §19 unless this
  file and the doc are updated with a reason. Build small things from scratch.
- **Single entrypoints.** `run_turn` is the only agent loop; `assemble` the only context
  builder; the tool dispatcher the only path from a model's tool call to execution.
- **Persist before act.** SQLite rows are written before the action they describe.
  Transcript events are append-only.
- **Fail closed.** Unknown tool or unset policy behaves as `ask`. Nothing static ever
  *denies* a shell command (the pre-filter may only grant).
- **Model-facing prose lives in `prompts/*.md`.** Never hardcode it.
- **Tests never call a live model or the machine's credential store.** The fake model
  client and the conftest fake keyring are the test harness.
- **No lint/type tooling uninvited.** UI type-checks via `tsc` during build.

## Commands (as designed; keep this block true as the repo takes shape)

`uv` is installed as a pip user package and is NOT on PATH — always `python -m uv`.

```bash
python -m uv run pytest                    # full suite, fast, no live model
python -m uv run loom serve --no-supervisor            # migrate, then serve http://127.0.0.1:8500
python -m uv run loom serve --no-supervisor --port N
python -m uv run loom serve --no-supervisor --fake     # fake model client, own home ~/.loom-fake, for UI work
python -m uv run loom serve                            # end-user path: runs under the supervisor
python -m uv run loom reindex              # rebuild vault indexes from markdown
python -m uv run loom send "text"          # CLI client; blocks until the turn ends
python -m uv run loom send --thread ID "text"
python -m uv run loom status               # health + model queue

npm --prefix ui install
npm --prefix ui run build                  # tsc -b && vite build → ui-dist/ (committed)
npm --prefix ui run test
npm --prefix ui run dev                    # proxies /api and /ws to :8500
```

## Environment quirks (Windows dev box)

- Use the Bash tool (Git Bash, POSIX syntax). PowerShell tool calls are denied.
- `python -m uv`, never bare `uv`. System Python is 3.11; the project runs on
  uv-managed 3.12.
- Set `PYTHONUTF8=1` when running scripts that print non-ASCII.
- Kill stray servers with `taskkill //PID <pid> //F`; bash `kill` only kills the subshell.
- Live-model work needs Ollama running locally (bar model + `nomic-embed-text`). A
  second Ollama host may exist on the LAN; models there swap and are slow on first call.

## Conventions

- Hand-written SQL migrations in `src/loom/db/migrations/NNNN_*.sql`; the migrator keys
  on `PRAGMA user_version`. No autogenerate.
- Tests live in `tests/`, named by module or phase; the four walkthroughs in `docs/01`
  are integration tests.
- Commits: `feat/fix/chore(scope): ...`.
- UI changes reach the served app only after `npm --prefix ui run build`.
- Build in the phase order of `03` §20; a phase is done when its UI, settings, tests and
  docs are done.
