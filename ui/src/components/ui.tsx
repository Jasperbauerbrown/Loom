// Shared primitives. No component library (03 §19): a handful of small, plain
// components every screen composes, so the whole app looks like one thing.

import { useEffect, useRef, useState, type ReactNode } from "react";
import { cls } from "../lib/format";

// --- buttons ---------------------------------------------------------------

type ButtonProps = React.ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: "primary" | "default" | "ghost" | "danger";
  size?: "sm" | "md";
};

export function Button({ variant = "default", size = "md", className, ...rest }: ButtonProps) {
  const base =
    "inline-flex items-center justify-center gap-1.5 rounded-md border font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-50 focus:outline-none focus-visible:ring-2 focus-visible:ring-sky-500/60";
  const sizes = size === "sm" ? "px-2 py-1 text-[12px]" : "px-3 py-1.5 text-[13px]";
  const variants = {
    primary:
      "border-sky-700 bg-sky-700 text-white hover:bg-sky-600 dark:border-sky-600 dark:bg-sky-600 dark:hover:bg-sky-500",
    default:
      "border-neutral-300 bg-white text-neutral-800 hover:bg-neutral-100 dark:border-neutral-700 dark:bg-neutral-900 dark:text-neutral-200 dark:hover:bg-neutral-800",
    ghost:
      "border-transparent bg-transparent text-neutral-600 hover:bg-neutral-100 dark:text-neutral-400 dark:hover:bg-neutral-800",
    danger:
      "border-red-300 bg-white text-red-700 hover:bg-red-50 dark:border-red-900 dark:bg-neutral-900 dark:text-red-400 dark:hover:bg-red-950/40",
  }[variant];
  return <button className={cls(base, sizes, variants, className)} {...rest} />;
}

// --- inputs ----------------------------------------------------------------

const inputBase =
  "rounded-md border border-neutral-300 bg-white px-2 py-1.5 text-[13px] text-neutral-900 placeholder:text-neutral-400 focus:border-sky-500 focus:outline-none focus:ring-1 focus:ring-sky-500 disabled:opacity-60 dark:border-neutral-700 dark:bg-neutral-900 dark:text-neutral-100";

/** Inputs fill their container unless the caller sets a width of its own: two `w-*`
 *  utilities on one element compete on stylesheet order, not on who wrote them. */
function widthOf(className?: string): string {
  return /(^|\s)w-\S+/.test(className ?? "") ? "" : "w-full";
}

export function Input({ className, ...rest }: React.InputHTMLAttributes<HTMLInputElement>) {
  return <input className={cls(inputBase, widthOf(className), className)} {...rest} />;
}

export function Textarea({ className, ...rest }: React.TextareaHTMLAttributes<HTMLTextAreaElement>) {
  return <textarea className={cls(inputBase, widthOf(className), "resize-y", className)} {...rest} />;
}

export function Select({ className, children, ...rest }: React.SelectHTMLAttributes<HTMLSelectElement>) {
  return (
    <select className={cls(inputBase, widthOf(className), "pr-6", className)} {...rest}>
      {children}
    </select>
  );
}

export function Toggle({
  checked,
  onChange,
  disabled,
  label,
}: {
  checked: boolean;
  onChange: (v: boolean) => void;
  disabled?: boolean;
  label?: string;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className={cls(
        "relative inline-flex h-5 w-9 shrink-0 items-center rounded-full border transition-colors disabled:opacity-50",
        checked
          ? "border-sky-700 bg-sky-700 dark:border-sky-600 dark:bg-sky-600"
          : "border-neutral-300 bg-neutral-200 dark:border-neutral-700 dark:bg-neutral-700",
      )}
    >
      <span
        className={cls(
          "inline-block h-3.5 w-3.5 transform rounded-full bg-white transition-transform",
          checked ? "translate-x-[18px]" : "translate-x-[3px]",
        )}
      />
    </button>
  );
}

export function Label({ children, hint }: { children: ReactNode; hint?: string }) {
  return (
    <div className="mb-1 flex items-baseline gap-2">
      <span className="text-[13px] font-medium text-neutral-800 dark:text-neutral-200">{children}</span>
      {hint ? <span className="text-[11px] text-neutral-500">{hint}</span> : null}
    </div>
  );
}

export function FieldRow({
  label,
  help,
  error,
  children,
  hint,
}: {
  label: ReactNode;
  help?: string;
  error?: string | null;
  hint?: string;
  children: ReactNode;
}) {
  return (
    <div className="py-2">
      <Label hint={hint}>{label}</Label>
      {children}
      {help ? <p className="mt-1 text-[11.5px] leading-4 text-neutral-500">{help}</p> : null}
      {error ? <p className="mt-1 text-[11.5px] leading-4 text-red-600 dark:text-red-400">{error}</p> : null}
    </div>
  );
}

// --- surfaces --------------------------------------------------------------

export function Card({ className, children }: { className?: string; children: ReactNode }) {
  return (
    <div
      className={cls(
        "rounded-lg border border-neutral-200 bg-white dark:border-neutral-800 dark:bg-neutral-900",
        className,
      )}
    >
      {children}
    </div>
  );
}

export function Section({
  title,
  description,
  actions,
  children,
}: {
  title: ReactNode;
  description?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section className="mb-6">
      <div className="mb-2 flex items-start justify-between gap-3">
        <div>
          <h2 className="text-[15px] font-semibold text-neutral-900 dark:text-neutral-100">{title}</h2>
          {description ? <p className="mt-0.5 text-[12px] text-neutral-500">{description}</p> : null}
        </div>
        {actions ? <div className="flex shrink-0 gap-2">{actions}</div> : null}
      </div>
      {children}
    </section>
  );
}

export function Badge({
  children,
  tone = "neutral",
  title,
  className,
}: {
  children: ReactNode;
  tone?: "neutral" | "green" | "amber" | "red" | "sky" | "violet";
  title?: string;
  className?: string;
}) {
  const tones = {
    neutral: "bg-neutral-100 text-neutral-700 dark:bg-neutral-800 dark:text-neutral-300",
    green: "bg-emerald-100 text-emerald-800 dark:bg-emerald-950 dark:text-emerald-300",
    amber: "bg-amber-100 text-amber-800 dark:bg-amber-950 dark:text-amber-300",
    red: "bg-red-100 text-red-800 dark:bg-red-950 dark:text-red-300",
    sky: "bg-sky-100 text-sky-800 dark:bg-sky-950 dark:text-sky-300",
    violet: "bg-violet-100 text-violet-800 dark:bg-violet-950 dark:text-violet-300",
  }[tone];
  return (
    <span
      title={title}
      className={cls("inline-flex items-center rounded px-1.5 py-0.5 text-[11px] font-medium", tones, className)}
    >
      {children}
    </span>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="px-3 py-6 text-center text-[12.5px] text-neutral-500">{children}</div>;
}

export function Spinner({ className }: { className?: string }) {
  return (
    <svg className={cls("h-3.5 w-3.5 animate-spin", className)} viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <circle cx="12" cy="12" r="10" stroke="currentColor" strokeOpacity="0.25" strokeWidth="3" />
      <path d="M22 12a10 10 0 0 0-10-10" stroke="currentColor" strokeWidth="3" strokeLinecap="round" />
    </svg>
  );
}

// --- tables ----------------------------------------------------------------

export function Table({ head, children }: { head: ReactNode[]; children: ReactNode }) {
  const showHead = head.some((h) => h !== "" && h !== null && h !== undefined);
  return (
    <div className="overflow-x-auto rounded-lg border border-neutral-200 dark:border-neutral-800">
      <table className="w-full border-collapse text-[13px]">
        {showHead ? <thead>
          <tr className="border-b border-neutral-200 bg-neutral-50 dark:border-neutral-800 dark:bg-neutral-900">
            {head.map((h, i) => (
              <th key={i} className="px-2.5 py-1.5 text-left text-[11.5px] font-semibold text-neutral-600 dark:text-neutral-400">
                {h}
              </th>
            ))}
          </tr>
        </thead> : null}
        <tbody>{children}</tbody>
      </table>
    </div>
  );
}

export function Tr({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <tr className={cls("border-b border-neutral-100 last:border-0 dark:border-neutral-800/70", className)}>
      {children}
    </tr>
  );
}

export function Td({
  children,
  className,
  title,
  colSpan,
}: {
  children?: ReactNode;
  className?: string;
  title?: string;
  colSpan?: number;
}) {
  return (
    <td className={cls("px-2.5 py-1.5 align-middle", className)} title={title} colSpan={colSpan}>
      {children}
    </td>
  );
}

// --- dialog ----------------------------------------------------------------

export function Dialog({
  title,
  onClose,
  children,
  footer,
  wide,
}: {
  title: ReactNode;
  onClose: () => void;
  children: ReactNode;
  footer?: ReactNode;
  wide?: boolean;
}) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        onClose();
      }
    };
    document.addEventListener("keydown", onKey);
    ref.current?.focus();
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);
  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-black/40 p-6" onMouseDown={onClose}>
      <div
        ref={ref}
        tabIndex={-1}
        role="dialog"
        aria-modal="true"
        onMouseDown={(e) => e.stopPropagation()}
        className={cls(
          "mt-10 w-full rounded-lg border border-neutral-200 bg-white shadow-xl outline-none dark:border-neutral-800 dark:bg-neutral-900",
          wide ? "max-w-3xl" : "max-w-lg",
        )}
      >
        <div className="flex items-center justify-between border-b border-neutral-200 px-4 py-2.5 dark:border-neutral-800">
          <h3 className="text-[14px] font-semibold">{title}</h3>
          <Button variant="ghost" size="sm" onClick={onClose} aria-label="Close">
            ✕
          </Button>
        </div>
        <div className="max-h-[70vh] overflow-y-auto px-4 py-3">{children}</div>
        {footer ? (
          <div className="flex justify-end gap-2 border-t border-neutral-200 px-4 py-2.5 dark:border-neutral-800">
            {footer}
          </div>
        ) : null}
      </div>
    </div>
  );
}

export function Confirm({
  title,
  body,
  confirmLabel = "Confirm",
  danger,
  onConfirm,
  onClose,
}: {
  title: string;
  body: ReactNode;
  confirmLabel?: string;
  danger?: boolean;
  onConfirm: () => void;
  onClose: () => void;
}) {
  return (
    <Dialog
      title={title}
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button
            variant={danger ? "danger" : "primary"}
            onClick={() => {
              onConfirm();
              onClose();
            }}
          >
            {confirmLabel}
          </Button>
        </>
      }
    >
      <div className="text-[13px] leading-5 text-neutral-700 dark:text-neutral-300">{body}</div>
    </Dialog>
  );
}

// --- disclosure ------------------------------------------------------------

export function Collapsible({
  summary,
  children,
  defaultOpen = false,
  className,
}: {
  summary: ReactNode;
  children: ReactNode;
  defaultOpen?: boolean;
  className?: string;
}) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <div className={className}>
      <button
        type="button"
        onClick={() => setOpen(!open)}
        className="flex w-full items-center gap-1.5 text-left text-[12px] text-neutral-600 hover:text-neutral-900 dark:text-neutral-400 dark:hover:text-neutral-100"
      >
        <span className={cls("transition-transform", open && "rotate-90")}>▸</span>
        {summary}
      </button>
      {open ? <div className="mt-1">{children}</div> : null}
    </div>
  );
}

/** JSON shown as collapsible, monospace, pretty-printed text. */
export function JsonBlock({ value, className }: { value: unknown; className?: string }) {
  let text: string;
  try {
    text = JSON.stringify(value, null, 2) ?? String(value);
  } catch {
    text = String(value);
  }
  return (
    <pre
      className={cls(
        "overflow-x-auto rounded-md border border-neutral-200 bg-neutral-50 p-2 font-mono text-[11.5px] leading-4 dark:border-neutral-800 dark:bg-neutral-950",
        className,
      )}
    >
      {text}
    </pre>
  );
}
