"use client";

import { useEffect, useRef } from "react";

export interface WaveVisualizerProps {
  levels: { client: number; rep: number };
  speaking: { client: boolean; rep: boolean };
  active: { client: boolean; rep: boolean };
  muted: { client: boolean; rep: boolean };
}

/*
 * The one question this instrument answers in 200 ms: is audio actually reaching
 * the server, and who is talking.
 *
 * The critical device is not the bars, it is the floor line. "Dead" and "merely
 * quiet" must never be confusable:
 *
 *   not captured        -> frozen history, one dim 1 px line, lane tag at 40 percent
 *   captured and silent -> history keeps scrolling at minimum bar height, line in
 *                          the lane colour, lane tag at full strength
 *   captured and hot    -> bars
 *   captured but muted  -> history keeps scrolling, so the device still reads as
 *                          alive, but the whole lane drops to --dim and the tag is
 *                          struck through. capture.ts keeps onLevel running while
 *                          muted, so without this the lane would bounce exactly as
 *                          it did a second earlier while not one byte reaches the
 *                          server, which is the one lie this instrument must not tell
 *
 * The draw loop never depends on the React render cycle. Props are mirrored into a
 * ref by a plain effect and one requestAnimationFrame loop reads that ref, so a
 * 30 per second level update costs one canvas repaint and no reconciliation.
 */

/** One bar is committed to each lane's history every 62 ms (DESIGN 5.6). */
const COMMIT_MS = 62;
/** Peak hold looks back over this many committed bars. */
const PEAK_WINDOW = 24;
/** The held peak falls this many level units per second toward the live level. */
const PEAK_DECAY_PER_SECOND = 0.6;
/** Glow is applied to the newest bars only, never to the whole lane. */
const GLOW_BARS = 12;
const GLOW_MAX_BLUR = 8;
const GLOW_RAMP_MS = 180;
const MIN_BARS = 48;
const MAX_BARS = 160;
/** Bar alpha at rest, and while the lane is speaking. */
const REST_ALPHA = 0.55;
const HOT_ALPHA = 1;
/** Floor line alpha: lane colour when captured, --dim when nothing is plugged in. */
const FLOOR_ALPHA_LIVE = 0.22;
const FLOOR_ALPHA_DEAD = 0.35;
const PEAK_ALPHA = 0.5;
/** A 3x phone canvas at 60 fps next to WebAudio and a socket is not worth it. */
const MAX_DPR = 2;
/** Level smoothing per frame. Fast to rise so a word is visible at once, slower to
 *  fall so the bar decays instead of flickering. */
const ATTACK = 0.55;
const RELEASE = 0.22;

/**
 * Used only if the stylesheet has not applied yet (the very first frame of a cold
 * load). Each value is the literal token from DESIGN section 2.1, so the canvas can
 * never paint a colour that is not in the palette.
 */
const TOKEN_FALLBACK = {
  accent: "#6EE7FF",
  accent2: "#A78BFA",
  dim: "#4B5768",
  line: "rgba(124, 164, 208, 0.10)",
} as const;

/**
 * Lane tags are DOM, never canvas text (DESIGN 5.6). Exactly two MICRO labels, and
 * the muted variant is the one state the canvas cannot carry on its own.
 */
const TAG_BASE = "font-mono text-micro uppercase";
const TAG_LIVE = "text-muted";
const TAG_MUTED = "line-through decoration-danger decoration-1 text-dim";

interface LaneBuffer {
  /** Ring of committed bar levels, 0..1. */
  bars: Float32Array;
  /** Index of the newest bar. */
  head: number;
  /** How many slots hold real data yet. */
  filled: number;
  /** Smoothed live level, 0..1. */
  level: number;
  /** Held peak, 0..1. */
  peak: number;
  /** 0..1 ramp toward the speaking state, drives alpha and glow. */
  hot: number;
}

interface Geometry {
  cssW: number;
  cssH: number;
  /** Left strip reserved for the DOM lane tags. No bar is ever drawn inside it. */
  gutter: number;
  pitch: number;
  barW: number;
  laneH: number;
  dividerY: number;
  /** Baseline of the client lane. Bars grow upward from here, never mirrored. */
  baseTop: number;
  /** Baseline of the rep lane. */
  baseBottom: number;
  centerTop: number;
  centerBottom: number;
  bars: number;
}

interface Palette {
  accent: string;
  accent2: string;
  dim: string;
  line: string;
}

function clamp(v: number, lo: number, hi: number): number {
  return v < lo ? lo : v > hi ? hi : v;
}

function createLane(size: number): LaneBuffer {
  return { bars: new Float32Array(size), head: size - 1, filled: 0, level: 0, peak: 0, hot: 0 };
}

/** Re allocate a lane's ring buffer, keeping the newest bars in the same order. */
function resizeLane(lane: LaneBuffer, size: number): void {
  if (lane.bars.length === size) return;
  const next = new Float32Array(size);
  const keep = Math.min(lane.filled, size);
  const old = lane.bars;
  const oldLen = old.length;
  for (let k = 0; k < keep; k += 1) {
    next[size - 1 - k] = old[(lane.head - k + oldLen) % oldLen];
  }
  lane.bars = next;
  lane.head = size - 1;
  lane.filled = keep;
}

/**
 * Perceptual bar height. Raw RMS spends most of its range near zero, so a linear
 * mapping looks dead even when the prospect is clearly audible.
 */
function barHeight(level: number, laneH: number): number {
  const h = 2 + (laneH - 8) * Math.pow(clamp(level, 0, 1), 0.65);
  return clamp(h, 2, laneH - 6);
}

function measureGeometry(cssW: number, cssH: number, wide: boolean): Geometry {
  const gutter = wide ? 76 : 56;
  const pitch = wide ? 4 : 3;
  const barW = wide ? 3 : 2;
  const padY = wide ? 4.5 : 3.5;
  const laneH = Math.max(8, (cssH - 1 - padY * 2) / 2);
  const dividerY = padY + laneH;
  const baseTop = dividerY;
  const baseBottom = dividerY + 1 + laneH;
  const usable = Math.max(0, cssW - gutter);
  const bars = clamp(Math.floor(usable / pitch), MIN_BARS, MAX_BARS);
  return {
    cssW,
    cssH,
    gutter,
    pitch,
    barW,
    laneH,
    dividerY,
    baseTop,
    baseBottom,
    centerTop: padY + laneH / 2,
    centerBottom: dividerY + 1 + laneH / 2,
    bars,
  };
}

function readPalette(): Palette {
  const cs = getComputedStyle(document.documentElement);
  const pick = (name: string, fallback: string) => cs.getPropertyValue(name).trim() || fallback;
  return {
    accent: pick("--accent", TOKEN_FALLBACK.accent),
    accent2: pick("--accent-2", TOKEN_FALLBACK.accent2),
    dim: pick("--dim", TOKEN_FALLBACK.dim),
    line: pick("--line", TOKEN_FALLBACK.line),
  };
}

export function WaveVisualizer(props: WaveVisualizerProps) {
  const propsRef = useRef<WaveVisualizerProps>(props);
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const clientTagRef = useRef<HTMLDivElement | null>(null);
  const repTagRef = useRef<HTMLDivElement | null>(null);

  // Keep the loop reading the newest props without re subscribing to anything.
  useEffect(() => {
    propsRef.current = props;
  });

  useEffect(() => {
    const wrap = wrapRef.current;
    const canvas = canvasRef.current;
    if (!wrap || !canvas) return;

    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    const lanes: [LaneBuffer, LaneBuffer] = [createLane(MIN_BARS), createLane(MIN_BARS)];
    let geom: Geometry = measureGeometry(0, 0, true);
    let palette: Palette = readPalette();
    let raf = 0;
    let lastFrame = 0;
    let lastCommit = 0;
    let running = false;
    const wasLive: [boolean, boolean] = [false, false];

    const motionQuery = window.matchMedia("(prefers-reduced-motion: reduce)");
    let reduced = motionQuery.matches;

    const applySize = () => {
      const rect = wrap.getBoundingClientRect();
      const cssW = Math.max(0, Math.round(rect.width));
      const cssH = Math.max(0, Math.round(rect.height));
      if (cssW === 0 || cssH === 0) return;

      geom = measureGeometry(cssW, cssH, window.innerWidth >= 1024);
      palette = readPalette();
      resizeLane(lanes[0], geom.bars);
      resizeLane(lanes[1], geom.bars);

      const dpr = Math.min(window.devicePixelRatio || 1, MAX_DPR);
      canvas.width = Math.round(cssW * dpr);
      canvas.height = Math.round(cssH * dpr);
      canvas.style.width = `${cssW}px`;
      canvas.style.height = `${cssH}px`;
      // setTransform, never a cumulative ctx.scale, so a resize cannot compound.
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

      if (clientTagRef.current) clientTagRef.current.style.top = `${geom.centerTop}px`;
      if (repTagRef.current) repTagRef.current.style.top = `${geom.centerBottom}px`;
    };

    const drawLane = (
      lane: LaneBuffer,
      base: number,
      laneColour: string,
      live: boolean,
      mute: boolean,
    ) => {
      // A muted lane sends nothing, so it paints in --dim, the same colour that
      // already means "this is not reaching the server". The history keeps
      // scrolling, and that is what keeps muted distinguishable from dead.
      const colour = mute ? palette.dim : laneColour;

      // Floor line first. This is the part of the instrument that carries the most
      // meaning: dim and frozen means nothing is plugged in.
      ctx.globalAlpha = live ? FLOOR_ALPHA_LIVE : FLOOR_ALPHA_DEAD;
      ctx.fillStyle = live ? colour : palette.dim;
      ctx.fillRect(geom.gutter, base - 1, geom.cssW - geom.gutter, 1);

      if (!live) {
        ctx.globalAlpha = 1;
        return;
      }

      const alpha = REST_ALPHA + (HOT_ALPHA - REST_ALPHA) * lane.hot;
      const total = lane.bars.length;
      const visible = Math.min(lane.filled, total);
      ctx.fillStyle = colour;

      // Bars older than the glow window, drawn with no shadow at all.
      ctx.globalAlpha = alpha;
      ctx.shadowBlur = 0;
      for (let k = GLOW_BARS; k < visible; k += 1) {
        const x = geom.cssW - geom.barW - k * geom.pitch;
        if (x < geom.gutter) break;
        const h = barHeight(lane.bars[(lane.head - k + total) % total], geom.laneH);
        ctx.fillRect(Math.round(x), base - h, geom.barW, h);
      }

      // The newest bars, with the speaking glow when motion is allowed. A muted
      // lane never glows: glow is the loudest "this is going out" cue there is.
      const glow = reduced || mute ? 0 : GLOW_MAX_BLUR * lane.hot;
      if (glow > 0.01) {
        ctx.shadowBlur = glow;
        ctx.shadowColor = colour;
      }
      const near = Math.min(GLOW_BARS, visible);
      for (let k = 0; k < near; k += 1) {
        const x = geom.cssW - geom.barW - k * geom.pitch;
        if (x < geom.gutter) break;
        const h = barHeight(lane.bars[(lane.head - k + total) % total], geom.laneH);
        ctx.fillRect(Math.round(x), base - h, geom.barW, h);
      }
      // Invariant 12: shadowBlur is back to zero before anything else is drawn.
      ctx.shadowBlur = 0;

      // Peak hold tick, so a long talker reads as an envelope instead of a wall.
      if (lane.peak > 0.005) {
        const ph = barHeight(lane.peak, geom.laneH);
        const x = geom.cssW - geom.barW;
        ctx.globalAlpha = PEAK_ALPHA;
        ctx.fillRect(Math.round(x), base - ph - 2, geom.barW, 2);
      }

      ctx.globalAlpha = 1;
    };

    const frame = (now: number) => {
      raf = requestAnimationFrame(frame);

      const dt = lastFrame === 0 ? 0.016 : Math.min(0.1, (now - lastFrame) / 1000);
      lastFrame = now;

      const { levels, speaking, active, muted } = propsRef.current;
      const targets = [clamp(levels.client, 0, 1), clamp(levels.rep, 0, 1)];
      const hots = [speaking.client, speaking.rep];
      const live = [active.client, active.rep];
      const mutes = [muted.client, muted.rep];

      const commit = now - lastCommit >= COMMIT_MS;
      if (commit) {
        // Never burst commit after a hidden tab or a long frame.
        lastCommit = now - Math.min(COMMIT_MS - 1, (now - lastCommit) % COMMIT_MS);
      }

      for (let i = 0; i < 2; i += 1) {
        const lane = lanes[i];
        const target = targets[i];
        lane.level += (target - lane.level) * (target > lane.level ? ATTACK : RELEASE);

        const hotTarget = hots[i] ? 1 : 0;
        if (reduced) {
          lane.hot = hotTarget;
        } else {
          const step = (dt * 1000) / GLOW_RAMP_MS;
          lane.hot = clamp(lane.hot + clamp(hotTarget - lane.hot, -step, step), 0, 1);
        }

        if (!live[i]) {
          // Nothing is plugged in. Drop the history so a restarted capture never
          // paints seconds of stale audio as if it had just arrived.
          if (wasLive[i]) {
            lane.filled = 0;
            wasLive[i] = false;
          }
          lane.level = 0;
          lane.peak = 0;
          continue;
        }
        wasLive[i] = true;

        const total = lane.bars.length;
        if (commit) {
          lane.head = (lane.head + 1) % total;
          lane.bars[lane.head] = lane.level;
          lane.filled = Math.min(lane.filled + 1, total);
        } else if (lane.filled > 0) {
          // Redraw the newest bar in place so a rising level is visible at once
          // instead of waiting up to 62 ms for the next commit.
          lane.bars[lane.head] = Math.max(lane.bars[lane.head], lane.level);
        }

        let windowMax = 0;
        const span = Math.min(PEAK_WINDOW, lane.filled);
        for (let k = 0; k < span; k += 1) {
          const v = lane.bars[(lane.head - k + total) % total];
          if (v > windowMax) windowMax = v;
        }
        lane.peak = Math.max(windowMax, lane.level, lane.peak - PEAK_DECAY_PER_SECOND * dt);
      }

      ctx.clearRect(0, 0, geom.cssW, geom.cssH);
      ctx.globalAlpha = 1;
      ctx.shadowBlur = 0;
      ctx.fillStyle = palette.line;
      ctx.fillRect(0, geom.dividerY, geom.cssW, 1);

      drawLane(lanes[0], geom.baseTop, palette.accent, live[0], mutes[0]);
      drawLane(lanes[1], geom.baseBottom, palette.accent2, live[1], mutes[1]);
    };

    const start = () => {
      if (running) return;
      running = true;
      lastFrame = 0;
      lastCommit = performance.now();
      raf = requestAnimationFrame(frame);
    };

    const stop = () => {
      if (!running) return;
      running = false;
      cancelAnimationFrame(raf);
      raf = 0;
    };

    const onVisibility = () => {
      if (document.hidden) stop();
      else start();
    };

    const onMotionChange = (ev: MediaQueryListEvent) => {
      reduced = ev.matches;
    };

    const observer = new ResizeObserver(() => {
      applySize();
    });
    observer.observe(wrap);

    // devicePixelRatio changes when the window moves to another display or the OS
    // zoom changes. There is no dedicated event, so watch the current ratio.
    let dprQuery: MediaQueryList | null = null;
    const onDprChange = () => {
      applySize();
      watchDpr();
    };
    const watchDpr = () => {
      if (dprQuery) dprQuery.removeEventListener("change", onDprChange);
      try {
        dprQuery = window.matchMedia(`(resolution: ${window.devicePixelRatio || 1}dppx)`);
        dprQuery.addEventListener("change", onDprChange);
      } catch {
        dprQuery = null;
      }
    };

    applySize();
    watchDpr();
    motionQuery.addEventListener("change", onMotionChange);
    document.addEventListener("visibilitychange", onVisibility);
    if (!document.hidden) start();

    return () => {
      stop();
      observer.disconnect();
      motionQuery.removeEventListener("change", onMotionChange);
      document.removeEventListener("visibilitychange", onVisibility);
      if (dprQuery) dprQuery.removeEventListener("change", onDprChange);
    };
  }, []);

  const { active, muted } = props;

  return (
    <div
      ref={wrapRef}
      role="group"
      aria-label="Live audio levels"
      className="relative h-full min-h-0 w-full min-w-0 overflow-hidden bg-surface"
    >
      <canvas ref={canvasRef} aria-hidden="true" className="pointer-events-none absolute inset-0 block" />

      <div
        ref={clientTagRef}
        className={`pointer-events-none absolute left-3 flex -translate-y-1/2 items-baseline ${
          active.client ? "opacity-100" : "opacity-40"
        }`}
      >
        <span className={`${TAG_BASE} ${muted.client ? TAG_MUTED : TAG_LIVE}`}>Client</span>
        {muted.client ? <span className="sr-only">, muted</span> : null}
      </div>

      <div
        ref={repTagRef}
        className={`pointer-events-none absolute left-3 flex -translate-y-1/2 items-baseline ${
          active.rep ? "opacity-100" : "opacity-40"
        }`}
      >
        <span className={`${TAG_BASE} ${muted.rep ? TAG_MUTED : TAG_LIVE}`}>You</span>
        {muted.rep ? <span className="sr-only">, muted</span> : null}
      </div>
    </div>
  );
}
