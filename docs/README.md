# Loom design documents

This folder is the design record and hand-off package for Loom. It becomes `/docs` in
the Loom repository; `CLAUDE.md` here moves to the repository root.

| Read | File | What it is |
|---|---|---|
| 1 | `00-charter.md` | Purpose, ethos, technical constraints, the assistant's shape (desk / knowledge / capabilities / threads), and the dated decision log. The place to check before re-opening a settled question. |
| 2 | `03-technical-design.md` | Implementation-level design: processes, schema, vault format, turn loop, context slots, tools and permissions, MCP and connectors, triggers, API, UI, settings inventory, promotion, dependencies, build order. |
| 3 | `04-soul.md` | The constraints the soul must keep (static prose, cache-stable prefix, line budgets). The prompts themselves live in `prompts/system.md`, `kind-desk.md`, `kind-thread.md`, the only model-facing text that says who Loom is. |
| 4 | `01-walkthroughs.md` | The four example requests traced through the design; also the specification for the integration tests. |
| 5 | `02-research-takeaways.md` | What was adopted, adapted and rejected from the field survey, with reasons. |
| ref | `reference/agentic-harnesses.md` | Survey of ~85 agentic harnesses (2026-09-17). |
| ref | `reference/harness-study.md`, `reference/context-lifecycle.md` | Hydra's context-management write-ups; the source of the compaction, calibration and repair mechanics adopted in `03`. Paths inside refer to Hydra. |

Conventions for editing these docs:
- Decisions go in the charter's decision log with a date and a one-line reason.
- Behaviour that diverges from `03` is a doc change in the same commit.
- Keep the charter's cause/effect form; keep `03` in "decided / lean" language.
