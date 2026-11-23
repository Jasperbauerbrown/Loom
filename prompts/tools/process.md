Manage background processes started with `shell(background=true)`: yours and everyone else's.

Arguments:
- `action` (string, required): `list`, `logs`, `wait`, `send` or `stop`.
- `id` (string): the process id, from the `started process …` result or from `list`. Required for `logs`, `wait`, `send` and `stop`.
- `input` (string, `send` only): text written to the process's stdin; a trailing newline is added unless `newline=false`.
- `newline` (boolean, optional, `send` only): set false to omit the trailing newline.
- `tail` (boolean, optional, `logs` only): read the last page from the current end of the log, however far it has grown. Wins over `since`/`offset`, and is the default when none of the three are given.
- `since` (integer, optional, `logs` only): character offset to read from, forward; for `wait`, where matching starts (default 0, so a line printed before you started waiting still matches). Hand it back the offset the previous read's footer named.
- `offset` (integer, optional, `logs` only): same meaning as `since`, kept for old calls.
- `until` (string, optional, `wait` only): a regular expression (multiline) matched against the log text.
- `timeout_s` (number, optional, `wait` only): seconds to wait, honoured up to the configured ceiling; a default applies if omitted.
- `limit` (integer, optional, `logs` only): characters to return; the default and the maximum are the tool output cap.

`list` returns one line per process — id, title, status (running, exited, stopped, failed, lost), pid, start and end times, exit code, log size, and the conversation it belongs to — running ones first. `logs` returns a slice of the process's output; its footer always names the offsets and the exact next call — follow a growing log by reading, then passing the footer's offset back as `since`, and jump to the end of one that has run far ahead with `tail=true`. `stop` kills the process and everything it started, and marks it stopped; the process's own conversation is told when someone else stops it. You are told in your conversation when a process you started exits, with its exit code and the last lines of output; you do not need to poll for that. To block on a process inside this step instead of polling `logs` or ending your turn, use `wait`: it returns when `until` matches (naming the character offset and quoting the matching line), when the process is no longer running (status, exit code, last output), or at `timeout_s` (still running, with the last output). Its footer names the `since` to continue from.

`send` feeds text to the stdin of an **interactive session** — a background process started with `shell(background=true, interactive=true)`, which holds its stdin open: a `python -i` REPL, `adb shell`, a telnet or serial bridge, gdb. Hold one open and send to it across tool calls instead of re-spawning (and re-connecting, re-parsing) every time. Its output is read back like any other process, with `logs` (tail the reply) or `wait` (block until a prompt or pattern appears). An interactive session lives only in this backend's memory, so it does **not** survive a backend restart: afterwards its handle is gone and `send` fails (the row reconciles like any other process). `send` to a non-interactive or exited process is refused, telling you why.
