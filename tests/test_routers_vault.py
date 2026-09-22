"""The /api/vault endpoints (03 §12.1). Few tests: the logic is covered elsewhere."""

from __future__ import annotations


async def test_tree_note_roundtrip_and_search(client):
    app = client.loom
    app.vault.write(
        "devices/ender-3.md",
        "A 3D printer. See [[pla-print]].",
        op="create",
        frontmatter={"aliases": ["the printer"]},
    )
    app.vault.write(
        "procedures/pla-print/SKILL.md",
        "Heat the bed.",
        op="create",
        frontmatter={"name": "pla-print", "description": "Print PLA"},
    )

    tree = (await client.get("/api/vault/tree")).json()
    assert tree["type"] == "dir"
    assert "devices" in [c["name"] for c in tree["children"]]

    note = (await client.get("/api/vault/note", params={"path": "devices/ender-3.md"})).json()
    assert note["title"] == "Ender 3" and note["type"] == "device"
    assert note["aliases"] == ["the printer"]
    assert note["links"] == ["procedures/pla-print/SKILL.md"]
    assert (await client.get("/api/vault/note", params={"path": "procedures/pla-print/SKILL.md"})).json()[
        "backlinks"
    ] == ["devices/ender-3.md"]

    saved = (
        await client.put(
            "/api/vault/note",
            json={"path": "devices/ender-3.md", "content": "---\ntitle: Ender 3\n---\nEdited in the UI.\n"},
        )
    ).json()
    assert "Edited in the UI." in saved["body"]
    assert app.vault.git.log("devices/ender-3.md")[0]["message"] == "user: edit devices/ender-3.md"

    hits = (await client.get("/api/vault/search", params={"q": "Edited in the UI"})).json()
    assert hits[0]["path"] == "devices/ender-3.md"


async def test_bad_paths_are_refused(client):
    for path in ("../escape.md", "C:/Windows/system.ini"):
        response = await client.get("/api/vault/note", params={"path": path})
        assert response.status_code == 400
    assert (await client.get("/api/vault/note", params={"path": "topics/nope.md"})).status_code == 400


async def test_history_core_wanted_and_procedures(client):
    app = client.loom
    app.vault.write("topics/x.md", "body linking [[nowhere]]", op="create")

    history = (await client.get("/api/vault/history", params={"path": "topics/x.md"})).json()
    assert history[0]["message"] == "loom: create topics/x.md"
    diff = (await client.get("/api/vault/diff", params={"sha": history[0]["sha"], "path": "topics/x.md"})).json()
    assert "body linking" in diff["diff"]
    assert (await client.get("/api/vault/recent-changes")).json()

    core = (await client.get("/api/vault/core")).json()
    assert {c["path"] for c in core} == {"core/about-me.md", "core/preferences.md", "core/conventions.md"}
    assert all(c["cap"] > 0 for c in core)

    wanted = (await client.get("/api/vault/wanted")).json()
    assert wanted == [{"target": "nowhere", "sources": ["topics/x.md"]}]

    graph = (await client.get("/api/vault/graph", params={"center": "topics/x.md"})).json()
    assert [n["path"] for n in graph["nodes"]] == ["topics/x.md"]


async def test_status_block_endpoints_are_gone(client):
    """A thread's status lives in its transcript (03 §6.7, 2026-09-22); notes carry no
    block, so the old /vault/status routes no longer exist."""
    assert (await client.get("/api/vault/status", params={"path": "projects/x.md"})).status_code in (404, 405)
    assert (await client.put("/api/vault/status", json={"path": "projects/x.md", "fields": {}})).status_code in (404, 405)


async def test_procedure_approval_endpoint(client):
    app = client.loom
    folder = app.vault.root / "procedures" / "third-party"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text("---\nname: third-party\ndescription: outside\n---\nsteps\n", encoding="utf-8")

    # Loom ships its own procedures (self-modification, 03 §15.1); this is about the
    # one that arrived from outside.
    listed = [p for p in (await client.get("/api/vault/procedures")).json() if p["name"] == "third-party"]
    assert listed == [
        {
            "name": "third-party",
            "description": "outside",
            "path": "procedures/third-party/SKILL.md",
            "approved": False,
            "approved_at": None,
            "error": None,
        }
    ]
    approved = (await client.post("/api/vault/procedures/third-party/approve")).json()
    assert approved["approved"] is True


async def test_delete_and_revert(client):
    app = client.loom
    app.vault.write("topics/x.md", "first", op="create")
    app.vault.write("topics/x.md", "second", op="rethink", reason="wrong shape")
    sha = app.vault.git.log("topics/x.md")[0]["sha"]

    reverted = (await client.post("/api/vault/revert", json={"commit": sha, "path": "topics/x.md"})).json()
    assert reverted["paths"] == ["topics/x.md"]
    assert "first" in app.vault.read("topics/x.md").body

    assert (await client.delete("/api/vault/note", params={"path": "topics/x.md"})).json()["deleted"] is True
    assert not app.vault.exists("topics/x.md")
