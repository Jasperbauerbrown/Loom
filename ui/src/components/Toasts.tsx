// Toast stack: API errors and `notification` bus events. A notification mirrors an
// item on the now board — it fades after `general.notification_fade_seconds` (0 keeps
// it until dismissed) and disappears outright once the item is seen or dismissed
// (store.ts reducer). Errors and other one-off toasts always fade after 8s. One timer
// mechanism (a per-toast countdown, pausable on hover), two durations.

import { useEffect, useRef, useState } from "react";
import { store, useStore } from "../lib/store";
import { navigate } from "../lib/router";
import { cls } from "../lib/format";
import { Button } from "./ui";
import { api } from "../lib/api";

const FADE_TRANSITION_MS = 300;
const DEFAULT_FADE_S = 6;
const FIXED_FADE_S = 8; // errors and info toasts

interface Timer {
  timeoutId: ReturnType<typeof setTimeout>;
  deadline: number; // ms epoch when the fade is due to start
  remainingMs: number;
  paused: boolean;
}

export function Toasts() {
  const toasts = useStore((s) => s.toasts, (a, b) => a === b);
  const fadeSeconds = useStore(
    (s) => s.settings?.values["general.notification_fade_seconds"] as number | undefined,
    (a, b) => a === b,
  );
  const [fading, setFading] = useState<Set<number>>(() => new Set());
  const timers = useRef<Record<number, Timer>>({});

  const durationMs = (kind: string) => {
    if (kind === "notification") {
      const secs = fadeSeconds ?? DEFAULT_FADE_S;
      return secs > 0 ? secs * 1000 : 0;
    }
    return FIXED_FADE_S * 1000;
  };

  const beginFade = (id: number) => {
    setFading((prev) => {
      const next = new Set(prev);
      next.add(id);
      return next;
    });
    setTimeout(() => store.dismissToast(id), FADE_TRANSITION_MS);
  };

  const schedule = (id: number, remaining: number) => {
    const timeoutId = setTimeout(() => beginFade(id), remaining);
    timers.current[id] = { timeoutId, deadline: Date.now() + remaining, remainingMs: remaining, paused: false };
  };

  useEffect(() => {
    const live = new Set(toasts.map((t) => t.id));
    for (const key of Object.keys(timers.current)) {
      const id = Number(key);
      if (!live.has(id)) {
        clearTimeout(timers.current[id].timeoutId);
        delete timers.current[id];
      }
    }
    for (const t of toasts) {
      if (timers.current[t.id]) continue;
      const ms = durationMs(t.kind);
      if (ms > 0) schedule(t.id, ms);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [toasts, fadeSeconds]);

  useEffect(() => {
    const live = timers.current;
    return () => {
      Object.values(live).forEach((t) => clearTimeout(t.timeoutId));
    };
  }, []);

  const pause = (id: number) => {
    const timer = timers.current[id];
    if (!timer || timer.paused) return;
    clearTimeout(timer.timeoutId);
    timer.remainingMs = Math.max(0, timer.deadline - Date.now());
    timer.paused = true;
  };

  const resume = (id: number) => {
    const timer = timers.current[id];
    if (!timer || !timer.paused) return;
    timer.paused = false;
    schedule(id, timer.remainingMs);
  };

  if (toasts.length === 0) return null;
  return (
    <div className="pointer-events-none fixed right-4 bottom-4 z-[60] flex w-80 flex-col gap-2">
      {toasts.map((t) => (
        <div
          key={t.id}
          onMouseEnter={() => pause(t.id)}
          onMouseLeave={() => resume(t.id)}
          className={cls(
            "pointer-events-auto rounded-lg border p-3 shadow-lg transition-opacity duration-300",
            fading.has(t.id) ? "opacity-0" : "opacity-100",
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
