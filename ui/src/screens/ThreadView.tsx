// Thread view (`#/c/<id>` for kind==="thread", 03 §6.7). Left: header + conversation.
// Right: status/plan panel (the scope note's status block, or for a task the status
// events in its transcript), walkthrough links, artifacts and lineage.

import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../lib/api";
import { store, useStore } from "../lib/store";
import type {
  Artifact,
  Conversation as ConversationRow,
  ThreadStatus,
  VaultSearchHit,
  VaultStatus,
  VaultTreeNode,
} from "../lib/types";
import { href, navigate, notePath } from "../lib/router";
import { bytes, localTime, relativeTime } from "../lib/format";
import { Badge, Button, Confirm, Empty, FieldRow, Section, Spinner, Textarea } from "../components/ui";
import { Conversation } from "../components/Conversation";
import { ConversationHeader } from "../components/ConversationHeader";

const STATUS_FIELDS: { key: keyof VaultStatus["fields"]; label: string; multiline?: boolean }[] = [
  { key: "state", label: "State" },
  { key: "goal", label: "Goal", multiline: true },
  { key: "done", label: "Done", multiline: true },
  { key: "doing", label: "Doing", multiline: true },
  { key: "next", label: "Next", multiline: true },
  { key: "blockers", label: "Blockers", multiline: true },
];

function StatusPanel({ id, scopeNote }: { id: string; scopeNote: string | null }) {
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
  // Re-read when the sink changes underneath us: a note write (knowledge.changed) or a
  // new `status` event in this thread's transcript. Unsaved edits are kept.
  const knowledgeStale = useStore((s) => s.knowledgeStale, Object.is);
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
  }, [id, scopeNote, knowledgeStale, lastStatusEvent]);

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
    status?.source === "note"
      ? `In the project note${status.owner ? ` · owned by ${status.owner}` : ""}`
      : status?.source === "transcript"
        ? `In the transcript${status.owner ? ` · last written by ${status.owner}` : ""}`
        : "";

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

/** Every note under these tree nodes, at any depth. */
function collectNotes(nodes: VaultTreeNode[]): VaultTreeNode[] {
  const out: VaultTreeNode[] = [];
  for (const node of nodes) {
    const isFolder = node.kind === "dir" || !!(node.children && node.children.length);
    if (isFolder) out.push(...collectNotes(node.children ?? []));
    else out.push(node);
  }
  return out;
}

type WalkthroughHit = { path: string; title?: string | null };

function WalkthroughLinks({ conversation }: { conversation: ConversationRow }) {
  // A project's walkthroughs live at projects/<name>/walkthrough-YYYY-MM-DD.md (03 §6.7):
  // list them from the vault tree by path, newest first. A task's walkthrough is a
  // section of the daily log naming the thread id: find those by search. Both refresh on
  // knowledge.changed, so a finish shows up here without a reload.
  const scopeNote = conversation.scope_note;
  const tree = useStore((s) => s.vaultTree);
  const knowledgeStale = useStore((s) => s.knowledgeStale, Object.is);
  const [logHits, setLogHits] = useState<VaultSearchHit[] | null>(null);

  useEffect(() => {
    if (scopeNote && tree === null) void store.loadVaultTree();
  }, [scopeNote, tree]);

  useEffect(() => {
    if (scopeNote) return;
    let cancelled = false;
    (async () => {
      const rows = await store.guard(() => api.vaultSearch(conversation.id));
      if (!cancelled) setLogHits((rows ?? []).filter((h) => h.path.startsWith("log/")));
    })();
    return () => {
      cancelled = true;
    };
  }, [scopeNote, conversation.id, knowledgeStale]);

  const prefix = (scopeNote ?? "").replace(/\.md$/, "") + "/walkthrough-";
  const hits = useMemo<WalkthroughHit[] | null>(() => {
    if (!scopeNote) return logHits;
    if (tree === null) return null;
    return collectNotes(tree)
      .filter((n) => n.path.startsWith(prefix))
      .sort((a, b) => (a.path < b.path ? 1 : -1));
  }, [scopeNote, tree, prefix, logHits]);

  if (hits === null) return <Spinner />;
  if (hits.length === 0) return <Empty>No walkthroughs yet.</Empty>;
  return (
    <div>
      {!scopeNote ? <p className="mb-1 text-[11px] text-neutral-500">In the daily log</p> : null}
      <ul className="space-y-1">
        {hits.map((h) => (
          <li key={h.path}>
            <a className="text-[12.5px] text-sky-700 hover:underline dark:text-sky-400" href={notePath(h.path)}>
              {h.title || h.path}
            </a>
          </li>
        ))}
      </ul>
    </div>
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
            <dt className="text-neutral-500">Scope note</dt>
            <dd className="truncate">
              {conversation.scope_note ? (
                <a className="text-sky-700 hover:underline dark:text-sky-400" href={notePath(conversation.scope_note)}>
                  {conversation.scope_note}
                </a>
              ) : (
                <span className="text-neutral-500" title="A task: status in its transcript, walkthrough in the daily log">
                  none (task)
                </span>
              )}
            </dd>
          </div>
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
          <StatusPanel id={id} scopeNote={conversation.scope_note} />
        </Section>
        <Section title="Walkthroughs">
          <WalkthroughLinks conversation={conversation} />
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
