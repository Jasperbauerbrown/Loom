// The conversation component: the transcript, the live stream, tool rows, inline
// approval cards, the composer and the status line. Used by the desk and by threads —
// one component, no special cases (charter §2.1).

import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { api, imageUrl } from "../lib/api";
import { parseToolCalls, store, useStore } from "../lib/store";
import type { ApprovalDecision, ConvEvent, ImageRef, QueuedMessage, ToolCall } from "../lib/types";
import { argsSummary, cls, compactionLabel, localTime, statusLabel, statusTitle, truncate } from "../lib/format";
import { Badge, Button, Collapsible, JsonBlock, Spinner } from "./ui";
import { Markdown } from "./Markdown";
import { ArtifactDrawer } from "./ArtifactDrawer";
import { IconPaperclip } from "./Icons";

const PAGE = 100;

function policyTone(policy?: string) {
  if (policy === "allow") return "green" as const;
  if (policy === "ask") return "amber" as const;
  if (policy === "classify") return "violet" as const;
  return "neutral" as const;
}

/** `meta.images` as typed refs; anything malformed is simply no images. */
function imageRefs(meta: Record<string, unknown> | undefined): ImageRef[] {
  const raw = meta?.images;
  if (!Array.isArray(raw)) return [];
  return raw.filter((r): r is ImageRef => !!r && typeof (r as ImageRef).id === "string");
}

/** Thumbnails of stored images (03 §4.4); each opens the full image in a new tab. */
function ImageStrip({
  conversationId,
  images,
  size = 96,
  className,
  frame = "border-neutral-200 bg-neutral-100 dark:border-neutral-700 dark:bg-neutral-900",
}: {
  conversationId: string;
  images: ImageRef[];
  size?: number;
  className?: string;
  /** border + letterbox colours, so a strip inside a filled bubble can match it */
  frame?: string;
}) {
  if (images.length === 0) return null;
  return (
    <div className={cls("flex flex-wrap gap-1.5", className)}>
      {images.map((img) => {
        const url = imageUrl(conversationId, img.id);
        return (
          <a key={img.id} href={url} target="_blank" rel="noreferrer" title={`${img.mime} — open full size`} className="block">
            <img
              src={url}
              alt={img.mime}
              loading="lazy"
              style={{ height: size }}
              className={cls("min-w-12 max-w-[320px] rounded-md border object-contain", frame)}
            />
          </a>
        );
      })}
    </div>
  );
}

function ToolRow({ call, onArtifact, awaiting }: { call: ToolCall; onArtifact: (id: string) => void; awaiting?: boolean }) {
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
        ) : awaiting ? (
          <Badge tone="amber">awaiting approval</Badge>
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
          {result.images && result.images.length > 0 ? (
            <ImageStrip conversationId={call.conversation_id} images={result.images} className="mb-1.5" />
          ) : null}
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
  // Widening the roots is a deliberate, second click: unchecked by default (2026-09-21).
  const [addRoot, setAddRoot] = useState(false);
  const [busy, setBusy] = useState(false);
  if (!approval || approval.status !== "pending") return null;
  const root = approval.options?.add_root;
  const decide = async (decision: ApprovalDecision) => {
    setBusy(true);
    // The backend widens the roots only on allow_always; the other decisions ignore it.
    await store.guard(() =>
      api.decideApproval(approval.id, decision, decision === "allow_always" && root ? addRoot : undefined),
    );
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
            Also allow this root with &ldquo;Allow always&rdquo;: <span className="font-mono">{String(root)}</span>
          </span>
        </label>
      ) : null}
      <div className="mt-2 flex gap-2">
        <Button size="sm" variant="primary" disabled={busy} onClick={() => void decide("allow")}>
          Allow
        </Button>
        <Button
          size="sm"
          disabled={busy}
          title={`Run ${approval.tool} with exactly these arguments without asking again, in this conversation only.`}
          onClick={() => void decide("allow_conversation")}
        >
          Allow for this conversation
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

/** Where a message came from, for anything that was not typed by the person. */
function sourceLabel(source: string, meta: Record<string, unknown>): string {
  if (source.startsWith("conversation:")) return `from ${String(meta.kind ?? "thread")}`;
  if (source.startsWith("schedule:")) return "from schedule";
  if (source.startsWith("connector:")) return `from ${source.slice("connector:".length)}`;
  if (source.startsWith("process:")) return `from process${meta.title ? ` "${String(meta.title)}"` : ""}`;
  if (source === "system") return "from system";
  return "";
}

/** Caption for a queued row: where it came from and when the runner will render it. */
function queuedCaption(row: QueuedMessage, status: string): string {
  const source = row.source || "user";
  const origin = sourceLabel(source, (row.meta ?? {}) as Record<string, unknown>);
  const when =
    status === "running"
      ? "delivered at the next step"
      : status === "waiting_approval"
        ? "delivered when the approval is answered"
        : "";
  return [origin, "queued", when].filter(Boolean).join(" · ");
}

/** A message that is posted but not yet in the transcript: it looks like the user
 *  bubble it will become, drawn hollow so nobody mistakes it for a delivered one. */
function QueuedCard({ row, status }: { row: QueuedMessage; status: string }) {
  const images = imageRefs(row.meta as Record<string, unknown>);
  return (
    <div className="my-3 flex justify-end" title={localTime(row.created_at)}>
      <div className="max-w-[80%]">
        <div className="mb-0.5 text-right text-[10.5px] tracking-wide text-neutral-500 uppercase">
          {queuedCaption(row, status)}
        </div>
        <div className="rounded-lg border border-dashed border-sky-500/60 bg-sky-50/60 px-3 py-2 text-neutral-800 dark:bg-sky-950/30 dark:text-neutral-200">
          {row.content ? (
            <Markdown source={row.content} className="md" />
          ) : images.length === 0 ? (
            <span className="text-neutral-500">(message queued)</span>
          ) : null}
          <ImageStrip
            conversationId={row.conversation_id}
            images={images}
            className={cls("justify-end opacity-80", row.content && "mt-1.5")}
          />
        </div>
      </div>
    </div>
  );
}

function EventView({
  event,
  toolCalls,
  onArtifact,
  pendingCalls,
}: {
  event: ConvEvent;
  toolCalls: Record<string, ToolCall>;
  onArtifact: (id: string) => void;
  pendingCalls: Set<string>;
}) {
  const meta = (event.meta ?? {}) as Record<string, any>;
  switch (event.kind) {
    case "user": {
      // Rows from threads, schedules and connectors are user-role for the model but
      // were not typed by the person: same side, hollow tone, a caption saying so.
      const source = typeof meta.source === "string" ? meta.source : "user";
      const foreign = source !== "user";
      const images = imageRefs(meta);
      return (
        <div className="my-3 flex justify-end">
          <div className="min-w-0 max-w-[80%]">
            {foreign ? (
              <div className="mb-0.5 text-right text-[10.5px] tracking-wide text-neutral-500 uppercase">
                {sourceLabel(source, meta)}
              </div>
            ) : null}
            <div
              className={cls(
                "rounded-lg px-3 py-2 break-words",
                foreign
                  ? "bg-neutral-200 text-neutral-900 dark:bg-neutral-800 dark:text-neutral-100"
                  : "bg-sky-700 text-white dark:bg-sky-600",
              )}
            >
              {event.content ? (
                <Markdown source={event.content} className={cls("md", !foreign && "[&_a]:text-white [&_code]:bg-sky-800/60")} />
              ) : null}
              <ImageStrip
                conversationId={event.conversation_id}
                images={images}
                className={cls("justify-end", event.content && "mt-1.5")}
                frame={
                  foreign
                    ? "border-neutral-300 bg-neutral-100 dark:border-neutral-600 dark:bg-neutral-900"
                    : "border-white/40 bg-sky-800/40 dark:border-white/30 dark:bg-sky-700/40"
                }
              />
            </div>
          </div>
        </div>
      );
    }
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
      // The model's narration before its calls ("Two big jobs. Spinning up threads…")
      // is persisted as `meta.text` (runner.py). It streamed live and then vanished
      // once the row landed, so mid-turn updates were only ever in reasoning traces
      // (2026-09-22). Same bubble as an assistant message, above the tool rows.
      const preamble = typeof meta.text === "string" ? meta.text.trim() : "";
      return (
        <>
          {preamble ? (
            <div className="my-3" data-testid="tool-call-preamble">
              <Markdown source={preamble} />
            </div>
          ) : null}
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
            return <ToolRow key={spec.id} call={call} onArtifact={onArtifact} awaiting={pendingCalls.has(spec.id)} />;
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
          {compactionLabel(event.meta, event.content)}
          <span className="h-px flex-1 bg-neutral-200 dark:bg-neutral-800" />
        </div>
      );
    case "status":
      return (
        <div className="my-1.5 text-[11.5px] text-neutral-500" title={localTime(event.ts)}>
          status · {event.content ? truncate(event.content, 300) : "updated"}
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
      : status === "waiting_approval" || status === "waiting_input"
        ? "amber"
        : status === "failed"
          ? "red"
          : status === "done"
            ? "green"
            : "neutral";
  return (
    <Badge tone={tone as any} title={statusTitle(status)}>
      {status === "running" ? (
        <span className="flex items-center gap-1">
          <Spinner /> running
        </span>
      ) : (
        statusLabel(status)
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
  const queued = useStore((s) => s.queued[id] ?? EMPTY_QUEUED, Object.is);
  const connection = useStore((s) => s.connection, Object.is);
  // The draft lives in the store (per conversation, persisted per tab), so leaving for
  // the vault and coming back, or switching threads, never loses what was typed.
  const draft = useStore((s) => s.drafts[id] ?? "", Object.is);
  const setDraft = (text: string) => store.setDraft(id, text);

  const [artifact, setArtifact] = useState<string | null>(null);
  const [sending, setSending] = useState(false);
  const scroller = useRef<HTMLDivElement>(null);
  const atBottom = useRef(true);
  const composer = useRef<HTMLTextAreaElement>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  // Image attachments: uploaded eagerly so a send carries only ids (03 §12.1). Kept per
  // conversation in component state, not in the persisted draft — a stored image with
  // no message is a bounded orphan that dies with the conversation's artifacts.
  const [attachmentsById, setAttachmentsById] = useState<Record<string, Attachment[]>>({});
  const attachments = attachmentsById[id] ?? EMPTY_ATTACHMENTS;
  const attachSeq = useRef(0);

  const updateAttachments = (fn: (list: Attachment[]) => Attachment[]) =>
    setAttachmentsById((all) => ({ ...all, [id]: fn(all[id] ?? []) }));

  const attachFiles = (files: File[]) => {
    const images = files.filter((f) => f.type.startsWith("image/"));
    if (images.length === 0) return;
    for (const file of images) {
      const key = `a${++attachSeq.current}`;
      updateAttachments((list) => [...list, { key, name: file.name, status: "uploading" }]);
      void (async () => {
        try {
          const res = await api.uploadImage(id, file);
          updateAttachments((list) =>
            list.map((a) => (a.key === key ? { ...a, status: "ready", id: res.id, mime: res.mime } : a)),
          );
        } catch (e) {
          store.error(e);
          updateAttachments((list) => list.filter((a) => a.key !== key));
        }
      })();
    }
  };
  const removeAttachment = (key: string) => updateAttachments((list) => list.filter((a) => a.key !== key));
  const uploading = attachments.some((a) => a.status === "uploading");
  const readyImages = attachments.filter((a) => a.status === "ready" && a.id).map((a) => a.id!);

  useEffect(() => {
    if (!id) return;
    void store.loadEvents(id, { limit: PAGE });
    void store.loadConversation(id);
  }, [id]);

  // Queued rows: on open and after every (re)connect, since a dropped socket may have
  // missed `inbox.posted` or the delivery that removed one.
  useEffect(() => {
    if (!id || connection !== "open") return;
    void store.loadQueued(id);
  }, [id, connection]);

  // Follow the stream unless the reader has scrolled up.
  useLayoutEffect(() => {
    const el = scroller.current;
    if (el && atBottom.current) el.scrollTop = el.scrollHeight;
  }, [events, streaming, queued, approvals]);

  // Content also grows without a store change: an approval card loading its detail,
  // an image decoding. Follow any height change, not just new rows.
  const content = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const inner = content.current;
    if (!inner || typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(() => {
      const el = scroller.current;
      if (el && atBottom.current) el.scrollTop = el.scrollHeight;
    });
    ro.observe(inner);
    return () => ro.disconnect();
  }, []);

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
  const pendingCallKey = useStore(
    (s) =>
      Object.values(s.approvals)
        .filter((a) => a.conversation_id === id && a.status === "pending")
        .map((a) => a.call_id)
        .join(","),
    Object.is,
  );
  const pendingCalls = useMemo(() => new Set(pendingCallKey ? pendingCallKey.split(",") : []), [pendingCallKey]);
  const status = conversation?.status ?? "idle";
  const running = status === "running";

  const canSend = (draft.trim().length > 0 || readyImages.length > 0) && !uploading && !sending;

  const send = async () => {
    const content = draft.trim();
    if (!canSend) return;
    setSending(true);
    const ok = await store.guard(() => api.sendMessage(id, content, readyImages));
    setSending(false);
    if (ok !== undefined) {
      setDraft("");
      updateAttachments(() => []);
      atBottom.current = true;
    }
  };

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div ref={scroller} onScroll={onScroll} className="min-h-0 flex-1 overflow-y-auto px-4 py-2">
        <div ref={content}>
        {!historyComplete && events.length > 0 ? (
          <div className="py-2 text-center text-[11.5px] text-neutral-500">scroll up for earlier events</div>
        ) : null}
        {events.length === 0 && !streaming ? (
          <div className="py-16 text-center text-[13px] text-neutral-500">
            Nothing here yet. Say something below.
          </div>
        ) : null}
        {events.map((event) => (
          <EventView key={event.id} event={event} toolCalls={toolCalls} onArtifact={setArtifact} pendingCalls={pendingCalls} />
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

        {queued.map((row) => (
          <QueuedCard key={row.id} row={row} status={status} />
        ))}

        {approvalIds.map((approvalId) => (
          <ApprovalCard key={approvalId} id={approvalId} />
        ))}
        </div>
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
        {attachments.length > 0 ? (
          <div className="mb-1.5 flex flex-wrap gap-1.5">
            {attachments.map((a) => (
              <div
                key={a.key}
                className="relative flex h-12 items-center gap-1.5 rounded-md border border-neutral-300 bg-neutral-50 px-1.5 dark:border-neutral-700 dark:bg-neutral-900"
                title={a.name}
              >
                {a.status === "ready" && a.id ? (
                  <img src={imageUrl(id, a.id)} alt={a.name} className="h-9 max-w-[120px] rounded object-contain" />
                ) : (
                  <span className="flex items-center gap-1 px-1 text-[11.5px] text-neutral-500">
                    <Spinner /> uploading…
                  </span>
                )}
                <button
                  type="button"
                  aria-label={`Remove ${a.name}`}
                  onClick={() => removeAttachment(a.key)}
                  className="rounded px-1 text-[13px] leading-none text-neutral-500 hover:bg-neutral-200 hover:text-neutral-900 dark:hover:bg-neutral-700 dark:hover:text-neutral-100"
                >
                  ×
                </button>
              </div>
            ))}
          </div>
        ) : null}
        <div
          className="flex items-end gap-2"
          onDragOver={(e) => {
            if (Array.from(e.dataTransfer.types).includes("Files")) e.preventDefault();
          }}
          onDrop={(e) => {
            const files = Array.from(e.dataTransfer.files ?? []);
            if (files.length === 0) return;
            e.preventDefault();
            attachFiles(files);
          }}
        >
          <input
            ref={fileInput}
            type="file"
            accept="image/*"
            multiple
            className="hidden"
            onChange={(e) => {
              if (e.target.files) attachFiles(Array.from(e.target.files));
              e.target.value = ""; // re-attaching the same file must fire again
            }}
          />
          <Button
            variant="ghost"
            className="shrink-0"
            title="Attach an image (or paste / drop one)"
            aria-label="Attach image"
            onClick={() => fileInput.current?.click()}
          >
            <IconPaperclip />
          </Button>
          <textarea
            ref={composer}
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onPaste={(e) => {
              const files = Array.from(e.clipboardData?.items ?? [])
                .filter((it) => it.kind === "file" && it.type.startsWith("image/"))
                .map((it) => it.getAsFile())
                .filter((f): f is File => f !== null);
              if (files.length === 0) return;
              e.preventDefault();
              attachFiles(files);
            }}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                void send();
              }
            }}
            rows={Math.min(8, Math.max(2, draft.split("\n").length))}
            placeholder={
              running
                ? "Message Loom — queued until the next step (Enter sends)"
                : "Message Loom — Enter sends, Shift+Enter for a newline"
            }
            className={cls(
              "w-full resize-none rounded-md border border-neutral-300 bg-white px-3 py-2 text-[13px] focus:border-sky-500 focus:outline-none focus:ring-1 focus:ring-sky-500 dark:border-neutral-700 dark:bg-neutral-900",
            )}
          />
          <Button variant="primary" onClick={() => void send()} disabled={!canSend}>
            Send
          </Button>
        </div>
      </div>

      {artifact ? <ArtifactDrawer id={artifact} onClose={() => setArtifact(null)} /> : null}
    </div>
  );
}

interface Attachment {
  key: string;
  name: string;
  status: "uploading" | "ready";
  id?: string;
  mime?: string;
}

const EMPTY: ConvEvent[] = [];
const EMPTY_QUEUED: QueuedMessage[] = [];
const EMPTY_ATTACHMENTS: Attachment[] = [];
