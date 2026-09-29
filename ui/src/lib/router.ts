// A ~40-line hash router. Routes are `#/c/<id>`, `#/threads`, `#/overview`,
// `#/vault/<path>`, `#/graph/<path>`, `#/approvals`, `#/settings/<group>`, `#/system`;
// the desk is `#/`.

import { useSyncExternalStore } from "react";

export interface Route {
  name: "desk" | "conversation" | "threads" | "overview" | "vault" | "graph" | "approvals" | "settings" | "system";
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
    case "overview":
      return { name: "overview", param: "" };
    case "vault":
      return { name: "vault", param: decodeURIComponent(param) };
    case "graph":
      return { name: "graph", param: decodeURIComponent(param) };
    case "approvals":
      return { name: "approvals", param: "" };
    case "settings":
      return { name: "settings", param: param || "general" };
    case "system":
      return { name: "system", param: "" };
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

export function graphPath(path: string): string {
  return href(`/graph/${path.split("/").map(encodeURIComponent).join("/")}`);
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
