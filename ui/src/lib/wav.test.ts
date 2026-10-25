import { describe, expect, it } from "vitest";
import { chunksToWav, clipTime, concat, encodeWav, mixToMono, resample, TARGET_RATE, toPcm16 } from "./wav";

function ascii(bytes: Uint8Array, offset: number, length: number): string {
  return String.fromCharCode(...bytes.slice(offset, offset + length));
}

describe("encodeWav", () => {
  it("writes a canonical PCM16 mono header", () => {
    const wav = encodeWav(new Int16Array([0, 1, -1, 32767, -32768]), 16000);
    const view = new DataView(wav.buffer);
    expect(wav.length).toBe(44 + 10);
    expect(ascii(wav, 0, 4)).toBe("RIFF");
    expect(view.getUint32(4, true)).toBe(36 + 10);
    expect(ascii(wav, 8, 4)).toBe("WAVE");
    expect(ascii(wav, 12, 4)).toBe("fmt ");
    expect(view.getUint32(16, true)).toBe(16);
    expect(view.getUint16(20, true)).toBe(1); // PCM
    expect(view.getUint16(22, true)).toBe(1); // mono
    expect(view.getUint32(24, true)).toBe(16000);
    expect(view.getUint32(28, true)).toBe(32000);
    expect(view.getUint16(32, true)).toBe(2);
    expect(view.getUint16(34, true)).toBe(16);
    expect(ascii(wav, 36, 4)).toBe("data");
    expect(view.getUint32(40, true)).toBe(10);
    expect([0, 1, 2, 3, 4].map((i) => view.getInt16(44 + i * 2, true))).toEqual([0, 1, -1, 32767, -32768]);
  });

  it("encodes an empty clip as a header alone", () => {
    const wav = encodeWav(new Int16Array(0));
    expect(wav.length).toBe(44);
    expect(new DataView(wav.buffer).getUint32(40, true)).toBe(0);
  });
});

describe("toPcm16", () => {
  it("scales and clips", () => {
    expect(Array.from(toPcm16(new Float32Array([0, 1, -1, 2, -2, 0.5])))).toEqual([
      0, 32767, -32768, 32767, -32768, 16384,
    ]);
  });
});

describe("resample", () => {
  it("is the identity at the target rate", () => {
    const x = new Float32Array([0.1, 0.2]);
    expect(resample(x, 16000, 16000)).toBe(x);
  });

  it("downsamples 48 kHz to 16 kHz by averaging each window of three", () => {
    const x = new Float32Array([0, 0.3, 0.6, 1, 1, 1, -0.3, -0.3, -0.3]);
    const y = Array.from(resample(x, 48000, 16000));
    expect(y).toHaveLength(3);
    expect(y[0]).toBeCloseTo(0.3);
    expect(y[1]).toBeCloseTo(1);
    expect(y[2]).toBeCloseTo(-0.3);
  });

  it("handles a non-integer ratio (44.1 kHz) with the expected length", () => {
    const x = new Float32Array(44100).fill(0.25);
    const y = resample(x, 44100, 16000);
    expect(y.length).toBe(16000);
    expect(Math.max(...y)).toBeCloseTo(0.25);
    expect(Math.min(...y)).toBeCloseTo(0.25);
  });

  it("keeps a speech-band tone and attenuates one above the new Nyquist", () => {
    const rate = 48000;
    const tone = (hz: number) => Float32Array.from({ length: rate }, (_, i) => Math.sin((2 * Math.PI * hz * i) / rate));
    const rms = (a: Float32Array) => Math.sqrt(a.reduce((s, v) => s + v * v, 0) / a.length);
    expect(rms(resample(tone(300), rate))).toBeGreaterThan(0.65);
    expect(rms(resample(tone(15000), rate))).toBeLessThan(0.3);
  });

  it("upsamples by interpolation", () => {
    expect(Array.from(resample(new Float32Array([0, 1]), 8000, 16000))).toEqual([0, 0.5, 1, 1]);
  });

  it("rejects a bad rate", () => {
    expect(() => resample(new Float32Array(1), 0, 16000)).toThrow();
  });
});

describe("mixToMono / concat", () => {
  it("averages channels and concatenates chunks", () => {
    expect(Array.from(mixToMono([new Float32Array([1, 0]), new Float32Array([0, 1])]))).toEqual([0.5, 0.5]);
    expect(Array.from(concat([new Float32Array([1]), new Float32Array([2, 3])]))).toEqual([1, 2, 3]);
    expect(mixToMono([]).length).toBe(0);
  });
});

describe("chunksToWav", () => {
  it("turns one second at 48 kHz into one second at 16 kHz", () => {
    const chunks = Array.from({ length: 375 }, () => new Float32Array(128)); // 48000 frames
    const wav = chunksToWav(chunks, 48000);
    const view = new DataView(wav.buffer);
    expect(view.getUint32(24, true)).toBe(TARGET_RATE);
    expect(view.getUint32(40, true)).toBe(TARGET_RATE * 2);
  });
});

describe("clipTime", () => {
  it("formats m:ss", () => {
    expect(clipTime(0)).toBe("0:00");
    expect(clipTime(7900)).toBe("0:07");
    expect(clipTime(83000)).toBe("1:23");
    expect(clipTime(-5)).toBe("0:00");
  });
});
