Run one command line on the local machine and return its output.

Arguments:
- `command` (string, required): the command line, in the configured shell's syntax. Pipes and `&&` chains are allowed.
- `cwd` (string, optional): the working directory. Must be inside an allowed root; defaults to the first allowed root.
- `timeout_s` (number, optional): seconds before the process tree is killed; defaults to the configured shell timeout. Ignored for a background process.
- `env` (object, optional): extra environment variables, added to the inherited environment. Values may use `{{secret:NAME}}`; the value is substituted only as the process starts and never appears in the transcript.
- `stdin` (string, optional): text piped to the process. Ignored for a background process.
- `background` (boolean, optional, default false): start the command as a background process and return at once with its id and log path. Use it for anything that must keep running — a server, a watcher, a bot. A foreground call that never ends is not "running in the background": it is killed when the timeout fires, and so is everything it started. Appending `&` does not help either; the call waits for the output pipe to close.
- `title` (string, optional): a short label for a background process, shown wherever it is listed.

Returns stdout and stderr merged in the order they were produced, ending with `[exit N]`. A command that only reads runs immediately; anything else is judged, and may pause for the user's approval. Long output is spilled to an artifact and recalled with `artifact_recall`. A background process returns `started process <id> …` instead: read its output with `process(action="logs", id=…)`, end it with `process(action="stop", id=…)`, and expect a message in this conversation when it exits.
