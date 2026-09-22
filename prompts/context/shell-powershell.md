Commands run through `powershell -NoProfile -Command`. A failing cmdlet does not set a
non-zero exit code by itself, so `[exit 0]` can follow an error: pass `-ErrorAction Stop`,
or end with `exit $LASTEXITCODE` when you need a native program's status.
