A thread turn just ended without updating its status. You are given the thread's goal,
the current status, and the turn's events (user messages, assistant messages, tool
calls with a short result preview). Produce the status fields as strict JSON with
exactly these keys:

{"state": "planning|working|blocked|waiting|done",
 "goal": "…", "done": "…", "doing": "…", "next": "…", "blockers": "…"}

Rules:
- Update only what the turn changed; carry the rest over unchanged.
- `done` accumulates: keep earlier items, add what this turn completed.
- `blockers` is empty unless something concrete stops progress; name it precisely.
- `state` is `waiting` when the thread asked the person a question and stopped;
  `blocked` when a blocker is set; `done` only if the thread called finish.
- Facts, absolute dates, no narration. Output the JSON object only.
