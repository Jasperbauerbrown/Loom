"""Now-board items (03 §12.1)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from ... import items
from ..deps import get_app

router = APIRouter()


@router.get("/items")
async def list_items(unseen: bool = False, include_dismissed: bool = False, limit: int = 200, app=Depends(get_app)) -> list[dict[str, Any]]:
    return items.list_items(app, unseen_only=unseen, include_dismissed=include_dismissed, limit=limit)


@router.get("/items/{item_id}")
async def get_item(item_id: str, app=Depends(get_app)) -> dict[str, Any]:
    row = items.get(app, item_id)
    if row is None:
        raise HTTPException(404, f"item {item_id} not found")
    return row


@router.post("/items/{item_id}/seen")
async def mark_seen(item_id: str, app=Depends(get_app)) -> dict[str, Any]:
    row = items.seen(app, item_id)
    if row is None:
        raise HTTPException(404, f"item {item_id} not found")
    return row


@router.post("/items/{item_id}/apply")
async def apply(item_id: str, app=Depends(get_app)) -> dict[str, Any]:
    """Run the tool call a maintenance hand-back carries; the click is the approval
    (03 §8.6). Success dismisses the item; failure keeps it with the error on
    `action.result`. Either way the row comes back — a tool error is a result, not a 4xx."""
    from ...knowledge import maintenance

    try:
        return await maintenance.apply_hand_back(app, item_id)
    except maintenance.NotApplicable as exc:
        raise HTTPException(409, str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(404, f"item {item_id} not found") from exc


@router.post("/items/{item_id}/dismiss")
async def dismiss(item_id: str, app=Depends(get_app)) -> dict[str, Any]:
    row = items.dismiss(app, item_id)
    if row is None:
        raise HTTPException(404, f"item {item_id} not found")
    return row
