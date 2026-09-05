"use client";

import { useCallback, useEffect, useId, useRef, useState } from "react";
import { Mic, MicOff, RefreshCw } from "lucide-react";
import type { StreamKind } from "@/lib/types";

export interface AudioSourcePickerProps {
  devices: MediaDeviceInfo[];
  captures: { client: boolean; rep: boolean };
  muted: { client: boolean; rep: boolean };
  sensitivity: number;
  busy: StreamKind | null;
  onStartClient(source: { type: "display" } | { type: "device"; deviceId: string }): void;
  onStartRep(deviceId?: string): void;
  onStop(kind: StreamKind): void;
  onToggleMute(kind: StreamKind): void;
  onRefreshDevices(): void;
  onSensitivity(v: number): void;
  /**
   * Latest capture failure per lane, already phrased for a human. Optional so a
   * caller that does not track failures per lane keeps compiling. When present it
   * turns that trigger dot danger red and prints the fix inside the row, which is
   * where the rep is already looking when a start fails (DESIGN 5.8).
   */
  errors?: { client: string | null; rep: string | null };
}

/*
 * A hand built popover in the top bar. No library, no portal, no backdrop filter.
 * It closes on Escape, on an outside pointerdown, and when focus leaves it, and it
 * hands focus back to the trigger on Escape so the keyboard user is never dropped
 * at the top of the document.
 */

const DISPLAY_VALUE = "display";
const SENS_MIN = 0.5;
const SENS_MAX = 3;
const SENS_STEP = 0.1;

/**
 * Higher sensitivity divides the VAD open threshold, so a bigger number means the
 * segmenter opens on quieter audio. The words below say that in plain language,
 * because "1.8" means nothing to a rep two minutes before a call.
 */
function describeSensitivity(v: number): string {
  if (v >= 2.4) return "Very sensitive, picks up quiet speech.";
  if (v >= 1.7) return "Sensitive, good for a quiet room.";
  if (v >= 1.2) return "Balanced, works in most rooms.";
  if (v >= 0.8) return "Firm, ignores light room noise.";
  return "Strict, ignores background noise.";
}

function deviceLabel(device: MediaDeviceInfo, index: number): string {
  return device.label.trim() || `Input ${index + 1}`;
}

const SELECT_CLASS =
  "h-9 min-w-0 flex-1 rounded-hair border border-line-strong bg-surface px-2 font-sans text-chip text-text disabled:opacity-40";

const ACTION_CLASS =
  "h-9 shrink-0 rounded-hair border border-line-strong px-3 font-mono text-[12px] font-semibold uppercase tracking-[0.12em] transition-colors duration-[120ms] ease-out hover:bg-surface disabled:pointer-events-none disabled:opacity-40";

const ICON_BUTTON_CLASS =
  "flex h-9 w-9 shrink-0 items-center justify-center rounded-hair border border-line-strong transition-colors duration-[120ms] ease-out hover:bg-surface disabled:pointer-events-none disabled:opacity-40";

interface SourceRowProps {
  label: string;
  selectId: string;
  selectLabel: string;
  running: boolean;
  muted: boolean;
  busy: boolean;
  value: string;
  options: { value: string; label: string }[];
  onValue(v: string): void;
  onStart(): void;
  onStop(): void;
  onToggleMute(): void;
  muteLabel: string;
  unmuteLabel: string;
  startLabel: string;
  stopLabel: string;
  error: string | null;
  errorId: string;
}

function SourceRow(props: SourceRowProps) {
  const {
    label,
    selectId,
    selectLabel,
    running,
    muted,
    busy,
    value,
    options,
    onValue,
    onStart,
    onStop,
    onToggleMute,
    muteLabel,
    unmuteLabel,
    startLabel,
    stopLabel,
    error,
    errorId,
  } = props;

  const shownError = running || busy ? null : error;
  const state = busy ? (running ? "Stopping" : "Starting") : running ? (muted ? "Muted" : "Live") : "Off";
  const stateClass = busy
    ? "text-muted"
    : running
      ? muted
        ? "text-warn"
        : "text-ok"
      : shownError
        ? "text-danger"
        : "text-muted";

  return (
    <div className="flex flex-col gap-1.5 py-2">
      <div className="flex items-baseline justify-between gap-2">
        <label htmlFor={selectId} className="font-mono text-micro uppercase text-muted">
          {label}
        </label>
        <span className={`font-mono text-micro uppercase ${stateClass}`} aria-live="polite">
          {shownError ? "Failed" : state}
        </span>
      </div>

      <div className="flex items-center gap-2">
        <select
          id={selectId}
          aria-label={selectLabel}
          className={SELECT_CLASS}
          value={value}
          disabled={running || busy}
          onChange={(e) => onValue(e.target.value)}
        >
          {options.map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </select>

        <button
          type="button"
          className={`${ACTION_CLASS} ${running ? "text-danger" : "text-accent"}`}
          onClick={running ? onStop : onStart}
          disabled={busy}
          aria-busy={busy}
          aria-describedby={shownError ? errorId : undefined}
        >
          {busy ? "Wait" : running ? stopLabel : startLabel}
        </button>

        <button
          type="button"
          className={`${ICON_BUTTON_CLASS} ${muted ? "text-warn" : "text-muted"}`}
          onClick={onToggleMute}
          disabled={!running || busy}
          aria-pressed={muted}
          aria-label={muted ? unmuteLabel : muteLabel}
          title={muted ? unmuteLabel : muteLabel}
        >
          {muted ? (
            <MicOff aria-hidden="true" className="h-4 w-4" />
          ) : (
            <Mic aria-hidden="true" className="h-4 w-4" />
          )}
        </button>
      </div>

      {shownError ? (
        <p id={errorId} className="font-sans text-[12px] leading-[18px] text-danger">
          {shownError}
        </p>
      ) : null}
    </div>
  );
}

export function AudioSourcePicker(props: AudioSourcePickerProps) {
  const {
    devices,
    captures,
    muted,
    sensitivity,
    busy,
    onStartClient,
    onStartRep,
    onStop,
    onToggleMute,
    onRefreshDevices,
    onSensitivity,
    errors,
  } = props;

  const rootRef = useRef<HTMLDivElement | null>(null);
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const panelRef = useRef<HTMLDivElement | null>(null);
  const refreshRef = useRef(onRefreshDevices);
  const [open, setOpen] = useState(false);
  const [clientChoice, setClientChoice] = useState<string>(DISPLAY_VALUE);
  const [repChoice, setRepChoice] = useState<string>("");

  const baseId = useId();
  const panelId = `${baseId}-panel`;
  const clientSelectId = `${baseId}-client`;
  const repSelectId = `${baseId}-rep`;
  const sensId = `${baseId}-sens`;
  const sensHintId = `${baseId}-sens-hint`;
  const clientErrorId = `${baseId}-client-error`;
  const repErrorId = `${baseId}-rep-error`;

  const clientError = captures.client ? null : (errors?.client ?? null);
  const repError = captures.rep ? null : (errors?.rep ?? null);

  useEffect(() => {
    refreshRef.current = onRefreshDevices;
  });

  const close = useCallback((returnFocus: boolean) => {
    setOpen(false);
    if (returnFocus) triggerRef.current?.focus();
  }, []);

  // Escape and outside pointerdown, bound only while the panel is open.
  useEffect(() => {
    if (!open) return;

    const onKeyDown = (ev: KeyboardEvent) => {
      if (ev.key === "Escape") {
        ev.stopPropagation();
        close(true);
      }
    };
    const onPointerDown = (ev: PointerEvent) => {
      const root = rootRef.current;
      const target = ev.target;
      if (root && target instanceof Node && !root.contains(target)) close(false);
    };

    document.addEventListener("keydown", onKeyDown);
    document.addEventListener("pointerdown", onPointerDown, true);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      document.removeEventListener("pointerdown", onPointerDown, true);
    };
  }, [open, close]);

  // Focus the panel on open, and take a fresh device list while we are at it.
  useEffect(() => {
    if (!open) return;
    panelRef.current?.focus();
    refreshRef.current();
  }, [open]);

  // A device can vanish (cable unplugged) while it is the selected one. Resolve the
  // fallback during render rather than in an effect: no cascading render, and the
  // original pick comes back by itself once the device is plugged in again.
  const clientChoiceLive =
    clientChoice === DISPLAY_VALUE || devices.some((d) => d.deviceId === clientChoice)
      ? clientChoice
      : DISPLAY_VALUE;
  const repChoiceLive =
    repChoice === "" || devices.some((d) => d.deviceId === repChoice) ? repChoice : "";

  const clientOptions = [
    { value: DISPLAY_VALUE, label: "Share a tab or screen" },
    ...devices.map((d, i) => ({ value: d.deviceId, label: deviceLabel(d, i) })),
  ];
  const repOptions = [
    { value: "", label: "System default microphone" },
    ...devices.map((d, i) => ({ value: d.deviceId, label: deviceLabel(d, i) })),
  ];

  const startClient = () => {
    if (clientChoiceLive === DISPLAY_VALUE) onStartClient({ type: "display" });
    else onStartClient({ type: "device", deviceId: clientChoiceLive });
  };

  const sensPct = ((sensitivity - SENS_MIN) / (SENS_MAX - SENS_MIN)) * 100;
  const sensClamped = sensPct < 0 ? 0 : sensPct > 100 ? 100 : sensPct;

  // Capturing wins over a message the caller has not cleared yet: a lane that is
  // live is not failing. Otherwise a failure is danger, and silence is dim.
  const dotClass = (on: boolean, failed: boolean) =>
    `h-[5px] w-[5px] rounded-full ${on ? "bg-ok" : failed ? "bg-danger" : "bg-dim"}`;

  return (
    <div
      ref={rootRef}
      className="relative"
      onBlur={(ev) => {
        // Close when focus genuinely leaves for another element. A null
        // relatedTarget (a click on dead space inside the panel) is not a leave.
        const next = ev.relatedTarget;
        if (next instanceof Node && !ev.currentTarget.contains(next)) setOpen(false);
      }}
    >
      <button
        ref={triggerRef}
        type="button"
        aria-haspopup="dialog"
        aria-expanded={open}
        aria-controls={open ? panelId : undefined}
        aria-label={`Audio sources. Client ${
          captures.client ? "capturing" : clientError ? "failed" : "off"
        }, you ${captures.rep ? "capturing" : repError ? "failed" : "off"}.`}
        onClick={() => (open ? close(true) : setOpen(true))}
        className="flex h-[26px] items-center gap-2 rounded-hair border border-line-strong px-2.5 font-mono text-micro uppercase text-muted transition-colors duration-[120ms] ease-out hover:bg-surface-2 hover:text-text"
      >
        <span aria-hidden="true" className="flex items-center gap-1">
          <span className={dotClass(captures.client, clientError !== null)} />
          <span className={dotClass(captures.rep, repError !== null)} />
        </span>
        <span aria-hidden="true">Sources</span>
      </button>

      {open ? (
        <div
          ref={panelRef}
          id={panelId}
          role="dialog"
          aria-modal="false"
          aria-label="Audio sources"
          tabIndex={-1}
          className="absolute right-0 top-[34px] z-50 w-[360px] max-w-[calc(100vw-24px)] rounded-pop border border-line-strong bg-surface-2 p-3 shadow-pop"
        >
          <div className="flex items-center justify-between gap-2 pb-1">
            <span className="font-mono text-micro uppercase text-muted">Audio sources</span>
            <button
              type="button"
              onClick={() => onRefreshDevices()}
              className="flex h-6 items-center gap-1.5 rounded-hair border border-line-strong px-2 font-mono text-micro uppercase text-muted transition-colors duration-[120ms] ease-out hover:bg-surface hover:text-text"
            >
              <RefreshCw aria-hidden="true" className="h-3 w-3" />
              <span>Look again</span>
            </button>
          </div>

          <div className="border-b border-line">
            <SourceRow
              label="Client"
              selectId={clientSelectId}
              selectLabel="Client audio source"
              running={captures.client}
              muted={muted.client}
              busy={busy === "client"}
              value={clientChoiceLive}
              options={clientOptions}
              onValue={setClientChoice}
              onStart={startClient}
              onStop={() => onStop("client")}
              onToggleMute={() => onToggleMute("client")}
              muteLabel="Mute the client lane"
              unmuteLabel="Unmute the client lane"
              startLabel="Start"
              stopLabel="Stop"
              error={clientError}
              errorId={clientErrorId}
            />
          </div>

          <div className="border-b border-line">
            <SourceRow
              label="You"
              selectId={repSelectId}
              selectLabel="Your microphone"
              running={captures.rep}
              muted={muted.rep}
              busy={busy === "rep"}
              value={repChoiceLive}
              options={repOptions}
              onValue={setRepChoice}
              onStart={() => onStartRep(repChoiceLive || undefined)}
              onStop={() => onStop("rep")}
              onToggleMute={() => onToggleMute("rep")}
              muteLabel="Mute your microphone"
              unmuteLabel="Unmute your microphone"
              startLabel="Start"
              stopLabel="Stop"
              error={repError}
              errorId={repErrorId}
            />
          </div>

          <div className="flex flex-col gap-1.5 border-b border-line py-2">
            <div className="flex items-baseline justify-between gap-2">
              <label htmlFor={sensId} className="font-mono text-micro uppercase text-muted">
                Sensitivity
              </label>
              <span className="font-mono text-micro tabnum text-dim">{sensitivity.toFixed(1)}x</span>
            </div>

            <div className="relative flex h-6 items-center">
              <span
                aria-hidden="true"
                className="pointer-events-none absolute left-0 right-0 top-1/2 h-[3px] -translate-y-1/2 rounded-hair bg-line-strong"
              />
              <span
                aria-hidden="true"
                className="pointer-events-none absolute left-0 top-1/2 h-[3px] -translate-y-1/2 rounded-hair bg-accent"
                style={{ width: `${sensClamped}%` }}
              />
              <input
                id={sensId}
                type="range"
                min={SENS_MIN}
                max={SENS_MAX}
                step={SENS_STEP}
                value={sensitivity}
                aria-describedby={sensHintId}
                aria-valuetext={`${sensitivity.toFixed(1)}. ${describeSensitivity(sensitivity)}`}
                onChange={(e) => onSensitivity(Number(e.target.value))}
                className="relative h-6 w-full cursor-pointer appearance-none bg-transparent [&::-moz-range-thumb]:h-[13px] [&::-moz-range-thumb]:w-[13px] [&::-moz-range-thumb]:rounded-hair [&::-moz-range-thumb]:border-0 [&::-moz-range-thumb]:bg-accent [&::-moz-range-track]:h-[3px] [&::-moz-range-track]:bg-transparent [&::-webkit-slider-runnable-track]:h-[3px] [&::-webkit-slider-runnable-track]:bg-transparent [&::-webkit-slider-thumb]:mt-[-5px] [&::-webkit-slider-thumb]:h-[13px] [&::-webkit-slider-thumb]:w-[13px] [&::-webkit-slider-thumb]:appearance-none [&::-webkit-slider-thumb]:rounded-hair [&::-webkit-slider-thumb]:bg-accent"
              />
            </div>

            <p id={sensHintId} className="font-sans text-[12px] leading-[18px] text-muted">
              {describeSensitivity(sensitivity)}
            </p>
          </div>

          <div className="pt-2 font-sans text-[12px] leading-[18px] text-muted">
            <p>Route call audio through BlackHole on mac or VB-Cable on Windows, then pick it here.</p>
            <p>Loopback on mac does the same job, send the call app output into the virtual device.</p>
            <p>Or share the browser tab with the call and tick Share tab audio in the picker.</p>
          </div>
        </div>
      ) : null}
    </div>
  );
}
