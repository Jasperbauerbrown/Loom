"""Knowledge: the vault (truth), the index (cache), the watcher (external edits).

`install(app)` bootstraps the vault — git repo, type folders, core templates — and
attaches `app.vault`, `app.index`, `app.watcher`, then registers the knowledge tools.
It must work against an empty directory, because that is the first-run case and the
test fixture's case alike.

`start(app)` rebuilds the index when it disagrees with the files (03 §3.2) and starts
the watcher; `stop(app)` stops the watcher. Nothing here can fail the backend: a missing
git binary costs history, a missing embedding model costs cosine search, and both
degrade to a working vault with alias and full-text retrieval.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)


def install(app: Any) -> None:
    from ..tools.native import knowledge as knowledge_tools
    from .index import Index
    from .vault import Vault

    app.vault = Vault(app)
    app.vault.bootstrap()
    app.index = Index(app)
    app.vault.ensure_builtin_procedures()  # after the index exists, so the notes are indexed
    from .watcher import Watcher

    app.watcher = Watcher(app)
    for tool in knowledge_tools.tools():
        app.tools.register(tool, replace=True)


async def start(app: Any) -> None:
    import asyncio

    try:
        if app.index.stale():
            result = await asyncio.to_thread(app.index.rebuild)
            log.info(
                "vault index rebuilt: %s notes, %s aliases, %s links in %sms",
                result["notes"], result["aliases"], result["links"], result["ms"],
            )
        else:
            app.index.load()
        # Embeddings need a model call (and may load the embedding model): never hold
        # startup for them. Alias/FTS retrieval works meanwhile; vectors arrive shortly.
        app.spawn("vault-embed-startup", app.index.embed_pending())
    except Exception:
        log.exception("knowledge: index startup failed; retrieval degrades to what is indexed")
    app.watcher.start()


async def stop(app: Any) -> None:
    if app.watcher is not None:
        app.watcher.stop()
