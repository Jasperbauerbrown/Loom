// Conversation header: occupancy meter, queue indicator, the preset actually in use,
// and the connection dot. Shared by the desk and thread views.

import { useEffect } from "react";
import { store, useStore } from "../lib/store";
import { cls } from "../lib/format";
import { href } from "../lib/router";
import { Badge } from "./ui";

function OccupancyMeter({ id }: { id: string }) {
  const occupancy = useStore((s) => s.occupancy[id], Object.is);
  const lastEventId = useStore((s) => {
    const list = s.events[id];
    return list && list.length ? list[list.length - 1].id : 0;
  }, Object.is);
  const streaming = useStore((s) => Boolean(s.streaming[id]), Object.is);

  // Refetch when a turn's events settle (message.end appends the last event).
  useEffect(() => {
    if (!id || streaming) return;
    void store.loadOccupancy(id);
  }, [id, lastEventId, streaming]);

  if (!occupancy || !occupancy.limit_tokens) return null;
  const fraction = Math.max(0, Math.min(1, occupancy.occupancy_tokens / occupancy.limit_tokens));
  const tone = fraction > 0.9 ? "bg-red-500" : fraction > 0.75 ? "bg-amber-500" : "bg-emerald-500";
  return (
    <div
      className="flex items-center gap-2"
      title={`${occupancy.occupancy_tokens.toLocaleString()} / ${occupancy.limit_tokens.toLocaleString()} tokens — effective context ${occupancy.effective_ctx.toLocaleString()}, token factor ${occupancy.token_factor.toFixed(
        2,
      )}`}
    >
      <div className="h-1.5 w-28 overflow-hidden rounded-full bg-neutral-200 dark:bg-neutral-800">
        <div className={cls("h-full rounded-full transition-all", tone)} style={{ width: `${fraction * 100}%` }} />
      </div>
      <span className="text-[11.5px] tabular-nums text-neutral-500">
        {Math.round(fraction * 100)}% of {Math.round(occupancy.limit_tokens / 1000)}k
      </span>
    </div>
  );
}

export function ConversationHeader({ id, children }: { id: string; children?: React.ReactNode }) {
  const conversation = useStore((s) => s.conversations[id], Object.is);
  const roles = useStore((s) => s.roles, Object.is);
  const queue = useStore((s) => s.queue, Object.is);

  useEffect(() => {
    if (roles.length === 0) void store.loadModels();
  }, [roles.length]);

  const preset =
    conversation?.preset || roles.find((r) => r.role === conversation?.role)?.preset || conversation?.role || "";

  return (
    <header className="flex flex-wrap items-center gap-3 border-b border-neutral-200 px-4 py-2 dark:border-neutral-800">
      <div className="min-w-0">
        <div className="truncate text-[14px] font-semibold">
          {conversation?.kind === "desk" ? "Desk" : conversation?.title || conversation?.goal || id}
        </div>
        {conversation?.kind === "thread" && conversation.scope_note ? (
          <a
            href={href(`/vault/${conversation.scope_note}`)}
            className="text-[11.5px] text-sky-700 hover:underline dark:text-sky-400"
          >
            {conversation.scope_note}
          </a>
        ) : null}
      </div>
      <div className="flex-1" />
      {children}
      <OccupancyMeter id={id} />
      {queue.depth > 0 || (queue.running?.length ?? 0) > 0 ? (
        <Badge tone="sky" title="Model queue: running / queued">
          queue {queue.running?.length ?? 0}/{queue.depth}
        </Badge>
      ) : null}
      {preset ? (
        <Badge tone="neutral" title={`Model preset in use (role: ${conversation?.role ?? "?"})`}>
          {preset}
        </Badge>
      ) : null}
    </header>
  );
}
