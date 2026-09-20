"""The per-provider priority queue (03 §5.3)."""

from __future__ import annotations

import asyncio

from loom.models.queue import ModelQueue


async def _hold(queue: ModelQueue, order: list[str], name: str, priority: int, gate: asyncio.Event) -> None:
    async with queue.slot("p", priority, {"purpose": name}, concurrency=1):
        order.append(name)
        await gate.wait()


async def test_priority_zero_is_served_before_a_queued_background_call():
    queue = ModelQueue()
    order: list[str] = []
    gate = asyncio.Event()

    holder = asyncio.create_task(_hold(queue, order, "holder", 0, gate))
    await asyncio.sleep(0)
    while not order:
        await asyncio.sleep(0)

    background = asyncio.create_task(_hold(queue, order, "background", 1, gate))
    await asyncio.sleep(0.01)
    interactive = asyncio.create_task(_hold(queue, order, "interactive", 0, gate))
    await asyncio.sleep(0.01)

    assert queue.state()["depth"] == 2
    gate.set()
    await asyncio.gather(holder, background, interactive)
    assert order == ["holder", "interactive", "background"]


async def test_fifo_within_one_priority():
    queue = ModelQueue()
    order: list[str] = []
    gate = asyncio.Event()

    first = asyncio.create_task(_hold(queue, order, "holder", 0, gate))
    await asyncio.sleep(0.01)
    tasks = []
    for name in ("a", "b", "c"):
        tasks.append(asyncio.create_task(_hold(queue, order, name, 1, gate)))
        await asyncio.sleep(0.01)
    gate.set()
    await asyncio.gather(first, *tasks)
    assert order == ["holder", "a", "b", "c"]


async def test_concurrency_one_serialises():
    queue = ModelQueue()
    live = 0
    peak = 0

    async def worker() -> None:
        nonlocal live, peak
        async with queue.slot("p", 0, {"purpose": "turn"}, concurrency=1):
            live += 1
            peak = max(peak, live)
            await asyncio.sleep(0.01)
            live -= 1

    await asyncio.gather(*(worker() for _ in range(5)))
    assert peak == 1


async def test_concurrency_four_runs_in_parallel():
    queue = ModelQueue()
    live = 0
    peak = 0

    async def worker() -> None:
        nonlocal live, peak
        async with queue.slot("hosted", 0, {"purpose": "turn"}, concurrency=4):
            live += 1
            peak = max(peak, live)
            await asyncio.sleep(0.02)
            live -= 1

    await asyncio.gather(*(worker() for _ in range(8)))
    assert peak == 4


async def test_providers_do_not_block_each_other():
    queue = ModelQueue()
    gate = asyncio.Event()
    order: list[str] = []

    async def hold(provider: str) -> None:
        async with queue.slot(provider, 0, {"purpose": "turn"}, concurrency=1):
            order.append(provider)
            await gate.wait()

    tasks = [asyncio.create_task(hold("a")), asyncio.create_task(hold("b"))]
    await asyncio.sleep(0.01)
    assert sorted(order) == ["a", "b"]
    gate.set()
    await asyncio.gather(*tasks)


async def test_state_shape_and_bus_event(app):
    queue = ModelQueue(app)
    seen: list[dict] = []
    app.bus.listen(lambda event: seen.append(event) if event["type"] == "system.queue" else None)

    assert queue.state() == {"depth": 0, "running": []}
    async with queue.slot("local-ollama", 2, {"purpose": "maintenance", "conversation_id": "c1", "model": "m"}):
        state = queue.state()
        assert state["depth"] == 0 and len(state["running"]) == 1
        entry = state["running"][0]
        assert entry["provider"] == "local-ollama"
        assert entry["purpose"] == "maintenance"
        assert entry["conversation_id"] == "c1"
        assert entry["priority"] == 2
        assert entry["model"] == "m"
        assert entry["since"]
    assert queue.state()["running"] == []
    assert len(seen) >= 2  # entered and left


async def test_a_cancelled_waiter_releases_its_place():
    queue = ModelQueue()
    gate = asyncio.Event()
    order: list[str] = []

    holder = asyncio.create_task(_hold(queue, order, "holder", 0, gate))
    await asyncio.sleep(0.01)
    doomed = asyncio.create_task(_hold(queue, order, "doomed", 0, gate))
    await asyncio.sleep(0.01)
    doomed.cancel()
    await asyncio.sleep(0.01)
    assert queue.state()["depth"] == 0

    follower = asyncio.create_task(_hold(queue, order, "follower", 0, gate))
    await asyncio.sleep(0.01)
    gate.set()
    await asyncio.gather(holder, follower)
    assert order == ["holder", "follower"]
