// Pane ordering for the Overview screen (03 §13.1): pure so it can be unit-tested
// without mounting anything. The desk first, then active threads (running,
// waiting_approval, waiting_input) newest `updated_at` first, then — only when asked —
// finished threads (done, failed) in the same order. Archived threads never appear.

import type { Conversation } from "./types";

const ACTIVE_STATUSES = new Set(["running", "waiting_approval", "waiting_input"]);
const FINISHED_STATUSES = new Set(["done", "failed"]);

function byUpdatedDesc(a: Conversation, b: Conversation): number {
  if (a.updated_at === b.updated_at) return 0;
  return a.updated_at < b.updated_at ? 1 : -1;
}

/** Ordered conversation ids for the Overview's panes. */
export function overviewIds(conversations: Conversation[], showFinished: boolean): string[] {
  const desk = conversations.find((c) => c.kind === "desk");
  const active = conversations.filter((c) => c.kind === "thread" && ACTIVE_STATUSES.has(c.status)).sort(byUpdatedDesc);
  const finished = showFinished
    ? conversations.filter((c) => c.kind === "thread" && FINISHED_STATUSES.has(c.status)).sort(byUpdatedDesc)
    : [];

  const ids: string[] = [];
  if (desk) ids.push(desk.id);
  for (const c of active) ids.push(c.id);
  for (const c of finished) ids.push(c.id);
  return ids;
}
