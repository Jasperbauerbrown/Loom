@echo off
rem  Loom launcher. With no arguments it serves the app (the end-user path: under the
rem  supervisor, browser opened for you). Anything else is passed straight through to
rem  the CLI, so `loom send "hi"`, `loom status` and `loom serve --no-supervisor --fake`
rem  all work from here too.
rem
rem  uv is installed as a pip user package and is not on PATH, hence `python -m uv`.
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1

python -m uv --version >nul 2>&1
if errorlevel 1 (
    echo loom: uv is not installed for this Python. Install it with:
    echo     python -m pip install --user uv
    exit /b 1
)

rem  Bring .venv in line with uv.lock. A no-op once it already is, so this costs
rem  nothing on a normal start; a fresh clone gets its environment built here. The
rem  second attempt re-resolves for the case where pyproject.toml has moved ahead
rem  of the lockfile.
python -m uv sync --frozen --quiet
if errorlevel 1 python -m uv sync --quiet
if errorlevel 1 (
    echo loom: could not install dependencies.
    exit /b 1
)

if "%~1"=="" (
    python -m uv run loom serve
) else (
    python -m uv run loom %*
)
exit /b %errorlevel%
