// The now board (03 §13.1): needs you / running threads / queued, every line actionable.

import { useEffect, useState } from "react";
import { api } from "../lib/api";
import { store, useStore } from "../lib/store";
import type { Approval, Conversation, Item } from "../lib/types";
import { navigate } from "../lib/router";
import { relativeTime, statusLabel, statusTitle, truncate } from "../lib/format";
import { Badge, Button, Empty } from "./ui";

function Group({ title, count, children }: { title: string; count: number; children: React.ReactNode }) {
  return (
    <section className="mb-4">
      <h3 className="mb-1.5 flex items-center gap-2 px-1 text-[11.5px] font-semibold tracking-wide text-neutral-500 uppercase">
        {title}
        {count > 0 ? <span className="rounded bg-neutral-200 px-1.5 text-[10.5px] dark:bg-neutral-800">{count}</span> : null}
      </h3>
      {children}
    </section>
  );
}

function Line({ children }: { children: React.ReactNode }) {
  return (
    <div className="mb-1.5 rounded-md border border-neutral-200 bg-white px-2.5 py-2 dark:border-neutral-800 dark:bg-neutral-900">
      {children}
    </div>
  );
}

function ApprovalLine({ approval }: { approval: Approval }) {
  return (
    <Line>
      <div className="flex items-center gap-2">
        <Badge tone="amber">approval</Badge>
        <span className="truncate font-mono text-[12px]">{approval.tool}</span>
      </div>
      {approval.reason ? (
        <p className="mt-1 text-[11.5px] text-neutral-500 [overflow-wrap:anywhere]">{truncate(approval.reason, 140)}</p>
      ) : null}
      <div className="mt-1.5 flex gap-1.5">
        <Button size="sm" variant="primary" onClick={() => void store.guard(() => api.decideApproval(approval.id, "allow"))}>
          Allow
        </Button>
        <Button size="sm" variant="danger" onClick={() => void store.guard(() => api.decideApproval(approval.id, "deny"))}>
          Deny
        </Button>
        <Button size="sm" variant="ghost" onClick={() => navigate(`/c/${approval.conversation_id}`)}>
          Open
        </Button>
      </div>
    </Line>
  );
}

function ItemLine({ item }: { item: Item }) {
  return (
    <Line>
      <div className="flex items-start gap-2">
        <Badge tone={item.kind === "setup" ? "sky" : "neutral"}>{item.kind}</Badge>
        <div className="min-w-0 flex-1">
          <div className="truncate text-[12.5px] font-medium">{item.title}</div>
          {item.body ? <div className="mt-0.5 text-[11.5px] text-neutral-500">{truncate(item.body, 160)}</div> : null}
          <div className="mt-0.5 text-[11px] text-neutral-400">{relativeTime(item.created_at)}</div>
        </div>
      </div>
      <div className="mt-1.5 flex gap-1.5">
        {item.conversation_id ? (
          <Button size="sm" onClick={() => navigate(`/c/${item.conversation_id}`)}>
            Open
          </Button>
        ) : null}
        {!item.seen_at ? (
          <Button size="sm" variant="ghost" onClick={() => void store.guard(() => api.seenItem(item.id))}>
            Mark seen
          </Button>
        ) : null}
        <Button size="sm" variant="ghost" onClick={() => void store.guard(() => api.dismissItem(item.id))}>
          Dismiss
        </Button>
      </div>
    </Line>
  );
}

function ThreadLine({ conversation }: { conversation: Conversation }) {
  return (
    <Line>
      <button className="w-full text-left" onClick={() => navigate(`/c/${conversation.id}`)}>
        <div className="flex items-center gap-2">
          <Badge tone={conversation.status === "running" ? "sky" : "amber"} title={statusTitle(conversation.status)}>
            {statusLabel(conversation.status)}
          </Badge>
          <span className="min-w-0 flex-1 truncate text-[12.5px] font-medium">
            {conversation.title || conversation.goal || conversation.id}
          </span>
        </div>
        {conversation.status_line ? (
          <div className="mt-0.5 truncate text-[11.5px] text-neutral-500">{conversation.status_line}</div>
        ) : null}
        <div className="mt-0.5 text-[11px] text-neutral-400">{relativeTime(conversation.updated_at)}</div>
      </button>
    </Line>
  );
}

export function NowBoard() {
  const approvals = useStore(
    (s) => Object.values(s.approvals).filter((a) => a.status === "pending"),
    (a, b) => a.length === b.length && a.every((x, i) => x === b[i]),
  );
  const items = useStore(
    (s) => Object.values(s.items).filter((i) => !i.dismissed_at),
    (a, b) => a.length === b.length && a.every((x, i) => x === b[i]),
  );
  const threads = useStore(
    (s) =>
      Object.values(s.conversations).filter(
        (c) => c.kind === "thread" && ["running", "waiting_approval", "waiting_input"].includes(c.status),
      ),
    (a, b) => a.length === b.length && a.every((x, i) => x === b[i]),
  );
  const maxThreads = useStore(
    (s) => Number(s.settings?.values["conversation.now_board_max_threads"] ?? 10),
    Object.is,
  );
  const [tick, setTick] = useState(0);

  useEffect(() => {
    void store.loadItems();
    void store.loadApprovals();
    const t = setInterval(() => setTick((n) => n + 1), 60000); // keep relative times honest
    return () => clearInterval(t);
  }, []);
  void tick;

  const setupItems = items.filter((i) => i.kind === "setup" && !i.seen_at);
  const queued = items.filter((i) => !i.seen_at && i.kind !== "setup");
  const seen = items.filter((i) => i.seen_at);
  const needsYou = approvals.length + setupItems.length;

  return (
    <aside className="h-full overflow-y-auto border-l border-neutral-200 bg-neutral-50 p-3 dark:border-neutral-800 dark:bg-neutral-900/50">
      <h2 className="mb-3 px-1 text-[13px] font-semibold">Now</h2>

      <Group title="Needs you" count={needsYou}>
        {needsYou === 0 ? (
          <Empty>Nothing waiting.</Empty>
        ) : (
          <>
            {approvals.map((a) => (
              <ApprovalLine key={a.id} approval={a} />
            ))}
            {setupItems.map((i) => (
              <ItemLine key={i.id} item={i} />
            ))}
          </>
        )}
      </Group>

      {/* Running, paused on an approval, or stopped without finishing: "running" was
          wrong for the last of those once the badge started saying "stopped" (2026-09-22). */}
      <Group title="Threads" count={threads.length}>
        {threads.length === 0 ? (
          <Empty>No active threads.</Empty>
        ) : (
          threads.slice(0, maxThreads).map((c) => <ThreadLine key={c.id} conversation={c} />)
        )}
      </Group>

      <Group title="Queued" count={queued.length}>
        {queued.length === 0 ? <Empty>Nothing queued.</Empty> : queued.map((i) => <ItemLine key={i.id} item={i} />)}
      </Group>

      {seen.length > 0 ? (
        <Group title="Seen" count={seen.length}>
          {seen.slice(0, 10).map((i) => (
            <ItemLine key={i.id} item={i} />
          ))}
        </Group>
      ) : null}
    </aside>
  );
}
