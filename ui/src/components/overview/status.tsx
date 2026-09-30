// Small shared pieces of the Overview: a conversation's display title, its status dot,
// and the per-conversation pending-approval count.

import type { Conversation, ConversationStatus } from "../../lib/types";
import { cls, statusLabel, statusTitle } from "../../lib/format";
import { useStore } from "../../lib/store";

export function windowTitle(c: Conversation | undefined, id: string): string {
  if (!c) return id;
  return c.kind === "desk" ? "Desk" : c.title || c.goal || c.id;
}

const DOT: Record<string, string> = {
  running: "bg-sky-500",
  waiting_approval: "bg-amber-500",
  waiting_input: "bg-amber-500",
  done: "bg-emerald-500",
  failed: "bg-red-500",
};

export function StatusDot({ status, className }: { status: ConversationStatus | undefined; className?: string }) {
  const s = status ?? "idle";
  const waiting = s === "waiting_approval" || s === "waiting_input";
  return (
    <span
      title={`${statusLabel(s)} — ${statusTitle(s)}`}
      className={cls("relative inline-flex h-2 w-2 shrink-0 rounded-full", DOT[s] ?? "bg-neutral-400 dark:bg-neutral-600", className)}
    >
      {s === "running" || waiting ? (
        <span className={cls("absolute inset-0 animate-ping rounded-full opacity-60", DOT[s])} />
      ) : null}
    </span>
  );
}

/** Pending approvals for one conversation. */
export function usePendingCount(id: string): number {
  return useStore((s) => {
    let n = 0;
    for (const a of Object.values(s.approvals)) if (a.conversation_id === id && a.status === "pending") n++;
    return n;
  }, Object.is);
}
