"""Package wiring for loom.connectors (stub until its work package lands).

Contract with app.py: install(app) is synchronous wiring (register tools, attach
objects to app); start(app)/stop(app) are optional async lifecycle hooks.
"""

from __future__ import annotations


def install(app) -> None:  # type: ignore[no-untyped-def]
    pass
