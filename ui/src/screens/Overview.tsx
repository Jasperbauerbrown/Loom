// Overview (`#/overview`, 03 §13.1): a workspace of conversation windows — the desk,
// every active thread, and anything you open — each the shared `Conversation` with its
// own composer. Windows move, resize, snap, stack, minimise to the dock, maximise, and
// tile or cascade on command; the layout is remembered per browser as fractions of the
// workspace. Below 900px wide the windows stack in page flow instead.
//
// All window logic is pure (lib/overview.ts, lib/overviewLayout.ts); this file wires
// it to the DOM, the store and the keyboard.

import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { conversationList, store, useStore } from "../lib/store";
import type { Conversation } from "../lib/types";
import {
  EMPTY_STATE,
  cascadeWindows,
  closeWindow,
  cycle,
  focusWindow,
  focusedId,
  lastEventIds,
  minimizeWindow,
  nextSeen,
  openWindow,
  parseState,
  placeMissing,
  prune,
  setGeometry,
  stacking,
  tileWindows,
  toggleMaximize,
  unreadIds,
  windowIds,
  type OverviewState,
} from "../lib/overview";
import { fromFrac, maximizedRect, toFrac, type Guides, type Rect, type Size } from "../lib/overviewLayout";
import { Button, Confirm } from "../components/ui";
import { IconCascade, IconChevronDown, IconKeyboard, IconPlus, IconRefresh, IconTile } from "../components/Icons";
import { ConversationWindow, type WindowActions } from "../components/overview/ConversationWindow";
import { Dock } from "../components/overview/Dock";
import { WindowsMenu } from "../components/overview/WindowsMenu";
import { Kbd, MenuItem, MenuLabel, MenuSeparator, Popover } from "../components/overview/Popover";
import { NewThreadDialog } from "./Threads";

const LAYOUT_KEY = "loom.overview.layout";
/** The old 1/2/3 column choice, replaced by the Tile presets. */
const OLD_COLUMNS_KEY = "loom.overview.columns";
/** Narrower than this, windows stack in page flow (no dragging). */
const STACK_BELOW = 900;

function loadState(): OverviewState {
  try {
    const raw = localStorage.getItem(LAYOUT_KEY);
    return raw ? parseState(JSON.parse(raw)) : EMPTY_STATE;
  } catch {
    return EMPTY_STATE;
  }
}

function saveState(st: OverviewState) {
  try {
    localStorage.setItem(LAYOUT_KEY, JSON.stringify(st));
    localStorage.removeItem(OLD_COLUMNS_KEY);
  } catch {
    /* blocked storage: the layout just doesn't survive a reload */
  }
}

const roundRect = (r: Rect): Rect => ({ x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.w), h: Math.round(r.h) });

/** Snap guides are drawn imperatively: a drag must not re-render the workspace. */
function drawGuides(el: HTMLDivElement | null, g: Guides | null) {
  if (!el) return;
  const lines: HTMLDivElement[] = [];
  for (const x of g?.x ?? []) {
    const d = document.createElement("div");
    d.className = "absolute top-0 bottom-0 w-px bg-sky-500/70";
    d.style.left = `${x}px`;
    lines.push(d);
  }
  for (const y of g?.y ?? []) {
    const d = document.createElement("div");
    d.className = "absolute left-0 right-0 h-px bg-sky-500/70";
    d.style.top = `${y}px`;
    lines.push(d);
  }
  el.replaceChildren(...lines);
}

const TILE_PRESETS: { label: string; columns: number | "auto" | "row" }[] = [
  { label: "Auto grid", columns: "auto" },
  { label: "1 column", columns: 1 },
  { label: "2 columns", columns: 2 },
  { label: "3 columns", columns: 3 },
  { label: "Side by side", columns: "row" },
];

const SHORTCUTS: [string, string][] = [
  ["Alt+]  /  Alt+[", "Next / previous window"],
  ["Alt+Enter", "Maximise or restore"],
  ["Esc", "Restore a maximised window"],
  ["Alt+M", "Minimise to the dock"],
  ["Alt+T", "Tile (auto grid)"],
];

export function Overview() {
  const conversations = useStore(conversationList);
  const [raw, setState] = useState<OverviewState>(loadState);
  const [bounds, setBounds] = useState<Size>({ w: 0, h: 0 });
  const [windowsOpen, setWindowsOpen] = useState(false);
  const [newThread, setNewThread] = useState(false);
  const [confirmReset, setConfirmReset] = useState(false);
  const workspace = useRef<HTMLDivElement>(null);
  const guidesEl = useRef<HTMLDivElement>(null);

  useEffect(() => {
    void store.loadConversations();
    void store.loadApprovals();
  }, []);

  useLayoutEffect(() => {
    const el = workspace.current;
    if (!el) return;
    const measure = () =>
      setBounds((b) => (b.w === el.clientWidth && b.h === el.clientHeight ? b : { w: el.clientWidth, h: el.clientHeight }));
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const stacked = bounds.w > 0 && bounds.w < STACK_BELOW;
  const pruned = useMemo(() => prune(raw, conversations), [raw, conversations]);
  const { shown, hidden } = useMemo(() => windowIds(conversations, pruned), [conversations, pruned]);
  // New windows are placed in the same render they appear in (no flash at 0,0), and
  // the placement is then kept.
  const st = useMemo(
    () => (stacked ? pruned : placeMissing(pruned, shown, bounds)),
    [pruned, shown, bounds, stacked],
  );
  useEffect(() => {
    if (st !== raw) setState(st);
  }, [st, raw]);
  useEffect(() => saveState(raw), [raw]);

  const minimized = useMemo(() => new Set(st.minimized), [st.minimized]);
  const maximized = useMemo(() => new Set(st.maximized), [st.maximized]);
  const z = useMemo(() => stacking(shown, st.order), [shown, st.order]);
  const focused = useMemo(() => focusedId(shown, st), [shown, st]);

  // Unread: newest event per conversation vs. what was showing when last focused.
  const last = useStore((s) => lastEventIds(s.events, Object.keys(s.events)));
  const [seen, setSeen] = useState<Record<string, number>>({});
  useEffect(() => setSeen((s) => nextSeen(s, last, focused)), [last, focused]);
  const unread = useMemo(() => unreadIds(seen, last, focused), [seen, last, focused]);

  const rects = useMemo(() => {
    const out: Record<string, { rect: Rect; restore: Rect }> = {};
    const max = maximizedRect(bounds);
    for (const id of shown) {
      const g = st.geom[id];
      const restore = g ? roundRect(fromFrac(g, bounds)) : max;
      out[id] = { restore, rect: maximized.has(id) ? max : restore };
    }
    return out;
  }, [shown, st.geom, bounds, maximized]);

  // The latest values, for callbacks that must stay stable across renders.
  const live = useRef({ rects, bounds, shown, minimized, maximized, focused, stacked });
  live.current = { rects, bounds, shown, minimized, maximized, focused, stacked };

  const actions: WindowActions = useMemo(
    () => ({
      focus: (id) => setState((s) => focusWindow(s, id)),
      commit: (id, r) => setState((s) => focusWindow(setGeometry(s, id, toFrac(r, live.current.bounds)), id)),
      minimize: (id) => setState((s) => minimizeWindow(s, id)),
      toggleMaximize: (id) => setState((s) => toggleMaximize(s, id)),
      close: (id) => setState((s) => closeWindow(s, id)),
      others: (id) => {
        const L = live.current;
        return L.shown
          .filter((o) => o !== id && !L.minimized.has(o) && !L.maximized.has(o) && L.rects[o])
          .map((o) => L.rects[o].rect);
      },
      guides: (g) => drawGuides(guidesEl.current, g),
    }),
    [],
  );

  const focusComposer = useCallback((id: string) => {
    requestAnimationFrame(() => {
      const el = workspace.current?.querySelector<HTMLTextAreaElement>(`[data-window-id="${CSS.escape(id)}"] textarea`);
      el?.focus({ preventScroll: !live.current.stacked });
      if (live.current.stacked) el?.closest("[data-window-id]")?.scrollIntoView({ block: "nearest", behavior: "smooth" });
    });
  }, []);

  const activate = useCallback(
    (id: string) => {
      actions.focus(id);
      focusComposer(id);
    },
    [actions, focusComposer],
  );

  const dockClick = useCallback(
    (id: string) => {
      const L = live.current;
      if (L.focused === id && !L.minimized.has(id) && !L.stacked) actions.minimize(id);
      else activate(id);
    },
    [actions, activate],
  );

  const tileWith = useCallback((columns: number | "auto" | "row") => {
    setState((s) => {
      const L = live.current;
      const n = columns === "auto" ? undefined : columns === "row" ? L.shown.length : columns;
      return tileWindows(s, L.shown, L.bounds, n);
    });
  }, []);

  const openConversation = useCallback(
    (id: string) => {
      setState((s) => openWindow(s, id));
      focusComposer(id);
    },
    [focusComposer],
  );

  // Keyboard: window cycling, maximise, minimise, tile; Escape restores a maximised
  // window. Menus and dialogs handle their own Escape first.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.defaultPrevented || document.querySelector('[role="dialog"]')) return;
      const L = live.current;
      if (e.key === "Escape") {
        if (L.focused && L.maximized.has(L.focused) && !L.stacked) {
          e.preventDefault();
          actions.toggleMaximize(L.focused);
        }
        return;
      }
      if (!e.altKey || e.ctrlKey || e.metaKey) return;
      switch (e.code) {
        case "BracketRight":
        case "BracketLeft": {
          const next = cycle(L.shown, L.focused, e.code === "BracketRight" ? 1 : -1);
          if (!next) return;
          e.preventDefault();
          activate(next);
          return;
        }
        case "Enter":
          if (!L.focused || L.stacked) return;
          e.preventDefault();
          actions.toggleMaximize(L.focused);
          return;
        case "KeyM":
          if (!L.focused) return;
          e.preventDefault();
          actions.minimize(L.focused);
          (document.activeElement as HTMLElement | null)?.blur?.();
          return;
        case "KeyT":
          if (L.stacked) return;
          e.preventDefault();
          tileWith("auto");
          return;
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [actions, activate, tileWith]);

  const activeCount = conversations.filter(
    (c) => c.kind === "thread" && (c.status === "running" || c.status === "waiting_approval" || c.status === "waiting_input"),
  ).length;
  const waitingCount = conversations.filter(
    (c) => shown.includes(c.id) && (c.status === "waiting_approval" || c.status === "waiting_input"),
  ).length;

  return (
    <div className="flex h-full min-h-0 flex-col p-4 pb-2">
      <div className="mb-3 flex flex-wrap items-center gap-x-3 gap-y-2">
        <h1 className="text-[16px] font-semibold text-neutral-900 dark:text-neutral-100">Overview</h1>
        <span className="text-[12px] text-neutral-500">
          {shown.length} window{shown.length === 1 ? "" : "s"}
          {" · "}
          {activeCount === 0 ? "no active threads" : `${activeCount} active thread${activeCount === 1 ? "" : "s"}`}
          {waitingCount ? (
            <span className="text-amber-700 dark:text-amber-400">
              {" · "}
              {waitingCount} waiting on you
            </span>
          ) : null}
        </span>
        <div className="flex-1" />
        <div className="flex flex-wrap items-center gap-1.5">
          <Button size="sm" variant="primary" onClick={() => setNewThread(true)}>
            <IconPlus className="h-3.5 w-3.5" /> New thread
          </Button>
          <WindowsMenu
            conversations={conversations}
            shown={shown}
            hidden={hidden}
            onOpen={openConversation}
            onClose={actions.close}
            onShowHidden={() => setState((s) => hidden.reduce(openWindow, s))}
            open={windowsOpen}
            onOpenChange={setWindowsOpen}
          />
          {stacked ? null : (
            <>
              <div className="flex items-stretch">
                <Button
                  size="sm"
                  className="rounded-r-none"
                  onClick={() => tileWith("auto")}
                  disabled={shown.length === 0}
                  title="Tile every window into a grid (Alt+T)"
                >
                  <IconTile className="h-3.5 w-3.5" /> Tile
                </Button>
                <Popover
                  title="Tile layouts"
                  width={200}
                  triggerClassName="-ml-px h-full rounded-l-none px-1.5"
                  trigger={<IconChevronDown className="h-3 w-3" />}
                >
                  {(close) => (
                    <>
                      <MenuLabel>Tile</MenuLabel>
                      {TILE_PRESETS.map((p) => (
                        <MenuItem
                          key={p.label}
                          disabled={shown.length === 0}
                          hint={p.columns === "auto" ? "Alt+T" : undefined}
                          onClick={() => {
                            tileWith(p.columns);
                            close();
                          }}
                        >
                          {p.label}
                        </MenuItem>
                      ))}
                    </>
                  )}
                </Popover>
              </div>
              <Button
                size="sm"
                onClick={() => setState((s) => cascadeWindows(s, live.current.shown, live.current.bounds))}
                disabled={shown.length === 0}
                title="Cascade every window from the top-left"
              >
                <IconCascade className="h-3.5 w-3.5" /> Cascade
              </Button>
            </>
          )}
          <Button
            size="sm"
            variant="ghost"
            onClick={() => setConfirmReset(true)}
            title="Forget the layout: re-tile, and bring back closed windows"
          >
            <IconRefresh className="h-3.5 w-3.5" /> Reset
          </Button>
          <Popover title="Keyboard shortcuts" width={300} trigger={<IconKeyboard className="h-3.5 w-3.5" />}>
            {() => (
              <div className="p-1">
                <MenuLabel>Keyboard</MenuLabel>
                <ul className="space-y-1 px-2 pb-1">
                  {SHORTCUTS.map(([k, what]) => (
                    <li key={k} className="flex items-center justify-between gap-3 text-[12px] text-neutral-700 dark:text-neutral-300">
                      <span>{what}</span>
                      <Kbd>{k}</Kbd>
                    </li>
                  ))}
                </ul>
                <MenuSeparator />
                <MenuLabel>Mouse</MenuLabel>
                <ul className="space-y-1 px-2 pb-1 text-[12px] leading-4 text-neutral-700 dark:text-neutral-300">
                  <li>Drag a title bar to move; drag any edge or corner to resize.</li>
                  <li>Windows snap to the workspace and to each other; hold Alt to place freely.</li>
                  <li>Double-click a title bar to maximise or restore.</li>
                  <li>Click a dock chip to focus it; click the focused one to minimise.</li>
                </ul>
              </div>
            )}
          </Popover>
        </div>
      </div>

      <div
        ref={workspace}
        className={
          stacked
            ? "relative flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto"
            : "relative isolate min-h-0 flex-1 overflow-hidden rounded-lg border border-neutral-200 bg-neutral-100/60 dark:border-neutral-800 dark:bg-neutral-900/40"
        }
      >
        {bounds.w > 0
          ? shown.map((id) => {
              const r = rects[id];
              if (!r) return null;
              return (
                <ConversationWindow
                  key={id}
                  id={id}
                  rect={r.rect}
                  restore={r.restore}
                  z={z[id] ?? 0}
                  bounds={bounds}
                  focused={id === focused}
                  minimized={minimized.has(id)}
                  maximized={maximized.has(id)}
                  unread={unread.has(id)}
                  stacked={stacked}
                  actions={actions}
                />
              );
            })
          : null}
        {shown.length === 0 && conversations.length > 0 ? (
          <div className="absolute inset-0 flex flex-col items-center justify-center gap-2 text-[13px] text-neutral-500">
            <span>No windows open.</span>
            <Button size="sm" onClick={() => setWindowsOpen(true)}>
              Open a conversation…
            </Button>
          </div>
        ) : null}
        {stacked ? null : <div ref={guidesEl} className="pointer-events-none absolute inset-0 z-[9999]" />}
      </div>

      {shown.length > 0 ? (
        <Dock
          ids={shown}
          focused={focused}
          minimized={minimized}
          unread={unread}
          onClick={dockClick}
          trailing={
            hidden.length ? (
              <button
                type="button"
                onClick={() => setWindowsOpen(true)}
                className="shrink-0 rounded-md border border-dashed border-neutral-300 px-2 py-1 text-[12px] text-neutral-600 hover:bg-neutral-100 dark:border-neutral-700 dark:text-neutral-400 dark:hover:bg-neutral-800"
                title="Closed windows of active conversations — click to bring one back"
              >
                {hidden.length} hidden
              </button>
            ) : null
          }
        />
      ) : null}

      {newThread ? (
        <NewThreadDialog
          onClose={() => setNewThread(false)}
          onCreated={(created: Conversation) => {
            store.set((s) => ({ ...s, conversations: { ...s.conversations, [created.id]: created } }));
            openConversation(created.id);
          }}
        />
      ) : null}
      {confirmReset ? (
        <Confirm
          title="Reset the Overview layout?"
          body="Every window is re-tiled, minimised and maximised windows are restored, closed windows come back, and windows you opened by hand (finished threads) are removed."
          confirmLabel="Reset"
          onConfirm={() => setState(EMPTY_STATE)}
          onClose={() => setConfirmReset(false)}
        />
      ) : null}
    </div>
  );
}
