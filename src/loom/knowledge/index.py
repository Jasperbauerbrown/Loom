"""The knowledge index: a cache of the vault, rebuildable from markdown (03 §8.1–8.7).

Four tables (`notes`, `aliases`, `links`, `notes_fts`) plus float32 embeddings in
`notes.embedding`. Nothing here is truth — delete the database and `rebuild()` restores
every row from the files. That is why a bad note never blocks: it is recorded with
`type='invalid'` and the rest of the vault indexes normally (03 §17).

Retrieval is two cheap mechanisms fused, never a gate (charter §2.4):
- deterministic: alias/title scan for referents, link graph for neighbours;
- statistical: FTS5 for words, brute-force cosine for meaning (`math.sumprod` over
  `array('f')`, no numpy, no vector database — 03 §5.6).

Each vector records the model that produced it (`notes.embed_model`). Only vectors from
the embedding role's current model are loaded; the rest are re-embedded, so changing
the embedding preset (or its model) never mixes incomparable vectors (2026-10-15).

Async where a model call is unavoidable (`search`, `resolve`, `a_rebuild` embed), sync
everywhere else so vault writes stay synchronous. `update()` refreshes rows immediately
and queues embeddings for a background drain, so a write never waits on a model.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import re
import time
from array import array
from pathlib import PurePosixPath
from typing import Any, Iterable, Sequence

from .. import db
from ..models import roles
from ..models.types import PRIORITY_MAINTENANCE, ModelError
from . import notes
from .vault import folder_type

log = logging.getLogger(__name__)

RRF_K = 60  # reciprocal rank fusion constant (03 §8.4)
EMBED_CHARS = 4000  # how much of a body is embedded
EMBED_BATCH = 16
SNIPPET_CHARS = 240


def _hash(text: str) -> str:
    return hashlib.blake2b(text.encode("utf-8", "replace"), digest_size=16).hexdigest()


def cosine(a: "array[float]", b: "array[float]") -> float:
    """Both vectors are L2-normalised at write time, so the dot product is the cosine."""
    if len(a) != len(b):
        return 0.0
    return float(math.sumprod(a, b))


def _normalise(vector: Sequence[float]) -> "array[float]":
    norm = math.sqrt(math.sumprod(vector, vector)) or 1.0
    return array("f", [v / norm for v in vector])


def fts_query(text: str, join: str = "AND") -> str:
    """User text → a safe FTS5 MATCH expression: bare terms, each quoted."""
    terms = [t for t in re.findall(r"[\w][\w'-]*", text or "", re.UNICODE) if len(t) > 1 or t.isdigit()]
    if not terms:
        return ""
    return f" {join} ".join('"' + t.replace('"', "") + '"' for t in terms[:24])


class Index:
    def __init__(self, app: Any):
        self.app = app
        self.db = app.db
        self.vectors: dict[str, "array[float]"] = {}
        self.vectors_model: str | None = None  # the model every loaded vector came from
        self._alias_re: re.Pattern[str] | None = None
        self._alias_targets: dict[str, list[str]] = {}
        self._pending_embed: set[str] = set()
        self._draining = False
        self._embed_warned = False
        app.bus.listen(self._on_event)

    # -- helpers ----------------------------------------------------------------
    @property
    def vault(self) -> Any:
        return self.app.vault

    def embeddings_enabled(self) -> bool:
        try:
            return bool(self.app.settings.get("models.embeddings_enabled"))
        except KeyError:
            return False

    def _setting(self, key: str, override: Any = None) -> Any:
        if override is not None:
            return override
        return self.app.settings.get(key)

    # -- building ---------------------------------------------------------------
    def rebuild(self) -> dict[str, Any]:
        """Full, synchronous scan of the vault. Embeddings are preserved for notes whose
        hash is unchanged and queued for the rest."""
        started = time.perf_counter()
        previous = {
            row["path"]: (row["hash"], row["embedding"], row["embed_model"])
            for row in self.db.execute("SELECT path, hash, embedding, embed_model FROM notes")
        }
        paths = self.vault.list()
        invalid = 0
        with db.transaction(self.db):
            self.db.execute("DELETE FROM notes")
            self.db.execute("DELETE FROM aliases")
            self.db.execute("DELETE FROM links")
            self.db.execute("DELETE FROM notes_fts")
            for path in paths:
                row = self._index_one(path, resolve_links=False, previous=previous)
                if row is None:
                    continue
                if row["type"] == "invalid":
                    invalid += 1
            for path in paths:
                self._resolve_links(path)
        self.load()
        self._alias_re = None
        counts = self._counts()
        counts.update(
            {
                "invalid": invalid,
                "embedded": len(self.vectors),
                "pending_embeddings": len(self._pending_embed),
                "ms": int((time.perf_counter() - started) * 1000),
            }
        )
        return counts

    async def a_rebuild(self) -> dict[str, Any]:
        """`rebuild()` off the event loop, then the embeddings it queued.

        The one place SQLite is touched from another thread: a full scan of the vault is
        long enough to stall the loop, it runs under a single transaction, and it happens
        at startup or on an explicit `reindex`. Everything else (including `update`) runs
        on the loop thread as the decision of 2026-09-20 prescribes."""
        result = await asyncio.to_thread(self.rebuild)
        embedded = await self.embed_pending()
        result["embedded"] = result.get("embedded", 0) + embedded
        result["pending_embeddings"] = len(self._pending_embed)
        return result

    def _counts(self) -> dict[str, Any]:
        one = lambda sql: int(self.db.execute(sql).fetchone()[0])  # noqa: E731
        return {
            "notes": one("SELECT COUNT(*) FROM notes"),
            "aliases": one("SELECT COUNT(*) FROM aliases"),
            "links": one("SELECT COUNT(*) FROM links"),
        }

    def stale(self) -> bool:
        """True when the tables do not match the files (03 §8.1: rebuild at startup)."""
        rows = {r["path"]: (r["mtime"], r["size"]) for r in self.db.execute("SELECT path, mtime, size FROM notes")}
        paths = self.vault.list()
        if len(rows) != len(paths):
            return True
        for path in paths:
            known = rows.get(path)
            if known is None:
                return True
            try:
                stat = self.vault.resolve(path).stat()
            except OSError:
                return True
            if abs(known[0] - stat.st_mtime) > 0.001 or known[1] != stat.st_size:
                return True
        return False

    def update(self, paths: Iterable[str]) -> None:
        """Re-index the given vault paths (added, changed or removed). Cheap and
        synchronous; embeddings are queued and drained in the background."""
        touched = [str(p).replace("\\", "/") for p in paths]
        if not touched:
            return
        marks = ",".join("?" for _ in touched)
        previous = {
            row["path"]: (row["hash"], row["embedding"], row["embed_model"])
            for row in self.db.execute(
                f"SELECT path, hash, embedding, embed_model FROM notes WHERE path IN ({marks})", touched
            )
        }
        changed = False
        for path in touched:
            if not self.vault.exists(path):
                self.remove(path)
                changed = True
                continue
            row = self._index_one(path, resolve_links=True, previous=previous)
            if row is None:
                continue
            changed = True
            if row["embedding"] is None:
                self._pending_embed.add(path)
            self._refresh_dangling(path)
        if changed:
            self._alias_re = None
            self._schedule_embeddings()

    def remove(self, path: str) -> None:
        path = str(path).replace("\\", "/")
        self.db.execute("DELETE FROM notes WHERE path=?", (path,))
        self.db.execute("DELETE FROM aliases WHERE path=?", (path,))
        self.db.execute("DELETE FROM links WHERE src=?", (path,))
        self.db.execute("DELETE FROM notes_fts WHERE path=?", (path,))
        self.vectors.pop(path, None)
        self._pending_embed.discard(path)
        self._alias_re = None
        # Links that pointed here become dangling again.
        self.db.execute("UPDATE links SET dst=?, resolved=0 WHERE dst=? AND resolved=1", (PurePosixPath(path).stem, path))

    def _index_one(
        self, path: str, *, resolve_links: bool, previous: dict[str, tuple[str, Any, Any]] | None = None
    ) -> dict[str, Any] | None:
        target = self.vault.resolve(path)
        try:
            stat = target.stat()
            raw = target.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            log.warning("index: cannot read %s (%s)", path, exc)
            return None
        note = self.vault._note(path, raw)
        digest = _hash(raw)
        keep, keep_model = None, None
        if previous is not None:
            known = previous.get(path)
            if known is not None and known[0] == digest:
                keep, keep_model = known[1], known[2]
        core_order = None
        if path.startswith("core/"):
            core_order = sorted(p for p in self.vault.list(prefix="core/")).index(path)

        self.db.execute("DELETE FROM notes WHERE path=?", (path,))
        self.db.execute("DELETE FROM aliases WHERE path=?", (path,))
        self.db.execute("DELETE FROM links WHERE src=?", (path,))
        self.db.execute("DELETE FROM notes_fts WHERE path=?", (path,))
        self.db.execute(
            "INSERT INTO notes(path, type, title, aliases, modified, mtime, size, hash, core_order, embedding, embed_model) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                path,
                note.type,
                note.title,
                db.dumps(note.aliases),
                str(note.frontmatter.get("modified") or ""),
                stat.st_mtime,
                stat.st_size,
                digest,
                core_order,
                keep,
                keep_model,
            ),
        )
        for alias in self._alias_keys(note):
            self.db.execute("INSERT INTO aliases(alias, path) VALUES (?, ?)", (alias, path))
        self.db.execute(
            "INSERT INTO notes_fts(path, title, body) VALUES (?, ?, ?)", (path, note.title, note.body)
        )
        seen: set[str] = set()
        for target_name in notes.wikilinks(note.body):
            key = target_name.strip()
            if not key or key in seen:
                continue
            seen.add(key)
            dst, resolved = (self._resolve_target(key), True) if resolve_links else (None, False)
            if dst is None:
                self.db.execute("INSERT INTO links(src, dst, resolved) VALUES (?, ?, 0)", (path, key))
            else:
                self.db.execute("INSERT INTO links(src, dst, resolved) VALUES (?, ?, 1)", (path, dst))
        current = keep is not None and keep_model is not None and keep_model == self.vectors_model
        if current:
            self.vectors[path] = array("f", bytes(keep))
        else:
            self.vectors.pop(path, None)
        return {"path": path, "type": note.type, "embedding": keep if current else None}

    def _alias_keys(self, note: Any) -> list[str]:
        keys = []
        stem = PurePosixPath(note.path).stem
        if stem.upper() == "SKILL":
            # A procedure is named by its folder; "skill" would collide across all of them.
            stem = PurePosixPath(note.path).parent.name
        for candidate in [note.title, note.frontmatter.get("name"), *note.aliases, stem]:
            if not candidate or not isinstance(candidate, str):
                continue
            key = notes.normalise_alias(candidate)
            if key and key not in keys:
                keys.append(key)
        return keys

    def _resolve_links(self, path: str) -> None:
        """Second pass of a full rebuild: resolve this note's raw targets."""
        rows = list(self.db.execute("SELECT rowid, dst FROM links WHERE src=? AND resolved=0", (path,)))
        for row in rows:
            dst = self._resolve_target(row["dst"])
            if dst is not None:
                self.db.execute("UPDATE links SET dst=?, resolved=1 WHERE rowid=?", (dst, row["rowid"]))

    def _refresh_dangling(self, path: str) -> None:
        """A new note may satisfy links that were dangling; resolve those now."""
        note_row = self.db.execute("SELECT title FROM notes WHERE path=?", (path,)).fetchone()
        if note_row is None:
            return
        for row in list(self.db.execute("SELECT rowid, dst FROM links WHERE resolved=0")):
            if self._resolve_target(row["dst"]) == path:
                self.db.execute("UPDATE links SET dst=?, resolved=1 WHERE rowid=?", (path, row["rowid"]))

    def _resolve_target(self, target: str) -> str | None:
        """Wikilink resolution order: filename stem → title → alias (03 §8.1)."""
        raw = str(target).strip().replace("\\", "/")
        if not raw:
            return None
        direct = raw if raw.endswith(".md") else f"{raw}.md"
        row = self.db.execute("SELECT path FROM notes WHERE path=?", (direct,)).fetchone()
        if row:
            return row["path"]
        stem = PurePosixPath(raw).name.lower().removesuffix(".md")
        row = self.db.execute(
            "SELECT path FROM notes WHERE lower(path)=? OR lower(path) LIKE ? OR lower(path) LIKE ? "
            "ORDER BY length(path), path LIMIT 1",
            (f"{stem}.md", f"%/{stem}.md", f"%/{stem}/skill.md"),
        ).fetchone()
        if row:
            return row["path"]
        row = self.db.execute("SELECT path FROM notes WHERE lower(title)=? ORDER BY path LIMIT 1", (raw.lower(),)).fetchone()
        if row:
            return row["path"]
        key = notes.normalise_alias(raw)
        if key:
            row = self.db.execute("SELECT path FROM aliases WHERE alias=? ORDER BY path LIMIT 1", (key,)).fetchone()
            if row:
                return row["path"]
        return None

    # -- embeddings (03 §5.6) ---------------------------------------------------
    def embed_model(self) -> str | None:
        """The embedding role's current model, or None when it does not resolve."""
        try:
            return roles.resolve(self.app, role="embedding").model
        except ModelError:
            return None

    def load(self) -> int:
        """Load the vectors made by the current embedding model into memory and queue
        every other note (no vector, or one from another model) for embedding."""
        model = self.embed_model()
        self.vectors.clear()
        self.vectors_model = model
        for row in self.db.execute("SELECT path, embedding, embed_model FROM notes"):
            if row["embedding"] is not None and model is not None and row["embed_model"] == model:
                vec = array("f")
                vec.frombytes(bytes(row["embedding"]))
                self.vectors[row["path"]] = vec
                self._pending_embed.discard(row["path"])
            else:
                self._pending_embed.add(row["path"])
        return len(self.vectors)

    def reconcile(self) -> bool:
        """Reload when the embedding model changed since the vectors were loaded, and
        start re-embedding. Cheap when nothing changed; True when it reloaded."""
        if self.embed_model() == self.vectors_model:
            return False
        self.load()
        self._schedule_embeddings()
        return True

    def _on_event(self, event: dict[str, Any]) -> None:
        if event.get("type") == "models.changed":
            self.reconcile()

    def _embed_text(self, path: str) -> str | None:
        try:
            note = self.vault.read(path)
        except Exception:
            return None
        return f"{note.title}\n{note.body[:EMBED_CHARS]}"

    async def embed_pending(self) -> int:
        """Embed queued notes. An embedding failure is logged once and the queue is
        dropped: retrieval degrades to alias + FTS, which is never a hard failure."""
        if not self.embeddings_enabled() or not self._pending_embed:
            return 0
        done = 0
        while self._pending_embed:
            if self.embed_model() != self.vectors_model:
                self.load()  # the model changed mid-drain: requeue what it made stale
                continue
            model = self.vectors_model
            batch = [self._pending_embed.pop() for _ in range(min(EMBED_BATCH, len(self._pending_embed)))]
            pairs = [(p, self._embed_text(p)) for p in batch]
            pairs = [(p, t) for p, t in pairs if t is not None]
            if not pairs:
                continue
            try:
                vectors = await self.app.models.embed([t for _, t in pairs], PRIORITY_MAINTENANCE)
            except ModelError as exc:
                if not self._embed_warned:
                    log.warning("index: embeddings unavailable (%s); alias + FTS only", exc)
                    self._embed_warned = True
                self._pending_embed.clear()
                return done
            except Exception as exc:  # a provider fault must not break indexing
                if not self._embed_warned:
                    log.warning("index: embedding call failed (%s); alias + FTS only", exc)
                    self._embed_warned = True
                self._pending_embed.clear()
                return done
            for (path, _), vector in zip(pairs, vectors):
                vec = _normalise(vector)
                self.db.execute("UPDATE notes SET embedding=?, embed_model=? WHERE path=?", (vec.tobytes(), model, path))
                if model == self.vectors_model:
                    self.vectors[path] = vec
                done += 1
        self._embed_warned = False
        return done

    def _schedule_embeddings(self) -> None:
        """Drain the embed queue in the background if an event loop is running."""
        if self._draining or not self._pending_embed or not self.embeddings_enabled():
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return

        async def drain() -> None:
            self._draining = True
            try:
                await self.embed_pending()
            finally:
                self._draining = False

        self.app.spawn("vault-embed", drain())

    async def embed_query(self, text: str) -> "array[float]" | None:
        if not self.embeddings_enabled():
            return None
        self.reconcile()
        if not self.vectors:
            return None
        try:
            vectors = await self.app.models.embed([text], PRIORITY_MAINTENANCE)
        except Exception:
            return None
        return _normalise(vectors[0]) if vectors else None

    def nearest(self, vector: "array[float]", k: int, *, exclude: set[str] = frozenset(), threshold: float = 0.0) -> list[tuple[str, float]]:
        scored = [
            (path, cosine(vector, vec))
            for path, vec in self.vectors.items()
            if path not in exclude and not path.startswith("core/")
        ]
        scored = [(p, s) for p, s in scored if s >= threshold]
        scored.sort(key=lambda t: (-t[1], t[0]))
        return scored[:k]

    # -- search (03 §8.4) -------------------------------------------------------
    def search_fts(self, query: str, type: str | None = None, k: int = 8) -> list[dict[str, Any]]:
        """FTS5 only; AND across terms, falling back to OR when that finds nothing."""
        for join in ("AND", "OR"):
            match = fts_query(query, join)
            if not match:
                return []
            try:
                rows = list(
                    self.db.execute(
                        "SELECT path, snippet(notes_fts, 2, '', '', '…', 16) AS snip, "
                        "bm25(notes_fts) AS score FROM notes_fts WHERE notes_fts MATCH ? "
                        "ORDER BY score LIMIT ?",
                        (match, max(k * 4, 20)),
                    )
                )
            except Exception as exc:  # a malformed MATCH must not raise at the model
                log.warning("index: fts query failed (%s)", exc)
                return []
            out = []
            for row in rows:
                meta = self._note_row(row["path"])
                if meta is None or (type and meta["type"] != type):
                    continue
                out.append({**meta, "snippet": (row["snip"] or "").strip(), "score": -float(row["score"])})
            if out:
                return out[:k]
        return []

    async def search(self, query: str, type: str | None = None, k: int = 8) -> list[dict[str, Any]]:
        """Fused retrieval: FTS5 rank + cosine, reciprocal rank fusion (03 §8.4)."""
        lexical = self.search_fts(query, type, k=max(k, 8) * 2)
        semantic: list[dict[str, Any]] = []
        vector = await self.embed_query(query)
        if vector is not None:
            for path, score in self.nearest(vector, max(k, 8) * 2):
                meta = self._note_row(path)
                if meta is None or (type and meta["type"] != type):
                    continue
                semantic.append({**meta, "snippet": self._snippet(path, query), "score": score})
        fused: dict[str, dict[str, Any]] = {}
        for ranking in (lexical, semantic):
            for rank, row in enumerate(ranking):
                entry = fused.setdefault(row["path"], {**row, "score": 0.0})
                entry["score"] += 1.0 / (RRF_K + rank + 1)
                if not entry.get("snippet") and row.get("snippet"):
                    entry["snippet"] = row["snippet"]
        results = sorted(fused.values(), key=lambda r: (-r["score"], r["path"]))[:k]
        for row in results:
            if not row.get("snippet"):
                row["snippet"] = self._snippet(row["path"], query)
        return results

    def _note_row(self, path: str) -> dict[str, Any] | None:
        row = self.db.execute("SELECT path, title, type FROM notes WHERE path=?", (path,)).fetchone()
        return None if row is None else {"path": row["path"], "title": row["title"], "type": row["type"]}

    def _snippet(self, path: str, query: str) -> str:
        """First line mentioning any query term, else the opening of the note."""
        try:
            body = self.vault.read(path).body
        except Exception:
            return ""
        terms = [t.lower() for t in re.findall(r"\w+", query or "") if len(t) > 2]
        for line in body.splitlines():
            stripped = line.strip()
            if stripped and any(t in stripped.lower() for t in terms):
                return stripped[:SNIPPET_CHARS]
        head = " ".join(body.split())
        return head[:SNIPPET_CHARS]

    # -- resolution (03 §8.2) ---------------------------------------------------
    def _alias_pattern(self) -> re.Pattern[str] | None:
        """One precompiled alternation over every alias and title, longest first so the
        longest match wins per span. Rebuilt whenever the alias table changes."""
        if self._alias_re is not None:
            return self._alias_re
        targets: dict[str, list[str]] = {}
        for row in self.db.execute("SELECT alias, path FROM aliases ORDER BY path"):
            if row["path"].startswith("core/"):
                continue
            targets.setdefault(row["alias"], []).append(row["path"])
        self._alias_targets = targets
        if not targets:
            self._alias_re = re.compile(r"(?!x)x")  # matches nothing
            return self._alias_re
        alternatives = sorted(targets, key=lambda a: (-len(a), a))
        self._alias_re = re.compile(r"\b(?:" + "|".join(re.escape(a) for a in alternatives) + r")\b")
        return self._alias_re

    def alias_scan(self, text: str) -> list[str]:
        pattern = self._alias_pattern()
        if pattern is None:
            return []
        haystack = notes.normalise_alias(text)
        out: list[str] = []
        for match in pattern.finditer(haystack):
            for path in self._alias_targets.get(match.group(0), []):
                if path not in out:
                    out.append(path)
        return out

    async def resolve(
        self,
        text: str,
        *,
        exclude: set[str] | Iterable[str] = (),
        max_entities: int | None = None,
        threshold: float | None = None,
    ) -> list[str]:
        """Entities this input is about (03 §8.2): alias/title scan first, embeddings
        only to fill the remaining slots. A head start, never a gate."""
        excluded = set(exclude or ())
        cap = int(self._setting("knowledge.preload_max_entities", max_entities))
        floor = float(self._setting("knowledge.retrieval_threshold", threshold))
        found = [p for p in self.alias_scan(text) if p not in excluded]
        if len(found) >= cap:
            return found
        vector = await self.embed_query(text)
        if vector is None:
            return found
        for path, _score in self.nearest(
            vector, cap - len(found), exclude=excluded | set(found), threshold=floor
        ):
            found.append(path)
        return found

    # -- pre-load (03 §8.3) -----------------------------------------------------
    def preload(self, paths: Sequence[str], budget_tokens: int | None = None) -> str:
        """Resolved notes first, then their first-hop links (procedures → same type →
        others), in one budget: every matched note outranks any link, so one link-heavy
        note cannot starve the next match (2026-09-30). Each note appears once; anything
        that does not fit is named so the model can `knowledge_read` it."""
        budget = int(self._setting("knowledge.preload_budget_tokens", budget_tokens))
        parts: list[str] = []
        seen: set[str] = set()
        size = 0

        def fits(text: str) -> bool:
            return (size + len(text)) // 4 <= budget

        matched = list(dict.fromkeys(paths))
        for candidate in matched + [hop for path in matched for hop in self._first_hop(path)]:
            if candidate in seen or candidate.startswith("core/"):
                continue
            seen.add(candidate)
            rendered = self._render(candidate)
            if rendered is None:
                continue
            if fits(rendered):
                parts.append(rendered)
                size += len(rendered)
            else:
                stub = f"- {candidate} (not loaded; knowledge_read for more)\n"
                parts.append(stub)
                size += len(stub)
        return "".join(parts).strip()

    def _render(self, path: str) -> str | None:
        try:
            note = self.vault.read(path)
        except Exception:
            return None
        body = note.body.strip()
        return f"### {path}\n{body}\n\n"

    def _first_hop(self, path: str) -> list[str]:
        """Outgoing resolved links, procedures first, then same-type notes, then rest."""
        source = self.db.execute("SELECT type FROM notes WHERE path=?", (path,)).fetchone()
        own_type = source["type"] if source else folder_type(path)
        rows = [r["dst"] for r in self.db.execute("SELECT dst FROM links WHERE src=? AND resolved=1", (path,))]
        typed = []
        for dst in dict.fromkeys(rows):
            meta = self._note_row(dst)
            if meta is None:
                continue
            rank = 0 if meta["type"] == "procedure" else (1 if meta["type"] == own_type else 2)
            typed.append((rank, dst))
        typed.sort(key=lambda t: (t[0], t[1]))
        return [dst for _, dst in typed]

    # -- core blocks (03 §8.5) --------------------------------------------------
    def core_paths(self) -> list[str]:
        rows = self.db.execute(
            "SELECT path FROM notes WHERE core_order IS NOT NULL ORDER BY core_order, path"
        ).fetchall()
        return [r["path"] for r in rows] or sorted(self.vault.list(prefix="core/"))

    def core_blocks(self, cap_chars: int | None = None) -> list[tuple[str, str]]:
        cap = int(self._setting("knowledge.core_block_chars", cap_chars))
        out: list[tuple[str, str]] = []
        for path in self.core_paths():
            try:
                body = self.vault.read(path).body.strip()
            except Exception:
                continue
            if len(body) > cap:
                body = body[:cap] + f"\n[truncated: {cap} of {len(body)} chars — edit {path} to shorten]"
            out.append((path, body))
        return out

    def core_fill(self, cap_chars: int | None = None) -> list[dict[str, Any]]:
        cap = int(self._setting("knowledge.core_block_chars", cap_chars))
        out = []
        for path in self.core_paths():
            try:
                note = self.vault.read(path)
            except Exception:
                continue
            chars = len(note.body.strip())
            out.append({"path": path, "title": note.title, "chars": chars, "cap": cap, "over": chars > cap})
        return out

    # -- procedures index (slot [4]) --------------------------------------------
    def procedures_index(self) -> str:
        lines = []
        for row in sorted(self.vault.procedures(), key=lambda r: r["name"].lower()):
            line = f"{row['name']} — {row['description']}".rstrip(" —")
            if not row["approved"]:
                line += " (awaiting approval)"
            lines.append(line)
        return "\n".join(lines)

    # -- graph ------------------------------------------------------------------
    def links(self, path: str, direction: str = "out") -> list[str]:
        if direction not in ("out", "in"):
            raise ValueError("direction must be 'out' or 'in'")
        if direction == "out":
            rows = self.db.execute(
                "SELECT DISTINCT dst AS p FROM links WHERE src=? AND resolved=1 ORDER BY dst", (path,)
            )
        else:
            rows = self.db.execute(
                "SELECT DISTINCT src AS p FROM links WHERE dst=? AND resolved=1 ORDER BY src", (path,)
            )
        return [r["p"] for r in rows]

    def dangling(self) -> list[dict[str, Any]]:
        """Wanted notes: link targets with no note (03 §8.1, shown in the vault UI)."""
        wanted: dict[str, list[str]] = {}
        for row in self.db.execute("SELECT src, dst FROM links WHERE resolved=0 ORDER BY dst, src"):
            wanted.setdefault(row["dst"], []).append(row["src"])
        return [{"target": t, "sources": s} for t, s in sorted(wanted.items())]

    def graph(self, center: str | None = None, depth: int = 1, limit: int = 200) -> dict[str, Any]:
        if center:
            frontier = {center}
            nodes = {center}
            for _ in range(max(0, int(depth))):
                nxt: set[str] = set()
                for path in frontier:
                    nxt.update(self.links(path, "out"))
                    nxt.update(self.links(path, "in"))
                nxt -= nodes
                nodes |= nxt
                frontier = nxt
                if len(nodes) >= limit:
                    break
            selected = sorted(nodes)[:limit]
        else:
            selected = [r["path"] for r in self.db.execute("SELECT path FROM notes ORDER BY path LIMIT ?", (limit,))]
        chosen = set(selected)
        out_nodes = []
        for path in selected:
            meta = self._note_row(path)
            out_nodes.append(meta or {"path": path, "title": notes.title_from_path(path), "type": "invalid"})
        edges = [
            {"src": r["src"], "dst": r["dst"]}
            for r in self.db.execute("SELECT src, dst FROM links WHERE resolved=1 ORDER BY src, dst")
            if r["src"] in chosen and r["dst"] in chosen
        ]
        return {"nodes": out_nodes, "edges": edges}

    # -- transcript recall (03 §8.7) --------------------------------------------
    def transcript_search(self, query: str, conversation_id: str | None = None, k: int = 8) -> list[dict[str, Any]]:
        for join in ("AND", "OR"):
            match = fts_query(query, join)
            if not match:
                return []
            sql = (
                "SELECT e.id AS event_id, e.conversation_id, e.kind, e.ts, "
                "       snippet(events_fts, 0, '', '', '…', 16) AS snip, "
                "       c.title AS title, bm25(events_fts) AS score "
                "FROM events_fts JOIN events e ON e.id = events_fts.rowid "
                "LEFT JOIN conversations c ON c.id = e.conversation_id "
                "WHERE events_fts MATCH ?"
            )
            params: list[Any] = [match]
            if conversation_id:
                sql += " AND e.conversation_id = ?"
                params.append(conversation_id)
            sql += " ORDER BY score LIMIT ?"
            params.append(int(k))
            try:
                rows = list(self.db.execute(sql, params))
            except Exception as exc:
                log.warning("index: transcript search failed (%s)", exc)
                return []
            if rows:
                return [
                    {
                        "event_id": r["event_id"],
                        "conversation_id": r["conversation_id"],
                        "title": r["title"] or "",
                        "kind": r["kind"],
                        "ts": r["ts"],
                        "snippet": (r["snip"] or "").strip(),
                    }
                    for r in rows
                ]
        return []

    def neighbours(self, event_id: int, n: int = 2) -> list[dict[str, Any]]:
        """The ±n events around a hit, for context (03 §8.7)."""
        row = self.db.execute("SELECT conversation_id FROM events WHERE id=?", (int(event_id),)).fetchone()
        if row is None:
            return []
        rows = self.db.execute(
            "SELECT id, kind, content, ts FROM events WHERE conversation_id=? AND id BETWEEN ? AND ? ORDER BY id",
            (row["conversation_id"], int(event_id) - int(n), int(event_id) + int(n)),
        )
        return [
            {"event_id": r["id"], "kind": r["kind"], "content": r["content"], "ts": r["ts"]} for r in rows
        ]
