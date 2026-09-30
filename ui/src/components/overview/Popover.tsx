// A small anchored popover for the Overview toolbar: a trigger button and a panel that
// closes on an outside press, on Escape, or when an item asks it to.

import { useEffect, useRef, useState, type ReactNode } from "react";
import { cls } from "../../lib/format";

export function Popover({
  trigger,
  title,
  children,
  align = "right",
  width = 280,
  open: controlled,
  onOpenChange,
  triggerClassName,
}: {
  /** the trigger's content; the button itself is styled here */
  trigger: ReactNode;
  title?: string;
  /** the panel; given `close` so an item can dismiss it */
  children: (close: () => void) => ReactNode;
  align?: "left" | "right";
  width?: number;
  open?: boolean;
  onOpenChange?: (open: boolean) => void;
  /** extra classes for the trigger button (e.g. to join a split button) */
  triggerClassName?: string;
}) {
  const [own, setOwn] = useState(false);
  const open = controlled ?? own;
  const setOpen = (v: boolean) => {
    if (controlled === undefined) setOwn(v);
    onOpenChange?.(v);
  };
  const root = useRef<HTMLDivElement>(null);
  const setOpenRef = useRef(setOpen);
  setOpenRef.current = setOpen;

  useEffect(() => {
    if (!open) return;
    const onDown = (e: PointerEvent) => {
      if (root.current && !root.current.contains(e.target as Node)) setOpenRef.current(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        // Handled here: the Overview's own Escape (restore a maximised window) skips it.
        e.preventDefault();
        setOpenRef.current(false);
      }
    };
    document.addEventListener("pointerdown", onDown, true);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("pointerdown", onDown, true);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  return (
    <div ref={root} className="relative">
      <button
        type="button"
        title={title}
        aria-expanded={open}
        aria-haspopup="menu"
        onClick={() => setOpen(!open)}
        className={cls(
          "inline-flex items-center gap-1.5 rounded-md border px-2 py-1 text-[12px] font-medium transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-sky-500/60",
          open
            ? "border-neutral-400 bg-neutral-100 text-neutral-900 dark:border-neutral-600 dark:bg-neutral-800 dark:text-neutral-100"
            : "border-neutral-300 bg-white text-neutral-800 hover:bg-neutral-100 dark:border-neutral-700 dark:bg-neutral-900 dark:text-neutral-200 dark:hover:bg-neutral-800",
          triggerClassName,
        )}
      >
        {trigger}
      </button>
      {open ? (
        <div
          role="menu"
          style={{ width }}
          className={cls(
            "absolute top-full z-40 mt-1 max-h-[70vh] overflow-y-auto rounded-lg border border-neutral-200 bg-white p-1 shadow-xl dark:border-neutral-700 dark:bg-neutral-900",
            align === "right" ? "right-0" : "left-0",
          )}
        >
          {children(() => setOpen(false))}
        </div>
      ) : null}
    </div>
  );
}

export function MenuItem({
  children,
  onClick,
  hint,
  active,
  disabled,
}: {
  children: ReactNode;
  onClick: () => void;
  /** right-aligned secondary text: a shortcut, a count */
  hint?: ReactNode;
  active?: boolean;
  disabled?: boolean;
}) {
  return (
    <button
      type="button"
      role="menuitem"
      disabled={disabled}
      onClick={onClick}
      className={cls(
        "flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-[12.5px] disabled:cursor-not-allowed disabled:opacity-50",
        active
          ? "bg-neutral-100 text-neutral-900 dark:bg-neutral-800 dark:text-neutral-100"
          : "text-neutral-700 hover:bg-neutral-100 dark:text-neutral-300 dark:hover:bg-neutral-800",
      )}
    >
      <span className="flex min-w-0 flex-1 items-center gap-2">{children}</span>
      {hint ? <span className="shrink-0 text-[11px] text-neutral-500">{hint}</span> : null}
    </button>
  );
}

export function MenuLabel({ children }: { children: ReactNode }) {
  return (
    <div className="px-2 pt-2 pb-1 text-[10.5px] font-semibold tracking-wide text-neutral-500 uppercase">{children}</div>
  );
}

export function MenuSeparator() {
  return <div className="my-1 h-px bg-neutral-200 dark:bg-neutral-800" />;
}

/** A keyboard key, as shown in menus and hints. */
export function Kbd({ children }: { children: ReactNode }) {
  return (
    <kbd className="rounded border border-neutral-300 bg-neutral-50 px-1 font-mono text-[10.5px] text-neutral-600 dark:border-neutral-700 dark:bg-neutral-800 dark:text-neutral-400">
      {children}
    </kbd>
  );
}
