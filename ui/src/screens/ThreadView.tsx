// Thread view (`#/c/<id>` for kind==="thread", 03 §6.7). Left: header + conversation.
// Right: status/plan panel (the status events in its transcript), pinned notes, the
// walkthrough link (a section of the daily log), artifacts and lineage.

import { useEffect, useRef, useState } from "react";
import { api } from "../lib/api";
import { store, useStore } from "../lib/store";
import type { Artifact, Conversation as ConversationRow, StatusFields, ThreadStatus } from "../lib/types";
import { href, navigate, notePath } from "../lib/router";
import { bytes, localTime, relativeTime } from "../lib/format";
import { Badge, Button, Confirm, Empty, FieldRow, Section, Spinner, Textarea } from "../components/ui";
import { Conversation } from "../components/Conversation";
import { ConversationHeader } from "../components/ConversationHeader";
import { NotePicker } from "../components/NotePicker";

const STATUS_FIELDS: { key: keyof StatusFields; label: string; multiline?: boolean }[] = [
  { key: "state", label: "State" },
  { key: "goal", label: "Goal", multiline: true },
  { key: "done", label: "Done", multiline: true },
  { key: "doing", label: "Doing", multiline: true },
  { key: "next", label: "Next", multiline: true },
  { key: "blockers", label: "Blockers", multiline: true },
];

function StatusPanel({ id }: { id: string }) {
  const [status, setStatus] = useState<ThreadStatus | null>(null);
  const [fields, setFields] = useState<Record<string, string>>({});
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [dirty, setDirty] = useState(false);
  const dirtyRef = useRef(false);
  const markDirty = (value: boolean) => {
    dirtyRef.current = value;
    setDirty(value);
  };
  // Re-read when a new `status` event lands in this thread's transcript. Unsaved edits
  // are kept.
  const lastStatusEvent = useStore((s) => {
    const list = s.events[id];
    if (!list) return 0;
    for (let i = list.length - 1; i >= 0; i--) if (list[i].kind === "status") return list[i].id;
    return 0;
  }, Object.is);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      const row = await store.guard(() => api.conversationStatus(id));
      if (cancelled) return;
      setStatus(row ?? null);
      if (!dirtyRef.current) setFields((row?.fields as Record<string, string>) ?? {});
      setLoading(false);
    })();
    return () => {
      cancelled = true;
    };
  }, [id, lastStatusEvent]);

  async function save() {
    setSaving(true);
    const row = await store.guard(() => api.saveConversationStatus(id, fields));
    setSaving(false);
    if (row) {
      setStatus(row);
      setFields((row.fields as Record<string, string>) ?? {});
      markDirty(false);
    }
  }

  if (loading) {
    return (
      <div className="flex items-center gap-2 text-[12.5px] text-neutral-500">
        <Spinner /> Loading status…
      </div>
    );
  }

  const where =
    status?.source === "transcript" && status.owner ? `Last written by ${status.owner}` : "";

  return (
    <div>
      {where ? <p className="mb-2 text-[11.5px] break-words text-neutral-500">{where}</p> : null}
      {STATUS_FIELDS.map(({ key, label, multiline }) => (
        <FieldRow key={key} label={label}>
          {multiline ? (
            <Textarea
              rows={2}
              value={fields[key] ?? ""}
              onChange={(e) => {
                setFields({ ...fields, [key]: e.target.value });
                markDirty(true);
              }}
            />
          ) : (
            <input
              className="w-full rounded-md border border-neutral-300 bg-white px-2 py-1.5 text-[13px] dark:border-neutral-700 dark:bg-neutral-900 dark:text-neutral-100"
              value={fields[key] ?? ""}
              onChange={(e) => {
                setFields({ ...fields, [key]: e.target.value });
                markDirty(true);
              }}
            />
          )}
        </FieldRow>
      ))}
      {fields.updated ? (
        <p className="mb-2 text-[11px] text-neutral-500">Last updated {relativeTime(fields.updated)}</p>
      ) : null}
      <div className="flex justify-end">
        <Button variant="primary" size="sm" disabled={!dirty || saving} onClick={() => void save()}>
          {saving ? <Spinner /> : null} Save
        </Button>
      </div>
    </div>
  );
}

function PinnedNotesPanel({ conversation }: { conversation: ConversationRow }) {
  // The notes in the model's context every turn. The thread pins with
  // `knowledge_read(pin=true)`, the spawner with `notes` at create time, the person here.
  // The row arrives back on `conversation.updated`; a missing note is a 400 the guard
  // shows as a toast.
  const [saving, setSaving] = useState(false);

  async function save(next: string[]) {
    setSaving(true);
    const row = await store.guard(() => api.updateConversation(conversation.id, { pinned_notes: next }));
    setSaving(false);
    if (row) void store.loadConversation(conversation.id);
  }

  return (
    <div>
      {conversation.pinned_notes.length === 0 ? (
        <p className="mb-1.5 text-[11.5px] text-neutral-500">Nothing pinned. The thread reads knowledge as it goes.</p>
      ) : null}
      <NotePicker value={conversation.pinned_notes} onChange={(next) => void save(next)} disabled={saving} />
    </div>
  );
}

function WalkthroughLink({ conversation }: { conversation: ConversationRow }) {
  // Finish appends the walkthrough as a section of the daily log and records the note's
  // path on the row (03 §6.7). Until then there is nothing to link.
  const path = conversation.walkthrough;
  if (!path) return <Empty>No walkthrough yet.</Empty>;
  const title = conversation.title || conversation.id;
  return (
    <p className="text-[12.5px]">
      <a className="text-sky-700 hover:underline dark:text-sky-400" href={notePath(path)}>
        {path}
      </a>
      <span className="text-neutral-500"> · section "{title}"</span>
    </p>
  );
}


function ArtifactsList({ id }: { id: string }) {
  const [artifacts, setArtifacts] = useState<Artifact[] | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      const rows = await store.guard(() => api.conversationArtifacts(id));
      if (!cancelled) setArtifacts(rows ?? []);
    })();
    return () => {
      cancelled = true;
    };
  }, [id]);

  if (artifacts === null) return <Spinner />;
  if (artifacts.length === 0) return <Empty>No artifacts yet.</Empty>;
  return (
    <ul className="space-y-1">
      {artifacts.map((a) => (
        <li key={a.id} className="flex items-center justify-between text-[12px]">
          <span className="truncate font-mono text-neutral-600 dark:text-neutral-400">{a.id}</span>
          <span className="shrink-0 text-neutral-500">
            {bytes(a.bytes)} · {relativeTime(a.created_at)}
          </span>
        </li>
      ))}
    </ul>
  );
}

export function ThreadView({ id }: { id: string }) {
  const conversation = useStore((s) => s.conversations[id], Object.is);
  const parent = useStore((s) => (conversation?.parent_id ? s.conversations[conversation.parent_id] : undefined));
  const [confirmArchive, setConfirmArchive] = useState(false);

  useEffect(() => {
    if (!conversation) void store.loadConversation(id);
  }, [id, conversation]);

  if (!conversation) {
    return (
      <div className="flex h-full items-center justify-center text-[13px] text-neutral-500">
        <Spinner className="mr-2" /> Loading thread…
      </div>
    );
  }

  return (
    <div className="flex h-full min-h-0">
      <div className="flex min-w-0 flex-1 flex-col">
        <ConversationHeader id={id} />
        <div className="min-h-0 flex-1">
          <Conversation id={id} />
        </div>
      </div>

      <aside className="w-80 shrink-0 overflow-y-auto border-l border-neutral-200 p-3 dark:border-neutral-800">
        <div className="mb-3 flex items-center justify-between">
          <h2 className="text-[13px] font-semibold text-neutral-900 dark:text-neutral-100">Thread</h2>
          <Button variant="danger" size="sm" onClick={() => setConfirmArchive(true)}>
            Archive
          </Button>
        </div>

        <dl className="mb-3 space-y-1 text-[12px]">
          {conversation.parent_id ? (
            <div className="flex justify-between gap-2">
              <dt className="text-neutral-500">Spawned by</dt>
              <dd className="truncate">
                <a
                  className="text-sky-700 hover:underline dark:text-sky-400"
                  href={href(`/c/${conversation.parent_id}`)}
                  onClick={(e) => {
                    e.preventDefault();
                    navigate(`/c/${conversation.parent_id}`);
                  }}
                >
                  {parent?.title || parent?.goal || conversation.parent_id}
                </a>
              </dd>
            </div>
          ) : null}
          <div className="flex justify-between gap-2">
            <dt className="text-neutral-500">Created</dt>
            <dd>{localTime(conversation.created_at)}</dd>
          </div>
          {conversation.preset ? (
            <div className="flex justify-between gap-2">
              <dt className="text-neutral-500">Preset</dt>
              <dd>
                <Badge tone="violet">{conversation.preset}</Badge>
              </dd>
            </div>
          ) : null}
        </dl>

        <Section title="Status">
          <StatusPanel id={id} />
        </Section>
        <Section title="Pinned notes">
          <PinnedNotesPanel conversation={conversation} />
        </Section>
        <Section title="Walkthrough">
          <WalkthroughLink conversation={conversation} />
        </Section>

        <Section title="Artifacts">
          <ArtifactsList id={id} />
        </Section>
      </aside>

      {confirmArchive ? (
        <Confirm
          title="Archive thread"
          body="This thread will be archived. It stays searchable and can be reopened later."
          confirmLabel="Archive"
          danger
          onClose={() => setConfirmArchive(false)}
          onConfirm={() => {
            void store.guard(() => api.archive(id));
          }}
        />
      ) : null}
    </div>
  );
}
