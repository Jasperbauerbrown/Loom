"""Image input (03 §4.4, §7, §9.2, §12): refs in the transcript, bytes in the artifact
store, data URLs only at assembly time, followers for tool images, placeholders for
text-only presets, the fs_read/http/screenshot tools, and the REST endpoints.

No live model, no network: the fake client, MockTransport, and a monkeypatched capture."""

from __future__ import annotations

import asyncio
import base64
import json
import os

import httpx
import pytest

from loom import conversation
from loom.artifacts import IMAGE_ID_RE, image_dimensions, sniff_image
from loom.conversation import compaction, context, events, runner
from loom.models.client import messages_for
from loom.models.fake import Scripted
from loom.models.types import DEFAULT_IMAGE_TOKENS, ChatRequest
from loom.tools import fs_guard
from loom.tools.native import http as http_tool
from loom.tools.native import screen
from loom.tools.registry import Tool, ToolContext, ToolResult

PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
PLACEHOLDER = "[An image was attached here. This model cannot view images.]"


def _events(app, cid, kinds=None):
    return events.list_events(app, cid, kinds=kinds, limit=10_000)


async def _run(app, cid):
    await runner.run_turn(app, cid)
    task = app.tasks.get(f"turn:{cid}")
    if task is not None and not task.done():
        await task


def _ctx(app):
    return ToolContext(app=app, conversation=app.conversation(app.desk_id()), call_id="c1", event_id=1)


# ---------------------------------------------------------------------------------
# store
# ---------------------------------------------------------------------------------
def test_store_find_read_and_data_url(app):
    desk = app.desk_id()
    ref = app.artifacts.store_image(desk, PNG_1PX, "image/png")
    assert IMAGE_ID_RE.match(ref["id"]) and ref["mime"] == "image/png" and ref["bytes"] == len(PNG_1PX)
    path, mime = app.artifacts.find_image(desk, ref["id"])
    assert path.is_file() and path.suffix == ".png" and mime == "image/png"
    assert path.parent.name == desk
    assert app.artifacts.read_image(desk, ref["id"]) == (PNG_1PX, "image/png")
    url = app.artifacts.image_data_url(desk, ref["id"])
    assert url == "data:image/png;base64," + base64.b64encode(PNG_1PX).decode()
    # a bad id shape is a miss, never a path lookup
    for bad in ("../../state.db", "img_../x", "img_short", ""):
        assert app.artifacts.find_image(desk, bad) is None
    assert app.artifacts.image_data_url(desk, "img_" + "0" * 26) is None


def test_store_refuses_unsupported_types_and_oversized_images(app):
    desk = app.desk_id()
    with pytest.raises(ValueError):
        app.artifacts.store_image(desk, b"hello", "text/plain")
    app.settings.set(app.db, "conversation.image_max_mb", 1)
    with pytest.raises(ValueError) as exc:
        app.artifacts.store_image(desk, PNG_1PX + b"\0" * (1024 * 1024 + 1), "image/png")
    assert "cap" in str(exc.value)


def test_sniff_and_dimensions():
    assert sniff_image(PNG_1PX) == "image/png"
    assert sniff_image(b"\xff\xd8\xff\xe0" + b"\0" * 12) == "image/jpeg"
    assert sniff_image(b"GIF89a" + b"\0" * 10) == "image/gif"
    assert sniff_image(b"RIFF\0\0\0\0WEBPVP8 ") == "image/webp"
    assert sniff_image(b"hello world, not an image") is None
    assert image_dimensions(PNG_1PX) == (1, 1)
    assert image_dimensions(b"GIF89a\x10\x00\x20\x00" + b"\0" * 4) == (16, 32)


# ---------------------------------------------------------------------------------
# wire shapes
# ---------------------------------------------------------------------------------
URL = "data:image/png;base64," + base64.b64encode(PNG_1PX).decode()


def test_messages_for_renders_images_per_provider_and_preset():
    messages = [{"role": "user", "content": "what is this?", "images": [URL]}]

    hosted = messages_for("openai_compatible", messages, multimodal=True)[0]
    assert hosted["content"] == [
        {"type": "text", "text": "what is this?"},
        {"type": "image_url", "image_url": {"url": URL}},
    ]
    assert "images" not in hosted

    local = messages_for("ollama", messages, multimodal=True)[0]
    assert local["content"] == "what is this?" and local["images"] == [base64.b64encode(PNG_1PX).decode()]

    for kind in ("openai_compatible", "ollama"):
        plain = messages_for(kind, messages, multimodal=False)[0]
        assert plain["content"] == f"what is this?\n{PLACEHOLDER}" and "images" not in plain

    # image-only message: parts hold only the image; placeholder stands alone
    only = messages_for("openai_compatible", [{"role": "user", "content": "", "images": [URL]}], multimodal=True)[0]
    assert only["content"] == [{"type": "image_url", "image_url": {"url": URL}}]
    assert messages_for("ollama", [{"role": "user", "content": "", "images": [URL, URL]}])[0]["content"] == f"{PLACEHOLDER}\n{PLACEHOLDER}"

    # no images: untouched strings, no new keys
    text_only = messages_for("openai_compatible", [{"role": "user", "content": "hi"}], multimodal=True)[0]
    assert text_only == {"role": "user", "content": "hi"}


async def test_real_client_sends_parts_or_placeholder_by_preset(app):
    from tests.test_models_client import drain, make_client, request_for, sse

    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, content=sse({"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}, "[DONE]"))

    client = make_client(app, handler, kind="openai_compatible")
    request = request_for("hosted", multimodal=True)
    request.messages = [{"role": "user", "content": "look", "images": [URL]}]
    await drain(client, request)
    assert seen["body"]["messages"][0]["content"][1]["type"] == "image_url"

    request = request_for("hosted")
    request.messages = [{"role": "user", "content": "look", "images": [URL]}]
    await drain(client, request)
    wire = seen["body"]["messages"][0]
    assert wire["content"] == f"look\n{PLACEHOLDER}" and "images" not in wire


def test_chars_counts_images_as_a_fixed_estimate():
    base = ChatRequest(messages=[{"role": "user", "content": "hi"}])
    with_image = ChatRequest(messages=[{"role": "user", "content": "hi", "images": [URL]}])
    assert with_image.chars() - base.chars() == DEFAULT_IMAGE_TOKENS * 4
    custom = ChatRequest(messages=[{"role": "user", "content": "hi", "images": [URL]}], params={"image_tokens": 250})
    assert custom.chars() - base.chars() == 250 * 4


def test_context_chars_counts_images(app):
    conv = app.conversation(app.desk_id())
    plain = context.Context(conv=conv, prefix=[], history=[{"role": "user", "content": "x"}], turn=[], resolved_paths=[], knowledge_text="")
    imaged = context.Context(conv=conv, prefix=[], history=[{"role": "user", "content": "x", "images": [URL]}], turn=[], resolved_paths=[], knowledge_text="")
    assert imaged.chars(app) - plain.chars(app) == DEFAULT_IMAGE_TOKENS * 4


# ---------------------------------------------------------------------------------
# replay
# ---------------------------------------------------------------------------------
def test_replay_attaches_user_images_and_follows_tool_results(app):
    desk = app.desk_id()
    ref = app.artifacts.store_image(desk, PNG_1PX, "image/png")
    e_user = events.append(app, desk, "user", "look at this", {"images": [ref]})
    e_call = events.append(app, desk, "tool_call", json.dumps([{"id": "c1", "name": "screenshot", "arguments": {}}, {"id": "c2", "name": "fs_list", "arguments": {}}]), {"text": ""})
    e_r1 = events.append(app, desk, "tool_result", "1×1 png", {"call_id": "c1", "name": "screenshot", "ok": True, "images": [ref]})
    e_r2 = events.append(app, desk, "tool_result", "a.txt", {"call_id": "c2", "name": "fs_list", "ok": True})
    e_asst = events.append(app, desk, "assistant", "I see a pixel.", {})
    evs = _events(app, desk)

    msgs = context.messages_from_events(app, desk, evs)
    assert [m["role"] for m in msgs] == ["user", "assistant", "tool", "tool", "user", "assistant"]
    assert msgs[0]["images"] == [URL] and msgs[0]["_event_id"] == e_user
    follower = msgs[4]
    assert follower["images"] == [URL] and "c1" in follower["content"] and follower["_event_id"] == e_r1
    assert msgs[3]["_event_id"] == e_r2 and msgs[5]["_event_id"] == e_asst
    del e_call

    # an evicted result has no follower and its user message survives untouched
    evicted = context.messages_from_events(app, desk, evs, {e_r1: None})
    assert [m["role"] for m in evicted] == ["user", "assistant", "tool", "tool", "assistant"]
    assert evicted[2]["content"] == "[evicted]"

    # a missing file is skipped, not fatal
    path, _ = app.artifacts.find_image(desk, ref["id"])
    path.unlink()
    gone = context.messages_from_events(app, desk, evs)
    assert "images" not in gone[0] and [m["role"] for m in gone] == ["user", "assistant", "tool", "tool", "assistant"]


def test_render_for_summary_marks_images():
    evs = [
        {"kind": "user", "content": "here", "meta": {"images": [{"id": "img_x", "mime": "image/png"}]}},
        {"kind": "tool_result", "content": "shot", "meta": {"ok": True, "images": [{"id": "a"}, {"id": "b"}]}},
        {"kind": "assistant", "content": "ok", "meta": {}},
    ]
    text = compaction.render_for_summary(evs)
    assert "USER: here [+1 image]" in text and "TOOL RESULT (ok): shot [+2 images]" in text


# ---------------------------------------------------------------------------------
# the turn
# ---------------------------------------------------------------------------------
def _image_tool() -> Tool:
    async def handler(ctx, args):
        return ToolResult("captured", images=[{"data": PNG_1PX, "mime": "image/png"}])

    return Tool(name="img_tool", description="returns an image", parameters={"type": "object", "properties": {}}, handler=handler, read_only=True)


async def test_tool_images_are_stored_referenced_and_followed(app, fake):
    desk = app.desk_id()
    app.tools.register(_image_tool(), replace=True)
    seen: list = []
    app.bus.listen(seen.append)
    fake.script(Scripted(content="", tool_calls=[("img_tool", {})]), Scripted(content="A single pixel."))
    conversation.post(app, desk, "take a look")
    await _run(app, desk)

    result = _events(app, desk, ["tool_result"])[0]
    refs = result["meta"]["images"]
    assert len(refs) == 1 and IMAGE_ID_RE.match(refs[0]["id"]) and refs[0]["mime"] == "image/png"
    assert f"[image {refs[0]['id']} attached to this result]" in result["content"]
    assert app.artifacts.find_image(desk, refs[0]["id"]) is not None

    bus = [e for e in seen if e["type"] == "tool.result"][0]
    assert bus["data"]["images"] == refs

    msgs = fake.calls[1].messages
    assert msgs[-2]["role"] == "tool" and msgs[-1]["role"] == "user"
    assert msgs[-1]["images"] == [URL] and "attached to this message" in msgs[-1]["content"]
    # the next turn replays the same shape from the transcript
    fake.script(Scripted(content="still a pixel"))
    conversation.post(app, desk, "and now?")
    await _run(app, desk)
    roles = [m["role"] for m in fake.calls[2].messages]
    assert roles[-5:] == ["tool", "user", "assistant", "user", "user"]  # result, follower, answer, volatile block, new message
    replayed = [m for m in fake.calls[2].messages if m.get("images")]
    assert len(replayed) == 1 and replayed[0]["images"] == [URL]


async def test_user_images_flow_from_inbox_to_event_to_request(app, fake):
    desk = app.desk_id()
    ref = app.artifacts.store_image(desk, PNG_1PX, "image/png")
    seen: list = []
    app.bus.listen(seen.append)
    fake.script(Scripted(content="one pixel"))
    conversation.post(app, desk, "what is this", meta={"images": [{"id": ref["id"], "mime": ref["mime"]}]})
    await _run(app, desk)
    user = _events(app, desk, ["user"])[-1]
    assert user["meta"]["images"] == [{"id": ref["id"], "mime": "image/png"}]
    posted = [e for e in seen if e["type"] == "inbox.posted"][0]
    assert posted["data"]["meta"]["images"][0]["id"] == ref["id"]
    last = fake.calls[0].messages[-1]
    assert last["role"] == "user" and last["content"] == "what is this" and last["images"] == [URL]


# ---------------------------------------------------------------------------------
# tools
# ---------------------------------------------------------------------------------
@pytest.fixture
def project(app, tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    fs_guard.add_root(app, str(root), added_by="test")
    return root


async def test_fs_read_returns_an_image_whole(app, project):
    ctx = _ctx(app)
    png = project / "pixel.png"
    png.write_bytes(PNG_1PX)
    result = await app.tools.get("fs_read").handler(ctx, {"path": str(png), "offset": 7, "limit": 1})
    assert result.ok and result.images == [{"data": PNG_1PX, "mime": "image/png"}]
    assert "image/png" in result.content and "1×1" in result.content
    (project / "blob.bin").write_bytes(b"\0\1\2 not an image")
    assert "binary" in (await app.tools.get("fs_read").handler(ctx, {"path": str(project / "blob.bin")})).content
    app.settings.set(app.db, "conversation.image_max_mb", 1)
    (project / "big.png").write_bytes(PNG_1PX + b"\0" * (1024 * 1024 + 1))
    over = await app.tools.get("fs_read").handler(ctx, {"path": str(project / "big.png")})
    assert not over.ok and "image_max_mb" in over.content


async def test_http_returns_an_image_response_as_an_image(app, monkeypatch):
    ctx = _ctx(app)

    def handler(request):
        return httpx.Response(200, content=PNG_1PX, headers={"content-type": "image/png"})

    monkeypatch.setattr(http_tool, "client_factory", lambda timeout, follow: httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=timeout))
    result = await app.tools.get("http").handler(ctx, {"method": "GET", "url": "http://cam.local/snap.png"})
    assert result.ok and result.images == [{"data": PNG_1PX, "mime": "image/png"}]
    assert result.content.startswith("HTTP 200 image/png") and "[image: image/png, 1×1" in result.content
    assert result.meta["mime"] == "image/png"


async def test_screenshot_tool_with_a_fake_capture(app, monkeypatch):
    ctx = _ctx(app)
    specs: list = []

    async def fake_capture(spec):
        specs.append(spec)
        return PNG_1PX

    monkeypatch.setattr(screen, "capture", fake_capture)
    result = await app.tools.get("screenshot").handler(ctx, {"monitor": 2, "scale": 0.5})
    assert result.ok and result.images == [{"data": PNG_1PX, "mime": "image/png"}]
    assert result.content.startswith("1×1 png, ") and "monitor 2" in result.content
    assert specs[0].monitor == 2 and specs[0].fmt == "png" and specs[0].out_path.startswith(str(app.artifacts_dir))
    assert result.meta["width"] == 1 and result.meta["captured"] == "monitor 2"

    region = await app.tools.get("screenshot").handler(ctx, {"region": {"x": 0, "y": 0, "width": 10, "height": 5}})
    assert region.ok and "region 0,0 10×5" in region.content
    assert not (await app.tools.get("screenshot").handler(ctx, {"scale": 3})).ok
    assert not (await app.tools.get("screenshot").handler(ctx, {"region": {"x": 0, "y": 0, "width": 0, "height": 5}})).ok
    assert not (await app.tools.get("screenshot").handler(ctx, {"format": "bmp"})).ok


async def test_screenshot_reports_a_failed_capture(app, monkeypatch):
    async def failing(spec):
        raise screen.CaptureError("no window whose title contains 'nope'")

    monkeypatch.setattr(screen, "capture", failing)
    result = await app.tools.get("screenshot").handler(_ctx(app), {"window": "nope"})
    assert not result.ok and "nope" in result.content


@pytest.mark.skipif(os.name != "nt", reason="the real capture path is exercised on Windows only")
async def test_screenshot_real_capture_on_windows(app):
    result = await app.tools.get("screenshot").handler(_ctx(app), {"scale": 0.25})
    assert result.ok, result.content
    data = result.images[0]["data"]
    assert sniff_image(data) == "image/png"
    width, height = image_dimensions(data)
    assert width > 0 and height > 0 and result.content.startswith(f"{width}×{height} png")


async def test_dispatch_stores_tool_images_and_marks_the_text(app):
    from loom.tools import dispatch

    app.tools.register(_image_tool(), replace=True)
    ctx = _ctx(app)
    result = await dispatch.execute(app, ctx, app.tools.get("img_tool"), {})
    assert result.images is None and len(result.meta["images"]) == 1
    ref = result.meta["images"][0]
    assert result.content == f"captured\n[image {ref['id']} attached to this result]"
    assert app.artifacts.find_image(app.desk_id(), ref["id"]) is not None


# ---------------------------------------------------------------------------------
# REST
# ---------------------------------------------------------------------------------
async def test_image_upload_and_download(client):
    app = client.loom
    desk = app.desk_id()
    up = await client.post(f"/api/conversations/{desk}/images", content=PNG_1PX, headers={"Content-Type": "image/png"})
    assert up.status_code == 200, up.text
    body = up.json()
    assert IMAGE_ID_RE.match(body["id"]) and body["mime"] == "image/png" and body["bytes"] == len(PNG_1PX)

    got = await client.get(f"/api/conversations/{desk}/images/{body['id']}")
    assert got.status_code == 200 and got.content == PNG_1PX
    assert got.headers["content-type"].startswith("image/png") and "immutable" in got.headers["cache-control"]

    # the bytes decide the type when the header lies or is missing
    sniffed = await client.post(f"/api/conversations/{desk}/images", content=PNG_1PX, headers={"Content-Type": "application/octet-stream"})
    assert sniffed.status_code == 200 and sniffed.json()["mime"] == "image/png"

    assert (await client.post(f"/api/conversations/{desk}/images", content=b"hello", headers={"Content-Type": "text/plain"})).status_code == 415
    assert (await client.post("/api/conversations/nope/images", content=PNG_1PX, headers={"Content-Type": "image/png"})).status_code == 404
    assert (await client.get(f"/api/conversations/{desk}/images/img_{'0' * 26}")).status_code == 404
    assert (await client.get(f"/api/conversations/{desk}/images/..%2F..%2Fstate.db")).status_code == 404

    app.settings.set(app.db, "conversation.image_max_mb", 1)
    big = await client.post(f"/api/conversations/{desk}/images", content=PNG_1PX + b"\0" * (1024 * 1024 + 1), headers={"Content-Type": "image/png"})
    assert big.status_code == 413


async def test_messages_endpoint_accepts_image_ids(client):
    app = client.loom
    desk = app.desk_id()
    image_id = (await client.post(f"/api/conversations/{desk}/images", content=PNG_1PX, headers={"Content-Type": "image/png"})).json()["id"]
    app.models.script(Scripted(content="a pixel"), Scripted(content="still a pixel"))

    unknown = await client.post(f"/api/conversations/{desk}/messages", json={"content": "x", "images": ["img_" + "0" * 26]})
    assert unknown.status_code == 400 and "unknown image" in unknown.json()["detail"]
    assert (await client.post(f"/api/conversations/{desk}/messages", json={"content": "   ", "images": []})).status_code == 400

    sent = await client.post(f"/api/conversations/{desk}/messages", json={"content": "", "images": [image_id]})
    assert sent.status_code == 202
    for _ in range(50):
        await asyncio.sleep(0.02)
        if app.conversation(desk)["status"] == "idle" and _events(app, desk, ["assistant"]):
            break
    user = _events(app, desk, ["user"])[-1]
    assert user["meta"]["images"] == [{"id": image_id, "mime": "image/png"}] and user["content"] == ""
    request = [c for c in app.models.calls if c.purpose == "turn"][-1]
    assert request.messages[-1]["images"] == [URL]
