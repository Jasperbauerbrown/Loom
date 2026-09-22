Manage background processes started with `shell(background=true)`: yours and everyone else's.

Arguments:
- `action` (string, required): `list`, `logs` or `stop`.
- `id` (string): the process id, from the `started process …` result or from `list`. Required for `logs` and `stop`.
- `offset` (integer, optional, `logs` only): character offset into the log to start at. Omitted, you get the tail — the most recent output.
- `limit` (integer, optional, `logs` only): characters to return; the default and the maximum are the tool output cap.

`list` returns one line per process — id, title, status (running, exited, stopped, failed, lost), pid, start and end times, exit code, log size, and the conversation it belongs to — running ones first. `logs` returns a slice of the process's output with a footer naming the next offset when more follows. `stop` kills the process and everything it started, and marks it stopped; the process's own conversation is told when someone else stops it. You are told in your conversation when a process you started exits, with its exit code and the last lines of output; you do not need to poll for that.
