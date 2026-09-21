// Fused vault search box with a type filter; results navigate to the note.

import { useState } from "react";
import type { VaultSearchHit } from "../../lib/types";
import { api } from "../../lib/api";
import { store } from "../../lib/store";
import { Input, Select, Spinner } from "../ui";
import { notePath } from "../../lib/router";

const TYPES = ["", "device", "project", "people", "connector", "course", "topic", "procedure", "log"];

export function VaultSearch() {
  const [q, setQ] = useState("");
  const [type, setType] = useState("");
  const [hits, setHits] = useState<VaultSearchHit[] | null>(null);
  const [loading, setLoading] = useState(false);

  async function run(query: string, t: string) {
    if (!query.trim()) {
      setHits(null);
      return;
    }
    setLoading(true);
    const rows = await store.guard(() => api.vaultSearch(query, t || undefined));
    setLoading(false);
    setHits(rows ?? []);
  }

  return (
    <div className="mb-2">
      <div className="flex gap-1.5">
        <Input
          className="min-w-0 flex-1"
          placeholder="Search vault…"
          value={q}
          onChange={(e) => {
            setQ(e.target.value);
            void run(e.target.value, type);
          }}
        />
        <Select
          className="w-28 shrink-0"
          value={type}
          onChange={(e) => {
            setType(e.target.value);
            void run(q, e.target.value);
          }}
        >
          {TYPES.map((t) => (
            <option key={t} value={t}>
              {t || "all"}
            </option>
          ))}
        </Select>
      </div>
      {loading ? (
        <div className="px-1 py-2">
          <Spinner />
        </div>
      ) : hits && hits.length > 0 ? (
        <div className="mt-1 max-h-72 overflow-y-auto rounded-md border border-neutral-200 dark:border-neutral-800">
          {hits.map((h) => (
            <a
              key={h.path}
              href={notePath(h.path)}
              className="block border-b border-neutral-100 px-2 py-1.5 text-[12.5px] last:border-0 hover:bg-neutral-50 dark:border-neutral-800/70 dark:hover:bg-neutral-900"
            >
              <div className="flex items-center justify-between gap-2">
                <span className="truncate font-medium text-neutral-800 dark:text-neutral-200">{h.title || h.path}</span>
                <span className="shrink-0 text-[10.5px] text-neutral-500">{h.type}</span>
              </div>
              {h.snippet ? <div className="truncate text-[11px] text-neutral-500">{h.snippet}</div> : null}
            </a>
          ))}
        </div>
      ) : hits && hits.length === 0 ? (
        <div className="px-1 py-2 text-[12px] text-neutral-500">No results.</div>
      ) : null}
    </div>
  );
}
