// The voice hotkey string (03 §5.7 / §13.2): `+`-joined, lowercase, modifiers in the
// fixed order ctrl, alt, shift, win, then exactly one key. The capture field turns a
// keydown into that string; the listener parses the same grammar. Keys are read from
// `KeyboardEvent.code` (the physical key), so the layout and Shift never change the name.

export const MODIFIERS = ["ctrl", "alt", "shift", "win"] as const;

/** Named non-character keys, by `KeyboardEvent.code` — exactly the listener's
 *  `NAMED_KEYS` (loom/voice/listener.py). Escape is absent on purpose: it cancels a
 *  recording and cancels the capture field. */
const NAMED: Record<string, string> = {
  Space: "space",
  Enter: "enter",
  NumpadEnter: "enter",
  Tab: "tab",
  Backspace: "backspace",
  Insert: "insert",
  Delete: "delete",
  Home: "home",
  End: "end",
  PageUp: "pageup",
  PageDown: "pagedown",
  ArrowUp: "up",
  ArrowDown: "down",
  ArrowLeft: "left",
  ArrowRight: "right",
  Pause: "pause",
  PrintScreen: "printscreen",
  ScrollLock: "scrolllock",
};

/** Keys that type something: bare, they would stop you typing them anywhere else
 *  (the listener's rule: single characters and `TYPING_KEYS`). */
function typesSomething(key: string): boolean {
  return key.length === 1 || ["space", "enter", "tab", "backspace"].includes(key);
}

const MODIFIER_CODES = new Set([
  "ControlLeft",
  "ControlRight",
  "AltLeft",
  "AltRight",
  "ShiftLeft",
  "ShiftRight",
  "MetaLeft",
  "MetaRight",
  "OSLeft",
  "OSRight",
]);

/** The key name for a physical key, or null when it cannot be a hotkey's main key. */
export function keyName(code: string): string | null {
  let m = /^Key([A-Z])$/.exec(code);
  if (m) return m[1].toLowerCase();
  m = /^Digit([0-9])$/.exec(code);
  if (m) return m[1];
  m = /^Numpad([0-9])$/.exec(code);
  if (m) return `numpad${m[1]}`;
  m = /^F([1-9]|1[0-9]|2[0-4])$/.exec(code);
  if (m) return `f${m[1]}`;
  return NAMED[code] ?? null;
}

export interface KeyLike {
  code: string;
  ctrlKey: boolean;
  altKey: boolean;
  shiftKey: boolean;
  metaKey: boolean;
}

export type CaptureResult =
  | { kind: "pending" } // only modifiers so far: keep listening
  | { kind: "cancel" } // Escape
  | { kind: "error"; error: string }
  | { kind: "hotkey"; hotkey: string };

/** One keydown in the capture field → what it means. A bare letter, digit, Space,
 *  Enter, Tab or Backspace would fire while typing in every other app, so those need a
 *  modifier; F-keys, the numpad and the navigation keys may stand alone. */
export function captureHotkey(e: KeyLike): CaptureResult {
  if (e.code === "Escape") return { kind: "cancel" };
  if (MODIFIER_CODES.has(e.code)) return { kind: "pending" };
  const key = keyName(e.code);
  if (!key) return { kind: "error", error: `${e.code || "that key"} cannot be a hotkey` };
  const mods = [e.ctrlKey && "ctrl", e.altKey && "alt", e.shiftKey && "shift", e.metaKey && "win"].filter(
    Boolean,
  ) as string[];
  if (mods.length === 0 && typesSomething(key)) {
    return { kind: "error", error: `add a modifier (Ctrl, Alt, Shift or Win): on its own ${key} would stop typing it anywhere` };
  }
  return { kind: "hotkey", hotkey: [...mods, key].join("+") };
}

/** "ctrl+alt+space" → "Ctrl + Alt + Space", for display only. */
export function formatHotkey(hotkey: string): string {
  if (!hotkey) return "";
  return hotkey
    .split("+")
    .filter(Boolean)
    .map((part) => {
      if (part === "win") return "Win";
      if (part === "pageup") return "PgUp";
      if (part === "pagedown") return "PgDn";
      if (/^f\d+$/.test(part)) return part.toUpperCase();
      if (part.startsWith("numpad")) return `Num ${part.slice(6)}`;
      return part.charAt(0).toUpperCase() + part.slice(1);
    })
    .join(" + ");
}
