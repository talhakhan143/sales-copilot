"use client";

/**
 * The teleprompter. The one thing on the page that matters.
 *
 * Everything here exists to keep the first line of text nailed to a fixed pixel
 * and to make sure a glyph never mutates under a reading eye:
 *
 *  - The first line baseline is fixed by the head strip height plus the top
 *    padding, inside a 1fr row of a 100dvh grid. It never moves between idle,
 *    cue, streaming, settled or replacement.
 *  - A partial token never renders. The hook commits deltas up to the last word
 *    boundary, so this component only ever receives whole words.
 *  - Text never resizes while streaming. The fit down step runs once, on settle.
 *  - The measure never scrolls. Overflow is answered by the fit down step and by
 *    nothing else, so the first line cannot be dragged out from under the eye.
 *  - An error never touches the measure. Only a new suggestion changes it.
 *
 * `status` is optional and additive to the frozen prop set. It carries the two
 * things the other props cannot express: the STT half of the dead window (the
 * server is thinking before a suggestion id exists), and a dead link, which is
 * what turns the boresight rose.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Check, Copy, RotateCcw } from "lucide-react";
import type { CallStatus, PromptStyle, SuggestionTrigger } from "@/lib/types";

export interface TeleprompterProps {
  /** The suggestion currently streaming, or the last completed one. */
  text: string;
  /** True from suggestion_start until suggestion_done. */
  streaming: boolean;
  /** What caused this suggestion, or null when nothing has arrived yet. */
  trigger: SuggestionTrigger | null;
  /** The prospect line, the objection label, or the typed text behind it. */
  sourceText: string | null;
  /** Second idle line. Uppercase, it renders in the micro label style. */
  idleHint?: string;
  /**
   * How the copilot writes. "full" is a whole line to read out loud, "points"
   * is a few words to speak around so the rep uses their own voice instead of
   * sounding like somebody reading a script.
   *
   * Optional, so the older call sites still type check. Without the setter the
   * switch is not drawn at all, rather than drawn and dead.
   */
  promptStyle?: PromptStyle;
  /** Called with the style the rep just picked. */
  onPromptStyle?: (next: PromptStyle) => void;
  /**
   * The hook's call status. Optional, so the frozen four prop call site still
   * type checks. Without it the cue window starts late (at the first token
   * instead of at the top of the utterance) and the boresight never reports a
   * dead link.
   */
  status?: CallStatus;
}

type Phase = "live" | "exit" | "blank";
type CaretState = "flowing" | "stalled" | "done";

interface RailStyle {
  transform: string;
  opacity: number;
  transition: string;
}

const IDLE_LINE = "Waiting for the client to speak.";
const DEFAULT_HINT = "PRESS 1 TO 8 FOR AN INSTANT LINE";

/** Motion budget, section 7 of the design spec. */
const EXIT_MS = 110;
const BLANK_MS = 60;
const STALL_MS = 400;
const SIGNOFF_MS = 1120;
const ACK_MS = 500;
const FLASH_MS = 300;
const COPIED_MS = 1200;

/** How long the head strip keeps saying LIVE after a suggestion settles. */
const LIVE_MS = 6000;

/**
 * Barge in window.
 *
 * The server cancels an in flight suggestion by emitting `suggestion_done` for
 * the dead id and then `suggestion_start` for the new one (CONTRACT 2.2, and
 * backend `_start_llm` which awaits the cancelled task before creating the next
 * one). Those are two socket frames, so React commits twice and this component
 * sees a settle immediately followed by a restart, with `streaming` already back
 * to false. Without this window the replacement would take the 320 ms graceful
 * path that the spec reserves for replacing genuinely settled text, and on a
 * barge in grace loses to latency.
 *
 * Anything the server sends back to back lands inside a few milliseconds, so 200
 * is generous. A real settle followed by a real new suggestion cannot happen
 * this fast: a speech trigger needs a whole utterance plus STT, and a chip needs
 * a round trip that the rep would have had to start before the line settled,
 * which is itself a barge in.
 */
const BARGE_MS = 200;

const RAIL_OFF: RailStyle = { transform: "scaleX(0)", opacity: 0, transition: "none" };

/**
 * Head strip kicker. The vocabulary is fixed by design 5.4 and it reports the
 * STATE, not the cause, because the one thing the rep needs from this strip is
 * whether the copy on the glass is live or stale. The cause gets its own word
 * beside it.
 */
/** The two ways the copilot can write, as the head strip draws them. */
const STYLE_CHOICES: readonly { value: PromptStyle; label: string; title: string }[] = [
  { value: "full", label: "Line", title: "Give me the whole line to read out loud" },
  { value: "points", label: "Points", title: "Give me a few words and I will say it myself" },
];

function kickerFor(cue: boolean, streaming: boolean, transcribing: boolean, live: boolean): string {
  if (cue) return "THINKING";
  if (streaming) return "RECEIVING";
  if (transcribing) return "HEARD";
  if (live) return "LIVE";
  return "STANDBY";
}

/** The cause, printed beside the state word rather than instead of it. */
function originFor(trigger: SuggestionTrigger | null): string | null {
  if (trigger === "speech") return "CLIENT SAID";
  if (trigger === "quick_action") return "OBJECTION";
  if (trigger === "manual") return "MANUAL";
  return null;
}

/** True when the keystroke belongs to a field, so the letter shortcuts stay dead. */
function isEditable(node: unknown): boolean {
  if (!node || typeof node !== "object") return false;
  const el = node as HTMLElement;
  const tag = typeof el.tagName === "string" ? el.tagName.toLowerCase() : "";
  if (tag === "input" || tag === "textarea" || tag === "select") return true;
  return el.isContentEditable === true;
}

/** Clipboard with a fallback for insecure origins, where navigator.clipboard is absent. */
async function writeClipboard(value: string): Promise<boolean> {
  try {
    if (typeof navigator !== "undefined" && navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(value);
      return true;
    }
  } catch {
    // Fall through to the textarea path.
  }
  try {
    const holder = document.createElement("textarea");
    holder.value = value;
    holder.setAttribute("readonly", "");
    holder.style.position = "fixed";
    holder.style.top = "0";
    holder.style.left = "0";
    holder.style.opacity = "0";
    holder.style.pointerEvents = "none";
    document.body.appendChild(holder);
    holder.select();
    const ok = document.execCommand("copy");
    document.body.removeChild(holder);
    return ok;
  } catch {
    return false;
  }
}

/** Monotonic clock, with a fallback for the rare environment without it. */
function nowMs(): number {
  return typeof performance !== "undefined" && typeof performance.now === "function"
    ? performance.now()
    : Date.now();
}

export function Teleprompter({
  text,
  streaming,
  trigger,
  sourceText,
  idleHint,
  status,
  promptStyle = "full",
  onPromptStyle,
}: TeleprompterProps) {
  const [phase, setPhase] = useState<Phase>("live");
  const [heldText, setHeldText] = useState("");
  const [generation, setGeneration] = useState(0);
  const [stalled, setStalled] = useState(false);
  const [signoff, setSignoff] = useState(false);
  const [live, setLive] = useState(false);
  const [ack, setAck] = useState(false);
  const [flash, setFlash] = useState(false);
  const [copied, setCopied] = useState(false);
  const [announced, setAnnounced] = useState("");
  const [rail, setRail] = useState<RailStyle>(RAIL_OFF);
  const [reduced, setReduced] = useState(false);

  const scrollRef = useRef<HTMLDivElement | null>(null);

  /* The copy and replay handlers are bound to a window listener that must not be
     torn down and rebuilt sixty times a second, so they read the text through a
     ref. The ref is written after the commit, never during render: a render
     React throws away must not be able to leave a value behind. */
  const textRef = useRef(text);
  useEffect(() => {
    textRef.current = text;
  });

  const prevTextRef = useRef("");
  const prevStreamingRef = useRef(false);
  const settledAtRef = useRef(0);
  const timersRef = useRef<number[]>([]);
  const rafRef = useRef(0);
  const copyTimerRef = useRef(0);

  const clearTimers = useCallback(() => {
    for (const id of timersRef.current) window.clearTimeout(id);
    timersRef.current = [];
    if (rafRef.current) {
      window.cancelAnimationFrame(rafRef.current);
      rafRef.current = 0;
    }
  }, []);

  const later = useCallback((fn: () => void, ms: number) => {
    const id = window.setTimeout(fn, ms);
    timersRef.current.push(id);
  }, []);

  /* ---------------------------------------------------------------- */
  /* Reduced motion                                                    */
  /* ---------------------------------------------------------------- */

  useEffect(() => {
    const query = window.matchMedia("(prefers-reduced-motion: reduce)");
    const apply = () => setReduced(query.matches);
    apply();
    query.addEventListener("change", apply);
    return () => query.removeEventListener("change", apply);
  }, []);

  /* ---------------------------------------------------------------- */
  /* Fit down. Runs once on settle, never while streaming.             */
  /* ---------------------------------------------------------------- */

  const resetFit = useCallback(() => {
    scrollRef.current?.style.setProperty("--tp-fit", "1");
  }, []);

  const runFit = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    el.style.setProperty("--tp-fit", "1");
    for (const step of ["0.90", "0.80"]) {
      if (el.scrollHeight <= el.clientHeight) break;
      el.style.setProperty("--tp-fit", step);
    }
  }, []);

  /* ---------------------------------------------------------------- */
  /* The one state machine: start, replace, settle.                    */
  /* ---------------------------------------------------------------- */

  useEffect(() => {
    const prevText = prevTextRef.current;
    const wasStreaming = prevStreamingRef.current;
    const at = nowMs();

    // A settle is never a restart, even when the server's final text is not a
    // literal extension of the streamed prefix (trimmed whitespace, say).
    const settled = wasStreaming && !streaming && text.length > 0;
    const restarted =
      !settled &&
      ((streaming && !wasStreaming) || (prevText.length > 0 && !text.startsWith(prevText)));
    const firstWord = prevText.length === 0 && text.length > 0;

    // A settle that is under 200 ms old was the server closing out a generation
    // it had just cancelled, so this restart is a barge in even though
    // `streaming` has already been through false. See BARGE_MS.
    const bargeIn = settledAtRef.current > 0 && at - settledAtRef.current <= BARGE_MS;

    if (restarted) {
      clearTimers();
      resetFit();
      settledAtRef.current = 0;
      setSignoff(false);
      setStalled(false);
      setLive(false);
      setAnnounced("");

      if (prevText.length > 0) {
        setFlash(true);
        later(() => setFlash(false), FLASH_MS);
      }

      // The rail fill curve: quick to 62 percent, then a slow crawl to 92.
      setRail({ transform: "scaleX(0)", opacity: 1, transition: "none" });
      rafRef.current = window.requestAnimationFrame(() => {
        rafRef.current = 0;
        setRail({
          transform: "scaleX(0.62)",
          opacity: 1,
          transition: "transform 1400ms var(--e-out)",
        });
      });
      later(
        () =>
          setRail({
            transform: "scaleX(0.92)",
            opacity: 1,
            transition: "transform 3000ms linear",
          }),
        1400,
      );

      if (wasStreaming || bargeIn || prevText.length === 0 || reduced) {
        // Barge in cuts in one frame, because the rep needs the new first word
        // more than they need a graceful handover. From idle it just enters.
        setPhase("live");
        setGeneration((g) => g + 1);
      } else {
        // Replacing settled text: exit, then a genuinely blank measure, then enter.
        setHeldText(prevText);
        setPhase("exit");
        later(() => setPhase("blank"), EXIT_MS);
        later(() => {
          setPhase("live");
          setGeneration((g) => g + 1);
        }, EXIT_MS + BLANK_MS);
      }
    }

    if (firstWord) {
      setAck(true);
      later(() => setAck(false), ACK_MS);
    }

    if (settled) {
      settledAtRef.current = at;
      setStalled(false);

      // Everything the settle paints is deferred by one frame on purpose. A
      // barge in arrives in the same frame as the settle it cancels, and the
      // restart branch above calls clearTimers(), which kills this callback. So
      // a generation the server already threw away never gets a signoff blink,
      // never completes the rail, never resizes the text and never reaches the
      // screen reader.
      rafRef.current = window.requestAnimationFrame(() => {
        rafRef.current = 0;
        setSignoff(true);
        setLive(true);
        setAnnounced(text);
        later(() => setSignoff(false), SIGNOFF_MS);
        later(() => setLive(false), LIVE_MS);

        setRail({ transform: "scaleX(1)", opacity: 1, transition: "transform 160ms var(--e-out)" });
        later(() => setRail((r) => ({ ...r, opacity: 0, transition: "opacity 320ms linear" })), 400);
        later(() => setRail(RAIL_OFF), 760);

        if (!reduced) runFit();
      });
    }

    prevTextRef.current = text;
    prevStreamingRef.current = streaming;
  }, [text, streaming, reduced, clearTimers, later, resetFit, runFit]);

  useEffect(() => clearTimers, [clearTimers]);

  /* ---------------------------------------------------------------- */
  /* Caret stall. The only blink in the product, it means "waiting".   */
  /* ---------------------------------------------------------------- */

  /* The reset lives in the cleanup rather than the body: the cleanup of the
     previous run fires on every dependency change and on unmount, so a new word
     or the end of the stream clears the stall without a synchronous setState in
     an effect body. */
  useEffect(() => {
    if (!streaming) return;
    const id = window.setTimeout(() => setStalled(true), STALL_MS);
    return () => {
      window.clearTimeout(id);
      setStalled(false);
    };
  }, [streaming, text]);

  /* ---------------------------------------------------------------- */
  /* Copy and replay                                                   */
  /* ---------------------------------------------------------------- */

  const doCopy = useCallback(() => {
    const value = textRef.current.trim();
    if (!value) return;
    void writeClipboard(value).then((ok) => {
      if (!ok) return;
      setCopied(true);
      window.clearTimeout(copyTimerRef.current);
      copyTimerRef.current = window.setTimeout(() => setCopied(false), COPIED_MS);
    });
  }, []);

  const doReplay = useCallback(() => {
    if (!textRef.current) return;
    setPhase("live");
    setGeneration((g) => g + 1);
  }, []);

  useEffect(() => () => window.clearTimeout(copyTimerRef.current), []);

  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if (event.ctrlKey || event.metaKey || event.altKey || event.repeat) return;
      if (isEditable(event.target) || isEditable(document.activeElement)) return;
      const key = event.key.toLowerCase();
      if (key === "c") {
        event.preventDefault();
        doCopy();
      } else if (key === "r") {
        event.preventDefault();
        doReplay();
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [doCopy, doReplay]);

  /* ---------------------------------------------------------------- */
  /* Derived render values                                             */
  /* ---------------------------------------------------------------- */

  const body = phase === "exit" ? heldText : phase === "blank" ? "" : text;
  const hasText = body.trim().length > 0;

  /* Points arrive as one per line with no full stops, so splitting on sentence
     ends would run all of them together into a paragraph and the rep would have
     to read it rather than scan it, which is the whole thing points are for.
     A newline in the text is the model saying "these are separate", so it wins
     over punctuation whenever it is there. */
  const sentences = useMemo(() => {
    if (!hasText) return [] as string[];
    if (body.includes("\n")) {
      const lines = body
        .split(/\n+/)
        .map((line) => line.trim())
        .filter((line) => line.length > 0);
      if (lines.length > 0) return lines;
    }
    const parts = body.split(/(?<=[.?!])\s+/).filter((part) => part.trim().length > 0);
    return parts.length > 0 ? parts : [body];
  }, [body, hasText]);

  const askIndex =
    sentences.length >= 2 && sentences[sentences.length - 1].trim().endsWith("?")
      ? sentences.length - 1
      : -1;

  const words = useMemo(() => {
    const trimmed = text.trim();
    return trimmed.length === 0 ? 0 : trimmed.split(/\s+/).length;
  }, [text]);

  const caret: CaretState | null = streaming
    ? stalled
      ? "stalled"
      : "flowing"
    : signoff
      ? "done"
      : null;

  /* The anxious window. It opens the moment the server says it is working, which
     is at the top of the utterance, well before a suggestion id exists, and it
     closes on the first committed word. Without `status` it can only open at
     suggestion_start, which is roughly half way through the silence. */
  const thinking = status === undefined ? streaming : status === "thinking";
  const cue = thinking && !(streaming && text.length > 0);

  /* The only fault signal inside the reading column. Rose means the link is
     gone, and it outranks every other boresight state. */
  const down = status === "error" || status === "closed";

  const transcribing = status === "transcribing";
  const kicker = kickerFor(cue, streaming, transcribing, live);
  const origin = originFor(trigger);
  const source = sourceText && sourceText.trim().length > 0 ? sourceText.trim() : null;

  return (
    <section
      className="group/tp relative flex h-full min-h-0 min-w-0 flex-1 flex-col overflow-hidden bg-bg"
      aria-label="Teleprompter"
    >
      <div className={flash ? "tp-rail is-flash" : "tp-rail"} aria-hidden="true">
        <div
          className="tp-rail-fill"
          data-streaming={streaming ? "true" : "false"}
          style={{ transform: rail.transform, opacity: rail.opacity, transition: rail.transition }}
        />
      </div>

      <div
        className="flex shrink-0 items-center justify-between gap-3 border-b border-line px-[var(--tp-pad-x)]"
        style={{ height: "var(--tp-head)" }}
      >
        <div className="flex min-w-0 items-center gap-2.5">
          <span className="shrink-0 font-mono text-micro uppercase text-muted">{kicker}</span>
          {origin ? (
            <>
              <span className="h-3 w-px shrink-0 bg-line-strong" aria-hidden="true" />
              <span className="shrink-0 font-mono text-micro uppercase text-muted">{origin}</span>
            </>
          ) : null}
          {source ? (
            <span className="truncate font-sans text-[12px] leading-4 text-muted" title={source}>
              {source}
            </span>
          ) : null}
        </div>

        <div className="flex shrink-0 items-center gap-1.5">
          {/* READ IT, or SAY IT. Two segments rather than a checkbox, because a
              checkbox makes the rep work out what the unchecked state means
              while somebody is talking in their ear. Both words are always on
              screen and one of them is lit. */}
          {onPromptStyle ? (
            <div
              role="radiogroup"
              aria-label="How the copilot writes"
              className="mr-1 hidden overflow-hidden rounded-hair border border-line-strong sm:flex"
            >
              {STYLE_CHOICES.map((choice) => {
                const on = promptStyle === choice.value;
                return (
                  <button
                    key={choice.value}
                    type="button"
                    role="radio"
                    aria-checked={on}
                    title={choice.title}
                    onClick={() => onPromptStyle(choice.value)}
                    className={`flex h-[24px] items-center px-2 font-mono text-micro uppercase tracking-[0.08em] transition-colors duration-[120ms] ease-out ${
                      on
                        ? "bg-surface-2 text-accent"
                        : "text-dim hover:bg-surface-2 hover:text-muted"
                    }`}
                  >
                    {choice.label}
                  </button>
                );
              })}
            </div>
          ) : null}

          <span className="inline-flex w-[56px] justify-end">
            {copied ? (
              <span className="copy-ack font-mono text-micro uppercase tracking-[0.08em] text-ok">
                COPIED
              </span>
            ) : words > 0 ? (
              <span className="tabnum font-mono text-micro tracking-[0.08em] text-dim">
                {words} W
              </span>
            ) : null}
          </span>

          <button
            type="button"
            onClick={doCopy}
            disabled={words === 0}
            title="Copy the line (C)"
            aria-label="Copy the current line"
            className="flex h-[26px] w-[26px] items-center justify-center rounded-hair text-muted opacity-45 transition-opacity duration-[140ms] ease-out hover:opacity-100 focus-visible:opacity-100 disabled:opacity-20 [@media(hover:none)]:opacity-100"
          >
            {copied ? (
              <Check className="h-4 w-4 text-ok" aria-hidden="true" />
            ) : (
              <Copy className="h-4 w-4" aria-hidden="true" />
            )}
          </button>

          <button
            type="button"
            onClick={doReplay}
            disabled={words === 0}
            title="Show it again (R)"
            aria-label="Show this line again"
            className="flex h-[26px] w-[26px] items-center justify-center rounded-hair text-muted opacity-45 transition-opacity duration-[140ms] ease-out hover:opacity-100 focus-visible:opacity-100 disabled:opacity-20 [@media(hover:none)]:opacity-100"
          >
            <RotateCcw className="h-4 w-4" aria-hidden="true" />
          </button>
        </div>
      </div>

      <div
        className={ack && !cue && !down ? "boresight is-ack" : "boresight"}
        data-state={down ? "down" : cue ? "thinking" : undefined}
        aria-hidden="true"
      />

      {/* overflow-hidden, never a scroll container. Overflow is answered by the
          fit down step on settle, so the first line can never be dragged up
          under a reading eye while tokens arrive. */}
      <div
        ref={scrollRef}
        className="relative flex min-h-0 flex-1 flex-col overflow-hidden"
        style={{
          padding:
            "var(--tp-pad-t) var(--tp-pad-x) var(--tp-pad-b) calc(var(--tp-pad-x) + var(--tp-gutter))",
        }}
      >
        <div aria-live="polite" aria-atomic="true" className="min-h-0">
          {phase === "exit" ? (
            <div key={`exit-${generation}`} className="tp-text tp-exit" aria-hidden="true">
              {sentences.map((sentence, index) => (
                <span key={index} className="tp-sentence">
                  {sentence}
                </span>
              ))}
            </div>
          ) : phase === "blank" ? (
            <div key={`blank-${generation}`} className="tp-text" aria-hidden="true" />
          ) : hasText ? (
            <div key={`live-${generation}`} className="tp-text tp-enter" aria-hidden="true">
              {sentences.map((sentence, index) => (
                <span
                  key={index}
                  className="tp-sentence"
                  data-ask={index === askIndex ? "1" : undefined}
                >
                  {sentence}
                  {index === sentences.length - 1 && caret ? (
                    <span className="caret" data-state={caret} />
                  ) : null}
                </span>
              ))}
            </div>
          ) : cue ? (
            // Mid cue with an empty measure. The idle instruction belongs to
            // "connected, no suggestion ever, or reset" and nothing else, so it
            // must not flash in the window where words are already on their way.
            // The boresight is carrying that signal.
            <div key={`cue-${generation}`} className="tp-text" aria-hidden="true" />
          ) : (
            <div
              key={`idle-${generation}`}
              className="tp-enter"
              style={{ minHeight: "calc(4 * var(--tp-line))" }}
              aria-hidden="true"
            >
              <p className="max-w-[46ch] font-sans text-idle text-muted">{IDLE_LINE}</p>
              <p className="mt-3.5 font-mono text-micro uppercase text-muted">
                {idleHint && idleHint.trim().length > 0 ? idleHint : DEFAULT_HINT}
              </p>
            </div>
          )}

          <p className="sr-only">{announced}</p>
        </div>
      </div>
    </section>
  );
}
