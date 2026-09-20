// Maintenance (03 §8.6, §13.1): what the nightly/idle pass is doing, what the last one
// did, and the button that starts one out of band. Status comes from `/api/maintenance`;
// the live line is the `maintenance.progress` stream the store collects.

import { useEffect, useState } from "react";
import { api } from "../../lib/api";
import { store, useStore } from "../../lib/store";
import { Badge, Button, Empty, Table, Td, Tr } from "../ui";
import { localTime, relativeTime } from "../../lib/format";

function duration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return "—";
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  return s % 60 ? `${m}m ${s % 60}s` : `${m}m`;
}

export function MaintenancePanel() {
  const status = useStore((s) => s.maintenance);
  const stale = useStore((s) => s.maintenanceStale);
  const progress = useStore((s) => s.maintenanceProgress);
  const [starting, setStarting] = useState(false);

  useEffect(() => {
    void store.loadMaintenance();
  }, []);

  // A pass that just finished rewrote the report; a running one moves the countdown.
  useEffect(() => {
    if (stale) void store.loadMaintenance();
  }, [stale]);

  useEffect(() => {
    const timer = window.setInterval(() => void store.loadMaintenance(), 30_000);
    return () => window.clearInterval(timer);
  }, []);

  async function run() {
    setStarting(true);
    const res = await store.guard(() => api.runMaintenance());
    setStarting(false);
    if (res !== undefined) store.toast("info", "Maintenance pass started");
    await store.loadMaintenance();
  }

  const running = Boolean(status?.running);
  const report = status?.last_report ?? null;
  const last = progress.length ? progress[progress.length - 1] : null;

  return (
    <div>
      <div className="mb-3 flex flex-wrap items-center gap-x-4 gap-y-2 text-[12.5px]">
        <span className="flex items-center gap-1.5">
          Pass
          <Badge tone={running ? "amber" : status?.enabled === false ? "neutral" : "green"}>
            {running ? `running${status?.reason ? ` (${status.reason})` : ""}` : status?.enabled === false ? "disabled" : "idle"}
          </Badge>
        </span>
        <span>
          Next nightly{" "}
          <span className="font-mono" title={status?.next_nightly_at ?? undefined}>
            {status ? relativeTime(status.next_nightly_at) : "—"}
          </span>
        </span>
        <span>
          Idle window left{" "}
          <span className="font-mono">{status ? duration(status.idle_seconds_remaining) : "—"}</span>
          {status ? <span className="text-neutral-500"> of {duration(status.idle_seconds)}</span> : null}
        </span>
        <Button size="sm" variant="primary" className="ml-auto" disabled={running || starting} onClick={run}>
          {running ? "Running…" : "Run maintenance now"}
        </Button>
      </div>

      {running || progress.length > 0 ? (
        <div className="mb-3 rounded-md border border-neutral-200 bg-neutral-50 px-3 py-2 text-[12px] dark:border-neutral-800 dark:bg-neutral-950">
          <span className="font-medium text-neutral-700 dark:text-neutral-300">{last?.stage ?? "—"}</span>{" "}
          <span className="text-neutral-500">{last?.message ?? ""}</span>
          {progress.length > 1 ? (
            <ol className="mt-1 max-h-32 space-y-0.5 overflow-y-auto">
              {progress.slice(0, -1).map((p, i) => (
                <li key={i} className="text-neutral-500">
                  <span className="font-mono text-[11px]">{p.stage}</span> {p.message}
                </li>
              ))}
            </ol>
          ) : null}
        </div>
      ) : null}

      <h3 className="mb-1 text-[12px] font-semibold text-neutral-600 dark:text-neutral-400">Last pass</h3>
      {!report ? (
        <Empty>No maintenance pass has run yet.</Empty>
      ) : (
        <>
          <Table head={["", ""]}>
            <Tr>
              <Td className="w-48 text-neutral-500">Started</Td>
              <Td>{localTime(report.started_at)}</Td>
            </Tr>
            <Tr>
              <Td className="text-neutral-500">Finished</Td>
              <Td>{report.finished_at ? localTime(report.finished_at) : "—"}</Td>
            </Tr>
            <Tr>
              <Td className="text-neutral-500">Reason</Td>
              <Td>
                <Badge>{report.reason}</Badge>
              </Td>
            </Tr>
            <Tr>
              <Td className="text-neutral-500">Consolidated</Td>
              <Td>
                {report.consolidated.length} conversation(s),{" "}
                {report.consolidated.reduce((n, c) => n + c.writes, 0)} note write(s) from{" "}
                {report.consolidated.reduce((n, c) => n + c.events, 0)} event(s)
              </Td>
            </Tr>
            <Tr>
              <Td className="text-neutral-500">Curated</Td>
              <Td>
                {report.curated.applied} applied of {report.curated.candidates} candidate(s)
              </Td>
            </Tr>
            <Tr>
              <Td className="text-neutral-500">Archived threads</Td>
              <Td>{report.archived}</Td>
            </Tr>
            <Tr>
              <Td className="text-neutral-500">Pruned artifacts</Td>
              <Td>{report.pruned_artifacts}</Td>
            </Tr>
            <Tr>
              <Td className="text-neutral-500">Commit</Td>
              <Td className="font-mono text-[11.5px]">
                {report.committed ?? "—"}
                {report.external_commit ? ` (external ${report.external_commit})` : ""}
              </Td>
            </Tr>
          </Table>
          {report.consolidated.length > 0 ? (
            <Table head={["Conversation", "Events", "Writes"]}>
              {report.consolidated.map((c) => (
                <Tr key={c.conversation_id}>
                  <Td className="font-mono text-[11.5px]">{c.conversation_id}</Td>
                  <Td>{c.events}</Td>
                  <Td>{c.writes}</Td>
                </Tr>
              ))}
            </Table>
          ) : null}
          {report.errors.length > 0 ? (
            <ul className="mt-2 space-y-0.5 text-[12px] text-red-600 dark:text-red-400">
              {report.errors.map((e, i) => (
                <li key={i}>{e}</li>
              ))}
            </ul>
          ) : null}
        </>
      )}
    </div>
  );
}
