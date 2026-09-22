# Loom

A local-first personal assistant with a technical focus. See `docs/README.md` for the
design record and `CLAUDE.md` for working conventions.

```bash
loom                               # Windows launcher: syncs deps, serves http://127.0.0.1:8500
loom send "hi"                     # any other CLI command passes straight through

python -m uv run loom serve        # http://127.0.0.1:8500
python -m uv run pytest
```
