// Conversation header: occupancy meter, queue indicator, the preset actually in use,
// and the connection dot. Shared by the desk and thread views.

import { useEffect, useRef, useState } from "react";
import { api } from "../lib/api";
import { store, useStore } from "../lib/store";
import { cls, compactNumber } from "../lib/format";
import { notePath } from "../lib/router";
import { Badge, Button, Confirm, Toggle } from "./ui";

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

/** Token totals for the whole conversation (03 §12.1 GET …/usage): compact numbers here,
 *  exact totals and the last turn's in/out/steps in the tooltip. Fetched on mount (and on
 *  switching conversations); refetched, lightly debounced, when this conversation's event
 *  count changes — so a streaming turn does not hammer the endpoint step by step. */
function UsageBadge({ id }: { id: string }) {
  const usage = useStore((s) => s.usage[id], Object.is);
  const eventCount = useStore((s) => s.events[id]?.length ?? 0, Object.is);
  const seenId = useRef<string | null>(null);

  useEffect(() => {
    if (!id) return;
    if (seenId.current !== id) {
      seenId.current = id;
      void store.loadUsage(id);
      return;
    }
    const t = setTimeout(() => void store.loadUsage(id), 600);
    return () => clearTimeout(t);
  }, [id, eventCount]);

  if (!usage || (!usage.prompt_tokens && !usage.completion_tokens)) return null;
  const last = usage.turns[usage.turns.length - 1];
  const title = [
    `${usage.prompt_tokens.toLocaleString()} prompt / ${usage.completion_tokens.toLocaleString()} completion tokens over ${usage.steps} step${usage.steps === 1 ? "" : "s"}`,
    last
      ? `last turn: in ${last.prompt_tokens.toLocaleString()} · out ${last.completion_tokens.toLocaleString()} (${last.steps} step${last.steps === 1 ? "" : "s"})`
      : null,
  ]
    .filter(Boolean)
    .join(" — ");
  return (
    <Badge tone="neutral" title={title}>
      tokens in {compactNumber(usage.prompt_tokens)} · out {compactNumber(usage.completion_tokens)}
    </Badge>
  );
}

/** The desk's provider-reported usage since its latest manual Clear context action.
 *  The original badge remains the all-time conversation total. */
function DeskUsageBadge({ id }: { id: string }) {
  const usage = useStore((s) => s.usage[id], Object.is);
  if (!usage) return null;
  const { prompt_tokens: input, completion_tokens: output, steps } = usage.since_clear;
  return (
    <Badge
      tone="sky"
      title={`${input.toLocaleString()} prompt / ${output.toLocaleString()} completion tokens since the latest manual Clear context marker (${steps} model steps)`}
    >
      since clear in {compactNumber(input)} · out {compactNumber(output)}
    </Badge>
  );
}

export function ConversationHeader({ id, children }: { id: string; children?: React.ReactNode }) {
  const conversation = useStore((s) => s.conversations[id], Object.is);
  const roles = useStore((s) => s.roles, Object.is);
  const queue = useStore((s) => s.queue, Object.is);
  const [confirmClear, setConfirmClear] = useState(false);
  const showFull = useStore((s) => s.showFullTranscript, Object.is);
  const busy = conversation?.status === "running" || conversation?.status === "waiting_approval";

  async function clearContext() {
    // 409 (a turn started in the meantime) arrives as its `detail` in a toast.
    const row = await store.guard(() => api.clearConversation(id));
    if (!row) return;
    store.toast("info", "Context cleared", "The transcript stays below the divider.");
    void store.loadOccupancy(id);
  }

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
        {conversation?.pinned_notes?.length ? (
          <div className="flex flex-wrap gap-1" title="Pinned notes: in the model's context every turn">
            {conversation.pinned_notes.map((p) => (
              <a
                key={p}
                href={notePath(p)}
                title={p}
                className="max-w-[22rem] truncate rounded bg-neutral-100 px-1.5 py-0.5 text-[11px] text-sky-700 hover:underline dark:bg-neutral-800 dark:text-sky-400"
              >
                {p}
              </a>
            ))}
          </div>
        ) : null}
      </div>
      <div className="flex-1" />
      {children}
      <label
        className="flex items-center gap-1.5 text-[12px] text-neutral-600 dark:text-neutral-400"
        title="Toggle between the transcript since the last Clear context and the full history."
      >
        <Toggle checked={showFull} onChange={(v) => store.setShowFullTranscript(v)} label="Show full transcript" />
        <span>Show full transcript</span>
      </label>
      <Button
        size="sm"
        onClick={() => setConfirmClear(true)}
        disabled={busy}
        title={
          busy
            ? `The conversation is ${conversation?.status}; cancel or answer it first.`
            : "Start the model's context fresh; the transcript stays."
        }
      >
        Clear context
      </Button>
      {confirmClear ? (
        <Confirm
          title="Clear context"
          body="Start fresh? Everything before now stays in the transcript below a divider and stays searchable, but the model will no longer see it."
          confirmLabel="Clear context"
          danger
          onConfirm={() => void clearContext()}
          onClose={() => setConfirmClear(false)}
        />
      ) : null}
      <OccupancyMeter id={id} />
      <UsageBadge id={id} />
      {conversation?.kind === "desk" ? <DeskUsageBadge id={id} /> : null}
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
