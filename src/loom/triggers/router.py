"""The event router (03 §11.2): the one write path from "something happened" to an
ordinary inbox row plus a now-board item.

    route(app, TriggerEvent(...)) -> {"inbox_id", "item_id", "conversation_id", "policy"}
    route_connector_event(app, name, title, content, meta=None, conversation_id=None)

There is no automation path: a routed event becomes an inbox row on a conversation, and
`conversation.post` wakes the ordinary turn. The item is the board entry (§11.3).

Order is persist-before-act *and* item-before-wake: the item row is written first, then
the inbox row, whose `post` schedules the turn. Both rows therefore exist before any
turn can run (charter §2; 03 §11.2 "items and inbox rows are written before any turn is
scheduled").

Policy per class comes from settings (`triggers.policy_*`) unless the event overrides it;
`interrupt` posts the item with `notify=True`, which emits the `notification` event that
every surface in `triggers.notification_targets` renders. `queue` posts the item only.

The conversation package notifies parents of thread events from `runner._notify_parent`
directly; that is the same two writes with the same policy setting and is left alone.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from .. import items

log = logging.getLogger(__name__)

KINDS = ("thread_event", "connector", "schedule")

POLICY_SETTING = {
    "thread_event": "triggers.policy_thread_event",
    "connector": "triggers.policy_connector",
    "schedule": "triggers.policy_schedule",
}

ITEM_KIND = {
    "thread_event": "thread_event",
    "connector": "notification",
    "schedule": "schedule_result",
}

BODY_CAP = 2000


@dataclass
class TriggerEvent:
    """One thing that happened and wants a turn.

    kind     which class of trigger (decides the default policy and the item kind)
    source   the inbox/item `source` string: `schedule:<id>`, `connector:<name>`,
             `conversation:<id>`
    target   `desk`, a conversation id, or None (resolved from `meta` per kind)
    title    one line for the board item
    content  the text that becomes the inbox row's content
    meta     inbox meta; `title` is what `[schedule "..."]` renders, and the kind-specific
             keys (`parent_id`, `conversation_id`) resolve the target
    policy   `interrupt` | `queue` | None (None → the setting for this kind)
    """

    kind: str
    source: str
    title: str
    content: str
    target: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)
    policy: str | None = None


def resolve_target(app, event: TriggerEvent) -> str:  # type: ignore[no-untyped-def]
    """The conversation id an event lands on. `desk` and None resolve to the desk."""
    if event.kind == "thread_event":
        target = event.meta.get("parent_id") or event.target
    elif event.kind == "connector":
        target = event.meta.get("conversation_id") or event.target
    else:
        target = event.target
    if not target or target == "desk":
        return app.desk_id()
    return str(target)


def policy_for(app, event: TriggerEvent) -> str:  # type: ignore[no-untyped-def]
    if event.policy in ("interrupt", "queue"):
        return event.policy
    key = POLICY_SETTING.get(event.kind)
    if key is None:
        return "queue"
    value = str(app.settings.get(key) or "")
    return value if value in ("interrupt", "queue") else "queue"  # fail closed: no notification


def route(app, event: TriggerEvent) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    """Write the item and the inbox row for one event; return both ids."""
    if event.kind not in KINDS:
        raise ValueError(f"unknown trigger kind {event.kind!r}")
    from .. import conversation

    target_id = resolve_target(app, event)
    if app.conversation(target_id) is None:
        raise KeyError(target_id)
    policy = policy_for(app, event)

    item = items.post(
        app,
        ITEM_KIND[event.kind],
        event.title,
        (event.content or "")[:BODY_CAP],
        conversation_id=target_id,
        source=event.source,
        action={"kind": "open_conversation", "conversation_id": target_id},
        notify=(policy == "interrupt"),
    )
    meta = dict(event.meta)
    meta.setdefault("title", event.title)
    inbox_id = conversation.post(app, target_id, event.content, source=event.source, meta=meta)
    log.debug("routed %s from %s to %s (%s)", event.kind, event.source, target_id, policy)
    return {"inbox_id": inbox_id, "item_id": item["id"], "conversation_id": target_id, "policy": policy}


def route_connector_event(  # type: ignore[no-untyped-def]
    app,
    connector_name: str,
    title: str,
    content: str,
    meta: dict[str, Any] | None = None,
    conversation_id: str | None = None,
) -> dict[str, Any]:
    """What the connector loader calls when a connector reports an event (03 §10.2)."""
    return route(
        app,
        TriggerEvent(
            kind="connector",
            source=f"connector:{connector_name}",
            title=title,
            content=content,
            target=conversation_id,
            meta=dict(meta or {}),
        ),
    )
