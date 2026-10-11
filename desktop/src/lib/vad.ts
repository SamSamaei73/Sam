/**
 * Local voice-activity detection: turns a continuous stream of microphone
 * frames into short speech SEGMENTS. Everything else (silence, room tone,
 * frames between segments) is dropped immediately and never leaves this
 * object. Deterministic and bounded:
 *
 * - local Silero speech probabilities in production;
 * - speech starts after sustained speech, with hysteresis on release;
 * - a turn ends after `endSilenceMs` of quiet (hysteresis, so natural
 *   pauses inside a sentence do not end it);
 * - a segment never exceeds `maxSegmentMs` (it is closed there);
 * - a short pre-roll keeps the first syllable ("S-am") from being clipped.
 */

export interface VadTiming {
  /** Quiet needed to close a turn. */
  endSilenceMs: number;
  /** Hard cap on a segment's length. */
  maxSegmentMs: number;
}

export type SegmentEnd = "silence" | "limit";

export const SLEEP_TIMING: VadTiming = { endSilenceMs: 450, maxSegmentMs: 4_000 };
export const TURN_TIMING: VadTiming = { endSilenceMs: 600, maxSegmentMs: 60_000 };

const MIN_SPEECH_MS = 120;
const PRE_ROLL_MS = 300;
/** Absolute floor on the speech threshold (RMS), so dead silence + a click is not speech. */
const MIN_THRESHOLD = 0.012;
const FLOOR_FACTOR = 3;
const RELEASE_FACTOR = 0.7;
const FLOOR_ADAPT = 0.05;

export function rms(frame: Float32Array): number {
  let sum = 0;
  for (const sample of frame) sum += sample * sample;
  return Math.sqrt(sum / Math.max(1, frame.length));
}

export class VoiceSegmenter {
  private floor = 0.004;
  private timing: VadTiming;
  private preRoll: Float32Array[] = [];
  private preRollSamples = 0;
  private segment: Float32Array[] = [];
  private segmentSamples = 0;
  private voicedMs = 0;
  private silenceMs = 0;
  private speaking = false;
  private pendingMs = 0;
  private continuation = false;
  private resetVersion = 0;

  constructor(
    private readonly sampleRate: number,
    private readonly onSegment: (samples: Float32Array, endedBy: SegmentEnd, voicedSeconds: number) => void,
    timing: VadTiming = SLEEP_TIMING,
    private readonly onSpeech: (active: boolean) => void = () => undefined,
  ) {
    this.timing = timing;
  }

  get isSpeaking(): boolean {
    return this.speaking;
  }

  /** A candidate onset is already live capture, even before it has enough
   * speech frames to qualify as a segment. Handoffs must preserve it. */
  get hasSpeechActivity(): boolean {
    return this.speaking || this.pendingMs > 0;
  }

  setTiming(timing: VadTiming): void {
    this.timing = timing;
  }

  /** Feed one frame. Production supplies local Silero's speech probability;
   * absent probabilities retain the deterministic energy mode for fixtures. */
  push(frame: Float32Array, probability?: number): number {
    const level = rms(frame);
    const neural = probability !== undefined;
    const threshold = neural ? 0.5 : Math.max(MIN_THRESHOLD, this.floor * FLOOR_FACTOR);
    const version = this.resetVersion;
    let offset = 0;
    while (offset < frame.length && version === this.resetVersion) {
      const limit = this.maxSamples;
      const room = this.speaking ? limit - this.segmentSamples : limit;
      if (room <= 0) {
        this.close("limit");
        continue;
      }
      const part = frame.subarray(offset, offset + Math.min(room, frame.length - offset));
      offset += part.length;
      const partLevel = neural ? probability! : part.length === frame.length ? level : rms(part);
      const ms = (part.length / this.sampleRate) * 1000;
      if (!this.speaking) {
        if (partLevel > threshold) {
          this.pendingMs += ms;
          this.keepPreRoll(part);
          if (this.pendingMs >= MIN_SPEECH_MS) {
            this.speaking = true;
            this.segment = this.preRoll;
            this.segmentSamples = this.preRollSamples;
            this.preRoll = [];
            this.preRollSamples = 0;
            this.voicedMs = this.pendingMs;
            this.silenceMs = 0;
            this.onSpeech(true);
          }
        } else {
          const candidateEnded = this.pendingMs > 0;
          this.pendingMs = 0;
          if (!neural) this.floor += (partLevel - this.floor) * FLOOR_ADAPT;
          this.keepPreRoll(part);
          if (candidateEnded) this.onSpeech(false);
        }
      } else {
        this.segment.push(new Float32Array(part));
        this.segmentSamples += part.length;
        // Hysteresis must not turn sound below the absolute speech floor into
        // sustained speech. Otherwise quiet AGC/room noise can hold a turn
        // open until the hard cap after the owner has stopped talking.
        const releaseThreshold = neural ? 0.35 : Math.max(MIN_THRESHOLD, threshold * RELEASE_FACTOR);
        if (partLevel > releaseThreshold) {
          this.voicedMs += ms;
          this.silenceMs = 0;
        } else {
          this.silenceMs += ms;
        }
      }
      if (this.speaking) {
        if (this.silenceMs >= this.timing.endSilenceMs) this.close("silence");
        else if (this.segmentSamples >= limit) this.close("limit");
      }
    }
    return level;
  }

  private get maxSamples(): number {
    return Math.max(1, Math.floor((this.timing.maxSegmentMs / 1000) * this.sampleRate));
  }

  /** Drop everything buffered (pause, stop, or a state change). */
  reset(): void {
    this.resetVersion++;
    const wasSpeaking = this.speaking;
    this.wipe();
    if (wasSpeaking) this.onSpeech(false);
  }

  private close(endedBy: SegmentEnd): void {
    const joined = new Float32Array(this.segmentSamples);
    let offset = 0;
    for (const part of this.segment) {
      joined.set(part, offset);
      offset += part.length;
    }
    const usable = this.voicedMs >= MIN_SPEECH_MS || this.continuation;
    const voicedSeconds = this.voicedMs / 1000;
    if (endedBy === "silence") {
      this.wipe();
      this.onSpeech(false);
    } else {
      // A size boundary is not the end of speech. Keep its continuation,
      // including the rest of this input frame, for the next bounded segment.
      for (const part of this.segment) part.fill(0);
      this.segment = [];
      this.segmentSamples = 0;
      this.voicedMs = 0;
      this.continuation = true;
    }
    try {
      if (usable && joined.length > 0) this.onSegment(joined, endedBy, voicedSeconds);
    } finally {
      joined.fill(0);
    }
  }

  private keepPreRoll(frame: Float32Array): void {
    this.preRoll.push(new Float32Array(frame));
    this.preRollSamples += frame.length;
    const limit = Math.min(Math.floor((PRE_ROLL_MS / 1000) * this.sampleRate), this.maxSamples);
    while (this.preRoll.length > 1 && this.preRollSamples - (this.preRoll[0]?.length ?? 0) >= limit) {
      const dropped = this.preRoll.shift();
      if (dropped) {
        this.preRollSamples -= dropped.length;
        dropped.fill(0);
      }
    }
    const excess = this.preRollSamples - limit;
    const first = this.preRoll[0];
    if (excess > 0 && first) {
      this.preRoll[0] = first.slice(excess);
      first.fill(0);
      this.preRollSamples -= excess;
    }
  }

  private wipe(): void {
    for (const part of this.segment) part.fill(0);
    for (const part of this.preRoll) part.fill(0);
    this.segment = [];
    this.segmentSamples = 0;
    this.preRoll = [];
    this.preRollSamples = 0;
    this.voicedMs = 0;
    this.silenceMs = 0;
    this.pendingMs = 0;
    this.speaking = false;
    this.continuation = false;
  }
}
