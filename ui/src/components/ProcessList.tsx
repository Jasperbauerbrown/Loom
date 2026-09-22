// Background processes (03 §9.6, 2026-09-22): the rows `shell(background=true)` writes,
// as the now board and a conversation's panel show them. One row view, one log drawer,
// one list; the board and the thread panel differ only in which rows they hand over.

import { useCallback, useEffect, useState } from "react";
import { api } from "../lib/api";
import { store, useStore } from "../lib/store";
import type { ProcessRow, ProcessStatus } from "../lib/types";
import { href, navigate } from "../lib/router";
import { bytes, cls, duration, relativeTime } from "../lib/format";
import { Badge, Button, Empty, Spinner } from "./ui";

export function processTone(row: ProcessRow): "neutral" | "green" | "amber" | "red" | "sky" {
  switch (row.status as ProcessStatus) {
    case "running":
    case "starting":
      return "sky";
    case "exited":
      return row.exit_code === 0 ? "green" : "amber";
    case "failed":
      return "red";
    case "lost":
      return "amber";
    default:
      return "neutral";
  }
}

export function processTitle(row: ProcessRow): string | undefined {
  if (row.status === "lost") return row.error || "was not running when Loom started; exit code unknown";
  if (row.status === "failed") return row.error || "could not start";
  if (row.status === "exited") return `exit code ${row.exit_code ?? "?"}`;
  if (row.status === "stopped") return row.error || "stopped";
  return row.command;
}

/** Newest first: the started_at stamp is a ULID-ordered ISO string. */
export function sortProcesses(rows: ProcessRow[]): ProcessRow[] {
  return rows.slice().sort((a, b) => (a.started_at < b.started_at ? 1 : a.started_at > b.started_at ? -1 : 0));
}

export function isLive(row: ProcessRow): boolean {
  return row.status === "running" || row.status === "starting";
}

function StopButton({ row }: { row: ProcessRow }) {
  const [confirm, setConfirm] = useState(false);
  const [busy, setBusy] = useState(false);
  if (!isLive(row)) return null;
  if (!confirm) {
    return (
      <Button size="sm" variant="danger" onClick={() => setConfirm(true)}>
        Stop
      </Button>
    );
  }
  return (
    <span className="inline-flex items-center gap-1">
      <span className="text-[11.5px] text-neutral-500">Stop pid {row.pid ?? "?"}?</span>
      <Button
        size="sm"
        variant="danger"
        disabled={busy}
        onClick={() => {
          setBusy(true);
          void store.guard(() => api.stopProcess(row.id)).finally(() => {
            setBusy(false);
            setConfirm(false);
          });
        }}
      >
        {busy ? <Spinner /> : "Confirm"}
      </Button>
      <Button size="sm" variant="ghost" disabled={busy} onClick={() => setConfirm(false)}>
        Keep
      </Button>
    </span>
  );
}

export function ProcessRowView({
  row,
  showOwner,
  onLogs,
}: {
  row: ProcessRow;
  showOwner?: boolean;
  onLogs: (id: string) => void;
}) {
  const owner = useStore((s) => s.conversations[row.conversation_id], Object.is);
  const live = isLive(row);
  const label = row.title || row.command;
  return (
    <div className="mb-1.5 rounded-md border border-neutral-200 bg-white px-2.5 py-2 dark:border-neutral-800 dark:bg-neutral-900">
      <div className="flex items-center gap-2">
        <Badge tone={processTone(row)} title={processTitle(row)}>
          {row.status}
        </Badge>
        <span className="min-w-0 flex-1 truncate text-[12.5px] font-medium" title={row.command}>
          {label}
        </span>
      </div>
      <div className="mt-0.5 flex flex-wrap gap-x-2 text-[11px] text-neutral-500">
        {row.pid ? <span className="font-mono">pid {row.pid}</span> : null}
        <span title={live ? "running for" : "ran for"}>
          {live ? "up " : ""}
          {duration(row.started_at, live ? null : row.ended_at)}
        </span>
        {!live && row.exit_code !== null && row.exit_code !== undefined ? (
          <span className="font-mono">exit {row.exit_code}</span>
        ) : null}
        {typeof row.log_bytes === "number" ? <span>{bytes(row.log_bytes)} log</span> : null}
        <span>{relativeTime(row.started_at)}</span>
        {showOwner ? (
          <a
            className="truncate text-sky-700 hover:underline dark:text-sky-400"
            href={href(`/c/${row.conversation_id}`)}
            onClick={(e) => {
              e.preventDefault();
              navigate(`/c/${row.conversation_id}`);
            }}
          >
            {owner?.kind === "desk" ? "desk" : owner?.title || owner?.goal || row.conversation_id}
          </a>
        ) : null}
      </div>
      <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
        <Button size="sm" onClick={() => onLogs(row.id)}>
          Logs
        </Button>
        <StopButton row={row} />
      </div>
    </div>
  );
}

/** A list of rows plus the drawer they share. `rows` come sorted by the caller. */
export function ProcessList({
  rows,
  showOwner,
  empty,
}: {
  rows: ProcessRow[];
  showOwner?: boolean;
  empty: string;
}) {
  const [logsFor, setLogsFor] = useState<string | null>(null);
  return (
    <>
      {rows.length === 0 ? <Empty>{empty}</Empty> : rows.map((r) => <ProcessRowView key={r.id} row={r} showOwner={showOwner} onLogs={setLogsFor} />)}
      {logsFor ? <ProcessLogDrawer id={logsFor} onClose={() => setLogsFor(null)} /> : null}
    </>
  );
}

// Asked-for page size; the backend clamps every window to its tool output cap, so the
// pager trusts the `offset`/`end` it gets back, never the size it asked for.
const PAGE = 8000;

/** The tail of a process log, with "Load earlier" paging backwards and Refresh
 *  re-reading the tail. Same shape as the artifact drawer (03 §4.4). */
export function ProcessLogDrawer({ id, onClose }: { id: string; onClose: () => void }) {
  const row = useStore((s) => s.processes[id], Object.is);
  const [text, setText] = useState("");
  const [start, setStart] = useState(0); // offset of the first char shown
  const [total, setTotal] = useState<number | null>(null);
  const [loading, setLoading] = useState(false);

  const loadTail = useCallback(async () => {
    setLoading(true);
    const page = await store.guard(() => api.processLogs(id, { limit: PAGE }));
    setLoading(false);
    if (!page) return;
    setText(page.text);
    setStart(page.offset);
    setTotal(page.total);
  }, [id]);

  const loadEarlier = useCallback(async () => {
    if (start <= 0) return;
    setLoading(true);
    let from = Math.max(0, start - PAGE);
    let page = await store.guard(() => api.processLogs(id, { offset: from, limit: start - from }));
    if (page && page.end !== start) {
      // Clamped: re-ask for the window that ends exactly where the shown text starts, so
      // the pages stay contiguous whatever the cap is.
      const got = Math.max(1, page.end - page.offset);
      from = Math.max(0, start - got);
      page = await store.guard(() => api.processLogs(id, { offset: from, limit: start - from }));
    }
    setLoading(false);
    if (!page || page.end !== start) return;
    setText((prev) => page.text + prev);
    setStart(page.offset);
    setTotal(page.total);
  }, [id, start]);

  useEffect(() => {
    setText("");
    setStart(0);
    setTotal(null);
    void loadTail();
  }, [id, loadTail]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  const earlier = start > 0;
  return (
    <div className="fixed inset-0 z-50 flex justify-end bg-black/30" onMouseDown={onClose}>
      <aside
        onMouseDown={(e) => e.stopPropagation()}
        className="flex h-full w-[min(760px,90vw)] flex-col border-l border-neutral-200 bg-white shadow-2xl dark:border-neutral-800 dark:bg-neutral-950"
      >
        <header className="flex items-center justify-between gap-3 border-b border-neutral-200 px-4 py-2.5 dark:border-neutral-800">
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              {row ? (
                <Badge tone={processTone(row)} title={processTitle(row)}>
                  {row.status}
                </Badge>
              ) : null}
              <span className="truncate text-[13px] font-semibold" title={row?.command}>
                {row?.title || row?.command || id}
              </span>
            </div>
            <div className="truncate text-[11.5px] text-neutral-500" title={row?.log_path}>
              {total !== null ? `${total.toLocaleString()} chars total` : ""}
              {total !== null && text ? ` — showing ${start.toLocaleString()}–${(start + text.length).toLocaleString()}` : ""}
              {row?.pid ? ` — pid ${row.pid}` : ""}
            </div>
          </div>
          <Button variant="ghost" size="sm" onClick={onClose} aria-label="Close">
            ✕
          </Button>
        </header>
        <pre className={cls("flex-1 overflow-auto p-4 font-mono text-[12px] leading-5 whitespace-pre-wrap", !text && "text-neutral-500")}>
          {text || (loading ? "" : "No output yet.")}
        </pre>
        <footer className="flex items-center gap-2 border-t border-neutral-200 px-4 py-2 dark:border-neutral-800">
          {loading ? <Spinner /> : null}
          <Button size="sm" onClick={() => void loadEarlier()} disabled={loading || !earlier}>
            Load earlier
          </Button>
          <Button size="sm" onClick={() => void loadTail()} disabled={loading}>
            Refresh
          </Button>
          {row ? <StopButton row={row} /> : null}
          {!earlier && total !== null ? <span className="text-[11.5px] text-neutral-500">Start of log.</span> : null}
        </footer>
      </aside>
    </div>
  );
}
