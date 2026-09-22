# Loom — soul and operating rules

**2026-09-20, revised 2026-09-22.** The soul is the only model-facing text that describes
who Loom *is*. It lives in the repository as prose files, and those files are the source
of truth — this document no longer carries a copy, because the copy drifted within two
days of the build starting:

| File | Role | Budget |
|---|---|---|
| `prompts/system.md` | **Soul** (identity, temperament, method) and **Operating rules** (knowledge, effort ladder, threads, tools, communication) | soul ≤ 40 lines (37 on 2026-09-22), rules ≤ 60 lines (65 on 2026-09-22: over budget after the day's additions; trim when the next rule lands) |
| `prompts/kind-desk.md` | what the desk conversation is and how it treats threads | ≤ 12 lines |
| `prompts/kind-thread.md` | what a thread is: goal line, pinned notes, status, endings | ≤ 12 lines |

The design constraints, which the files must keep honouring:

- **Static prose, never state.** No model names, paths, device lists, project names,
  roots or rosters. Those are mutable state; they live in the vault or in the harness
  context block (03 §7) and arrive through context, not the prefix.
- **Cache-stable prefix.** All three files sit at the top of every turn, so every added
  line costs every turn. A rule earns its place by changing behaviour that was observed
  to go wrong; each such addition is logged in the charter's decision log (00 §6) with
  the observation that justified it.
- **Soul changes rarely; rules change with the harness.** A new tool or a new failure
  class may add a rule. Identity does not move.
- **Kind files add, never contradict.** Anything true for both kinds belongs in
  `system.md`.

When a rule is added or changed, the same change updates the section of `03` that
describes the mechanism it refers to, and the decision log records why. Reading order
for someone new: `00-charter.md` §2 for the ethos, then the three prompt files, then this
page for the constraints they must keep.
