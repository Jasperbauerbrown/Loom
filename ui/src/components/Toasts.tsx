// Toast stack: API errors and `notification` bus events. Notifications stay until
// dismissed (they mirror an item on the now board); errors fade after 8s.

import { useEffect } from "react";
import { store, useStore } from "../lib/store";
import { navigate } from "../lib/router";
import { cls } from "../lib/format";
import { Button } from "./ui";
import { api } from "../lib/api";

export function Toasts() {
  const toasts = useStore((s) => s.toasts, (a, b) => a === b);

  useEffect(() => {
    const timers = toasts
      .filter((t) => t.kind !== "notification")
      .map((t) => setTimeout(() => store.dismissToast(t.id), 8000));
    return () => timers.forEach(clearTimeout);
  }, [toasts]);

  if (toasts.length === 0) return null;
  return (
    <div className="pointer-events-none fixed right-4 bottom-4 z-[60] flex w-80 flex-col gap-2">
      {toasts.map((t) => (
        <div
          key={t.id}
          className={cls(
            "pointer-events-auto rounded-lg border p-3 shadow-lg",
            t.kind === "error"
              ? "border-red-300 bg-red-50 text-red-900 dark:border-red-900 dark:bg-red-950 dark:text-red-200"
              : "border-neutral-200 bg-white text-neutral-900 dark:border-neutral-700 dark:bg-neutral-900 dark:text-neutral-100",
          )}
        >
          <div className="flex items-start justify-between gap-2">
            <div className="min-w-0">
              <div className="text-[13px] font-medium break-words">{t.title}</div>
              {t.body ? <div className="mt-0.5 text-[12px] break-words opacity-80">{t.body}</div> : null}
            </div>
            <button
              className="shrink-0 opacity-60 hover:opacity-100"
              onClick={() => store.dismissToast(t.id)}
              aria-label="Dismiss"
            >
              ✕
            </button>
          </div>
          {t.kind === "notification" ? (
            <div className="mt-2 flex gap-2">
              {t.conversation_id ? (
                <Button
                  size="sm"
                  onClick={() => {
                    navigate(`/c/${t.conversation_id}`);
                    store.dismissToast(t.id);
                  }}
                >
                  Open
                </Button>
              ) : null}
              {t.item_id ? (
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() => {
                    void store.guard(() => api.seenItem(t.item_id!));
                    store.dismissToast(t.id);
                  }}
                >
                  Mark seen
                </Button>
              ) : null}
            </div>
          ) : null}
        </div>
      ))}
    </div>
  );
}
