// Search-to-centre for the Graph toolbar: fused vault search, arrow keys to move,
// Enter to pick, Escape to close.

import { useEffect, useState } from "react";
import type { VaultSearchHit } from "../../../lib/types";
import { api } from "../../../lib/api";
import { store } from "../../../lib/store";
import { cls } from "../../../lib/format";
import { Input } from "../../ui";
import { HUE_SWATCH, hueOf } from "./style";

export function CenterPicker({ onPick }: { onPick: (path: string) => void }) {
  const [query, setQuery] = useState("");
  const [hits, setHits] = useState<VaultSearchHit[]>([]);
  const [cursor, setCursor] = useState(0);

  useEffect(() => {
    if (!query.trim()) {
      setHits([]);
      return;
    }
    let cancelled = false;
    const t = setTimeout(async () => {
      const rows = await store.guard(() => api.vaultSearch(query));
      if (!cancelled) {
        setHits((rows ?? []).slice(0, 8));
        setCursor(0);
      }
    }, 180);
    return () => {
      cancelled = true;
      clearTimeout(t);
    };
  }, [query]);

  function pick(path: string) {
    onPick(path);
    setQuery("");
    setHits([]);
    (document.activeElement as HTMLElement | null)?.blur();
  }

  return (
    <div className="relative">
      <svg
        viewBox="0 0 16 16"
        className="pointer-events-none absolute left-2 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-neutral-400"
        fill="none"
        stroke="currentColor"
        strokeWidth={1.6}
        strokeLinecap="round"
        aria-hidden="true"
      >
        <circle cx="7" cy="7" r="4.5" />
        <path d="M10.5 10.5 14 14" />
      </svg>
      <Input
        value={query}
        placeholder="Centre on a note…"
        className="w-56 pl-7"
        aria-label="Centre the graph on a note"
        onChange={(e) => setQuery(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "ArrowDown" && hits.length) {
            e.preventDefault();
            setCursor((c) => (c + 1) % hits.length);
          } else if (e.key === "ArrowUp" && hits.length) {
            e.preventDefault();
            setCursor((c) => (c - 1 + hits.length) % hits.length);
          } else if (e.key === "Enter" && hits.length) {
            pick(hits[Math.min(cursor, hits.length - 1)].path);
          } else if (e.key === "Escape") {
            setQuery("");
            setHits([]);
          }
        }}
      />
      {hits.length ? (
        <div className="absolute z-20 mt-1 w-80 overflow-hidden rounded-md border border-neutral-200 bg-white py-1 text-left shadow-lg dark:border-neutral-800 dark:bg-neutral-900">
          {hits.map((h, i) => (
            <button
              key={h.path}
              type="button"
              className={cls(
                "flex w-full items-center gap-2 px-2.5 py-1.5 text-left text-[12.5px]",
                i === cursor ? "bg-neutral-100 dark:bg-neutral-800" : "hover:bg-neutral-50 dark:hover:bg-neutral-800/60",
              )}
              onMouseEnter={() => setCursor(i)}
              onClick={() => pick(h.path)}
            >
              <span className={cls("h-2 w-2 shrink-0 rounded-full", HUE_SWATCH[hueOf(h.type)])} />
              <span className="truncate text-neutral-800 dark:text-neutral-200">{h.title || h.path}</span>
              <span className="ml-auto shrink-0 truncate font-mono text-[11px] text-neutral-400">{h.path}</span>
            </button>
          ))}
        </div>
      ) : null}
    </div>
  );
}
