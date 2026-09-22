"""The maintenance role (03 §8.6): consolidation, curation, housekeeping.

One asyncio task, one lock, priority 2. A *pass* is always the same three stages in the
same order, whatever started it:

1. **Consolidation** — every conversation with events past its `maint_watermark` is
   shown to the maintenance model in batches, together with the notes that already cover
   that ground. The model decides what is durable and writes it with `knowledge_write`;
   this module never extracts facts itself (charter §2.4). The watermark advances only
   after a batch completes, so a failure re-shows the same events next pass.
2. **Curation** — alias collisions, near-duplicate pairs (cosine ≥
   `knowledge.curation_similarity`) and dangling links become candidates; the model
   merges, cross-links or leaves them alone.
3. **Housekeeping** — archive idle finished threads, prune archived conversations'
   artifacts past retention, commit external (Obsidian) vault edits, and a safety-net
   `maintenance: <date>` commit.

Every model tool call goes through the one dispatcher, so maintenance obeys exactly the
same permissions as a turn. There is no turn to pause, though, so an approval is handled
by one defined behaviour (03 §8.6): the approval is denied immediately — no stale
pending row is left behind — and the proposed call is posted as a `knowledge` item on
the now board, where the person can see it and do it themselves. The sweep continues.

Nothing here asks permission to *think*: there is no approval gate on the pass itself
(takeaways §3). What it did is visible instead — git commits, now-board items, and the
last report on `GET /api/maintenance`.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import logging
import time
import zoneinfo
from typing import Any

from .. import db, items, prompts
from ..models.types import PRIORITY_MAINTENANCE
from ..tools.dispatch import ApprovalNeeded, dispatch
from ..tools.registry import ToolContext

log = logging.getLogger(__name__)

# The tools a maintenance pass may use. It reads and writes knowledge; nothing else.
KNOWLEDGE_TOOLS = ("knowledge_search", "knowledge_read", "knowledge_write", "knowledge_links")

CONSOLIDATE_KINDS = ["user", "assistant", "tool_call"]

POLL_SECONDS = 15.0        # how often the scheduler looks at the model queue
BATCH_CHARS = 12_000       # transcript characters per consolidation model call
RELATED_K = 6              # related notes shown with a consolidation batch
RELATED_CHARS = 1_500      # per related note
NOTE_CHARS = 3_000         # per note in a curation candidate
MAX_STEPS = 6              # tool-call rounds per model conversation
MAX_CANDIDATES = 10        # curation candidates per pass
SOURCE = "maintenance"


class MaintenanceError(RuntimeError):
    """A model call that did not come back usable. Logged in the report, never raised out
    of `run` — a failed stage is an input, not an incident."""


# ---------------------------------------------------------------------------------
# Scheduling arithmetic (pure: the tests call these directly)
# ---------------------------------------------------------------------------------
def parse_hhmm(text: str) -> tuple[int, int]:
    """`"03:00"` → (3, 0). Anything unparseable is 03:00, the documented default."""
    try:
        hour, _, minute = str(text).partition(":")
        h, m = int(hour), int(minute or 0)
        if 0 <= h < 24 and 0 <= m < 60:
            return h, m
    except (TypeError, ValueError):
        pass
    return 3, 0


def slot_date(now: _dt.datetime, at: str) -> str:
    """The date (YYYY-MM-DD) of the most recent nightly slot at or before `now`.

    Before today's slot that is yesterday, after it today. Comparing this against the
    last slot a pass ran for is the whole nightly rule: no persistence, no drift, and a
    clock change or a restart cannot make a slot fire twice.
    """
    hour, minute = parse_hhmm(at)
    day = now.date() if (now.hour, now.minute) >= (hour, minute) else now.date() - _dt.timedelta(days=1)
    return day.isoformat()


def next_slot(now: _dt.datetime, at: str) -> _dt.datetime:
    """The next nightly slot strictly after `now`, in `now`'s timezone."""
    hour, minute = parse_hhmm(at)
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= now:
        candidate += _dt.timedelta(days=1)
    return candidate


def nightly_due(now: _dt.datetime, at: str, last_slot: str) -> bool:
    return slot_date(now, at) != last_slot


def idle_due(*, idle_for: float | None, since_last_pass: float, idle_seconds: float, has_work: bool) -> bool:
    """The idle window: the model queue has been empty long enough, the previous pass is
    at least that far behind us, and there is something to do."""
    if not has_work or idle_for is None:
        return False
    return idle_for >= idle_seconds and since_last_pass >= idle_seconds


# ---------------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------------
def new_report(reason: str) -> dict[str, Any]:
    return {
        "started_at": db.now_iso(),
        "finished_at": None,
        "reason": reason,
        "consolidated": [],                       # [{conversation_id, events, writes}]
        "curated": {"candidates": 0, "applied": 0},
        "archived": 0,
        "pruned_artifacts": 0,
        "committed": None,                        # sha of the safety-net commit
        "external_commit": None,                  # sha of the "user: edit ..." commit
        "errors": [],
    }


class Maintenance:
    """`app.maintenance`."""

    def __init__(self, app: Any):
        self.app = app
        self.lock = asyncio.Lock()
        self.report: dict[str, Any] | None = None   # the last finished pass
        self.current: dict[str, Any] | None = None  # the pass in flight, if any
        self.running_task = False
        self._idle_since: float | None = None
        self._last_pass_at: float = time.monotonic()
        self._last_slot: str = ""  # set in start(): the current slot counts as handled

    # -- settings ---------------------------------------------------------------
    def setting(self, key: str, default: Any = None) -> Any:
        try:
            value = self.app.settings.get(key)
        except KeyError:
            return default
        return default if value is None else value

    @property
    def enabled(self) -> bool:
        return bool(self.setting("knowledge.maintenance_enabled", True))

    @property
    def idle_seconds(self) -> float:
        return float(self.setting("knowledge.maintenance_idle_seconds", 120))

    @property
    def nightly_at(self) -> str:
        return str(self.setting("knowledge.maintenance_nightly_at", "03:00"))

    @property
    def tz(self) -> _dt.tzinfo:
        try:
            return zoneinfo.ZoneInfo(str(self.setting("general.timezone", "UTC") or "UTC"))
        except Exception:
            return _dt.timezone.utc

    def now_local(self) -> _dt.datetime:
        return _dt.datetime.now(self.tz)

    # -- lifecycle --------------------------------------------------------------
    def start(self) -> None:
        if self.running_task:
            return
        self.running_task = True
        self._last_slot = slot_date(self.now_local(), self.nightly_at)
        self._last_pass_at = time.monotonic()
        self.app.spawn("maintenance", self._loop())

    async def stop(self) -> None:
        self.running_task = False
        task = self.app.tasks.get("maintenance")
        if task is not None and not task.done():
            task.cancel()

    async def _loop(self) -> None:
        while self.running_task and not self.app.shutting_down:
            try:
                await asyncio.sleep(POLL_SECONDS)
            except asyncio.CancelledError:
                return
            try:
                self.poll_queue()
                reason = await self.due()
                if reason:
                    await self.run(reason)
            except asyncio.CancelledError:
                return
            except Exception:
                log.exception("maintenance scheduler")

    # -- idle detection ---------------------------------------------------------
    def poll_queue(self) -> None:
        """One `queue_state()` sample. Polling beats listening on the bus: it needs no
        subscription, it is correct after a restart, and 15s is fine for a 120s window."""
        try:
            state = self.app.models.queue_state()
        except Exception:
            state = {"depth": 0, "running": []}
        busy = int(state.get("depth") or 0) > 0 or bool(state.get("running"))
        if busy:
            self._idle_since = None
        elif self._idle_since is None:
            self._idle_since = time.monotonic()

    def idle_for(self) -> float | None:
        return None if self._idle_since is None else time.monotonic() - self._idle_since

    async def due(self, now: _dt.datetime | None = None) -> str | None:
        """Why a pass should start now, or None. Nightly always runs; idle runs only when
        there is work."""
        if not self.enabled or self.lock.locked():
            return None
        now = now or self.now_local()
        if nightly_due(now, self.nightly_at, self._last_slot):
            return "nightly"
        if idle_due(
            idle_for=self.idle_for(),
            since_last_pass=time.monotonic() - self._last_pass_at,
            idle_seconds=self.idle_seconds,
            has_work=await self.has_work(),
        ):
            return "idle"
        return None

    async def has_work(self) -> bool:
        """Work that a pass *clears*. Curation candidates deliberately do not count: a
        pair the model decides to leave alone, or a dangling link it cannot honestly
        resolve, would otherwise re-trigger a pass every idle window, forever. Curation
        rides along on passes that idle work or the nightly slot started."""
        if self.pending_conversations():
            return True
        if self.archivable_threads():
            return True
        if self.setting("knowledge.auto_commit_external_edits", True):
            try:
                if await self.app.vault.git.a_status_porcelain():
                    return True
            except Exception:
                return False
        return False

    # -- the pass ---------------------------------------------------------------
    async def run(self, reason: str = "manual") -> dict[str, Any]:
        """One pass. A second call while one is in flight gets the in-progress marker;
        nothing is queued, because the next pass would see the same world anyway."""
        if self.lock.locked():
            marker = dict(self.current or {})
            marker["in_progress"] = True
            return marker

        async with self.lock:
            report = new_report(reason)
            self.current = report
            self._progress("start", f"maintenance pass ({reason})")
            try:
                for stage, fn in (
                    ("consolidate", self._consolidate),
                    ("curate", self._curate),
                    ("housekeep", self._housekeep),
                ):
                    try:
                        await fn(report)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        log.exception("maintenance stage %s failed", stage)
                        report["errors"].append(f"{stage}: {type(exc).__name__}: {exc}")
                self._announce(report)
            finally:
                report["finished_at"] = db.now_iso()
                self.report = report
                self.current = None
                self._last_pass_at = time.monotonic()
                self._idle_since = None
                self._last_slot = slot_date(self.now_local(), self.nightly_at)
                self._progress("done", self._summary(report) or "nothing to do")
        return report

    def _progress(self, stage: str, message: str) -> None:
        self.app.bus.emit("maintenance.progress", {"stage": stage, "message": message})

    # -- 1. consolidation (03 §8.6.1) -------------------------------------------
    def pending_conversations(self) -> list[dict[str, Any]]:
        rows = self.app.db.execute(
            "SELECT c.id, c.kind, c.title, c.goal, c.maint_watermark FROM conversations c "
            "WHERE c.status != 'archived' AND EXISTS ("
            "  SELECT 1 FROM events e WHERE e.conversation_id = c.id AND e.id > c.maint_watermark) "
            "ORDER BY c.id"
        ).fetchall()
        return [db.row_to_dict(r) for r in rows]  # type: ignore[misc]

    async def _consolidate(self, report: dict[str, Any]) -> None:
        from ..conversation import events

        for conv in self.pending_conversations():
            cid = str(conv["id"])
            watermark = int(conv["maint_watermark"] or 0)
            top = events.max_id(self.app, cid)
            evs = events.list_events(self.app, cid, after=watermark, limit=5000, kinds=CONSOLIDATE_KINDS)
            if not evs:  # only tool results / system noise: nothing to show, still seen
                events.update(self.app, cid, maint_watermark=top)
                continue

            entry = {"conversation_id": cid, "events": len(evs), "writes": 0}
            report["consolidated"].append(entry)
            batches = batch_events(evs)
            for index, batch in enumerate(batches, start=1):
                self._progress(
                    "consolidate",
                    f"{self._label(conv)}: batch {index}/{len(batches)}, {len(batch)} events",
                )
                try:
                    entry["writes"] += await self._consolidate_batch(conv, batch)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    report["errors"].append(f"consolidate {cid}: {type(exc).__name__}: {exc}")
                    break  # leave the watermark where it is: these events come back next pass
                events.update(self.app, cid, maint_watermark=int(batch[-1]["id"]))
            else:
                events.update(self.app, cid, maint_watermark=top)

    async def _consolidate_batch(self, conv: dict[str, Any], batch: list[dict[str, Any]]) -> int:
        from ..conversation.compaction import render_for_summary

        excerpt = render_for_summary(batch)
        related = await self._related(excerpt[:2000])
        user = (
            f"## Conversation excerpt — {self._label(conv)}\n\n{excerpt}\n\n"
            f"## Current related notes\n\n{related}"
        )
        return await self._converse(prompts.load("maintenance-consolidate"), user)

    async def _related(self, query: str) -> str:
        try:
            hits = await self.app.index.search(query, k=RELATED_K)
        except Exception as exc:
            log.warning("maintenance: related-note search failed (%r)", exc)
            hits = []
        parts = []
        for hit in hits:
            body = self._body(hit["path"], RELATED_CHARS)
            if body is not None:
                parts.append(f"### {hit['path']}\n{body}")
        return "\n\n".join(parts) if parts else "(no related notes; create one if something here is durable)"

    # -- 2. curation (03 §8.6.2) ------------------------------------------------
    def candidates(self) -> list[dict[str, Any]]:
        """Alias collisions, near-duplicates, dangling links — deterministic, no model
        call. Capped so one pass cannot run away."""
        out: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()

        by_alias: dict[str, list[str]] = {}
        for row in self.app.db.execute("SELECT alias, path FROM aliases ORDER BY alias, path"):
            paths = by_alias.setdefault(row["alias"], [])
            if row["path"] not in paths:
                paths.append(row["path"])
        for alias, paths in sorted(by_alias.items()):
            for i, left in enumerate(paths):
                for right in paths[i + 1 :]:
                    key = (left, right)
                    if key in seen:
                        continue
                    seen.add(key)
                    out.append({"kind": "pair", "left": left, "right": right, "why": f"both answer to the alias “{alias}”"})

        index = self.app.index
        if index is not None:
            threshold = float(self.setting("knowledge.curation_similarity", 0.92))
            for path, vector in sorted(index.vectors.items()):
                if path.startswith("core/"):
                    continue
                for other, score in index.nearest(vector, 3, exclude={path}, threshold=threshold):
                    key = (path, other) if path < other else (other, path)
                    if key in seen:
                        continue
                    seen.add(key)
                    out.append({"kind": "pair", "left": key[0], "right": key[1], "why": f"cosine similarity {score:.2f}"})

            for wanted in index.dangling():
                out.append({
                    "kind": "dangling",
                    "target": wanted["target"],
                    "sources": list(wanted["sources"]),
                    "why": f"{len(wanted['sources'])} note(s) link to a note that does not exist",
                })

        return out[:MAX_CANDIDATES]

    async def _curate(self, report: dict[str, Any]) -> None:
        candidates = self.candidates()
        report["curated"]["candidates"] = len(candidates)
        for candidate in candidates:
            label = self._candidate_label(candidate)
            self._progress("curate", label)
            try:
                report["curated"]["applied"] += await self._converse(
                    prompts.load("maintenance-curate"), self._curate_user(candidate)
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                report["errors"].append(f"curate {label}: {type(exc).__name__}: {exc}")

    def _curate_user(self, candidate: dict[str, Any]) -> str:
        if candidate["kind"] == "dangling":
            lines = [
                f"## Dangling link: [[{candidate['target']}]]",
                f"({candidate['why']})",
                "",
                "Notes that link to it:",
            ]
            for source in candidate["sources"][:5]:
                lines.append(f"- {source}: {self._link_line(source, candidate['target'])}")
            return "\n".join(lines)

        left, right = self._by_age(candidate["left"], candidate["right"])
        return (
            f"## Two notes that may overlap\n({candidate['why']})\n\n"
            f"### A — {left} (older)\n{self._body(left, NOTE_CHARS) or '(unreadable)'}\n\n"
            f"### B — {right} (newer)\n{self._body(right, NOTE_CHARS) or '(unreadable)'}"
        )

    def _by_age(self, left: str, right: str) -> tuple[str, str]:
        """(older, newer). Frontmatter `created` first, file mtime as the fallback; the
        prompt tells the model to keep the older path on a merge."""
        return (left, right) if self._age_key(left) <= self._age_key(right) else (right, left)

    def _age_key(self, path: str) -> str:
        try:
            note = self.app.vault.read(path)
        except Exception:
            return "9999"
        created = note.frontmatter.get("created")
        if created:
            return str(created)
        try:
            mtime = self.app.vault.resolve(path).stat().st_mtime
            return _dt.datetime.fromtimestamp(mtime, _dt.timezone.utc).isoformat()
        except OSError:
            return "9999"

    def _link_line(self, source: str, target: str) -> str:
        body = self._body(source, 20_000) or ""
        for line in body.splitlines():
            if f"[[{target}" in line:
                return line.strip()[:200]
        return "(link not found in the body)"

    # -- 3. housekeeping --------------------------------------------------------
    def archivable_threads(self) -> list[str]:
        days = int(self.setting("conversation.thread_archive_days", 14))
        cutoff = _iso_days_ago(days)
        rows = self.app.db.execute(
            "SELECT id FROM conversations WHERE kind='thread' AND status IN ('done','failed') "
            "AND updated_at < ? ORDER BY id",
            (cutoff,),
        ).fetchall()
        return [r["id"] for r in rows]

    async def _housekeep(self, report: dict[str, Any]) -> None:
        from ..conversation import events

        for cid in self.archivable_threads():
            events.set_status(self.app, cid, "archived", archived_at=db.now_iso())
            report["archived"] += 1
        if report["archived"]:
            self._progress("housekeep", f"archived {report['archived']} finished thread(s)")

        if self.app.artifacts is not None:
            try:
                report["pruned_artifacts"] = self.app.artifacts.prune_archived()
            except Exception as exc:
                report["errors"].append(f"prune: {type(exc).__name__}: {exc}")
        if getattr(self.app, "processes", None) is not None:
            try:
                report["pruned_processes"] = self.app.processes.prune_archived()
            except Exception as exc:
                report["errors"].append(f"prune processes: {type(exc).__name__}: {exc}")

        # `auto_commit_external_edits` off means the pass commits nothing at all: the
        # safety net stages the whole worktree, so running it would auto-commit the very
        # edits the setting says to leave alone (03 §8.8).
        if not self.setting("knowledge.auto_commit_external_edits", True):
            return
        git = self.app.vault.git
        try:
            dirty = await git.a_status_porcelain()
        except Exception as exc:
            dirty = []
            report["errors"].append(f"git status: {type(exc).__name__}: {exc}")
        if dirty:
            self._progress("housekeep", f"committing {len(dirty)} external edit(s)")
            report["external_commit"] = await git.a_commit_external_changes(external_message(dirty))

        # Safety net: the knowledge tools commit per write, so this is usually a no-op.
        report["committed"] = await git.a_commit(f"maintenance: {self.now_local().date().isoformat()}")

    # -- model conversation (one dispatcher, one loop) --------------------------
    def tool_schemas(self) -> list[dict[str, Any]]:
        out = []
        for name in KNOWLEDGE_TOOLS:
            tool = self.app.tools.get(name)
            if tool is not None:
                out.append(tool.to_openai())
        return out

    async def _converse(self, system: str, user: str) -> int:
        """System + one user message, then at most `MAX_STEPS` rounds of tool calls.
        Returns the number of successful `knowledge_write` calls."""
        from ..models import roles

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        tools = self.tool_schemas()
        writes = 0
        for step in range(MAX_STEPS):
            request = roles.build_request(
                self.app, role="maintenance", messages=messages, tools=tools, purpose="maintenance"
            )
            result = await self.app.models.chat(request, PRIORITY_MAINTENANCE)
            if result.outcome != "ok":
                raise MaintenanceError(result.error or f"model outcome {result.outcome}")
            if not result.tool_calls:
                return writes
            messages.append({
                "role": "assistant",
                "content": result.content,
                "tool_calls": [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {"name": call.name, "arguments": db.dumps(call.arguments)},
                    }
                    for call in result.tool_calls
                ],
            })
            for call in result.tool_calls:
                content, ok = await self._run_call(call)
                if ok and call.name == "knowledge_write":
                    writes += 1
                messages.append({"role": "tool", "tool_call_id": call.id, "content": content})
        log.info("maintenance: step budget (%s) reached; ending this candidate", MAX_STEPS)
        return writes

    async def _run_call(self, call: Any) -> tuple[str, bool]:
        ctx = ToolContext(
            app=self.app,
            conversation=self.app.conversation(self.app.desk_id()),
            call_id=call.id,
            request_text=SOURCE,
            priority=PRIORITY_MAINTENANCE,
            extras={"maintenance": True},
        )
        outcome = await dispatch(self.app, ctx, call)
        if isinstance(outcome, ApprovalNeeded):
            await self._hand_back(outcome, call)
            return (
                "skipped: this needs the person's approval, and a maintenance pass has no turn to "
                "pause. It is on the now board for them to decide; do not retry it.",
                False,
            )
        return outcome.content, outcome.ok

    async def _hand_back(self, outcome: ApprovalNeeded, call: Any) -> None:
        """03 §8.6: maintenance never leaves an approval pending. The row is decided at
        once so nothing stale lingers, and the proposal becomes a now-board item.

        The desk's turn lock is held across the decision for one reason: `approvals.decide`
        wakes the conversation an approval belongs to, and here no turn is waiting. An
        already-locked conversation needs no help — `wake` declines a locked conversation
        too. Acquiring an unlocked asyncio lock does not suspend, so nothing interleaves.
        """
        from ..conversation import events
        from ..tools import approvals

        cid = self.app.desk_id()
        lock = self.app.lock_for(cid)
        held = False
        if not lock.locked():
            await lock.acquire()
            held = True
        try:
            approvals.decide(self.app, outcome.approval_id, "deny")
            row = self.app.conversation(cid)
            if row and row["status"] == "waiting_approval" and not approvals.pending(self.app, cid):
                events.set_status(self.app, cid, "idle")
        finally:
            if held:
                lock.release()

        args = call.arguments if isinstance(call.arguments, dict) else {}
        target = str(args.get("path") or "")
        preview = str(args.get("content") or "")[:400]
        items.post(
            self.app,
            "knowledge",
            f"Maintenance wants to write {target or call.name}",
            body=f"{outcome.reason}\n\n{preview}".strip(),
            source=SOURCE,
            action={"kind": call.name, "args": args},
            notify=False,
        )

    # -- report helpers ---------------------------------------------------------
    def _announce(self, report: dict[str, Any]) -> None:
        summary = self._summary(report)
        if not summary:
            return
        body = "\n".join(report["errors"]) if report["errors"] else ""
        items.post(self.app, "knowledge", f"Maintenance: {summary}", body=body, source=SOURCE, notify=False)

    @staticmethod
    def _summary(report: dict[str, Any]) -> str:
        writes = sum(int(c["writes"]) for c in report["consolidated"]) + int(report["curated"]["applied"])
        parts = []
        if writes:
            parts.append(f"{writes} note write{'s' if writes != 1 else ''}")
        if report["archived"]:
            parts.append(f"{report['archived']} thread{'s' if report['archived'] != 1 else ''} archived")
        if report["pruned_artifacts"]:
            parts.append(f"{report['pruned_artifacts']} artifact(s) pruned")
        if report["external_commit"]:
            parts.append("external edits committed")
        return ", ".join(parts)

    def status(self) -> dict[str, Any]:
        idle_for = self.idle_for()
        remaining = None if idle_for is None else max(0.0, self.idle_seconds - idle_for)
        return {
            "enabled": self.enabled,
            "running": self.lock.locked(),
            "reason": (self.current or {}).get("reason"),
            "idle_seconds": self.idle_seconds,
            "idle_seconds_remaining": remaining,
            "next_nightly_at": next_slot(self.now_local(), self.nightly_at).isoformat(),
            "last_report": self.report,
        }

    # -- small helpers ----------------------------------------------------------
    def _body(self, path: str, cap: int) -> str | None:
        try:
            return self.app.vault.read(path).body[:cap]
        except Exception:
            return None

    @staticmethod
    def _label(conv: dict[str, Any]) -> str:
        return str(conv.get("title") or conv.get("goal") or conv.get("kind") or conv["id"])

    @staticmethod
    def _candidate_label(candidate: dict[str, Any]) -> str:
        if candidate["kind"] == "dangling":
            return f"dangling [[{candidate['target']}]]"
        return f"{candidate['left']} ↔ {candidate['right']}"


# ---------------------------------------------------------------------------------
# Module helpers
# ---------------------------------------------------------------------------------
def batch_events(evs: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Split events into ≤ `BATCH_CHARS` batches, oldest first, so a long backlog is
    consolidated in coherent chunks instead of one unreadable wall."""
    from ..conversation.compaction import render_for_summary

    batches: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    size = 0
    for event in evs:
        length = len(render_for_summary([event])) + 1
        if current and size + length > BATCH_CHARS:
            batches.append(current)
            current, size = [], 0
        current.append(event)
        size += length
    if current:
        batches.append(current)
    return batches


def external_message(paths: list[str], shown: int = 5) -> str:
    """03 §8.8: `user: edit <n> files`, naming up to five of them."""
    if len(paths) == 1:
        return f"user: edit {paths[0]}"
    listed = ", ".join(paths[:shown])
    more = f" (+{len(paths) - shown} more)" if len(paths) > shown else ""
    return f"user: edit {len(paths)} files: {listed}{more}"


def _iso_days_ago(days: int) -> str:
    moment = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=int(days))
    return moment.replace(microsecond=0).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------------
# Package hooks
# ---------------------------------------------------------------------------------
def install(app: Any) -> None:
    app.maintenance = Maintenance(app)


async def start(app: Any) -> None:
    app.maintenance.start()


async def stop(app: Any) -> None:
    if app.maintenance is not None:
        await app.maintenance.stop()
