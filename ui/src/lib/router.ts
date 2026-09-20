// A ~40-line hash router. Routes are `#/c/<id>`, `#/threads`, `#/vault/<path>`,
// `#/approvals`, `#/settings/<group>`, `#/system`, `#/setup`; the desk is `#/`.

import { useSyncExternalStore } from "react";

export interface Route {
  name: "desk" | "conversation" | "threads" | "vault" | "approvals" | "settings" | "system" | "setup";
  param: string;
}

const listeners = new Set<() => void>();
let current = parse(location.hash);

function parse(hash: string): Route {
  const raw = hash.replace(/^#\/?/, "");
  const [head, ...rest] = raw.split("/");
  const param = rest.join("/");
  switch (head) {
    case "c":
      return { name: "conversation", param: decodeURIComponent(param) };
    case "threads":
      return { name: "threads", param: decodeURIComponent(param) };
    case "vault":
      return { name: "vault", param: decodeURIComponent(param) };
    case "approvals":
      return { name: "approvals", param: "" };
    case "settings":
      return { name: "settings", param: param || "general" };
    case "system":
      return { name: "system", param: "" };
    case "setup":
      return { name: "setup", param: "" };
    default:
      return { name: "desk", param: "" };
  }
}

function onHashChange() {
  current = parse(location.hash);
  for (const fn of listeners) fn();
}

if (typeof window !== "undefined") window.addEventListener("hashchange", onHashChange);

export function navigate(path: string) {
  const next = path.startsWith("#") ? path : `#${path.startsWith("/") ? path : `/${path}`}`;
  if (location.hash === next) return;
  location.hash = next;
}

export function href(path: string): string {
  return path.startsWith("#") ? path : `#${path.startsWith("/") ? path : `/${path}`}`;
}

export function notePath(path: string): string {
  return href(`/vault/${path.split("/").map(encodeURIComponent).join("/")}`);
}

export function useRoute(): Route {
  return useSyncExternalStore(
    (fn) => {
      listeners.add(fn);
      return () => listeners.delete(fn);
    },
    () => current,
    () => current,
  );
}
