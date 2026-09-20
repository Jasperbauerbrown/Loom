"""The scheduler and the event router (03 §11): caps, firing, one-shots, catch-up,
policy. The fake model client runs the turn a firing produces; no live model."""

from __future__ import annotations

import datetime as _dt

import pytest

from loom import db, items
from loom.models.fake import Scripted
from loom.triggers import route_connector_event
from loom.triggers.scheduler import SchedulerError
from loom.triggers.spec import UTC, SpecError


@pytest.fixture
def sched(app):
    return app.scheduler


def make(sched, spec="daily at 09:00", payload="check the build", **kw):
    return sched.create(spec=spec, payload=payload, **kw)


def due_now(app, row_id):
    """Pull a schedule's next firing into the past so a tick picks it up."""
    past = (_dt.datetime.now(UTC) - _dt.timedelta(minutes=1)).isoformat().replace("+00:00", "Z")
    app.db.execute("UPDATE schedules SET next_run_at=? WHERE id=?", (past, row_id))


# -- create --------------------------------------------------------------------------

def test_create_validates_spec_target_and_payload(app, sched):
    row = make(sched)
    assert row["spec"] == "daily at 09:00" and row["target"] == "desk" and row["enabled"] == 1
    assert row["next_run_at"] and row["runs"] == 0 and row["created_by"] == "user"

    with pytest.raises(SpecError):
        make(sched, spec="whenever")
    with pytest.raises(SchedulerError):
        make(sched, target="01NOSUCHCONVERSATION")
    with pytest.raises(SchedulerError):
        make(sched, payload="   ")
    with pytest.raises(SchedulerError):
        make(sched, policy="shout")


def test_create_honours_the_two_caps(app, sched):
    app.settings.set(app.db, "triggers.min_interval_minutes", 5)
    with pytest.raises(SchedulerError) as exc:
        make(sched, spec="every 30s")
    assert "min_interval_minutes" in str(exc.value)
    make(sched, spec="every 5m")  # exactly at the floor is fine

    app.settings.set(app.db, "triggers.max_active_schedules", 2)
    make(sched, spec="every 10m")
    with pytest.raises(SchedulerError) as exc:
        make(sched, spec="every 10m")
    assert "max_active_schedules (2)" in str(exc.value)
    # a disabled schedule does not count against the cap
    sched.enable(sched.list()[0]["id"], False)
    make(sched, spec="every 10m")


# -- firing --------------------------------------------------------------------------

async def test_fire_writes_inbox_and_item_before_moving_the_row_and_runs_a_turn(app, sched, fake):
    fake.script(Scripted(content="noted"))
    desk = app.desk_id()
    row = make(sched, spec="every 15m", payload="check the build")
    due_now(app, row["id"])

    fired = await sched.tick()
    assert fired == 1

    inbox = app.db.execute("SELECT * FROM inbox WHERE conversation_id=?", (desk,)).fetchall()
    assert [r["source"] for r in inbox] == [f"schedule:{row['id']}"]
    assert inbox[0]["content"] == "check the build"
    assert db.loads(inbox[0]["meta"])["title"] == "check the build"

    board = items.list_items(app)
    assert board[0]["kind"] == "schedule_result"
    assert board[0]["source"] == f"schedule:{row['id']}"
    assert board[0]["action"] == {"kind": "open_conversation", "conversation_id": desk}

    after = sched.get(row["id"])
    assert after["runs"] == 1 and after["last_run_at"] and after["next_run_at"] > db.now_iso()

    await app.tasks[f"turn:{desk}"]
    kinds = [r["kind"] for r in app.db.execute("SELECT kind FROM events WHERE conversation_id=?", (desk,))]
    assert "user" in kinds and "assistant" in kinds


async def test_one_shot_disables_itself_after_firing(app, sched, fake):
    fake.script(Scripted(content="ok"))
    when = (_dt.datetime.now(UTC) + _dt.timedelta(days=1)).astimezone(sched.tz)
    row = make(sched, spec=f"at {when.strftime('%Y-%m-%dT%H:%M')}", payload="ship it")
    due_now(app, row["id"])

    assert await sched.tick() == 1
    after = sched.get(row["id"])
    assert after["enabled"] == 0 and after["next_run_at"] is None and after["runs"] == 1
    assert await sched.tick() == 0


async def test_run_now_fires_without_shifting_the_schedule(app, sched, fake):
    fake.script(Scripted(content="ok"))
    row = make(sched, spec="daily at 09:00")
    after = sched.run_now(row["id"])
    assert after["runs"] == 1 and after["next_run_at"] == row["next_run_at"]
    assert items.list_items(app)[0]["kind"] == "schedule_result"


def test_a_missing_target_disables_the_schedule_instead_of_looping(app, sched):
    row = make(sched)
    app.db.execute("UPDATE schedules SET target='01GONE' WHERE id=?", (row["id"],))
    assert sched.fire(sched.get(row["id"]))["fired"] is False
    assert sched.get(row["id"])["enabled"] == 0


# -- policy --------------------------------------------------------------------------

def test_interrupt_policy_emits_a_notification_and_queue_does_not(app, sched):
    seen = []
    app.bus.listen(lambda e: seen.append(e["type"]))

    queued = make(sched, payload="quiet one")
    sched.run_now(queued["id"])
    assert "notification" not in seen

    loud = make(sched, spec="every 30m", payload="loud one", policy="interrupt")
    sched.run_now(loud["id"])
    assert "notification" in seen


def test_the_setting_is_the_default_policy(app, sched):
    app.settings.set(app.db, "triggers.policy_schedule", "interrupt")
    seen = []
    app.bus.listen(lambda e: seen.append(e["type"]))
    sched.run_now(make(sched)["id"])
    assert "notification" in seen


# -- catch-up ------------------------------------------------------------------------

def test_catch_up_fires_inside_the_window_and_reschedules_older_misses(app, sched):
    app.settings.set(app.db, "triggers.schedule_catchup_hours", 6)
    recent = make(sched, spec="every 30m", payload="recent")
    old = make(sched, spec="every 30m", payload="old")
    now = _dt.datetime.now(UTC)
    for row_id, delta in ((recent["id"], _dt.timedelta(hours=2)), (old["id"], _dt.timedelta(hours=30))):
        app.db.execute(
            "UPDATE schedules SET next_run_at=? WHERE id=?",
            ((now - delta).isoformat().replace("+00:00", "Z"), row_id),
        )

    assert sched.catch_up() == 1
    assert sched.get(recent["id"])["runs"] == 1
    assert sched.get(old["id"])["runs"] == 0
    assert sched.get(old["id"])["next_run_at"] > db.now_iso()  # rescheduled forward, not fired
    titles = [i["title"] for i in items.list_items(app)]
    assert titles == ["Schedule: recent (missed while offline)"]


# -- update / delete / router --------------------------------------------------------

def test_update_revalidates_and_recomputes_the_next_run(app, sched):
    row = make(sched, spec="daily at 09:00")
    changed = sched.update(row["id"], spec="daily at 23:30", payload="later")
    assert changed["spec"] == "daily at 23:30" and changed["payload"] == "later"
    assert changed["next_run_at"] != row["next_run_at"]
    with pytest.raises(SpecError):
        sched.update(row["id"], spec="nope")
    with pytest.raises(SchedulerError):
        sched.update(row["id"], created_by="someone")

    events = []
    app.bus.listen(lambda e: events.append(e["type"]))
    sched.delete(row["id"])
    assert "schedule.deleted" in events and sched.get(row["id"]) is None
    with pytest.raises(KeyError):
        sched.delete(row["id"])


def test_connector_events_take_the_same_write_path(app):
    routed = route_connector_event(app, "canvas", "New assignment", "Maths, due Friday")
    assert routed["conversation_id"] == app.desk_id() and routed["policy"] == "queue"
    inbox = app.db.execute("SELECT * FROM inbox WHERE id=?", (routed["inbox_id"],)).fetchone()
    assert inbox["source"] == "connector:canvas"
    assert items.get(app, routed["item_id"])["kind"] == "notification"


# -- the tools -----------------------------------------------------------------------

async def run_tool(app, name, args):
    from loom.tools.registry import ToolContext

    ctx = ToolContext(app=app, conversation=app.conversation(app.desk_id()), call_id="c1")
    return await app.tools.get(name).handler(ctx, args)


async def test_schedule_tools_create_list_and_delete(app, sched):
    created = await run_tool(app, "schedule_create", {"spec": "daily at 09:00", "payload": "morning check"})
    assert created.ok and created.content.startswith("scheduled ")
    row = sched.get(created.meta["id"])
    assert row["created_by"] == f"conversation:{app.desk_id()}"  # the calling conversation
    assert row["target"] == app.desk_id()  # defaults to the caller, not literally "desk"

    listed = await run_tool(app, "schedule_list", {})
    assert row["id"] in listed.content and "queue (default)" in listed.content

    deleted = await run_tool(app, "schedule_delete", {"id": row["id"]})
    assert deleted.ok and sched.get(row["id"]) is None
    assert (await run_tool(app, "schedule_delete", {"id": row["id"]})).ok is False


async def test_schedule_create_turns_refusals_into_error_results(app, sched):
    bad = await run_tool(app, "schedule_create", {"spec": "whenever", "payload": "x"})
    assert bad.ok is False and "accepted forms" in bad.content

    app.settings.set(app.db, "triggers.max_active_schedules", 0)
    capped = await run_tool(app, "schedule_create", {"spec": "daily at 09:00", "payload": "x"})
    assert capped.ok is False and "max_active_schedules (0)" in capped.content
