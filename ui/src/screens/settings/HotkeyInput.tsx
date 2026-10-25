// The key-capture control for a `hotkey` settings field (voice.hotkey): click, press the
// combination, done. Esc or clicking away leaves the stored value alone. Combinations the
// browser or OS swallow before the page sees them (Ctrl+W, Win+L) can be typed instead.

import { useState } from "react";
import { captureHotkey, formatHotkey } from "../../lib/hotkey";
import { cls } from "../../lib/format";
import { Button, Input } from "../../components/ui";

export function HotkeyInput({
  value,
  disabled,
  onCommit,
  onError,
}: {
  value: string;
  disabled?: boolean;
  onCommit: (hotkey: string) => void;
  onError: (error: string | null) => void;
}) {
  const [capturing, setCapturing] = useState(false);
  const [typing, setTyping] = useState(false);
  const [text, setText] = useState(value);

  if (typing) {
    return (
      <div className="flex items-center gap-2">
        <Input
          autoFocus
          className="font-mono"
          value={text}
          disabled={disabled}
          placeholder="ctrl+alt+space"
          onChange={(e) => setText(e.target.value)}
          onBlur={() => {
            setTyping(false);
            const next = text.trim().toLowerCase().replace(/\s+/g, "");
            if (next && next !== value) onCommit(next);
          }}
          onKeyDown={(e) => {
            if (e.key === "Enter") (e.target as HTMLElement).blur();
            if (e.key === "Escape") {
              setText(value);
              setTyping(false);
            }
          }}
        />
      </div>
    );
  }

  return (
    <div className="flex items-center gap-2">
      <button
        type="button"
        disabled={disabled}
        aria-label="Hotkey: click, then press the combination"
        onClick={() => {
          onError(null);
          setCapturing(true);
        }}
        onBlur={() => setCapturing(false)}
        onKeyDown={(e) => {
          if (!capturing) {
            if (e.key === "Enter" || e.key === " ") {
              e.preventDefault();
              onError(null);
              setCapturing(true);
            }
            return;
          }
          e.preventDefault();
          e.stopPropagation();
          const result = captureHotkey(e);
          if (result.kind === "pending") return;
          setCapturing(false);
          if (result.kind === "cancel") return;
          if (result.kind === "error") {
            onError(result.error);
            return;
          }
          onError(null);
          if (result.hotkey !== value) onCommit(result.hotkey);
        }}
        className={cls(
          "min-w-0 flex-1 rounded-md border px-2 py-1.5 text-left font-mono text-[13px] focus:outline-none disabled:opacity-60",
          capturing
            ? "border-sky-500 bg-sky-50 text-sky-800 ring-1 ring-sky-500 dark:bg-sky-950/40 dark:text-sky-300"
            : "border-neutral-300 bg-white text-neutral-900 hover:bg-neutral-50 dark:border-neutral-700 dark:bg-neutral-900 dark:text-neutral-100 dark:hover:bg-neutral-800",
        )}
      >
        {capturing ? "Press the combination… (Esc to cancel)" : formatHotkey(value) || "(none)"}
      </button>
      <Button
        size="sm"
        variant="ghost"
        disabled={disabled}
        title="Type the hotkey instead (for combinations the browser intercepts)"
        onClick={() => {
          setText(value);
          setTyping(true);
        }}
      >
        Type
      </Button>
    </div>
  );
}
