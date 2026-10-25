import { describe, expect, it } from "vitest";
import { voiceBadge } from "./voice";
import { roleRows } from "./roles";
import type { VoiceStatus } from "./types";

const status = (over: Partial<VoiceStatus> = {}): VoiceStatus => ({
  enabled: true,
  running: true,
  pid: 42,
  state: "idle",
  hotkey: "ctrl+alt+space",
  mode: "hold",
  error: null,
  devices: [],
  device: "",
  updated_at: null,
  ...over,
});

describe("voiceBadge", () => {
  it("covers off, unknown, error, not running and live states", () => {
    expect(voiceBadge(null).label).toBe("unknown");
    expect(voiceBadge(status({ enabled: false, error: "x" }))).toMatchObject({ label: "off", tone: "neutral" });
    expect(voiceBadge(status({ state: "error", error: "hotkey ctrl+alt+space is taken" }))).toMatchObject({
      tone: "red",
      title: "hotkey ctrl+alt+space is taken",
    });
    expect(voiceBadge(status({ running: false }))).toMatchObject({ label: "not running", tone: "amber" });
    expect(voiceBadge(status({ state: "recording" })).tone).toBe("sky");
    expect(voiceBadge(status({ state: "idle" })).tone).toBe("green");
    expect(voiceBadge(status({ state: "unsupported" })).tone).toBe("amber");
    expect(voiceBadge(status({ state: "disabled" })).label).toBe("off");
    expect(voiceBadge(status({ state: "stopped" })).tone).toBe("amber");
    expect(voiceBadge(status({ state: "transcribing" })).tone).toBe("sky");
    expect(voiceBadge(status({ state: "something-new" }))).toMatchObject({ label: "something-new", tone: "neutral" });
  });
});

describe("roleRows", () => {
  it("lists known roles in order, then any the backend adds", () => {
    const rows = roleRows(["embedding", "desk", "future", "transcription", "worker"].map((role) => ({ role })));
    expect(rows).toEqual(["desk", "worker", "embedding", "transcription", "future"]);
  });

  it("falls back to the known roles before the list loads", () => {
    expect(roleRows([])).toContain("transcription");
  });
});
