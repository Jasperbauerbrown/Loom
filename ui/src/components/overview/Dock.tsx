// The Overview's dock: one chip per open window, in overview order. A chip shows the
// status dot, title, unread dot and approval count, glows amber when the conversation
// waits on the user and sky while it runs. Click: restore / focus, or minimise the
// focused window (the usual taskbar behaviour).

import { memo } from "react";
import { useStore } from "../../lib/store";
import { attention } from "../../lib/overview";
import { cls, statusLabel } from "../../lib/format";
import { StatusDot, usePendingCount, windowTitle } from "./status";

const DockItem = memo(function DockItem({
  id,
  focused,
  minimized,
  unread,
  onClick,
}: {
  id: string;
  focused: boolean;
  minimized: boolean;
  unread: boolean;
  onClick: (id: string) => void;
}) {
  const conversation = useStore((s) => s.conversations[id], Object.is);
  const pending = usePendingCount(id);
  const title = windowTitle(conversation, id);
  const cue = attention(conversation?.status, pending);
  const tip = [
    title,
    statusLabel(conversation?.status ?? "idle"),
    conversation?.status_line || "",
    pending ? `${pending} to approve` : "",
    unread ? "new activity" : "",
    minimized ? "minimised — click to restore" : focused ? "click to minimise" : "click to focus",
  ]
    .filter(Boolean)
    .join(" · ");
  return (
    <button
      type="button"
      title={tip}
      aria-pressed={focused}
      onClick={() => onClick(id)}
      className={cls(
        "flex max-w-56 min-w-0 shrink-0 items-center gap-1.5 rounded-md border px-2 py-1 text-[12px] transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-sky-500/60",
        focused
          ? "border-neutral-300 bg-white font-medium text-neutral-900 shadow-sm dark:border-neutral-600 dark:bg-neutral-800 dark:text-neutral-100"
          : "border-neutral-200 bg-neutral-50 text-neutral-700 hover:bg-white dark:border-neutral-800 dark:bg-neutral-900 dark:text-neutral-300 dark:hover:bg-neutral-800",
        minimized && "border-dashed opacity-75",
        cue === "amber" && "border-amber-400 ring-2 ring-amber-400/40 dark:border-amber-500/80 dark:ring-amber-500/30",
        cue === "sky" && "border-sky-400/80 dark:border-sky-500/70",
      )}
    >
      <StatusDot status={conversation?.status} />
      <span className={cls("min-w-0 truncate", minimized && "italic")}>{title}</span>
      {pending > 0 ? (
        <span className="shrink-0 rounded bg-amber-100 px-1 text-[10.5px] font-semibold text-amber-800 dark:bg-amber-950 dark:text-amber-300">
          {pending}
        </span>
      ) : null}
      {unread ? <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-sky-500" /> : null}
    </button>
  );
});

export function Dock({
  ids,
  focused,
  minimized,
  unread,
  onClick,
  trailing,
}: {
  ids: string[];
  focused: string | null;
  minimized: Set<string>;
  unread: Set<string>;
  onClick: (id: string) => void;
  trailing?: React.ReactNode;
}) {
  return (
    <div className="flex h-10 shrink-0 items-center gap-1.5 pt-2" role="toolbar" aria-label="Open windows">
      <div className="flex min-w-0 flex-1 items-center gap-1.5 overflow-x-auto">
        {ids.map((id) => (
          <DockItem
            key={id}
            id={id}
            focused={id === focused}
            minimized={minimized.has(id)}
            unread={unread.has(id)}
            onClick={onClick}
          />
        ))}
      </div>
      {trailing}
    </div>
  );
}
