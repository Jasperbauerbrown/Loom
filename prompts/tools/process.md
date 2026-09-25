Manage background processes started with `shell(background=true)`: yours and everyone else's.

Arguments:
- `action` (string, required): `list`, `logs` or `stop`.
- `id` (string): the process id, from the `started process …` result or from `list`. Required for `logs` and `stop`.
- `tail` (boolean, optional, `logs` only): read the last page from the current end of the log, however far it has grown. Wins over `since`/`offset`, and is the default when none of the three are given.
- `since` (integer, optional, `logs` only): character offset to read from, forward. Hand it back the offset the previous read's footer named.
- `offset` (integer, optional, `logs` only): same meaning as `since`, kept for old calls.
- `limit` (integer, optional, `logs` only): characters to return; the default and the maximum are the tool output cap.

`list` returns one line per process — id, title, status (running, exited, stopped, failed, lost), pid, start and end times, exit code, log size, and the conversation it belongs to — running ones first. `logs` returns a slice of the process's output; its footer always names the offsets and the exact next call — follow a growing log by reading, then passing the footer's offset back as `since`, and jump to the end of one that has run far ahead with `tail=true`. `stop` kills the process and everything it started, and marks it stopped; the process's own conversation is told when someone else stops it. You are told in your conversation when a process you started exits, with its exit code and the last lines of output; you do not need to poll for that.
