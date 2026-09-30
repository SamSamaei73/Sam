/**
 * Local voice-activity detection: turns a continuous stream of microphone
 * frames into short speech SEGMENTS. Everything else (silence, room tone,
 * frames between segments) is dropped immediately and never leaves this
 * object. Deterministic and bounded:
 *
 * - an adaptive noise floor (only learned while nobody is speaking);
 * - speech starts after `minSpeechMs` of energy above the threshold;
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

export const SLEEP_TIMING: VadTiming = { endSilenceMs: 450, maxSegmentMs: 4_000 };
export const TURN_TIMING: VadTiming = { endSilenceMs: 900, maxSegmentMs: 30_000 };

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

  constructor(
    private readonly sampleRate: number,
    private readonly onSegment: (samples: Float32Array) => void,
    timing: VadTiming = SLEEP_TIMING,
    private readonly onSpeech: (active: boolean) => void = () => undefined,
  ) {
    this.timing = timing;
  }

  get isSpeaking(): boolean {
    return this.speaking;
  }

  setTiming(timing: VadTiming): void {
    this.timing = timing;
  }

  /** Feed one frame. Returns its level (RMS) for the UI. */
  push(frame: Float32Array): number {
    const level = rms(frame);
    const ms = (frame.length / this.sampleRate) * 1000;
    const threshold = Math.max(MIN_THRESHOLD, this.floor * FLOOR_FACTOR);

    if (!this.speaking) {
      if (level > threshold) {
        this.pendingMs += ms;
        this.keepPreRoll(frame);
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
        this.pendingMs = 0;
        this.floor += (level - this.floor) * FLOOR_ADAPT;
        this.keepPreRoll(frame);
      }
      return level;
    }

    this.segment.push(new Float32Array(frame));
    this.segmentSamples += frame.length;
    if (level > threshold * RELEASE_FACTOR) {
      this.voicedMs += ms;
      this.silenceMs = 0;
    } else {
      this.silenceMs += ms;
    }
    const lengthMs = (this.segmentSamples / this.sampleRate) * 1000;
    if (this.silenceMs >= this.timing.endSilenceMs || lengthMs >= this.timing.maxSegmentMs) {
      this.close();
    }
    return level;
  }

  /** Drop everything buffered (pause, stop, or a state change). */
  reset(): void {
    const wasSpeaking = this.speaking;
    this.wipe();
    if (wasSpeaking) this.onSpeech(false);
  }

  private close(): void {
    const joined = new Float32Array(this.segmentSamples);
    let offset = 0;
    for (const part of this.segment) {
      joined.set(part, offset);
      offset += part.length;
    }
    const voiced = this.voicedMs;
    this.wipe();
    this.onSpeech(false);
    if (voiced >= MIN_SPEECH_MS) this.onSegment(joined);
    joined.fill(0);
  }

  private keepPreRoll(frame: Float32Array): void {
    this.preRoll.push(new Float32Array(frame));
    this.preRollSamples += frame.length;
    const limit = (PRE_ROLL_MS / 1000) * this.sampleRate;
    while (this.preRoll.length > 1 && this.preRollSamples - (this.preRoll[0]?.length ?? 0) >= limit) {
      const dropped = this.preRoll.shift();
      if (dropped) {
        this.preRollSamples -= dropped.length;
        dropped.fill(0);
      }
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
  }
}
