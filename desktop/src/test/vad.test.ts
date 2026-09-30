import { describe, expect, it, vi } from "vitest";
import { SLEEP_TIMING, TURN_TIMING, VoiceSegmenter } from "../lib/vad";

const RATE = 16_000;
const FRAME = 1_600; // 100 ms

const speech = (amplitude = 0.2) => new Float32Array(FRAME).map((_, i) => amplitude * Math.sin(i / 3));
const quiet = () => new Float32Array(FRAME);
const feed = (seg: VoiceSegmenter, frames: Float32Array[]) => frames.forEach((f) => seg.push(f));
const repeat = (make: () => Float32Array, n: number) => Array.from({ length: n }, make);

describe("voice activity detection", () => {
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

  it("never lets a segment grow past its cap", () => {
    const onSegment = vi.fn();
    const seg = new VoiceSegmenter(RATE, onSegment, SLEEP_TIMING);
    feed(seg, repeat(() => speech(), 60)); // 6 s of continuous sound
    expect(onSegment.mock.calls.length).toBeGreaterThanOrEqual(1);
    for (const [samples] of onSegment.mock.calls as [Float32Array][]) {
      expect(samples.length / RATE).toBeLessThanOrEqual(SLEEP_TIMING.maxSegmentMs / 1000 + 0.1);
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
