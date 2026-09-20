Run one command line on the local machine and return its output.

Arguments:
- `command` (string, required): the command line, in the configured shell's syntax. Pipes and `&&` chains are allowed.
- `cwd` (string, optional): the working directory. Must be inside an allowed root; defaults to the first allowed root.
- `timeout_s` (number, optional): seconds before the process tree is killed; defaults to the configured shell timeout.
- `env` (object, optional): extra environment variables, added to the inherited environment. Values may use `{{secret:NAME}}`; the value is substituted only as the process starts and never appears in the transcript.
- `stdin` (string, optional): text piped to the process.

Returns stdout and stderr merged in the order they were produced, ending with `[exit N]`. A command that only reads runs immediately; anything else is judged, and may pause for the user's approval. Long output is spilled to an artifact and recalled with `artifact_recall`.
