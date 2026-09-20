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


@router.post("/items/{item_id}/dismiss")
async def dismiss(item_id: str, app=Depends(get_app)) -> dict[str, Any]:
    row = items.dismiss(app, item_id)
    if row is None:
        raise HTTPException(404, f"item {item_id} not found")
    return row
