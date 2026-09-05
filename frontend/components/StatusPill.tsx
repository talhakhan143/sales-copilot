import type { CallStatus } from "@/lib/types";

export interface StatusPillProps {
  state: CallStatus;
  detail?: string | null;
  /**
   * True while suggestion tokens are landing on the glass. This is the derived LIVE
   * state, and it wins over `state` unless the link itself is down, because during a
   * stream the hook still reports `thinking` and the rep needs to know the difference
   * between the machine being silent and words arriving right now.
   */
  streaming?: boolean;
}

interface PillLook {
  /** The one word. Short, uppercase, mono. */
  word: string;
  /** The 6px square mark. Square, so it is never confused with the round RTT dot. */
  mark: string;
  /** The word colour. */
  ink: string;
  /** True for error and closed: the container hairline goes danger. */
  down: boolean;
  /** True for thinking: a 2px sweep runs under the word. */
  sweep: boolean;
  /** Spoken by a screen reader in place of the terse word. */
  spoken: string;
}

/**
 * There are no spinners in this product. A solid mark reads faster than one, and
 * a hollow mark reads as "nothing is happening yet" without any motion at all.
 */
const LOOK: Record<CallStatus, PillLook> = {
  connecting: {
    word: "CONNECTING",
    mark: "border border-line-strong bg-transparent",
    ink: "text-muted",
    down: false,
    sweep: false,
    spoken: "Connecting",
  },
  ready: {
    word: "READY",
    mark: "border border-line-strong bg-transparent",
    ink: "text-muted",
    down: false,
    sweep: false,
    spoken: "Ready and waiting",
  },
  listening: {
    word: "LISTENING",
    mark: "bg-accent animate-breathe",
    ink: "text-text",
    down: false,
    sweep: false,
    spoken: "Listening",
  },
  transcribing: {
    word: "HEARING",
    mark: "bg-accent",
    ink: "text-text",
    down: false,
    sweep: false,
    spoken: "Writing down what they said",
  },
  thinking: {
    word: "THINKING",
    mark: "bg-accent-2",
    ink: "text-text",
    down: false,
    sweep: true,
    spoken: "Thinking of your answer",
  },
  error: {
    word: "OFFLINE",
    mark: "bg-danger",
    ink: "text-danger",
    down: true,
    sweep: false,
    spoken: "Offline, the link is broken",
  },
  closed: {
    word: "OFFLINE",
    mark: "bg-danger",
    ink: "text-danger",
    down: true,
    sweep: false,
    spoken: "Offline, the link is closed",
  },
};

/**
 * The derived state. Not a member of CallStatus: the hook reports `thinking` for the
 * whole generation, so the only thing that separates "still silent" from "words are
 * landing" is the stream flag. Solid mark, no sweep, no motion of any kind, because
 * the glass beside it is already moving and two moving things read as noise.
 */
const LIVE_LOOK: PillLook = {
  word: "LIVE",
  mark: "bg-accent-2",
  ink: "text-text",
  down: false,
  sweep: false,
  spoken: "The answer is coming in now",
};

/**
 * The one question this answers in 200 ms: what is the machine doing right now.
 *
 * A hairline box of fixed width holding a 6px square mark and one mono word. The
 * box never resizes between states, and the word is always left aligned at the
 * same x, so the eye lands on a glyph rather than on a search.
 */
export function StatusPill({ state, detail, streaming = false }: StatusPillProps) {
  const down = state === "error" || state === "closed";
  const look = streaming && !down ? LIVE_LOOK : LOOK[state];
  const trimmed = detail ? detail.trim() : "";

  return (
    <div
      className={`relative flex h-[26px] min-w-[116px] items-center gap-2 rounded-hair border px-2.5 ${
        look.down ? "border-danger" : "border-line-strong"
      }`}
    >
      <span
        aria-hidden="true"
        className={`h-1.5 w-1.5 shrink-0 ${look.mark}`}
      />

      <div
        role="status"
        aria-live="polite"
        className="flex min-w-0 items-baseline gap-2"
      >
        <span className={`shrink-0 font-mono text-status uppercase ${look.ink}`}>
          {look.word}
        </span>
        {trimmed ? (
          <span className="max-w-[132px] truncate font-mono text-micro uppercase text-muted">
            {trimmed}
          </span>
        ) : null}
        <span className="sr-only">{look.spoken}</span>
      </div>

      {look.sweep ? <span aria-hidden="true" className="status-sweep" /> : null}
    </div>
  );
}
