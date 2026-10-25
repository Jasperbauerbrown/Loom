// The composer's mic button (03 §13.1): click to record, click again to stop; the clip
// is transcribed by the transcription role and the text lands in this conversation's
// draft (never sent on its own — the person reads it first). Recording shows a timer and
// a cancel; Esc cancels too. The clip is capped at `voice.max_seconds`.

import { useEffect, useRef, useState } from "react";
import { api } from "../lib/api";
import { store, useStore } from "../lib/store";
import { micAvailability, startRecording, type Recording } from "../lib/recorder";
import { clipTime } from "../lib/wav";
import { cls } from "../lib/format";
import { Button, Spinner } from "./ui";
import { IconMic } from "./Icons";

type Phase = "idle" | "starting" | "recording" | "transcribing";

const DEFAULT_MAX_SECONDS = 120;

export function MicButton({ conversationId, onText }: { conversationId: string; onText?: () => void }) {
  const maxSeconds = useStore(
    (s) => (s.settings?.values["voice.max_seconds"] as number | undefined) ?? DEFAULT_MAX_SECONDS,
    Object.is,
  );
  const [phase, setPhase] = useState<Phase>("idle");
  const [elapsed, setElapsed] = useState(0);
  const recording = useRef<Recording | null>(null);
  const target = useRef(conversationId); // the clip belongs to where it was started
  const available = micAvailability();

  // Leaving the conversation (or the screen) mid-clip drops it.
  useEffect(
    () => () => {
      recording.current?.cancel();
      recording.current = null;
    },
    [],
  );

  const cancel = () => {
    recording.current?.cancel();
    recording.current = null;
    setPhase("idle");
  };

  const finish = async () => {
    const rec = recording.current;
    if (!rec) return;
    recording.current = null;
    setPhase("transcribing");
    try {
      const wav = await rec.stop();
      const res = await api.uploadAudio(target.current, new Blob([wav as BlobPart], { type: "audio/wav" }), "draft");
      if (res.dropped || !res.text.trim()) {
        store.toast("info", "Nothing heard", "The clip had no speech to transcribe.");
      } else {
        store.applyVoiceDraft(target.current, res.audio_id, res.text);
        onText?.();
      }
    } catch (e) {
      // 409 (no transcription model) and 502 (transcription failed) carry the backend's
      // own words; a failed clip also leaves a Retry in the transcript.
      store.error(e);
    } finally {
      setPhase("idle");
    }
  };

  const start = async () => {
    target.current = conversationId;
    setPhase("starting");
    try {
      recording.current = await startRecording();
      setElapsed(0);
      setPhase("recording");
    } catch (e) {
      store.error(e);
      setPhase("idle");
    }
  };

  // Timer + cap + Esc while recording.
  useEffect(() => {
    if (phase !== "recording") return;
    const began = Date.now();
    const tick = setInterval(() => {
      const ms = Date.now() - began;
      setElapsed(ms);
      if (ms >= maxSeconds * 1000) void finish();
    }, 250);
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        cancel();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => {
      clearInterval(tick);
      window.removeEventListener("keydown", onKey);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [phase, maxSeconds]);

  if (phase === "recording") {
    return (
      <div className="flex shrink-0 items-center gap-1">
        <Button
          variant="default"
          className="border-sky-500 text-sky-700 dark:border-sky-500 dark:text-sky-400"
          title="Stop and transcribe into the draft"
          aria-label="Stop recording"
          onClick={() => void finish()}
        >
          <span className="h-2.5 w-2.5 animate-pulse rounded-full bg-sky-600 dark:bg-sky-400" aria-hidden="true" />
          <span className="font-mono text-[12px] tabular-nums">{clipTime(elapsed)}</span>
        </Button>
        <button
          type="button"
          aria-label="Cancel recording"
          title="Cancel (Esc)"
          onClick={cancel}
          className="rounded px-1 text-[13px] leading-none text-neutral-500 hover:bg-neutral-200 hover:text-neutral-900 dark:hover:bg-neutral-700 dark:hover:text-neutral-100"
        >
          ×
        </button>
      </div>
    );
  }

  const busy = phase !== "idle";
  return (
    <Button
      variant="ghost"
      className={cls("shrink-0", phase === "transcribing" && "text-sky-700 dark:text-sky-400")}
      disabled={busy || !available.ok}
      title={
        !available.ok
          ? available.reason
          : phase === "transcribing"
            ? "Transcribing…"
            : "Dictate into the draft (click to start, click again to stop)"
      }
      aria-label={phase === "transcribing" ? "Transcribing" : "Record voice"}
      onClick={() => void start()}
    >
      {busy ? <Spinner /> : <IconMic />}
    </Button>
  );
}
