import { describe, expect, it, vi } from "vitest";
import { SLEEP_TIMING, TURN_TIMING, VoiceSegmenter } from "../lib/vad";

const RATE = 16_000;
const FRAME = 1_600; // 100 ms

const speech = (amplitude = 0.2) => new Float32Array(FRAME).map((_, i) => amplitude * Math.sin(i / 3));
const quiet = () => new Float32Array(FRAME);
const feed = (seg: VoiceSegmenter, frames: Float32Array[]) => frames.forEach((f) => seg.push(f));
const repeat = (make: () => Float32Array, n: number) => Array.from({ length: n }, make);

describe("voice activity detection", () => {
  it("uses speech probability, not loudness, and ends after 608 ms of non-speech", () => {
    const captured: Float32Array[] = [];
    const endings: string[] = [];
    const seg = new VoiceSegmenter(RATE, (samples, endedBy) => {
      captured.push(samples.slice());
      endings.push(endedBy);
    }, TURN_TIMING);
    const noise = Float32Array.from({ length: 512 }, (_, i) => i % 2 ? 0.3 : -0.3);
    const softSpeech = Float32Array.from({ length: 512 }, (_, i) => 0.005 * Math.sin(i / 9));
    const feedNeural = (frame: Float32Array, probability: number, count: number) => {
      for (let i = 0; i < count; i++) seg.push(frame, probability);
    };
    feedNeural(noise, 0.02, 100); // loud non-speech never creates a request
    expect(seg.isSpeaking).toBe(false);
    expect(captured).toHaveLength(0);
    feedNeural(softSpeech, 0.95, 25);
    feedNeural(noise, 0.05, 16); // 512 ms pause remains inside this question
    expect(seg.isSpeaking).toBe(true);
    expect(captured).toHaveLength(0);
    feedNeural(softSpeech, 0.9, 25);
    feedNeural(noise, 0.05, 18);
    expect(captured).toHaveLength(0);
    feedNeural(noise, 0.05, 1);
    expect(endings).toEqual(["silence"]);
    expect(seg.isSpeaking).toBe(false);
    expect(captured[0]?.some((sample) => Math.abs(sample) < 0.006 && sample !== 0)).toBe(true);
    feedNeural(noise, 0.05, 100);
    expect(captured).toHaveLength(1);
  });

  it("room silence never becomes a segment", () => {
    const onSegment = vi.fn();
    const seg = new VoiceSegmenter(RATE, onSegment, SLEEP_TIMING);
    feed(seg, repeat(quiet, 100));
    feed(seg, repeat(() => speech(0.004), 50)); // faint room tone
    expect(onSegment).not.toHaveBeenCalled();
  });

  it("a short click is not speech", () => {
    const onSegment = vi.fn();
    const seg = new VoiceSegmenter(RATE, onSegment, SLEEP_TIMING);
    feed(seg, [speech(), ...repeat(quiet, 10)]); // 100 ms < minimum speech
    expect(onSegment).not.toHaveBeenCalled();
  });

  it("closes a spoken word after a short silence, with a pre-roll", () => {
    const onSegment = vi.fn();
    const seg = new VoiceSegmenter(RATE, onSegment, SLEEP_TIMING);
    feed(seg, [...repeat(quiet, 5), ...repeat(() => speech(), 6), ...repeat(quiet, 6)]);
    expect(onSegment).toHaveBeenCalledTimes(1);
    const samples = onSegment.mock.calls[0]?.[0] as Float32Array;
    const seconds = samples.length / RATE;
    expect(seconds).toBeGreaterThan(0.6);
    expect(seconds).toBeLessThan(1.3); // speech + pre-roll + the closing silence only
  });

  it("does not cut a turn at a natural pause", () => {
    const onSegment = vi.fn();
    const seg = new VoiceSegmenter(RATE, onSegment, TURN_TIMING);
    feed(seg, [...repeat(() => speech(), 8), ...repeat(quiet, 5), ...repeat(() => speech(), 8)]);
    expect(onSegment).not.toHaveBeenCalled(); // a 500 ms pause is inside the turn
    feed(seg, repeat(quiet, 10));
    expect(onSegment).toHaveBeenCalledTimes(1);
  });

  it.each([16_000, 44_100, 48_000])("ends promptly over sub-threshold room noise without clipping a natural pause (%i Hz)", (rate) => {
    const frameSize = 2048; // the actual microphone callback size
    const frameMs = frameSize * 1000 / rate;
    const captured: Float32Array[] = [];
    const endings: string[] = [];
    const segmenter = new VoiceSegmenter(rate, (samples, endedBy) => {
      captured.push(samples.slice());
      endings.push(endedBy);
    }, TURN_TIMING);
    const voiced = Float32Array.from({ length: frameSize }, (_, i) => 0.2 * Math.sin(2 * Math.PI * 200 * i / rate));
    const room = Float32Array.from({ length: frameSize }, (_, i) => Math.SQRT2 * 0.01 * Math.sin(2 * Math.PI * 1_000 * i / rate));
    const feedFrames = (frame: Float32Array, count: number) => {
      for (let i = 0; i < count; i++) segmenter.push(frame);
    };
    const speechFrames = Math.ceil(800 / frameMs);
    const pauseFrames = Math.floor(500 / frameMs);
    feedFrames(voiced, speechFrames);
    feedFrames(room, pauseFrames); // preserve a natural pause of up to 500 ms
    expect(captured).toHaveLength(0);
    feedFrames(voiced, speechFrames);
    let endFrames = 0;
    while (captured.length === 0 && endFrames * frameMs < 1200) {
      segmenter.push(room);
      endFrames++;
    }
    expect(captured).toHaveLength(1);
    expect(endings).toEqual(["silence"]);
    expect(endFrames * frameMs).toBeGreaterThanOrEqual(600);
    expect(endFrames * frameMs).toBeLessThan(600 + frameMs);
    expect(captured[0]?.length).toBe((speechFrames * 2 + pauseFrames + endFrames) * frameSize);
    expect(segmenter.isSpeaking).toBe(false);
    // The same noise cannot immediately become a fresh speech segment.
    feedFrames(room, Math.ceil(2000 / frameMs));
    expect(captured).toHaveLength(1);
  });

  it("never lets a segment grow past its cap", () => {
    const onSegment = vi.fn();
    const seg = new VoiceSegmenter(RATE, onSegment, SLEEP_TIMING);
    feed(seg, repeat(() => speech(), 60)); // 6 s of continuous sound
    expect(onSegment.mock.calls.length).toBeGreaterThanOrEqual(1);
    for (const [samples] of onSegment.mock.calls as [Float32Array][]) {
      expect(samples.length / RATE).toBeLessThanOrEqual(SLEEP_TIMING.maxSegmentMs / 1000);
    }
  });

  it.each([16_000, 44_100, 48_000])("preserves every sample across exact caps at %i Hz", (rate) => {
    for (const timing of [SLEEP_TIMING, TURN_TIMING]) {
      const captured: Float32Array[] = [];
      const endings: string[] = [];
      const segmenter = new VoiceSegmenter(rate, (samples, endedBy) => {
        captured.push(samples.slice()); // emitted buffers are wiped after the callback
        endings.push(endedBy);
      }, timing);
      const signal = Float32Array.from(
        { length: Math.ceil(rate * (timing.maxSegmentMs / 1000 + 1.25)) },
        (_, i) => 0.12 + (i % 97) / 1000,
      );
      for (let offset = 0; offset < signal.length; offset += 2048) segmenter.push(signal.subarray(offset, offset + 2048));
      for (let i = 0; i < Math.ceil(rate / 2048); i++) segmenter.push(new Float32Array(2048));
      expect(captured).toHaveLength(2);
      expect(endings).toEqual(["limit", "silence"]);
      expect(captured[0]?.length).toBe(rate * timing.maxSegmentMs / 1000);
      const joined = new Float32Array(captured.reduce((count, part) => count + part.length, 0));
      let offset = 0;
      for (const part of captured) {
        expect(part.length).toBeLessThanOrEqual(rate * timing.maxSegmentMs / 1000);
        joined.set(part, offset);
        offset += part.length;
      }
      expect(joined.subarray(0, signal.length).every((sample, i) => sample === signal[i])).toBe(true);
      expect(joined.subarray(signal.length).every((sample) => sample === 0)).toBe(true);
    }
  });

  it("reset drops anything buffered", () => {
    const onSegment = vi.fn();
    const onSpeech = vi.fn();
    const seg = new VoiceSegmenter(RATE, onSegment, SLEEP_TIMING, onSpeech);
    feed(seg, repeat(() => speech(), 4));
    expect(seg.isSpeaking).toBe(true);
    seg.reset();
    expect(seg.isSpeaking).toBe(false);
    feed(seg, repeat(quiet, 10));
    expect(onSegment).not.toHaveBeenCalled();
    expect(onSpeech).toHaveBeenLastCalledWith(false);
  });
});
