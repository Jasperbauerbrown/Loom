// Pure helpers for the voice listener's status card (03 §13.2, Settings › Voice).

import type { VoiceStatus } from "./types";

export type Tone = "neutral" | "green" | "amber" | "red" | "sky";

/** One badge for the listener: disabled, not running, an error, or its own state. */
export function voiceBadge(status: VoiceStatus | null): { label: string; tone: Tone; title: string } {
  if (!status) return { label: "unknown", tone: "neutral", title: "No status from the backend yet." };
  if (!status.enabled || status.state === "disabled") {
    return { label: "off", tone: "neutral", title: "Voice input is turned off." };
  }
  if (status.error) return { label: status.state || "error", tone: "red", title: status.error };
  if (status.running === false) {
    return { label: "not running", tone: "amber", title: "The listener process is not running; the backend restarts it with backoff." };
  }
  switch (status.state) {
    case "starting":
      return { label: "starting", tone: "neutral", title: "The listener is starting." };
    case "stopped":
      return { label: "stopped", tone: "amber", title: "The listener has not reported, or has stopped." };
    case "recording":
      return { label: "recording", tone: "sky", title: "Recording a clip." };
    case "transcribing":
      return { label: status.state, tone: "sky", title: "Sending the clip for transcription." };
    case "unsupported":
      return { label: "unsupported", tone: "amber", title: "The hotkey listener runs on Windows only; the composer mic works everywhere." };
    case "idle":
      return { label: "idle", tone: "green", title: "Waiting for the hotkey." };
    default:
      return { label: status.state || "unknown", tone: "neutral", title: status.state };
  }
}
