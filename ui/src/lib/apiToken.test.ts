import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { answerTokenPrompt, api, onTokenPrompt, setToken, token } from "./api";

function stubStorage() {
  const mem = new Map<string, string>();
  vi.stubGlobal("localStorage", {
    getItem: (k: string) => mem.get(k) ?? null,
    setItem: (k: string, v: string) => void mem.set(k, v),
    removeItem: (k: string) => void mem.delete(k),
  });
}

describe("401 token prompt", () => {
  beforeEach(() => {
    stubStorage();
    setToken(null);
  });
  afterEach(() => vi.unstubAllGlobals());

  it("prompts once for many failures, stores the token and retries them all", async () => {
    const fetchMock = vi.fn(async (_url: string, init: RequestInit) => {
      const auth = (init.headers as Record<string, string>)["Authorization"];
      if (auth === "Bearer s3cret") return new Response(JSON.stringify({ ok: true }), { status: 200 });
      return new Response(JSON.stringify({ detail: "missing or invalid bearer token" }), { status: 401 });
    });
    vi.stubGlobal("fetch", fetchMock);
    const opens: boolean[] = [];
    const off = onTokenPrompt((o) => opens.push(o));

    const calls = Promise.all([api.request("GET", "/a"), api.request("GET", "/b"), api.request("GET", "/c")]);
    await vi.waitFor(() => expect(opens).toEqual([true]));
    answerTokenPrompt("s3cret");
    const results = await calls;

    expect(results).toEqual([{ ok: true }, { ok: true }, { ok: true }]);
    expect(opens).toEqual([true, false]);
    expect(token()).toBe("s3cret");
    off();
  });

  it("cancelling the prompt surfaces the 401", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ detail: "no" }), { status: 401 })));
    const off = onTokenPrompt((o) => o || undefined);
    const call = api.request("GET", "/a");
    await vi.waitFor(() => undefined);
    await new Promise((r) => setTimeout(r, 0));
    answerTokenPrompt(null);
    await expect(call).rejects.toMatchObject({ status: 401 });
    off();
  });
});
