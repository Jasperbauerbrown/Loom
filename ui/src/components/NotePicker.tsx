// Note picker: a list of vault paths with a search box to add more. Used for a thread's
// pinned notes (03 §6.7): in the new-thread dialog (sent as `notes`) and in the thread
// panel (PATCH `pinned_notes`). Any note type qualifies — a project, a device, a
// procedure; Loom does not care what is pinned.

import { useEffect, useState } from "react";
import { api } from "../lib/api";
import { store } from "../lib/store";
import type { VaultSearchHit } from "../lib/types";
import { notePath } from "../lib/router";
import { normaliseNotePath } from "../lib/format";
import { Input } from "./ui";

export function NotePicker({
  value,
  onChange,
  disabled,
  placeholder = "Search notes, or type a vault path…",
}: {
  value: string[];
  onChange: (next: string[]) => void;
  disabled?: boolean;
  placeholder?: string;
}) {
  const [query, setQuery] = useState("");
  const [hits, setHits] = useState<VaultSearchHit[]>([]);

  useEffect(() => {
    if (!query.trim()) {
      setHits([]);
      return;
    }
    let cancelled = false;
    const t = setTimeout(async () => {
      const rows = await store.guard(() => api.vaultSearch(query));
      if (!cancelled) setHits((rows ?? []).filter((h) => !value.includes(h.path)).slice(0, 8));
    }, 200);
    return () => {
      cancelled = true;
      clearTimeout(t);
    };
  }, [query, value]);

  function add(path: string) {
    const p = normaliseNotePath(path);
    if (!p || value.includes(p)) return;
    onChange([...value, p]);
    setQuery("");
    setHits([]);
  }

  return (
    <div>
      {value.length ? (
        <ul className="mb-1.5 space-y-1">
          {value.map((p) => (
            <li
              key={p}
              className="flex items-center justify-between gap-2 rounded bg-neutral-100 px-2 py-1 text-[12px] dark:bg-neutral-800"
            >
              <a className="truncate text-sky-700 hover:underline dark:text-sky-400" href={notePath(p)}>
                {p}
              </a>
              <button
                type="button"
                className="shrink-0 text-neutral-500 hover:text-red-600 disabled:opacity-50"
                disabled={disabled}
                title="Unpin"
                onClick={() => onChange(value.filter((x) => x !== p))}
              >
                Unpin
              </button>
            </li>
          ))}
        </ul>
      ) : null}
      <Input
        value={query}
        disabled={disabled}
        placeholder={placeholder}
        onChange={(e) => setQuery(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter") {
            e.preventDefault();
            if (hits.length === 1) add(hits[0].path);
            else add(query);
          }
        }}
      />
      {hits.length ? (
        <div className="mt-1 max-h-40 overflow-y-auto rounded-md border border-neutral-200 dark:border-neutral-800">
          {hits.map((h) => (
            <button
              key={h.path}
              type="button"
              className="block w-full truncate px-2 py-1 text-left text-[12.5px] hover:bg-neutral-100 dark:hover:bg-neutral-800"
              onClick={() => add(h.path)}
            >
              {h.title || h.path} <span className="text-neutral-500">{h.path}</span>
            </button>
          ))}
        </div>
      ) : null}
    </div>
  );
}
