// Core block fill meters: how full each always-in-context core note is vs its cap.

import { useEffect, useState } from "react";
import type { CoreMeter } from "../../lib/types";
import { api } from "../../lib/api";
import { store } from "../../lib/store";
import { Empty, Spinner } from "../ui";
import { notePath } from "../../lib/router";
import { cls } from "../../lib/format";

function barColor(ratio: number): string {
  if (ratio >= 0.95) return "bg-red-500";
  if (ratio >= 0.75) return "bg-amber-500";
  return "bg-emerald-500";
}

export function CoreMeters() {
  const [meters, setMeters] = useState<CoreMeter[] | null>(null);

  useEffect(() => {
    (async () => {
      const rows = await store.guard(() => api.vaultCore());
      setMeters(rows ?? []);
    })();
  }, []);

  if (meters === null) return <Spinner />;
  if (meters.length === 0) return <Empty>No core notes.</Empty>;

  return (
    <div className="space-y-2">
      {meters.map((m) => {
        const ratio = m.cap > 0 ? Math.min(1, m.chars / m.cap) : 0;
        return (
          <div key={m.path}>
            <div className="mb-0.5 flex items-center justify-between text-[11.5px]">
              <a href={notePath(m.path)} className="truncate font-medium text-neutral-700 hover:underline dark:text-neutral-300">
                {m.path}
              </a>
              <span className="shrink-0 text-neutral-500">
                {m.chars.toLocaleString()} / {m.cap.toLocaleString()}
              </span>
            </div>
            <div className="h-1.5 overflow-hidden rounded-full bg-neutral-200 dark:bg-neutral-800">
              <div className={cls("h-full rounded-full", barColor(ratio))} style={{ width: `${ratio * 100}%` }} />
            </div>
          </div>
        );
      })}
    </div>
  );
}
