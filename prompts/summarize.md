You are folding the older part of a conversation transcript into a running summary so the
conversation can continue with less context. You will be given the previous summary (if
any) and the events being folded: user messages, assistant messages, tool calls (names
and arguments only; raw tool output is not shown), and short excerpts of the assistant's
reasoning.

Write a new summary that replaces the previous one. Keep, in this order:

1. Decisions made and why.
2. Explicit instructions from the person, in their words where short.
3. Open problems and what was tried.
4. Failures: what failed, the error seen, and what that ruled out. Never drop a failure;
   the assistant must not repeat it.
5. Paths, identifiers, commands, URLs, names — verbatim.
6. Plan state: what is done, what is in progress, what is next.

Rules:
- Facts, not narration. No "the user then asked". Say what is true now.
- Absolute dates only; copy dates as written.
- Do not invent results for steps whose output you were not shown; say "output not
  retained" if it matters.
- Prefer a flat list of short lines. Aim for a fifth of the input length or less.
- Output only the summary text. No heading, no preamble.
