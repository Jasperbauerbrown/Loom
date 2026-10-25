// Audio encoding for voice input (03 §13.1): whatever rate the browser captures at is
// mixed to mono, resampled to 16 kHz and written as a PCM16 WAV — the one format Loom
// produces (the listener writes the same). Pure functions; no Web Audio here.

export const TARGET_RATE = 16000;

/** Average N channels into one. One channel is returned as-is. */
export function mixToMono(channels: Float32Array[]): Float32Array {
  if (channels.length === 0) return new Float32Array(0);
  if (channels.length === 1) return channels[0];
  const length = Math.min(...channels.map((c) => c.length));
  const out = new Float32Array(length);
  for (const channel of channels) for (let i = 0; i < length; i++) out[i] += channel[i];
  for (let i = 0; i < length; i++) out[i] /= channels.length;
  return out;
}

/** Concatenate captured chunks into one buffer. */
export function concat(chunks: Float32Array[]): Float32Array {
  let length = 0;
  for (const c of chunks) length += c.length;
  const out = new Float32Array(length);
  let offset = 0;
  for (const c of chunks) {
    out.set(c, offset);
    offset += c.length;
  }
  return out;
}

/** Resample `input` from `fromRate` to `toRate`. Downsampling averages each output
 *  sample's source window (a box filter: enough anti-aliasing for speech); upsampling,
 *  which a real microphone never needs, interpolates linearly. */
export function resample(input: Float32Array, fromRate: number, toRate: number = TARGET_RATE): Float32Array {
  if (!(fromRate > 0) || !(toRate > 0)) throw new Error(`bad sample rate ${fromRate} → ${toRate}`);
  if (fromRate === toRate || input.length === 0) return input;
  const ratio = fromRate / toRate;
  const length = Math.floor(input.length / ratio);
  const out = new Float32Array(length);
  if (ratio > 1) {
    for (let i = 0; i < length; i++) {
      const start = Math.floor(i * ratio);
      const end = Math.min(input.length, Math.max(start + 1, Math.floor((i + 1) * ratio)));
      let sum = 0;
      for (let j = start; j < end; j++) sum += input[j];
      out[i] = sum / (end - start);
    }
  } else {
    for (let i = 0; i < length; i++) {
      const pos = i * ratio;
      const j = Math.floor(pos);
      const next = Math.min(j + 1, input.length - 1);
      out[i] = input[j] + (input[next] - input[j]) * (pos - j);
    }
  }
  return out;
}

/** Float samples in [-1, 1] to signed 16-bit, clipped. */
export function toPcm16(samples: Float32Array): Int16Array {
  const out = new Int16Array(samples.length);
  for (let i = 0; i < samples.length; i++) {
    const s = Math.max(-1, Math.min(1, samples[i]));
    out[i] = s < 0 ? Math.round(s * 0x8000) : Math.round(s * 0x7fff);
  }
  return out;
}

/** A canonical 44-byte-header RIFF/WAVE file: PCM, mono, 16-bit. */
export function encodeWav(pcm: Int16Array, sampleRate: number = TARGET_RATE): Uint8Array {
  const dataBytes = pcm.length * 2;
  const buffer = new ArrayBuffer(44 + dataBytes);
  const view = new DataView(buffer);
  const ascii = (offset: number, text: string) => {
    for (let i = 0; i < text.length; i++) view.setUint8(offset + i, text.charCodeAt(i));
  };
  ascii(0, "RIFF");
  view.setUint32(4, 36 + dataBytes, true);
  ascii(8, "WAVE");
  ascii(12, "fmt ");
  view.setUint32(16, 16, true); // fmt chunk size
  view.setUint16(20, 1, true); // PCM
  view.setUint16(22, 1, true); // mono
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * 2, true); // byte rate
  view.setUint16(32, 2, true); // block align
  view.setUint16(34, 16, true); // bits per sample
  ascii(36, "data");
  view.setUint32(40, dataBytes, true);
  for (let i = 0; i < pcm.length; i++) view.setInt16(44 + i * 2, pcm[i], true);
  return new Uint8Array(buffer);
}

/** The whole pipeline: captured mono chunks at `rate` → a 16 kHz PCM16 WAV. */
export function chunksToWav(chunks: Float32Array[], rate: number): Uint8Array {
  return encodeWav(toPcm16(resample(concat(chunks), rate, TARGET_RATE)), TARGET_RATE);
}

/** "0:07", "1:23" — the recording timer. */
export function clipTime(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000));
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, "0")}`;
}
