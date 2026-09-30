import type { CSSProperties } from "react";
import type { VoiceState } from "../session";

/**
 * Sam's presence: a layered, luminous core drawn as inline SVG (scalable,
 * no raster, no third-party artwork). Its motion is driven ONLY by Sam's
 * real interaction state (see ``session.tsx``); the listening rings follow
 * the real microphone level. Reduced motion freezes every animation.
 *
 *   idle        slow breathing glow
 *   listening   outward rings that swell with the owner's voice
 *   thinking    counter-rotating inner arcs
 *   speaking    a steady pulse with a waveform ring
 *   permission  a calm amber halo (the owner is being asked)
 *   error       a brief, muted warm tint
 *   offline     dim and still
 */
export function SamPresence({
  state,
  level = 0,
  attention = false,
  guest = false,
  label,
}: {
  state: VoiceState;
  level?: number;
  attention?: boolean;
  guest?: boolean;
  /** Overrides the spoken description (e.g. while Sam starts up). */
  label?: string;
}) {
  const style = { "--level": level.toFixed(3) } as CSSProperties;
  return (
    <div
      className="presence"
      data-state={state}
      data-attention={attention ? "true" : "false"}
      data-guest={guest ? "true" : "false"}
      style={style}
      role="img"
      aria-label={label ?? `Sam is ${STATE_WORDS[state]}`}
    >
      <svg viewBox="0 0 400 400" className="presence-svg" aria-hidden="true" focusable="false">
        <defs>
          <radialGradient id="sam-core" cx="42%" cy="38%" r="70%">
            <stop offset="0%" stopColor="#f2f8ff" />
            <stop offset="22%" stopColor="#9fd0ff" />
            <stop offset="55%" stopColor="#3f7dff" />
            <stop offset="100%" stopColor="#0a1a6e" />
          </radialGradient>
          <radialGradient id="sam-halo" cx="50%" cy="50%" r="50%">
            <stop offset="45%" stopColor="rgba(80,150,255,0.30)" />
            <stop offset="100%" stopColor="rgba(20,40,160,0)" />
          </radialGradient>
          <linearGradient id="sam-arc" x1="0" y1="0" x2="1" y2="1">
            <stop offset="0%" stopColor="#cfe8ff" />
            <stop offset="100%" stopColor="#2a4dff" stopOpacity="0" />
          </linearGradient>
          <filter id="sam-blur" x="-50%" y="-50%" width="200%" height="200%">
            <feGaussianBlur stdDeviation="6" />
          </filter>
        </defs>
        <circle className="halo" cx="200" cy="200" r="190" fill="url(#sam-halo)" />
        <g className="listen-rings">
          <circle className="listen-ring r1" cx="200" cy="200" r="118" />
          <circle className="listen-ring r2" cx="200" cy="200" r="118" />
          <circle className="listen-ring r3" cx="200" cy="200" r="118" />
        </g>
        <circle className="orbit" cx="200" cy="200" r="150" />
        <g className="ticks">
          {Array.from({ length: 48 }, (_, i) => (
            <line
              key={i}
              x1="200"
              y1="44"
              x2="200"
              y2={i % 4 === 0 ? 54 : 50}
              transform={`rotate(${i * 7.5} 200 200)`}
            />
          ))}
        </g>
        <g className="arcs outer">
          <path d="M200 72 A128 128 0 0 1 328 200" />
          <path d="M200 328 A128 128 0 0 1 72 200" />
        </g>
        <g className="arcs inner">
          <path d="M200 96 A104 104 0 0 0 96 200" />
          <path d="M200 304 A104 104 0 0 0 304 200" />
        </g>
        <path
          className="wave"
          d={wavePath(200, 200, 112, 7, 36)}
        />
        <circle className="core-glow" cx="200" cy="200" r="78" filter="url(#sam-blur)" />
        <circle className="core" cx="200" cy="200" r="70" fill="url(#sam-core)" />
        <ellipse className="core-hi" cx="178" cy="172" rx="30" ry="18" />
      </svg>
    </div>
  );
}

const STATE_WORDS: Record<VoiceState, string> = {
  idle: "ready",
  sleeping: "waiting for its name",
  waking: "awake",
  attending: "waiting for you to speak",
  listening: "listening",
  thinking: "thinking",
  speaking: "speaking",
  permission: "waiting for your permission",
  error: "having a problem",
  offline: "offline",
};

/** A closed wave around a circle (deterministic; animated by CSS only). */
function wavePath(cx: number, cy: number, r: number, amplitude: number, lobes: number): string {
  const points: string[] = [];
  const steps = 360;
  for (let i = 0; i <= steps; i++) {
    const a = (i / steps) * Math.PI * 2;
    const rr = r + amplitude * Math.sin(a * lobes) * Math.sin(a * 3);
    points.push(`${(cx + rr * Math.cos(a)).toFixed(1)} ${(cy + rr * Math.sin(a)).toFixed(1)}`);
  }
  return `M${points.join(" L")} Z`;
}
