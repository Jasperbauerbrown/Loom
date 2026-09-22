// New-note dialog: path + template type -> seeds initial content and saves it.

import { useState } from "react";
import { api } from "../../lib/api";
import { store } from "../../lib/store";
import { Button, Dialog, FieldRow, Input, Select } from "../ui";
import { navigate } from "../../lib/router";

const TEMPLATES: Record<string, (title: string) => string> = {
  device: (title) => `---\ntitle: ${title}\naliases: []\ntype: device\ntags: []\n---\n`,
  // Mirrors prompts/templates/project.md: a project is ordinary knowledge, no status
  // block (threads keep status in their transcript, 2026-09-22).
  project: (title) =>
    `---\ntitle: ${title}\naliases: []\ntype: project\nroot: \ntags: []\n---\nWhat it is, and what "done" looks like.\n\n## Where it lives\nThe folder (also \`root:\` above), the stack, how to run it, how to check it.\n\n## Decisions\nDated, with the reason.\n\n## Walkthroughs\nLinks to the \`log/\` sections that touched it, e.g. [[log/2026-09-22#Title]].\n`,
  person: (title) => `---\ntitle: ${title}\naliases: []\ntype: people\ntags: []\n---\n`,
  connector: (title) => `---\ntitle: ${title}\naliases: []\ntype: connector\ntags: []\n---\n`,
  topic: (title) => `---\ntitle: ${title}\naliases: []\ntype: topic\ntags: []\n---\n`,
  blank: () => "",
};

export function NewNoteDialog({ onClose, onCreated }: { onClose: () => void; onCreated: () => void }) {
  const [path, setPath] = useState("");
  const [template, setTemplate] = useState("blank");
  const [saving, setSaving] = useState(false);

  async function create() {
    const clean = path.trim().replace(/^\/+/, "");
    if (!clean) return;
    setSaving(true);
    const title = clean.split("/").pop()!.replace(/\.md$/, "");
    const withExt = clean.endsWith(".md") ? clean : `${clean}.md`;
    const content = TEMPLATES[template](title);
    const ok = await store.guard(() => api.saveNote(withExt, content));
    setSaving(false);
    if (ok) {
      onCreated();
      onClose();
      navigate(`/vault/${withExt.split("/").map(encodeURIComponent).join("/")}`);
    }
  }

  return (
    <Dialog
      title="New note"
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button variant="primary" onClick={create} disabled={saving || !path.trim()}>
            {saving ? "Creating…" : "Create"}
          </Button>
        </>
      }
    >
      <FieldRow label="Path" help="e.g. projects/music-bot.md (folder sets the type)">
        <Input
          autoFocus
          value={path}
          onChange={(e) => setPath(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && create()}
          placeholder="projects/my-thing.md"
        />
      </FieldRow>
      <FieldRow label="Template">
        <Select value={template} onChange={(e) => setTemplate(e.target.value)}>
          <option value="blank">Blank</option>
          <option value="device">Device</option>
          <option value="project">Project</option>
          <option value="person">Person</option>
          <option value="connector">Connector</option>
          <option value="topic">Topic</option>
        </Select>
      </FieldRow>
    </Dialog>
  );
}
