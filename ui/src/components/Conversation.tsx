// The conversation component: the transcript, the live stream, tool rows, inline
// approval cards, the composer and the status line. Used by the desk and by threads —
// one component, no special cases (charter §2.1).

import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { api } from "../lib/api";
import { parseToolCalls, store, useStore } from "../lib/store";
import type { ConvEvent, ToolCall } from "../lib/types";
import { argsSummary, cls, localTime, truncate } from "../lib/format";
import { Badge, Button, Collapsible, JsonBlock, Spinner } from "./ui";
import { Markdown } from "./Markdown";
import { ArtifactDrawer } from "./ArtifactDrawer";

const PAGE = 100;

function policyTone(policy?: string) {
  if (policy === "allow") return "green" as const;
  if (policy === "ask") return "amber" as const;
  if (policy === "classify") return "violet" as const;
  return "neutral" as const;
}

function ToolRow({ call, onArtifact }: { call: ToolCall; onArtifact: (id: string) => void }) {
  const result = call.result;
  return (
    <div className="my-1.5 rounded-md border border-neutral-200 bg-neutral-50 px-2.5 py-1.5 dark:border-neutral-800 dark:bg-neutral-900/60">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-mono text-[12.5px] font-medium">{call.name}</span>
        {call.policy ? (
          <Badge tone={policyTone(call.policy)} title={call.reason || undefined}>
            {call.policy}
            {call.decision ? ` → ${call.decision}` : ""}
          </Badge>
        ) : null}
        {result ? (
          <Badge tone={result.ok ? "neutral" : "red"}>{result.ok ? "ok" : "failed"}</Badge>
        ) : (
          <span className="flex items-center gap-1 text-[11.5px] text-neutral-500">
            <Spinner /> running
          </span>
        )}
        <span className="min-w-0 flex-1 truncate font-mono text-[11.5px] text-neutral-500" title={argsSummary(call.args, 400)}>
          {argsSummary(call.args)}
        </span>
      </div>
      {call.args !== undefined ? (
        <Collapsible summary="arguments" className="mt-1">
          <JsonBlock value={call.args} />
        </Collapsible>
      ) : null}
      {result ? (
        <div className="mt-1">
          <pre className="max-h-48 overflow-auto rounded border border-neutral-200 bg-white p-2 font-mono text-[11.5px] leading-4 whitespace-pre-wrap dark:border-neutral-800 dark:bg-neutral-950">
            {truncate(result.preview ?? "", 4000)}
          </pre>
          {result.artifact_id ? (
            <button
              className="mt-1 text-[11.5px] text-sky-700 underline underline-offset-2 dark:text-sky-400"
              onClick={() => onArtifact(result.artifact_id!)}
            >
              open artifact {result.artifact_id}
            </button>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

function ApprovalCard({ id }: { id: string }) {
  const approval = useStore((s) => s.approvals[id], Object.is);
  const [addRoot, setAddRoot] = useState(true);
  const [busy, setBusy] = useState(false);
  if (!approval || approval.status !== "pending") return null;
  const root = approval.options?.add_root;
  const decide = async (decision: "allow" | "deny" | "allow_always") => {
    setBusy(true);
    await store.guard(() => api.decideApproval(approval.id, decision, root ? addRoot : undefined));
    setBusy(false);
  };
  return (
    <div className="my-2 rounded-lg border border-amber-300 bg-amber-50 p-3 dark:border-amber-900 dark:bg-amber-950/40">
      <div className="flex items-center gap-2">
        <Badge tone="amber">approval needed</Badge>
        <span className="font-mono text-[12.5px] font-medium">{approval.tool}</span>
      </div>
      {approval.reason ? (
        <p className="mt-1 text-[12px] text-neutral-700 dark:text-neutral-300">{approval.reason}</p>
      ) : null}
      <Collapsible summary="arguments" className="mt-1.5" defaultOpen>
        <JsonBlock value={approval.args} />
      </Collapsible>
      {root ? (
        <label className="mt-2 flex items-center gap-2 text-[12px]">
          <input type="checkbox" checked={addRoot} onChange={(e) => setAddRoot(e.target.checked)} />
          <span>
            also allow this root: <span className="font-mono">{String(root)}</span>
          </span>
        </label>
      ) : null}
      <div className="mt-2 flex gap-2">
        <Button size="sm" variant="primary" disabled={busy} onClick={() => void decide("allow")}>
          Allow
        </Button>
        <Button size="sm" disabled={busy} onClick={() => void decide("allow_always")}>
          Allow always
        </Button>
        <Button size="sm" variant="danger" disabled={busy} onClick={() => void decide("deny")}>
          Deny
        </Button>
      </div>
    </div>
  );
}

function EventView({
  event,
  toolCalls,
  onArtifact,
}: {
  event: ConvEvent;
  toolCalls: Record<string, ToolCall>;
  onArtifact: (id: string) => void;
}) {
  const meta = (event.meta ?? {}) as Record<string, any>;
  switch (event.kind) {
    case "user":
      return (
        <div className="my-3 flex justify-end">
          <div className="max-w-[80%] rounded-lg bg-sky-700 px-3 py-2 text-white dark:bg-sky-600">
            <Markdown source={event.content} className="md [&_a]:text-white [&_code]:bg-sky-800/60" />
          </div>
        </div>
      );
    case "assistant":
      return (
        <div className="my-3">
          {meta.reasoning ? (
            <Collapsible summary={`reasoning · ${String(meta.reasoning).length} chars`} className="mb-1">
              <div className="rounded-md border border-neutral-200 bg-neutral-50 p-2 text-[12px] whitespace-pre-wrap text-neutral-600 dark:border-neutral-800 dark:bg-neutral-900 dark:text-neutral-400">
                {String(meta.reasoning)}
              </div>
            </Collapsible>
          ) : null}
          <Markdown source={event.content} />
        </div>
      );
    case "thinking":
      return (
        <Collapsible summary={`reasoning · ${event.content.length} chars`} className="my-1.5">
          <div className="rounded-md border border-neutral-200 bg-neutral-50 p-2 text-[12px] whitespace-pre-wrap text-neutral-600 dark:border-neutral-800 dark:bg-neutral-900 dark:text-neutral-400">
            {event.content}
          </div>
        </Collapsible>
      );
    case "tool_call": {
      const specs = parseToolCalls(event);
      return (
        <>
          {specs.map((spec) => {
            const call: ToolCall = toolCalls[spec.id] ?? {
              call_id: spec.id,
              conversation_id: event.conversation_id,
              name: spec.name,
              args: spec.arguments,
              policy: meta.policy,
              reason: meta.reason,
              decision: meta.decision,
              ts: event.ts,
            };
            return <ToolRow key={spec.id} call={call} onArtifact={onArtifact} />;
          })}
        </>
      );
    }
    case "tool_result":
      // Results render inside their tool row; an orphan (no matching call) still shows.
      if (meta.call_id && toolCalls[meta.call_id]) return null;
      return (
        <pre className="my-1.5 max-h-48 overflow-auto rounded border border-neutral-200 bg-neutral-50 p-2 font-mono text-[11.5px] whitespace-pre-wrap dark:border-neutral-800 dark:bg-neutral-900">
          {event.content}
        </pre>
      );
    case "compaction":
      return (
        <div className="my-4 flex items-center gap-2 text-[11px] text-neutral-500">
          <span className="h-px flex-1 bg-neutral-200 dark:bg-neutral-800" />
          context compacted
          <span className="h-px flex-1 bg-neutral-200 dark:bg-neutral-800" />
        </div>
      );
    case "status":
      return (
        <div className="my-1.5 text-[11.5px] text-neutral-500" title={localTime(event.ts)}>
          status · {truncate(event.content, 300)}
        </div>
      );
    case "system":
    default:
      return (
        <div className="my-2 rounded-md border border-neutral-200 bg-neutral-50 px-2.5 py-1.5 text-[12px] text-neutral-600 dark:border-neutral-800 dark:bg-neutral-900 dark:text-neutral-400">
          {event.content}
        </div>
      );
  }
}

export function StatusLine({ status }: { status: string }) {
  const tone =
    status === "running"
      ? "sky"
      : status === "waiting_approval"
        ? "amber"
        : status === "failed"
          ? "red"
          : status === "done"
            ? "green"
            : "neutral";
  return (
    <Badge tone={tone as any}>
      {status === "running" ? (
        <span className="flex items-center gap-1">
          <Spinner /> running
        </span>
      ) : (
        status.replace("_", " ")
      )}
    </Badge>
  );
}

export function Conversation({ id }: { id: string }) {
  const events = useStore((s) => s.events[id] ?? EMPTY, Object.is);
  const streaming = useStore((s) => s.streaming[id], Object.is);
  const toolCalls = useStore((s) => s.toolCalls, Object.is);
  const conversation = useStore((s) => s.conversations[id], Object.is);
  const approvals = useStore(
    (s) =>
      Object.values(s.approvals)
        .filter((a) => a.conversation_id === id && a.status === "pending")
        .map((a) => a.id)
        .join(","),
    Object.is,
  );
  const historyComplete = useStore((s) => s.historyComplete[id] ?? false, Object.is);

  const [draft, setDraft] = useState("");
  const [artifact, setArtifact] = useState<string | null>(null);
  const [sending, setSending] = useState(false);
  const scroller = useRef<HTMLDivElement>(null);
  const atBottom = useRef(true);
  const composer = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    if (!id) return;
    void store.loadEvents(id, { limit: PAGE });
    void store.loadConversation(id);
  }, [id]);

  // Follow the stream unless the reader has scrolled up.
  useLayoutEffect(() => {
    const el = scroller.current;
    if (el && atBottom.current) el.scrollTop = el.scrollHeight;
  }, [events, streaming]);

  const onScroll = () => {
    const el = scroller.current;
    if (!el) return;
    atBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
    if (el.scrollTop < 40 && !historyComplete && events.length > 0) {
      const before = events[0].id;
      const previousHeight = el.scrollHeight;
      void store.loadEvents(id, { before, limit: PAGE }).then(() => {
        requestAnimationFrame(() => {
          if (scroller.current) scroller.current.scrollTop = scroller.current.scrollHeight - previousHeight;
        });
      });
    }
  };

  const approvalIds = useMemo(() => (approvals ? approvals.split(",") : []), [approvals]);
  const status = conversation?.status ?? "idle";
  const running = status === "running";

  const send = async () => {
    const content = draft.trim();
    if (!content || sending) return;
    setSending(true);
    const ok = await store.guard(() => api.sendMessage(id, content));
    setSending(false);
    if (ok !== undefined) {
      setDraft("");
      atBottom.current = true;
    }
  };

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div ref={scroller} onScroll={onScroll} className="min-h-0 flex-1 overflow-y-auto px-4 py-2">
        {!historyComplete && events.length > 0 ? (
          <div className="py-2 text-center text-[11.5px] text-neutral-500">scroll up for earlier events</div>
        ) : null}
        {events.length === 0 && !streaming ? (
          <div className="py-16 text-center text-[13px] text-neutral-500">
            Nothing here yet. Say something below.
          </div>
        ) : null}
        {events.map((event) => (
          <EventView key={event.id} event={event} toolCalls={toolCalls} onArtifact={setArtifact} />
        ))}

        {streaming ? (
          <div className="my-3">
            {streaming.reasoning ? (
              <Collapsible summary={`thinking… ${streaming.reasoning.length} chars`} className="mb-1">
                <div className="rounded-md border border-neutral-200 bg-neutral-50 p-2 text-[12px] whitespace-pre-wrap text-neutral-600 dark:border-neutral-800 dark:bg-neutral-900 dark:text-neutral-400">
                  {streaming.reasoning}
                </div>
              </Collapsible>
            ) : null}
            {streaming.content ? (
              <Markdown source={streaming.content} />
            ) : (
              <div className="flex items-center gap-1.5 text-[12px] text-neutral-500">
                <Spinner /> thinking…
              </div>
            )}
          </div>
        ) : null}

        {approvalIds.map((approvalId) => (
          <ApprovalCard key={approvalId} id={approvalId} />
        ))}
      </div>

      <div className="border-t border-neutral-200 px-4 py-2.5 dark:border-neutral-800">
        <div className="mb-1.5 flex items-center gap-2">
          <StatusLine status={status} />
          {conversation?.step_budget ? (
            <span className="text-[11.5px] text-neutral-500">step budget {conversation.step_budget}</span>
          ) : null}
          <div className="flex-1" />
          {running ? (
            <Button size="sm" variant="danger" onClick={() => void store.guard(() => api.cancel(id))}>
              Cancel
            </Button>
          ) : null}
        </div>
        <div className="flex items-end gap-2">
          <textarea
            ref={composer}
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                void send();
              }
            }}
            rows={Math.min(8, Math.max(2, draft.split("\n").length))}
            placeholder="Message Loom — Enter sends, Shift+Enter for a newline"
            className={cls(
              "w-full resize-none rounded-md border border-neutral-300 bg-white px-3 py-2 text-[13px] focus:border-sky-500 focus:outline-none focus:ring-1 focus:ring-sky-500 dark:border-neutral-700 dark:bg-neutral-900",
            )}
          />
          <Button variant="primary" onClick={() => void send()} disabled={!draft.trim() || sending}>
            Send
          </Button>
        </div>
      </div>

      {artifact ? <ArtifactDrawer id={artifact} onClose={() => setArtifact(null)} /> : null}
    </div>
  );
}

const EMPTY: ConvEvent[] = [];
