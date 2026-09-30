// The Overview's "Windows" menu: every non-archived conversation, grouped as open,
// hidden (closed by you while active) and the rest (finished, idle), each a toggle —
// open it as a window, or close its window. The one place windows are added or
// brought back.

import { useMemo, useState } from "react";
import type { Conversation } from "../../lib/types";
import { relativeTime } from "../../lib/format";
import { IconWindows, IconChevronDown } from "../Icons";
import { Popover, MenuItem, MenuLabel, MenuSeparator } from "./Popover";
import { StatusDot, windowTitle } from "./status";

export function WindowsMenu({
  conversations,
  shown,
  hidden,
  onOpen,
  onClose,
  onShowHidden,
  open,
  onOpenChange,
}: {
  conversations: Conversation[];
  shown: string[];
  hidden: string[];
  onOpen: (id: string) => void;
  onClose: (id: string) => void;
  onShowHidden: () => void;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const [filter, setFilter] = useState("");
  const byId = useMemo(() => new Map(conversations.map((c) => [c.id, c])), [conversations]);
  const shownSet = useMemo(() => new Set(shown), [shown]);
  const hiddenSet = useMemo(() => new Set(hidden), [hidden]);
  const rest = useMemo(
    () => conversations.filter((c) => c.status !== "archived" && !shownSet.has(c.id) && !hiddenSet.has(c.id)),
    [conversations, shownSet, hiddenSet],
  );
  const q = filter.trim().toLowerCase();
  const match = (id: string) => {
    if (!q) return true;
    const c = byId.get(id);
    return `${windowTitle(c, id)} ${c?.goal ?? ""} ${id}`.toLowerCase().includes(q);
  };

  const row = (id: string, isShown: boolean, close: () => void) => {
    const c = byId.get(id);
    return (
      <MenuItem
        key={id}
        active={isShown}
        onClick={() => {
          if (isShown) onClose(id);
          else {
            onOpen(id);
            close();
          }
        }}
        hint={isShown ? "close" : relativeTime(c?.updated_at)}
      >
        <StatusDot status={c?.status} />
        <span className="truncate" title={c?.goal || undefined}>
          {windowTitle(c, id)}
        </span>
      </MenuItem>
    );
  };

  const shownRows = shown.filter(match);
  const hiddenRows = hidden.filter(match);
  const restRows = rest.map((c) => c.id).filter(match);

  return (
    <Popover
      open={open}
      onOpenChange={(v) => {
        onOpenChange(v);
        if (!v) setFilter("");
      }}
      width={320}
      title="Open, close or bring back windows"
      trigger={
        <>
          <IconWindows className="h-3.5 w-3.5" />
          Windows
          {hidden.length ? (
            <span className="rounded bg-neutral-200 px-1 text-[10.5px] font-semibold text-neutral-700 dark:bg-neutral-700 dark:text-neutral-200">
              {hidden.length} hidden
            </span>
          ) : null}
          <IconChevronDown className="h-3 w-3 text-neutral-500" />
        </>
      }
    >
      {(close) => (
        <div>
          <div className="p-1">
            <input
              autoFocus
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
              placeholder="Filter conversations…"
              className="w-full rounded-md border border-neutral-300 bg-white px-2 py-1 text-[12.5px] text-neutral-900 placeholder:text-neutral-400 focus:border-sky-500 focus:ring-1 focus:ring-sky-500 focus:outline-none dark:border-neutral-700 dark:bg-neutral-950 dark:text-neutral-100"
            />
          </div>
          {shownRows.length ? (
            <>
              <MenuLabel>Open</MenuLabel>
              {shownRows.map((id) => row(id, true, close))}
            </>
          ) : null}
          {hiddenRows.length ? (
            <>
              <MenuLabel>Hidden</MenuLabel>
              {hiddenRows.map((id) => row(id, false, close))}
              {!q && hiddenRows.length > 1 ? (
                <MenuItem
                  onClick={() => {
                    onShowHidden();
                    close();
                  }}
                >
                  <span className="text-sky-700 dark:text-sky-400">Bring back all {hiddenRows.length}</span>
                </MenuItem>
              ) : null}
            </>
          ) : null}
          {restRows.length ? (
            <>
              <MenuLabel>Other conversations</MenuLabel>
              {restRows.slice(0, 60).map((id) => row(id, false, close))}
              {restRows.length > 60 ? (
                <div className="px-2 py-1 text-[11.5px] text-neutral-500">
                  {restRows.length - 60} more — filter to find them
                </div>
              ) : null}
            </>
          ) : null}
          {!shownRows.length && !hiddenRows.length && !restRows.length ? (
            <div className="px-2 py-3 text-center text-[12px] text-neutral-500">No matching conversations.</div>
          ) : null}
          <MenuSeparator />
          <div className="px-2 py-1 text-[11px] leading-4 text-neutral-500">
            The desk and active threads open by themselves; a closed window stays closed until you bring it back.
          </div>
        </div>
      )}
    </Popover>
  );
}
