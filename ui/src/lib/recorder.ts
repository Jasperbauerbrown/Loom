// Browser microphone capture for the composer's mic button (03 §13.1). Web Audio taps
// the mic through an AudioWorklet (a ScriptProcessor where worklets are missing or fail
// to load), collects mono float chunks at the context's native rate, and on stop hands
// them to `wav.ts`, which resamples to 16 kHz PCM16. No MediaRecorder: its codecs vary
// by browser, and Loom produces exactly one format.

import { chunksToWav } from "./wav";

export interface Recording {
  /** Stop capturing and return the clip as a 16 kHz mono PCM16 WAV. */
  stop(): Promise<Uint8Array>;
  /** Stop capturing and drop the clip. */
  cancel(): void;
}

/** Inlined so the worklet needs no separate built asset. Averages the input's channels
 *  and posts each 128-frame block to the main thread. */
const WORKLET_SOURCE = `
class LoomCapture extends AudioWorkletProcessor {
  process(inputs) {
    const input = inputs[0];
    if (input && input.length > 0 && input[0].length > 0) {
      const out = new Float32Array(input[0].length);
      for (const channel of input) for (let i = 0; i < out.length; i++) out[i] += channel[i];
      if (input.length > 1) for (let i = 0; i < out.length; i++) out[i] /= input.length;
      this.port.postMessage(out, [out.buffer]);
    }
    return true;
  }
}
registerProcessor("loom-capture", LoomCapture);
`;

/** Whether this page can reach a microphone at all; the reason when it cannot. */
export function micAvailability(): { ok: true } | { ok: false; reason: string } {
  if (typeof navigator === "undefined" || typeof window === "undefined") return { ok: false, reason: "no browser" };
  if (!window.isSecureContext) {
    return {
      ok: false,
      reason: "Browsers only allow the microphone on https or localhost; this page is plain http. Use the hotkey listener, or open Loom at localhost.",
    };
  }
  if (!navigator.mediaDevices?.getUserMedia) return { ok: false, reason: "This browser has no microphone access." };
  const Ctx = window.AudioContext ?? (window as any).webkitAudioContext;
  if (!Ctx) return { ok: false, reason: "This browser has no Web Audio." };
  return { ok: true };
}

function micError(e: unknown): Error {
  const name = (e as DOMException)?.name;
  if (name === "NotAllowedError" || name === "SecurityError") return new Error("Microphone permission was denied.");
  if (name === "NotFoundError" || name === "OverconstrainedError") return new Error("No microphone found.");
  if (name === "NotReadableError") return new Error("The microphone is in use by another application.");
  return new Error(`Could not open the microphone: ${(e as Error)?.message ?? String(e)}`);
}

export async function startRecording(): Promise<Recording> {
  const available = micAvailability();
  if (!available.ok) throw new Error(available.reason);

  let stream: MediaStream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });
  } catch (e) {
    throw micError(e);
  }

  const Ctx: typeof AudioContext = window.AudioContext ?? (window as any).webkitAudioContext;
  const ctx = new Ctx();
  const chunks: Float32Array[] = [];
  const source = ctx.createMediaStreamSource(stream);
  // Capture nodes only run while connected to the destination; a muted gain keeps the
  // mic out of the speakers.
  const mute = ctx.createGain();
  mute.gain.value = 0;
  mute.connect(ctx.destination);

  let tap: AudioNode;
  try {
    if (!ctx.audioWorklet) throw new Error("no AudioWorklet");
    const url = URL.createObjectURL(new Blob([WORKLET_SOURCE], { type: "application/javascript" }));
    try {
      await ctx.audioWorklet.addModule(url);
    } finally {
      URL.revokeObjectURL(url);
    }
    const node = new AudioWorkletNode(ctx, "loom-capture", { numberOfInputs: 1, numberOfOutputs: 1 });
    node.port.onmessage = (msg) => chunks.push(msg.data as Float32Array);
    tap = node;
  } catch {
    const node = ctx.createScriptProcessor(4096, 1, 1);
    node.onaudioprocess = (e) => chunks.push(new Float32Array(e.inputBuffer.getChannelData(0)));
    tap = node;
  }
  source.connect(tap);
  tap.connect(mute);
  if (ctx.state === "suspended") await ctx.resume().catch(() => undefined);

  let done = false;
  const teardown = () => {
    if (done) return;
    done = true;
    try {
      source.disconnect();
      tap.disconnect();
      mute.disconnect();
    } catch {
      /* already disconnected */
    }
    if (tap instanceof AudioWorkletNode) tap.port.onmessage = null;
    for (const track of stream.getTracks()) track.stop();
    void ctx.close().catch(() => undefined);
  };

  return {
    async stop() {
      const rate = ctx.sampleRate;
      teardown();
      return chunksToWav(chunks, rate);
    },
    cancel() {
      teardown();
      chunks.length = 0;
    },
  };
}
