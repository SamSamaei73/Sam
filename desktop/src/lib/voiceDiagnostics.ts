/** Numeric, in-memory capture health. No audio, transcript or identity data. */
export interface CaptureDiagnostics {
  listener: "STOPPED" | "STARTING" | "LISTENING" | "ERROR";
  microphone: "STOPPED" | "READY" | "DENIED" | "SILENT" | "ERROR";
  device: string | null;
  rmsDbfs: number | null;
  peakDbfs: number | null;
  nonZeroRatio: number | null;
  maxRmsDbfs: number | null;
  reason: string | null;
}

export const emptyCapture: CaptureDiagnostics = {
  listener: "STOPPED", microphone: "STOPPED", device: null,
  rmsDbfs: null, peakDbfs: null, nonZeroRatio: null, maxRmsDbfs: null, reason: null,
};

// Numeric updates only notify the expanded diagnostics panel. The sleeping
// Home screen must not rerender on microphone frames or aggregate readings.
let capture: CaptureDiagnostics = { ...emptyCapture };
const subscribers = new Set<() => void>();
export const getCaptureDiagnostics = () => capture;
export function publishCaptureDiagnostics(value: CaptureDiagnostics): void {
  capture = value;
  subscribers.forEach((notify) => notify());
}
export function subscribeCaptureDiagnostics(notify: () => void): () => void {
  subscribers.add(notify);
  return () => { subscribers.delete(notify); };
}

export class CaptureMeter {
  private count = 0;
  private nonZero = 0;
  private energy = 0;
  private peak = 0;
  private maxRms = -120;

  /** At most one update per second; store only aggregate numbers. */
  push(frame: Float32Array, rate: number): Pick<CaptureDiagnostics, "rmsDbfs" | "peakDbfs" | "nonZeroRatio" | "maxRmsDbfs" | "microphone" | "reason"> | null {
    for (const value of frame) {
      const sample = Number.isFinite(value) ? value : 0;
      this.count++;
      this.energy += sample * sample;
      this.peak = Math.max(this.peak, Math.abs(sample));
      if (sample !== 0) this.nonZero++;
    }
    if (this.count < rate) return null;
    const db = (level: number) => Math.max(-120, 20 * Math.log10(Math.max(level, 0.000001)));
    const rmsDbfs = db(Math.sqrt(this.energy / this.count));
    this.maxRms = Math.max(this.maxRms, rmsDbfs);
    const result = {
      rmsDbfs, peakDbfs: db(this.peak), nonZeroRatio: this.nonZero / this.count, maxRmsDbfs: this.maxRms,
      microphone: this.nonZero === 0 ? "SILENT" as const : "READY" as const,
      reason: this.nonZero === 0 ? "microphone_digital_silence" : null,
    };
    this.count = this.nonZero = this.energy = this.peak = 0;
    return result;
  }
}
