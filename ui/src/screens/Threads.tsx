// Threads list (`#/threads`, 03 §6.7). A tree by parent_id, filterable by status, plus
// a "new thread" dialog that spawns a conversation via `conversation.create`.

import { useEffect, useMemo, useState } from "react";
import { api } from "../lib/api";
import { conversationList, store, useStore } from "../lib/store";
import type { Conversation, ConversationStatus } from "../lib/types";
import { navigate } from "../lib/router";
import { cls, relativeTime, statusLabel, statusTitle } from "../lib/format";
import { Badge, Button, Dialog, Empty, FieldRow, Input, Select, Textarea, Toggle } from "../components/ui";
import { IconPlus } from "../components/Icons";
import { NotePicker } from "../components/NotePicker";

type Filter = "all" | "running" | "waiting" | "done" | "failed" | "archived";

function statusTone(status: ConversationStatus): "neutral" | "green" | "amber" | "red" | "sky" {
  switch (status) {
    case "running":
      return "sky";
    case "waiting_approval":
    case "waiting_input":
      return "amber";
    case "done":
      return "green";
    case "failed":
      return "red";
    default:
      return "neutral";
  }
}

function matchesFilter(status: ConversationStatus, filter: Filter): boolean {
  if (filter === "all") return true;
  if (filter === "running") return status === "running";
  if (filter === "waiting") return status === "waiting_approval" || status === "waiting_input";
  if (filter === "done") return status === "done";
  if (filter === "failed") return status === "failed";
  if (filter === "archived") return status === "archived";
  return true;
}

/** Flatten threads into a depth-ordered list: each root followed by its descendants. */
function orderTree(threads: Conversation[]): { row: Conversation; indent: number }[] {
  const byParent = new Map<string, Conversation[]>();
  const byId = new Map<string, Conversation>();
  for (const t of threads) byId.set(t.id, t);
  for (const t of threads) {
    const key = t.parent_id && byId.has(t.parent_id) ? t.parent_id : "";
    if (!byParent.has(key)) byParent.set(key, []);
    byParent.get(key)!.push(t);
  }
  for (const list of byParent.values()) {
    list.sort((a, b) => (a.updated_at < b.updated_at ? 1 : -1));
  }
  const out: { row: Conversation; indent: number }[] = [];
  const visit = (parentKey: string, indent: number) => {
    for (const t of byParent.get(parentKey) ?? []) {
      out.push({ row: t, indent });
      visit(t.id, indent + 1);
    }
  };
  visit("", 0);
  return out;
}

function NewThreadDialog({ onClose }: { onClose: () => void }) {
  const presets = useStore((s) => s.presets, (a, b) => a.length === b.length);
  const [goal, setGoal] = useState("");
  const [notes, setNotes] = useState<string[]>([]);
  const [roots, setRoots] = useState<string[]>([]);
  const [newRoot, setNewRoot] = useState("");
  const [isolate, setIsolate] = useState(false);
  const [preset, setPreset] = useState("");
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    void store.loadModels();
  }, []);

  function addRoot() {
    const p = newRoot.trim();
    if (!p || roots.includes(p)) return;
    setRoots([...roots, p]);
    setNewRoot("");
  }

  async function submit() {
    if (!goal.trim()) return;
    setSubmitting(true);
    const created = await store.guard(() =>
      api.createConversation({
        kind: "thread",
        goal: goal.trim(),
        notes: notes.length ? notes : undefined,
        roots: roots.length ? roots : undefined,
        isolation: isolate && roots.length ? "worktree" : undefined,
        preset: preset || undefined,
      }),
    );
    setSubmitting(false);
    if (!created) return;
    onClose();
    navigate(`/c/${created.id}`);
  }

  return (
    <Dialog
      title="New thread"
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button variant="primary" disabled={!goal.trim() || submitting} onClick={() => void submit()}>
            Create
          </Button>
        </>
      }
    >
      <FieldRow label="Goal" help="What should this thread accomplish?">
        <Textarea
          rows={3}
          value={goal}
          autoFocus
          onChange={(e) => setGoal(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              void submit();
            }
          }}
        />
      </FieldRow>

      <FieldRow
        label="Notes to pin (optional)"
        help="Vault notes the thread keeps in its context every turn: a project, a device, a procedure. Its status stays in its transcript and its walkthrough goes to the daily log either way."
      >
        <NotePicker value={notes} onChange={setNotes} />
      </FieldRow>

      <FieldRow label="Roots" help="Filesystem paths this thread may work in.">
        <div className="flex gap-1.5">
          <Input
            value={newRoot}
            onChange={(e) => setNewRoot(e.target.value)}
            placeholder="C:/Users/…/Projects/…"
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                addRoot();
              }
            }}
          />
          <Button onClick={addRoot}>Add</Button>
        </div>
        {roots.length ? (
          <ul className="mt-1.5 space-y-1">
            {roots.map((r) => (
              <li key={r} className="flex items-center justify-between rounded bg-neutral-100 px-2 py-1 text-[12px] dark:bg-neutral-800">
                <span className="truncate font-mono">{r}</span>
                <button
                  type="button"
                  className="text-neutral-500 hover:text-red-600"
                  onClick={() => setRoots(roots.filter((x) => x !== r))}
                >
                  Remove
                </button>
              </li>
            ))}
          </ul>
        ) : null}
      </FieldRow>

      <FieldRow
        label="Isolation"
        help="Work in a git worktree branched from the first root (branch loom/<thread id>); the branch is how the work is handed back. The root must be inside a git repository."
      >
        <Toggle checked={isolate && roots.length > 0} onChange={setIsolate} disabled={roots.length === 0} label="Own git worktree" />
      </FieldRow>

      <FieldRow label="Preset" help="Model preset override; leave blank for the role default.">
        <Select value={preset} onChange={(e) => setPreset(e.target.value)}>
          <option value="">(role default)</option>
          {presets.map((p) => (
            <option key={p.name} value={p.name}>
              {p.name}
            </option>
          ))}
        </Select>
      </FieldRow>
    </Dialog>
  );
}

export function Threads() {
  const all = useStore(conversationList);
  const threads = useMemo(() => all.filter((c) => c.kind === "thread"), [all]);
  const [filter, setFilter] = useState<Filter>("all");
  const [dialogOpen, setDialogOpen] = useState(false);

  useEffect(() => {
    void store.loadConversations();
  }, []);

  const filtered = useMemo(() => threads.filter((t) => matchesFilter(t.status, filter)), [threads, filter]);
  const rows = useMemo(() => orderTree(filtered), [filtered]);

  return (
    <div className="flex h-full flex-col overflow-y-auto p-4">
      <div className="mb-3 flex items-center justify-between gap-3">
        <h1 className="text-[16px] font-semibold text-neutral-900 dark:text-neutral-100">Threads</h1>
        <div className="flex items-center gap-2">
          <Select value={filter} onChange={(e) => setFilter(e.target.value as Filter)} className="w-40">
            <option value="all">All</option>
            <option value="running">Running</option>
            <option value="waiting">Waiting</option>
            <option value="done">Done</option>
            <option value="failed">Failed</option>
            <option value="archived">Archived</option>
          </Select>
          <Button variant="primary" onClick={() => setDialogOpen(true)}>
            <IconPlus className="h-3.5 w-3.5" /> New thread
          </Button>
        </div>
      </div>

      {rows.length === 0 ? (
        <Empty>No threads match this filter.</Empty>
      ) : (
        <div className="overflow-hidden rounded-lg border border-neutral-200 dark:border-neutral-800">
          {rows.map(({ row, indent }) => (
            <button
              key={row.id}
              type="button"
              onClick={() => navigate(`/c/${row.id}`)}
              className="flex w-full items-center gap-3 border-b border-neutral-100 px-3 py-2 text-left last:border-0 hover:bg-neutral-50 dark:border-neutral-800/70 dark:hover:bg-neutral-800/50"
              style={{ paddingLeft: `${12 + indent * 20}px` }}
            >
              <Badge tone={statusTone(row.status)} title={statusTitle(row.status)}>
                {statusLabel(row.status)}
              </Badge>
              <div className="min-w-0 flex-1">
                <div className="truncate text-[13px] font-medium text-neutral-900 dark:text-neutral-100">
                  {row.title || row.goal || row.id}
                </div>
                {row.goal && row.title ? (
                  <div className="truncate text-[11.5px] text-neutral-500">{row.goal}</div>
                ) : null}
              </div>
              {row.preset ? <Badge tone="violet">{row.preset}</Badge> : null}
              <span className={cls("shrink-0 text-[11.5px] text-neutral-500")}>{relativeTime(row.updated_at)}</span>
            </button>
          ))}
        </div>
      )}

      {dialogOpen ? <NewThreadDialog onClose={() => setDialogOpen(false)} /> : null}
    </div>
  );
}
