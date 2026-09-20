// Dangling wikilink targets: notes referenced but not yet created.

import { useEffect, useState } from "react";
import { api } from "../../lib/api";
import { store } from "../../lib/store";
import { Empty, Spinner } from "../ui";
import { notePath } from "../../lib/router";

export function WantedNotes() {
  const [wanted, setWanted] = useState<{ target: string; sources: string[] }[] | null>(null);

  useEffect(() => {
    (async () => {
      const rows = await store.guard(() => api.vaultWanted());
      setWanted(rows ?? []);
    })();
  }, []);

  if (wanted === null) return <Spinner />;
  if (wanted.length === 0) return <Empty>No dangling links.</Empty>;

  return (
    <ul className="space-y-1">
      {wanted.map((w) => (
        <li key={w.target} className="text-[12.5px]">
          <a href={notePath(w.target)} className="font-medium text-sky-700 hover:underline dark:text-sky-400">
            {w.target}
          </a>
          <span className="ml-1 text-[11px] text-neutral-500">from {w.sources.length} note{w.sources.length === 1 ? "" : "s"}</span>
        </li>
      ))}
    </ul>
  );
}
