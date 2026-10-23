// The transcript view boundary: by default the conversation shows only events at or
// after the last manual "clear context" compaction (the divider itself stays as the top
// row), backward paging stops there, and a "Show full transcript" switch (persisted in
// localStorage) reveals the full history. The boundary keys on `meta.kind === "clear"`
// ONLY — summary_cutoff, fold and evict compactions are not boundaries.

import { describe, expect, it, vi } from "vitest";
import { api } from "./api";
import {
  SHOW_FULL_KEY,
  Store,
  lastClearId,
  loadShowFullTranscript,
  saveShowFullTranscript,
  sinceLastClear,
} from "./store";
import type { ConvEvent } from "./types";

const CID = "01J000000000000000000DESK";

function ev(id: number, kind: string, meta?: Record<string, unknown>): ConvEvent {
  return { id, conversation_id: CID, kind: kind as ConvEvent["kind"], content: "", meta: meta ?? {}, ts: "t" };
}

function memStorage(initial: Record<string, string> = {}) {
  const map = new Map(Object.entries(initial));
  return {
    getItem: (k: string) => map.get(k) ?? null,
    setItem: (k: string, v: string) => void map.set(k, v),
  };
}

describe("lastClearId", () => {
  it("finds the newest compaction with meta.kind === 'clear'", () => {
    const events = [ev(1, "user"), ev(2, "compaction", { kind: "clear" }), ev(3, "assistant"), ev(4, "compaction", { kind: "clear" })];
    expect(lastClearId(events)).toBe(4);
  });

  it("is 0 with no clear present", () => {
    expect(lastClearId([ev(1, "user"), ev(2, "assistant")])).toBe(0);
  });

  it("ignores fold and evict compactions (their events stay visible)", () => {
    const events = [ev(1, "user"), ev(2, "compaction", { kind: "fold" }), ev(3, "compaction", { kind: "evict" })];
    expect(lastClearId(events)).toBe(0);
  });

  it("ignores a compaction carrying summary_cutoff rather than kind=clear", () => {
    expect(lastClearId([ev(1, "compaction", { summary_cutoff: 5 })])).toBe(0);
  });

  it("ignores a non-compaction event that happens to carry kind=clear", () => {
    expect(lastClearId([ev(1, "system", { kind: "clear" })])).toBe(0);
  });
});

describe("sinceLastClear", () => {
  it("keeps the clear divider as the top row and hides everything older", () => {
    const events = [ev(1, "user"), ev(2, "assistant"), ev(3, "compaction", { kind: "clear" }), ev(4, "user"), ev(5, "assistant")];
    expect(sinceLastClear(events).map((e) => e.id)).toEqual([3, 4, 5]);
  });

  it("returns the transcript unchanged when there is no clear", () => {
    const events = [ev(1, "user"), ev(2, "assistant")];
    expect(sinceLastClear(events)).toBe(events);
  });

  it("cuts at the last clear, not the first", () => {
    const events = [ev(1, "compaction", { kind: "clear" }), ev(2, "user"), ev(3, "compaction", { kind: "clear" }), ev(4, "user")];
    expect(sinceLastClear(events).map((e) => e.id)).toEqual([3, 4]);
  });

  it("does not cut on fold or evict compactions", () => {
    const events = [ev(1, "user"), ev(2, "compaction", { kind: "fold" }), ev(3, "compaction", { kind: "evict" }), ev(4, "assistant")];
    expect(sinceLastClear(events).map((e) => e.id)).toEqual([1, 2, 3, 4]);
  });
});

describe("Show full transcript switch persistence", () => {
  it("defaults to off when unset", () => {
    expect(loadShowFullTranscript(memStorage())).toBe(false);
  });

  it("round-trips on and off", () => {
    const storage = memStorage();
    saveShowFullTranscript(true, storage);
    expect(loadShowFullTranscript(storage)).toBe(true);
    expect(storage.getItem(SHOW_FULL_KEY)).toBe("1");
    saveShowFullTranscript(false, storage);
    expect(loadShowFullTranscript(storage)).toBe(false);
  });

  it("reads a stored '1' from a plain object storage", () => {
    expect(loadShowFullTranscript({ getItem: (k) => (k === SHOW_FULL_KEY ? "1" : null), setItem: () => {} })).toBe(true);
  });
});

describe("loadEvents paging stops at the clear boundary", () => {
  it("does not fetch a page older than the last clear while the default view is active", async () => {
    const store = new Store();
    const eventsMock = vi.spyOn(api, "events").mockResolvedValue([ev(1, "user")]);

    store.set((s) => ({
      ...s,
      showFullTranscript: false,
      events: { ...s.events, [CID]: [ev(5, "compaction", { kind: "clear" }), ev(6, "user"), ev(7, "assistant")] },
    }));

    await store.loadEvents(CID, { before: 4, limit: 100 });
    expect(eventsMock).not.toHaveBeenCalled();

    await store.loadEvents(CID, { before: 7, limit: 100 });
    expect(eventsMock).toHaveBeenCalled();
    eventsMock.mockRestore();
  });

  it("fetches older pages when Show full transcript is on", async () => {
    const store = new Store();
    const eventsMock = vi.spyOn(api, "events").mockResolvedValue([ev(1, "user")]);

    store.set((s) => ({
      ...s,
      showFullTranscript: true,
      events: { ...s.events, [CID]: [ev(5, "compaction", { kind: "clear" }), ev(6, "user")] },
    }));

    await store.loadEvents(CID, { before: 4, limit: 100 });
    expect(eventsMock).toHaveBeenCalled();
    eventsMock.mockRestore();
  });

  it("a crossing page discovers the boundary and later pages stop there", async () => {
    const store = new Store();
    const eventsMock = vi
      .spyOn(api, "events")
      .mockResolvedValue([ev(3, "user"), ev(4, "compaction", { kind: "clear" }), ev(5, "assistant")]);

    store.set((s) => ({ ...s, showFullTranscript: false, events: { ...s.events, [CID]: [ev(6, "user")] } }));

    // First backward page crosses the boundary (the clear arrives inside it).
    await store.loadEvents(CID, { before: 6, limit: 100 });
    expect(eventsMock).toHaveBeenCalledTimes(1);
    expect(store.getState().events[CID].map((e) => e.id)).toEqual([3, 4, 5, 6]);
    // Partial page (3 < limit 100) also marks history complete; either way paging stops.
    expect(store.getState().historyComplete[CID]).toBe(true);

    // The next backward page must not be fetched: it is older than the boundary.
    await store.loadEvents(CID, { before: 3, limit: 100 });
    expect(eventsMock).toHaveBeenCalledTimes(1);
    eventsMock.mockRestore();
  });
});

describe("loadAllEvents backfills older pages when the switch turns on", () => {
  it("pages backward until the oldest event is loaded", async () => {
    const store = new Store();
    const eventsMock = vi.spyOn(api, "events").mockImplementation(async (_id, params) =>
      params && params.before === 5 ? [ev(1, "user"), ev(2, "assistant"), ev(3, "user"), ev(4, "assistant")] : [],
    );

    store.set((s) => ({
      ...s,
      showFullTranscript: true,
      historyComplete: { ...s.historyComplete, [CID]: false },
      events: { ...s.events, [CID]: [ev(5, "compaction", { kind: "clear" }), ev(6, "user")] },
    }));

    await store.loadAllEvents(CID);

    expect(eventsMock).toHaveBeenCalledTimes(1);
    expect(store.getState().events[CID].map((e) => e.id)).toEqual([1, 2, 3, 4, 5, 6]);
    expect(store.getState().historyComplete[CID]).toBe(true);

    // Already complete: no further fetch.
    await store.loadAllEvents(CID);
    expect(eventsMock).toHaveBeenCalledTimes(1);
    eventsMock.mockRestore();
  });
});
