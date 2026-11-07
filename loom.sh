#!/bin/sh
# Loom launcher (Linux/macOS). With no arguments it serves the app (the end-user path:
# under the supervisor). Anything else is passed straight through to the CLI, so
# `./loom.sh send "hi"`, `./loom.sh status` and `./loom.sh serve --no-supervisor --fake`
# all work from here too. The POSIX twin of loom.bat.
#
# uv is found on PATH, then at ~/.local/bin/uv (the standalone installer's default),
# then as a pip package (`python3 -m uv`).
set -e
cd "$(dirname "$0")"
export PYTHONUTF8=1

if command -v uv >/dev/null 2>&1; then
    UV=uv
elif [ -x "$HOME/.local/bin/uv" ]; then
    UV="$HOME/.local/bin/uv"
elif python3 -m uv --version >/dev/null 2>&1; then
    UV="python3 -m uv"
else
    echo "loom: uv is not installed. Install it with:" >&2
    echo "    curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
    exit 1
fi

# Bring .venv in line with uv.lock: a no-op once it already is; the second attempt
# re-resolves for the case where pyproject.toml has moved ahead of the lockfile.
$UV sync --frozen --quiet || $UV sync --quiet || {
    echo "loom: could not install dependencies." >&2
    exit 1
}

if [ $# -eq 0 ]; then
    exec $UV run loom serve
else
    exec $UV run loom "$@"
fi
