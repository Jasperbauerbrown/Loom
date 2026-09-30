// One Overview window: a title bar (status, title, role/preset, approvals, unread, and
// minimise / maximise / open-full / close), eight resize handles, and the shared
// `Conversation` inside. Dragging and resizing write the frame's style directly (one
// rAF per frame) and commit to the Overview's state once, on release — the transcript
// never re-renders during a drag.

import { memo, useRef, type PointerEvent as ReactPointerEvent } from "react";
import { Conversation } from "../Conversation";
import { StatusLine } from "../Conversation";
import { useStore } from "../../lib/store";
import { href } from "../../lib/router";
import { attention } from "../../lib/overview";
import {
  moveRect,
  resizeRect,
  restoreUnderPointer,
  snapMove,
  snapResize,
  type Edge,
  type Guides,
  type Rect,
  type Size,
} from "../../lib/overviewLayout";
import { cls } from "../../lib/format";
import { IconClose, IconDesk, IconExternal, IconMaximize, IconMinimize, IconRestore } from "../Icons";
import { usePendingCount, windowTitle } from "./status";

/** Stable callbacks from the Overview (one object for every window, so memo holds). */
export interface WindowActions {
  focus(id: string): void;
  commit(id: string, rect: Rect): void;
  minimize(id: string): void;
  toggleMaximize(id: string): void;
  close(id: string): void;
  /** the other visible windows' rects, for snapping */
  others(id: string): Rect[];
  guides(g: Guides | null): void;
}

const DenseConversation = memo(Conversation);

const HANDLES: { edge: Edge; className: string; cursor: string }[] = [
  { edge: "n", className: "-top-1 left-3 right-3 h-2", cursor: "ns-resize" },
  { edge: "s", className: "-bottom-1 left-3 right-3 h-2", cursor: "ns-resize" },
  { edge: "w", className: "-left-1 top-3 bottom-3 w-2", cursor: "ew-resize" },
  { edge: "e", className: "-right-1 top-3 bottom-3 w-2", cursor: "ew-resize" },
  { edge: "nw", className: "-top-1 -left-1 h-4 w-4", cursor: "nwse-resize" },
  { edge: "se", className: "-bottom-1 -right-1 h-4 w-4", cursor: "nwse-resize" },
  { edge: "ne", className: "-top-1 -right-1 h-4 w-4", cursor: "nesw-resize" },
  { edge: "sw", className: "-bottom-1 -left-1 h-4 w-4", cursor: "nesw-resize" },
];

interface Drag {
  kind: "move" | Edge;
  start: Rect;
  x0: number;
  y0: number;
  next: Rect;
  guides: Guides | null;
  moved: boolean;
  others: Rect[];
  raf: number;
  fromMaximized: boolean;
}

const round = (r: Rect): Rect => ({ x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.w), h: Math.round(r.h) });

function WindowButton({
  label,
  onClick,
  children,
  danger,
}: {
  label: string;
  onClick: () => void;
  children: React.ReactNode;
  danger?: boolean;
}) {
  return (
    <button
      type="button"
      title={label}
      aria-label={label}
      onClick={onClick}
      className={cls(
        "flex h-6 w-6 items-center justify-center rounded text-neutral-500 transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-sky-500/60",
        danger
          ? "hover:bg-red-100 hover:text-red-700 dark:hover:bg-red-950/60 dark:hover:text-red-300"
          : "hover:bg-neutral-200 hover:text-neutral-900 dark:hover:bg-neutral-700 dark:hover:text-neutral-100",
      )}
    >
      {children}
    </button>
  );
}

export const ConversationWindow = memo(function ConversationWindow({
  id,
  rect,
  restore,
  z,
  bounds,
  focused,
  minimized,
  maximized,
  unread,
  stacked,
  actions,
}: {
  id: string;
  /** where it is drawn (the maximised rect while maximised) */
  rect: Rect;
  /** its own, un-maximised rect */
  restore: Rect;
  z: number;
  bounds: Size;
  focused: boolean;
  minimized: boolean;
  maximized: boolean;
  unread: boolean;
  /** the small-screen layout: in page flow, no dragging or resizing */
  stacked: boolean;
  actions: WindowActions;
}) {
  const conversation = useStore((s) => s.conversations[id], Object.is);
  const pending = usePendingCount(id);
  const frame = useRef<HTMLDivElement>(null);
  const drag = useRef<Drag | null>(null);
  const title = windowTitle(conversation, id);
  const cue = attention(conversation?.status, pending);
  const meta = conversation ? [conversation.role, conversation.preset].filter(Boolean).join(" · ") : "";

  const apply = (r: Rect) => {
    const el = frame.current;
    if (!el) return;
    el.style.transform = `translate3d(${r.x}px, ${r.y}px, 0)`;
    el.style.width = `${r.w}px`;
    el.style.height = `${r.h}px`;
  };

  const begin = (e: ReactPointerEvent<HTMLElement>, kind: "move" | Edge) => {
    if (stacked || e.button !== 0) return;
    if (kind === "move" && (e.target as HTMLElement).closest("button, a, input, textarea, select")) return;
    if (kind !== "move" && maximized) return;
    e.preventDefault();
    actions.focus(id);
    e.currentTarget.setPointerCapture(e.pointerId);
    drag.current = {
      kind,
      start: rect,
      x0: e.clientX,
      y0: e.clientY,
      next: rect,
      guides: null,
      moved: false,
      others: actions.others(id),
      raf: 0,
      fromMaximized: maximized,
    };
  };

  const move = (e: ReactPointerEvent<HTMLElement>) => {
    const d = drag.current;
    if (!d) return;
    const dx = e.clientX - d.x0;
    const dy = e.clientY - d.y0;
    if (!d.moved) {
      if (Math.hypot(dx, dy) < 3) return;
      d.moved = true;
      const el = frame.current;
      if (el) el.style.transition = "none";
      document.body.style.userSelect = "none";
      document.body.style.cursor = d.kind === "move" ? "grabbing" : HANDLES.find((h) => h.edge === d.kind)!.cursor;
      if (d.fromMaximized && el?.parentElement) {
        // Pull a maximised window out under the pointer at its restored size.
        const left = el.parentElement.getBoundingClientRect().left;
        d.start = restoreUnderPointer(restore, d.x0 - left, bounds);
      }
    }
    let next: Rect;
    let guides: Guides | null = null;
    if (d.kind === "move") {
      next = moveRect(d.start, dx, dy, bounds);
      if (!e.altKey) ({ rect: next, guides } = snapMove(next, d.others, bounds));
    } else {
      next = resizeRect(d.start, d.kind, dx, dy, bounds);
      if (!e.altKey) ({ rect: next, guides } = snapResize(next, d.kind, d.others, bounds));
    }
    d.next = round(next);
    d.guides = guides;
    if (!d.raf) {
      d.raf = requestAnimationFrame(() => {
        d.raf = 0;
        apply(d.next);
        actions.guides(d.guides);
      });
    }
  };

  const end = () => {
    const d = drag.current;
    if (!d) return;
    drag.current = null;
    if (d.raf) cancelAnimationFrame(d.raf);
    if (!d.moved) return;
    apply(d.next);
    if (frame.current) frame.current.style.transition = "";
    document.body.style.userSelect = "";
    document.body.style.cursor = "";
    actions.guides(null);
    actions.commit(id, d.next);
  };

  const dragProps = (kind: "move" | Edge) => ({
    onPointerDown: (e: ReactPointerEvent<HTMLElement>) => begin(e, kind),
    onPointerMove: move,
    onPointerUp: end,
    onPointerCancel: end,
    onLostPointerCapture: end,
  });

  const frameStyle: React.CSSProperties = stacked
    ? { height: minimized ? undefined : "min(70vh, 640px)", minHeight: minimized ? undefined : 360 }
    : {
        transform: `translate3d(${rect.x}px, ${rect.y}px, 0)`,
        width: rect.w,
        height: rect.h,
        zIndex: z + 1,
        visibility: minimized ? "hidden" : undefined,
      };

  const border =
    cue === "amber"
      ? "border-amber-400 ring-2 ring-amber-400/40 dark:border-amber-500/80 dark:ring-amber-500/30"
      : cue === "sky"
        ? "border-sky-400/80 ring-1 ring-sky-400/30 dark:border-sky-500/70 dark:ring-sky-500/25"
        : focused
          ? "border-neutral-300 dark:border-neutral-600"
          : "border-neutral-200 dark:border-neutral-800";

  return (
    <div
      ref={frame}
      data-window-id={id}
      role="group"
      aria-label={title}
      aria-hidden={minimized && !stacked ? true : undefined}
      onPointerDownCapture={() => actions.focus(id)}
      style={frameStyle}
      className={cls(
        stacked
          ? "relative w-full"
          : "absolute top-0 left-0 transition-[transform,width,height] duration-150 ease-out motion-reduce:transition-none",
        minimized && !stacked && "pointer-events-none",
      )}
    >
      <div
        className={cls(
          "flex h-full min-h-0 flex-col overflow-hidden rounded-lg border bg-white dark:bg-neutral-950",
          border,
          focused && !stacked ? "shadow-xl shadow-neutral-900/15 dark:shadow-black/50" : "shadow-md shadow-neutral-900/5 dark:shadow-black/30",
        )}
      >
        <div
          {...dragProps("move")}
          onDoubleClick={(e) => {
            if (stacked || (e.target as HTMLElement).closest("button, a")) return;
            actions.toggleMaximize(id);
          }}
          className={cls(
            "@container flex h-9 shrink-0 items-center gap-2 border-b pr-1 pl-3 select-none",
            stacked ? "" : maximized ? "cursor-default" : "cursor-grab active:cursor-grabbing",
            focused
              ? "border-neutral-200 bg-neutral-100 dark:border-neutral-800 dark:bg-neutral-900"
              : "border-neutral-200 bg-neutral-50 dark:border-neutral-800 dark:bg-neutral-900/60",
          )}
          style={{ touchAction: "none" }}
        >
          {conversation?.kind === "desk" ? <IconDesk className="h-3.5 w-3.5 shrink-0 text-neutral-500" /> : null}
          <span
            className={cls(
              "min-w-0 flex-1 truncate text-[13px]",
              focused ? "font-semibold text-neutral-900 dark:text-neutral-100" : "font-medium text-neutral-700 dark:text-neutral-300",
            )}
            title={conversation?.goal && conversation.goal !== title ? `${title}\n${conversation.goal}` : title}
          >
            {title}
            {meta ? (
              <span className="ml-2 hidden text-[11.5px] font-normal text-neutral-500 @md:inline" title="role · preset">
                {meta}
              </span>
            ) : null}
          </span>
          {unread ? (
            <span className="h-2 w-2 shrink-0 rounded-full bg-sky-500" title="New activity since you last looked" />
          ) : null}
          {pending > 0 ? (
            <span
              className="shrink-0 rounded bg-amber-100 px-1.5 py-0.5 text-[11px] font-medium text-amber-800 dark:bg-amber-950 dark:text-amber-300"
              title={`${pending} tool call${pending === 1 ? "" : "s"} waiting for approval`}
            >
              {pending} to approve
            </span>
          ) : null}
          <span className="hidden shrink-0 @xs:inline-flex">
            <StatusLine status={conversation?.status ?? "idle"} />
          </span>
          <div className="flex shrink-0 items-center">
            <a
              href={href(`/c/${id}`)}
              title="Open full screen"
              aria-label="Open full screen"
              className="flex h-6 w-6 items-center justify-center rounded text-neutral-500 hover:bg-neutral-200 hover:text-neutral-900 dark:hover:bg-neutral-700 dark:hover:text-neutral-100"
            >
              <IconExternal className="h-3.5 w-3.5" />
            </a>
            <WindowButton label={minimized && stacked ? "Expand" : "Minimise"} onClick={() => (minimized ? actions.focus(id) : actions.minimize(id))}>
              <IconMinimize className="h-3.5 w-3.5" />
            </WindowButton>
            {stacked ? null : (
              <WindowButton label={maximized ? "Restore" : "Maximise"} onClick={() => actions.toggleMaximize(id)}>
                {maximized ? <IconRestore className="h-3.5 w-3.5" /> : <IconMaximize className="h-3.5 w-3.5" />}
              </WindowButton>
            )}
            <WindowButton label="Close window (reopen from Windows)" onClick={() => actions.close(id)} danger>
              <IconClose className="h-3.5 w-3.5" />
            </WindowButton>
          </div>
        </div>
        {stacked && minimized ? null : (
          <div className="min-h-0 flex-1">
            <DenseConversation id={id} dense />
          </div>
        )}
      </div>
      {stacked || maximized || minimized
        ? null
        : HANDLES.map((h) => (
            <div
              key={h.edge}
              {...dragProps(h.edge)}
              className={cls("absolute z-10", h.className)}
              style={{ cursor: h.cursor, touchAction: "none" }}
            />
          ))}
    </div>
  );
});
