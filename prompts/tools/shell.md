Run one command line on the local machine and return its output.

Arguments:
- `command` (string, required): the command line, in the configured shell's syntax. Pipes and `&&` chains are allowed.
- `cwd` (string, optional): the working directory. Must be inside an allowed root; defaults to the first allowed root.
- `timeout_s` (number, optional): seconds before the process tree is killed, honoured up to the configured ceiling; defaults to the configured shell timeout. A call that runs out of time still returns the output the command produced before the kill. Ignored for a background process.
- `env` (object, optional): extra environment variables, added to the inherited environment. Values may use `{{secret:NAME}}`; the value is substituted only as the process starts and never appears in the transcript.
- `stdin` (string, optional): text piped to the process. Ignored for a background process.
- `background` (boolean, optional, default false): start the command as a background process and return at once with its id and log path. Use it for anything that must keep running — a server, a watcher, a bot. A foreground call that never ends is not "running in the background": it is killed when the timeout fires, and so is everything it started. Appending `&` does not help either; the call waits for the output pipe to close.
- `title` (string, optional): a short label for a background process, shown wherever it is listed.

Write commands for the shell flavour named below. Everything a foreground call starts ends when the call ends, timeout or not. The harness block names the backend Loom itself runs as: never stop it, kill its process tree, or bind a server over its address.

Returns stdout and stderr merged in the order they were produced, ending with `[exit N]`; when the call ends processes the command left running, a last line names them (`[ended 1 process the command left running: game.exe pid 5732]`) — use `background=true` for anything that must outlive the call. A command that only reads runs immediately; anything else, and any command that names a protected path, is judged, and may pause for the user's approval. Long output is spilled to an artifact and recalled with `artifact_recall`. A background process returns `started process <id> …` instead: read its output with `process(action="logs", id=…)`, end it with `process(action="stop", id=…)`, and expect a message in this conversation when it exits.
