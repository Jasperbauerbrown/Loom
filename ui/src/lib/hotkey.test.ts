import { describe, expect, it } from "vitest";
import { captureHotkey, formatHotkey, keyName, type KeyLike } from "./hotkey";

const key = (code: string, mods: Partial<KeyLike> = {}): KeyLike => ({
  code,
  ctrlKey: false,
  altKey: false,
  shiftKey: false,
  metaKey: false,
  ...mods,
});

describe("keyName", () => {
  it("names letters, digits, function keys and named keys by physical code", () => {
    expect(keyName("KeyA")).toBe("a");
    expect(keyName("Digit7")).toBe("7");
    expect(keyName("Numpad3")).toBe("numpad3");
    expect(keyName("F1")).toBe("f1");
    expect(keyName("F24")).toBe("f24");
    expect(keyName("Space")).toBe("space");
    expect(keyName("PageDown")).toBe("pagedown");
    expect(keyName("ArrowLeft")).toBe("left");
    expect(keyName("NumpadEnter")).toBe("enter");
  });

  it("refuses keys that cannot be a main key", () => {
    expect(keyName("F25")).toBeNull();
    expect(keyName("Escape")).toBeNull();
    expect(keyName("Minus")).toBeNull(); // not in the listener's NAMED_KEYS
    expect(keyName("ControlLeft")).toBeNull();
    expect(keyName("")).toBeNull();
  });
});

describe("captureHotkey", () => {
  it("joins modifiers in the fixed order ctrl, alt, shift, win", () => {
    expect(captureHotkey(key("Space", { ctrlKey: true, altKey: true }))).toEqual({
      kind: "hotkey",
      hotkey: "ctrl+alt+space",
    });
    expect(captureHotkey(key("KeyD", { metaKey: true, shiftKey: true, ctrlKey: true, altKey: true }))).toEqual({
      kind: "hotkey",
      hotkey: "ctrl+alt+shift+win+d",
    });
  });

  it("names the physical key, so Shift does not change it", () => {
    expect(captureHotkey(key("Digit1", { shiftKey: true, altKey: true }))).toEqual({
      kind: "hotkey",
      hotkey: "alt+shift+1",
    });
  });

  it("keeps waiting while only modifiers are down", () => {
    expect(captureHotkey(key("ControlLeft", { ctrlKey: true }))).toEqual({ kind: "pending" });
    expect(captureHotkey(key("MetaRight", { metaKey: true }))).toEqual({ kind: "pending" });
  });

  it("cancels on Escape", () => {
    expect(captureHotkey(key("Escape", { ctrlKey: true }))).toEqual({ kind: "cancel" });
  });

  it("needs a modifier only for keys that type something", () => {
    expect(captureHotkey(key("KeyA")).kind).toBe("error");
    expect(captureHotkey(key("Digit5")).kind).toBe("error");
    expect(captureHotkey(key("Space")).kind).toBe("error");
    expect(captureHotkey(key("Enter")).kind).toBe("error");
    expect(captureHotkey(key("F13"))).toEqual({ kind: "hotkey", hotkey: "f13" });
    expect(captureHotkey(key("Pause"))).toEqual({ kind: "hotkey", hotkey: "pause" });
    expect(captureHotkey(key("Numpad0"))).toEqual({ kind: "hotkey", hotkey: "numpad0" });
  });

  it("reports a key it cannot name", () => {
    expect(captureHotkey(key("IntlRo", { ctrlKey: true })).kind).toBe("error");
  });
});

describe("formatHotkey", () => {
  it("renders a readable label", () => {
    expect(formatHotkey("ctrl+alt+space")).toBe("Ctrl + Alt + Space");
    expect(formatHotkey("win+shift+f13")).toBe("Win + Shift + F13");
    expect(formatHotkey("ctrl+pagedown")).toBe("Ctrl + PgDn");
    expect(formatHotkey("alt+numpad5")).toBe("Alt + Num 5");
    expect(formatHotkey("")).toBe("");
  });
});
