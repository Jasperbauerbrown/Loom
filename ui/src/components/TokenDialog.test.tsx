// @vitest-environment jsdom
// Regression: the token dialog's password input must keep focus across re-renders.
// Dialog used to depend on the parent's inline `onClose` closure, so its effect re-ran
// on every render and re-focused the dialog container — on mobile that dismissed the
// soft keyboard on every keystroke. Runs in jsdom (the only DOM-env test); everything
// else stays in the node environment.

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { api, answerTokenPrompt, setToken, tokenPromptOpen } from "../lib/api";
import { TokenDialog } from "./TokenDialog";

declare global {
  // eslint-disable-next-line no-var
  var IS_REACT_ACT_ENVIRONMENT: boolean;
}

let container: HTMLDivElement;
let root: Root;

function tokenInput(): HTMLInputElement {
  const el = container.querySelector<HTMLInputElement>('input[aria-label="LAN token"]');
  if (!el) throw new Error("token input not rendered");
  return el;
}

async function render() {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  await act(async () => {
    root.render(<TokenDialog />);
  });
}

/** Trigger a 401 so the shared prompt opens and the dialog mounts. */
async function openVia401() {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => new Response(JSON.stringify({ detail: "no" }), { status: 401 })),
  );
  await act(async () => {
    void api.request("GET", "/ping").catch(() => undefined);
    await new Promise((r) => setTimeout(r, 0));
  });
}

/** Type into a controlled input the way a browser does: native setter + input event. */
async function type(value: string) {
  const input = tokenInput();
  await act(async () => {
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
    setter?.call(input, value);
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
}

beforeAll(() => {
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
});

describe("TokenDialog focus", () => {
  beforeEach(async () => {
    setToken(null);
    await render();
    await openVia401();
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
    if (tokenPromptOpen()) answerTokenPrompt(null); // listeners are gone by now
    vi.unstubAllGlobals();
  });

  it("keeps focus on the password input across keystrokes", async () => {
    const input = tokenInput();
    expect(document.activeElement).toBe(input);

    await type("ab");
    expect(document.activeElement).toBe(input);
    expect(input.value).toBe("ab");

    await type("abc");
    expect(document.activeElement).toBe(input);
    expect(input.value).toBe("abc");
  });

  it("does not move focus to the dialog container while the input is focused", async () => {
    const input = tokenInput();
    const dialogEl = container.querySelector('[role="dialog"]');
    expect(dialogEl).toBeTruthy();

    await type("xy");

    expect(document.activeElement).toBe(input);
    expect(document.activeElement).not.toBe(dialogEl);
  });

  it("still closes on Escape via the latest onClose closure", async () => {
    await type("zz");
    await act(async () => {
      document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
      await new Promise((r) => setTimeout(r, 0));
    });

    expect(tokenPromptOpen()).toBe(false);
    expect(container.querySelector('[role="dialog"]')).toBeNull();
  });
});