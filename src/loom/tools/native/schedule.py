"""The schedule tools (03 §9.2, §11.1): how the assistant gives itself a future.

Registered by `loom.triggers.install`, not by `tools.native.install`: the scheduler is
the owner of the table these tools write, and a build without it has no schedule tools.
"""

from __future__ import annotations

from typing import Any

from ... import prompts
from ...triggers.scheduler import SchedulerError
from ...triggers.spec import SpecError
from ..registry import Tool, ToolContext, ToolResult


def _scheduler(ctx: ToolContext):  # type: ignore[no-untyped-def]
    scheduler = ctx.app.scheduler
    if scheduler is None:  # pragma: no cover - triggers.install always attaches one
        raise SchedulerError("the scheduler is not running")
    return scheduler


async def create_handler(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    try:
        scheduler = _scheduler(ctx)
        row = scheduler.create(
            spec=str(args.get("spec") or ""),
            target=str(args.get("target") or ctx.conversation_id),
            payload=str(args.get("payload") or ""),
            policy=args.get("policy"),
            created_by=f"conversation:{ctx.conversation_id}",
        )
    except (SpecError, SchedulerError) as exc:
        return ToolResult.error(str(exc))
    return ToolResult(
        content=f"scheduled {row['id']}: {row['spec']}; next {scheduler.local(row['next_run_at'])}",
        meta={"id": row["id"], "next_run_at": row["next_run_at"]},
    )


async def list_handler(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    scheduler = _scheduler(ctx)
    rows = scheduler.list(include_disabled=True)
    if not rows:
        return ToolResult(content="[no schedules]", meta={"count": 0})
    header = ["id", "spec", "target", "policy", "next run", "runs", "enabled", "created by"]
    table = [header]
    default = str(ctx.app.settings.get("triggers.policy_schedule"))
    for row in rows:
        table.append([
            row["id"],
            row["spec"],
            row["target"],
            row["policy"] or f"{default} (default)",
            scheduler.local(row["next_run_at"]),
            str(row["runs"]),
            "yes" if row["enabled"] else "no",
            row["created_by"],
        ])
    widths = [max(len(r[i]) for r in table) for i in range(len(header))]
    lines = ["  ".join(cell.ljust(widths[i]) for i, cell in enumerate(r)).rstrip() for r in table]
    lines.insert(1, "  ".join("-" * w for w in widths))
    lines.append(f"\ntimes are local ({scheduler.tz_name})")
    return ToolResult(content="\n".join(lines), meta={"count": len(rows)})


async def delete_handler(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    scheduler = _scheduler(ctx)
    schedule_id = str(args.get("id") or "").strip()
    if not schedule_id:
        return ToolResult.error("id is empty")
    try:
        row = scheduler.delete(schedule_id)
    except KeyError:
        return ToolResult.error(f"no schedule {schedule_id}")
    return ToolResult(content=f"deleted schedule {row['id']} ({row['spec']})", meta={"id": row["id"]})


def tools() -> list[Tool]:
    return [
        Tool(
            name="schedule_create",
            description=prompts.load("tools/schedule_create"),
            parameters={
                "type": "object",
                "properties": {
                    "spec": {
                        "type": "string",
                        "description": (
                            "When. 'every 15m' (units s/m/h/d), 'daily at 09:00', "
                            "'weekly on mon,thu at 18:30', 'monthly on 1 at 08:00', "
                            "'at 2026-09-26T18:00' (one-shot), or a 5-field cron 'm h dom mon dow'."
                        ),
                    },
                    "payload": {
                        "type": "string",
                        "description": "The text delivered to the conversation when it fires.",
                    },
                    "target": {
                        "type": "string",
                        "description": "'desk' or a conversation id. Defaults to this conversation.",
                    },
                    "policy": {
                        "type": "string",
                        "enum": ["interrupt", "queue"],
                        "description": "Override the schedule trigger policy for this one.",
                    },
                },
                "required": ["spec", "payload"],
            },
            handler=create_handler,
        ),
        Tool(
            name="schedule_list",
            description=prompts.load("tools/schedule_list"),
            parameters={"type": "object", "properties": {}},
            handler=list_handler,
            read_only=True,
        ),
        Tool(
            name="schedule_delete",
            description=prompts.load("tools/schedule_delete"),
            parameters={
                "type": "object",
                "properties": {"id": {"type": "string", "description": "Schedule id from schedule_list."}},
                "required": ["id"],
            },
            handler=delete_handler,
            destructive=True,
        ),
    ]
