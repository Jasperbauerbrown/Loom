// Autocomplete popup for `[[` inside the note editor textarea.

import type { VaultSearchHit } from "../../lib/types";
import { cls } from "../../lib/format";

export function WikilinkPopup({
  hits,
  activeIndex,
  onPick,
}: {
  hits: VaultSearchHit[];
  activeIndex: number;
  onPick: (hit: VaultSearchHit) => void;
}) {
  if (hits.length === 0) {
    return (
      <div className="absolute z-20 mt-1 w-64 rounded-md border border-neutral-200 bg-white p-2 text-[12px] text-neutral-500 shadow-lg dark:border-neutral-700 dark:bg-neutral-900">
        No matches
      </div>
    );
  }
  return (
    <div className="absolute z-20 mt-1 max-h-56 w-72 overflow-y-auto rounded-md border border-neutral-200 bg-white py-1 shadow-lg dark:border-neutral-700 dark:bg-neutral-900">
      {hits.map((h, i) => (
        <button
          key={h.path}
          type="button"
          onMouseDown={(e) => {
            e.preventDefault();
            onPick(h);
          }}
          className={cls(
            "flex w-full flex-col items-start gap-0 px-2.5 py-1 text-left text-[12.5px]",
            i === activeIndex
              ? "bg-sky-100 dark:bg-sky-950"
              : "hover:bg-neutral-100 dark:hover:bg-neutral-800",
          )}
        >
          <span className="truncate font-medium text-neutral-800 dark:text-neutral-200">{h.title || h.path}</span>
          <span className="truncate text-[11px] text-neutral-500">{h.path}</span>
        </button>
      ))}
    </div>
  );
}
