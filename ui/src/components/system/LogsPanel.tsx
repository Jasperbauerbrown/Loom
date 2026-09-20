// Logs tail: level filter, auto-refresh every 2s while mounted. Handles either
// LogLine[] or {lines: LogLine[]} from the endpoint.

import { useEffect, useRef, useState } from "react";
import type { LogLine } from "../../lib/types";
import { api } from "../../lib/api";
import { store } from "../../lib/store";
import { Empty, Select } from "../ui";
import { cls } from "../../lib/format";

const LEVELS = ["", "DEBUG", "INFO", "WARNING", "ERROR"];

function levelTone(level?: string): string {
  switch ((level ?? "").toUpperCase()) {
    case "ERROR":
      return "text-red-600 dark:text-red-400";
    case "WARNING":
      return "text-amber-600 dark:text-amber-400";
    case "DEBUG":
      return "text-neutral-400 dark:text-neutral-500";
    default:
      return "text-neutral-700 dark:text-neutral-300";
  }
}

function normalize(res: LogLine[] | { lines: LogLine[] } | undefined): LogLine[] {
  if (!res) return [];
  return Array.isArray(res) ? res : (res.lines ?? []);
}

export function LogsPanel() {
  const [level, setLevel] = useState("");
  const [lines, setLines] = useState<LogLine[]>([]);
  const boxRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let cancelled = false;
    async function tick() {
      const res = await store.guard(() => api.logs({ level: level || undefined, limit: 300 }));
      if (!cancelled) setLines(normalize(res));
    }
    void tick();
    const id = window.setInterval(tick, 2000);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, [level]);

  useEffect(() => {
    const el = boxRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [lines]);

  return (
    <div>
      <div className="mb-2 flex justify-end">
        <Select className="w-32" value={level} onChange={(e) => setLevel(e.target.value)}>
          {LEVELS.map((l) => (
            <option key={l} value={l}>
              {l || "all levels"}
            </option>
          ))}
        </Select>
      </div>
      {lines.length === 0 ? (
        <Empty>No log lines.</Empty>
      ) : (
        <div
          ref={boxRef}
          className="max-h-80 overflow-y-auto rounded-md border border-neutral-200 bg-neutral-50 p-2 font-mono text-[11.5px] leading-5 dark:border-neutral-800 dark:bg-neutral-950"
        >
          {lines.map((l, i) => (
            <div key={i} className={cls("whitespace-pre-wrap", levelTone(l.level))}>
              {l.ts ? `${l.ts} ` : ""}
              {l.level ? `[${l.level}] ` : ""}
              {l.logger ? `${l.logger}: ` : ""}
              {l.message ?? JSON.stringify(l)}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
