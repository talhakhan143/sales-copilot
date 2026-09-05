"use client";

/**
 * The call launcher, a popover in the top bar right next to SOURCES.
 *
 * One question, answered before anything is pressed: how is this call going to
 * be placed, and what does that cost. Four options, in the order the backend
 * sends them, every one of them showing its price and, when it cannot work yet,
 * the exact environment variables that are still missing.
 *
 * The honesty rules from CONTRACT_CALLING.md section 5 are the whole point of
 * this file, so they are stated once here and enforced everywhere below:
 *
 *   1. The cost is on screen BEFORE the button is pressed, always, for every
 *      option, ready or not. A rep must never learn that Twilio charges money by
 *      being charged.
 *   2. A provider that cannot place a call is drawn disabled and says what is
 *      missing, word for word, for example "Needs TWILIO_ACCOUNT_SID and
 *      PUBLIC_BASE_URL in backend/.env". The Start button for it stays dead.
 *   3. The button says what will actually happen: "Open WhatsApp" opens an app,
 *      "Ring my phone" rings a phone, "I will dial" dials nothing at all.
 *   4. The screen never says a dial is happening when nothing is dialling. The
 *      backend starts every provider at the same "dialing" state and only Twilio
 *      ever leaves it, because only Twilio reports back. So the words and the
 *      one moving dot are chosen per option, through appDials and appWatches
 *      below, and a call the rep placed on their own phone keeps the picker on
 *      screen instead of pretending the app is busy on the wire.
 *
 * Mechanics are copied from AudioSourcePicker on purpose, because the two sit on
 * the same bar and a rep should not have to learn two popovers: hand built, no
 * library, no portal, no backdrop filter, closes on Escape, on an outside
 * pointerdown and when focus leaves, and hands focus back to the trigger.
 *
 * The provider list is a real radio group with a roving tab index, so the whole
 * choice is two keys away for a rep who never touches the mouse.
 */

import { useCallback, useEffect, useId, useRef, useState } from "react";
import type { KeyboardEvent as ReactKeyboardEvent } from "react";
import type { LucideIcon } from "lucide-react";
import { Hand, MessageCircle, Phone, PhoneForwarded, PhoneOff, RefreshCw } from "lucide-react";

import type { CallProvider, CallProviderInfo, CallState } from "@/lib/types";

export interface CallLauncherProps {
  /** The four options, from GET /api/call/providers, in backend order. */
  providers: CallProviderInfo[];
  /** The option that is picked right now. */
  value: CallProvider;
  /** Where the phone call is, from the socket or the status poll. */
  state: CallState;
  /** True while a start or a hang up request is in flight. */
  busy: boolean;
  /** The last failure, already phrased for a human, or null. */
  error: string | null;
  /** The client's phone number, controlled by the page. */
  toNumber: string;
  /** The rep's own phone number, controlled by the page. Twilio only. */
  repNumber: string;
  onProvider(p: CallProvider): void;
  onToNumber(v: string): void;
  onRepNumber(v: string): void;
  onStart(): void;
  onHangUp(): void;
  /** Read the provider list again, so a fresh backend/.env shows up at once. */
  onRefresh(): void;
}

/* ------------------------------------------------------------------ */
/* Words                                                               */
/* ------------------------------------------------------------------ */

/**
 * One face per option, so the option is readable before a word of it is.
 *
 * A switch rather than a lookup object, so a key the backend added after this
 * build shipped still gets an icon instead of rendering an empty box.
 */
function iconFor(key: CallProvider): LucideIcon {
  switch (key) {
    case "twilio":
      return PhoneForwarded;
    case "whatsapp_link":
      return MessageCircle;
    case "whatsapp_cloud":
      return Phone;
    default:
      return Hand;
  }
}

/**
 * What the button promises, per option.
 *
 * The contract names three of these. The fourth, WhatsApp from the app, would be
 * a lie as "Open WhatsApp", because nothing opens on the rep's phone in that
 * mode, the server places the call. So it gets its own honest label.
 */
function startLabelFor(key: CallProvider): string {
  switch (key) {
    case "twilio":
      return "Ring my phone";
    case "whatsapp_link":
      return "Open WhatsApp";
    case "whatsapp_cloud":
      return "Call on WhatsApp";
    default:
      return "I will dial";
  }
}

/**
 * True when the app itself puts this call on the wire.
 *
 * Twilio rings the rep's phone from the server, and WhatsApp from the app asks
 * Meta to place the call. The other two dial nothing at all: the rep lifts their
 * own phone, or presses the green button inside WhatsApp, and the app only
 * listens. That split decides whether the picker is allowed to stay on screen
 * while a call is running, so it lives in one function rather than being spelled
 * out again at each place that needs it.
 */
function appDials(key: CallProvider): boolean {
  return key === "twilio" || key === "whatsapp_cloud";
}

/**
 * True when the app is also told what the call does next.
 *
 * Only Twilio. Its status callbacks walk the call from dialing to ringing to
 * live to ended, so for Twilio those words stay true minute by minute. The other
 * three are written as "dialing" once, at the start, and are never written
 * again, because nothing here can see that phone. See START_STATE in
 * backend/app/api/routes_call.py, which says the same thing from the other side.
 */
function appWatches(key: CallProvider): boolean {
  return key === "twilio";
}

/**
 * The one word the state chip and the live panel both print.
 *
 * "Dialing" is only ever printed for a call the app is really dialling. For the
 * three options that never leave the backend's start state the same state means
 * "this call is on", and a chip reading DIALING through twenty minutes of a live
 * conversation is exactly the small lie this file exists to stop.
 *
 * The option is optional because the top bar chip is handed the call state on
 * its own. With no option to go on the plain word is kept and the dot is held
 * still, which is the careful half of the pair: a still dot says less than it
 * could, a moving one would say more than is true.
 */
function stateWord(state: CallState, key?: CallProvider): string {
  switch (state) {
    case "dialing":
      return key !== undefined && !appWatches(key) ? "On" : "Dialing";
    case "ringing":
      return "Ringing";
    case "live":
      return "Live";
    case "ended":
      return "Ended";
    case "failed":
      return "Failed";
    default:
      return "Idle";
  }
}

/**
 * Should the state dot move.
 *
 * DESIGN.md section 7 gives mark-pulse one job, the socket mark, and the reason
 * is that a 1400 ms loop beside the reading surface costs attention for as long
 * as it runs. A dot pulsing for a whole five minute call is that cost with none
 * of the news, so the pulse is spent only where it is short and true.
 *
 * "ringing" is written by Twilio's own callback and by nothing else, so it is
 * always a real call being set up this second. "dialing" is where all four
 * options start, including the three that stay there for good, so it moves only
 * when we know Twilio owns the call.
 */
function pulsesFor(state: CallState, key?: CallProvider): boolean {
  if (state === "ringing") return true;
  if (state !== "dialing") return false;
  return key !== undefined && appWatches(key);
}

/** The same state, as a line a nervous person can read in one glance. */
function stateLine(state: CallState, key: CallProvider): string {
  switch (state) {
    case "dialing": {
      /* One state, four meanings. This line has to read true for a Twilio call
         two seconds old AND for a manual call twenty minutes old, because the
         manual call never moves off this state. So each option answers for
         itself, and none of them claims the app is dialling when it is not. */
      switch (key) {
        case "twilio":
          return "We are calling your phone now.";
        case "whatsapp_cloud":
          return "We asked WhatsApp to call the client. We cannot see when they pick up.";
        case "whatsapp_link":
          return "WhatsApp is open in a new tab. Press call there. The app is only listening.";
        default:
          return "You call this one yourself. The app is only listening.";
      }
    }
    case "ringing":
      return key === "twilio"
        ? "Your phone is ringing. Pick it up and we dial the client."
        : "The phone is ringing.";
    case "live":
      return "The call is on. The app is listening to both sides.";
    case "ended":
      return "The call is over.";
    case "failed":
      return "The call did not go through.";
    default:
      return "No call yet.";
  }
}

/**
 * One plain line under the button, so nobody is surprised after pressing it.
 *
 * These four are the last sentences a rep reads before spending money, so they
 * are the easiest sentences on the page: short words, one idea each, and the
 * whole price of the option said out loud. WhatsApp from the app carries the
 * rule that decides the feature, which is that Meta refuses a cold call, and it
 * is said here because the panel is the only place a rep sees it before paying
 * for a business number.
 */
function warningFor(key: CallProvider): string {
  switch (key) {
    case "twilio":
      return "This call costs money. Twilio takes money from you for every minute you talk.";
    case "whatsapp_link":
      return "WhatsApp opens in a new tab. Share the sound of that tab so the app can hear.";
    case "whatsapp_cloud":
      return (
        "The app makes the call. Meta takes money from you for it. Meta must say yes to your " +
        "number first. And you can only call a person who already sent a WhatsApp message to " +
        "your business number."
      );
    default:
      return "The app will not call anyone. You call them yourself, then the app listens.";
  }
}

/** Every option except manual needs the client's number. */
function needsClientNumber(key: CallProvider): boolean {
  return key !== "manual";
}

/** Only Twilio rings the rep first, so only Twilio needs the rep's number. */
function needsRepNumber(key: CallProvider): boolean {
  return key === "twilio";
}

/**
 * True when this option costs money.
 *
 * Read from the backend's own cost string rather than from a hard coded list of
 * keys, so a provider added later is treated as paid unless it says it is free.
 * Guessing "free" would be the one wrong way to be wrong here.
 */
function costsMoney(info: CallProviderInfo): boolean {
  return info.cost.trim().toLowerCase() !== "free";
}

/** "A, B and C". Plain English, no serial comma, no em dash, ever. */
function joinWords(items: string[]): string {
  if (items.length === 0) return "";
  if (items.length === 1) return items[0];
  const head = items.slice(0, -1).join(", ");
  return `${head} and ${items[items.length - 1]}`;
}

/** What is still missing, as one sentence the rep can act on. */
function missingLine(info: CallProviderInfo): string {
  if (info.missing.length === 0) return "This one is not set up yet.";
  return `Needs ${joinWords(info.missing)} in backend/.env.`;
}

/* ------------------------------------------------------------------ */
/* Class strings                                                       */
/* ------------------------------------------------------------------ */

/* 40px tall, which is the smallest comfortable touch target, and the reason
   these are taller than the 36px controls in AudioSourcePicker. */
const FIELD_CLASS =
  "h-10 w-full rounded-hair border border-line-strong bg-surface px-2.5 font-sans text-chip text-text placeholder:text-dim disabled:opacity-40";

const ACTION_CLASS =
  "flex h-10 w-full items-center justify-center gap-2 rounded-hair border border-line-strong px-3 font-mono text-[12px] font-semibold uppercase tracking-[0.12em] transition-colors duration-[120ms] ease-out hover:bg-surface disabled:pointer-events-none disabled:opacity-40";

/* The small button inside the "call is on" strip. Quieter than ACTION_CLASS on
   purpose: the rep placed this call themselves, so ending it in the app is
   bookkeeping, not the main thing on the panel. */
const SMALL_ACTION_CLASS =
  "flex h-6 shrink-0 items-center gap-1.5 rounded-hair border border-line-strong px-2 font-mono text-micro uppercase transition-colors duration-[120ms] ease-out hover:bg-surface-2 disabled:pointer-events-none disabled:opacity-40";

const HINT_CLASS = "font-sans text-[12px] leading-[18px] text-muted";

/* ------------------------------------------------------------------ */
/* The top bar chip                                                    */
/* ------------------------------------------------------------------ */

/**
 * The call state, as one word on the top bar.
 *
 * Renders nothing at all while the state is idle, because a chip that says
 * "IDLE" on every screen teaches the eye to skip the place where LIVE will
 * appear. Cyan is the live link, rose is a failure, everything else is muted,
 * which is the colour law in DESIGN.md section 6.
 *
 * `provider` is optional so the chip can be dropped on a bar that only knows the
 * state. Pass it whenever the page has it: with the option in hand the chip says
 * ON for a call the rep is holding on their own phone, instead of DIALING for a
 * dial that finished long ago or never started.
 */
export function CallStateChip({
  state,
  provider,
}: {
  state: CallState;
  provider?: CallProvider;
}) {
  if (state === "idle") return null;

  const word = stateWord(state, provider).toUpperCase();
  const inFlight = pulsesFor(state, provider);

  const tone =
    state === "live" ? "text-accent" : state === "failed" ? "text-danger" : "text-muted";
  const dot = state === "live" ? "bg-accent" : state === "failed" ? "bg-danger" : "bg-dim";

  return (
    <span className="inline-flex h-[26px] shrink-0 items-center gap-1.5 rounded-hair border border-line-strong px-2">
      <span
        aria-hidden="true"
        className={`h-[5px] w-[5px] shrink-0 rounded-full ${dot} ${inFlight ? "animate-mark" : ""}`}
      />
      <span aria-live="polite" className={`font-mono text-micro tabnum uppercase ${tone}`}>
        {word}
      </span>
    </span>
  );
}

/* ------------------------------------------------------------------ */
/* One provider row                                                    */
/* ------------------------------------------------------------------ */

interface ProviderRowProps {
  info: CallProviderInfo;
  /* Resolved by the caller rather than here, so the icon is a prop and never a
     component built during this row's own render. */
  Icon: LucideIcon;
  selected: boolean;
  tabStop: boolean;
  descId: string;
  onPick(): void;
  onKeyDown(event: ReactKeyboardEvent<HTMLButtonElement>): void;
  buttonRef(node: HTMLButtonElement | null): void;
}

function ProviderRow(props: ProviderRowProps) {
  const { info, Icon, selected, tabStop, descId, onPick, onKeyDown, buttonRef } = props;

  const paid = costsMoney(info);
  const off = !info.ready;

  /* aria-disabled, not the disabled attribute. A row that cannot call is the one
     row a rep most needs to read, because it holds the fix. The DOM attribute
     would take it out of the tab order and out of the arrow walk, and a screen
     reader user would never hear which variables are missing. So it stays
     reachable, it just refuses to be picked. */
  return (
    <button
      type="button"
      role="radio"
      aria-checked={selected}
      aria-disabled={off || undefined}
      aria-label={`${info.label}. ${paid ? "Costs money" : "Free"}.`}
      aria-describedby={descId}
      tabIndex={tabStop ? 0 : -1}
      ref={buttonRef}
      onClick={onPick}
      onKeyDown={onKeyDown}
      className={[
        "group relative flex w-full flex-col gap-1.5 p-3 text-left",
        "transition-colors duration-[120ms] ease-out focus-visible:z-10",
        off ? "cursor-not-allowed" : selected ? "bg-surface" : "hover:bg-surface",
      ].join(" ")}
    >
      {/* The chosen mark, a 2px accent bar in the row's own left gutter. Same
          mark, same colour and same meaning as the teleprompter boresight. */}
      {selected ? (
        <span aria-hidden="true" className="absolute inset-y-0 left-0 w-0.5 bg-accent" />
      ) : null}

      <span className="flex items-center gap-2">
        <Icon
          aria-hidden="true"
          className={`h-4 w-4 shrink-0 transition-colors duration-[120ms] ${
            off ? "text-dim" : selected ? "text-accent" : "text-muted group-hover:text-accent"
          }`}
        />
        <span className={`truncate font-sans text-chip ${off ? "text-muted" : "text-text"}`}>
          {info.label}
        </span>

        {/* The price, on screen before anything is pressed. Warn coloured when it
            costs money, quiet when it does not. One MICRO word, which is all the
            colour law allows a warn hue to be. */}
        <span
          className={`ml-auto shrink-0 font-mono text-micro uppercase ${paid ? "text-warn" : "text-muted"}`}
        >
          {paid ? "Paid" : "Free"}
        </span>

        {/* 6px square. Filled means picked. Squares are selection in this
            product and circles are signal, and the two never swap. */}
        <span
          aria-hidden="true"
          className={`h-1.5 w-1.5 shrink-0 ${selected ? "bg-accent" : "border border-line-strong"}`}
        />
      </span>

      <span id={descId} className="flex flex-col gap-1">
        <span className={HINT_CLASS}>{info.blurb}</span>
        {/* The price in full, in muted ink. The warn hue stays on the one MICRO
            word above, because a whole sentence in warn is not what an exception
            colour is for. A free option is skipped here on purpose: its cost
            string IS the word "Free" already printed beside the label, and
            saying it twice in four lines just teaches the eye to skip the
            price. */}
        {paid ? <span className={HINT_CLASS}>{info.cost}</span> : null}
        {off ? (
          <span className={`${HINT_CLASS} break-words`}>{missingLine(info)}</span>
        ) : null}
      </span>
    </button>
  );
}

/* ------------------------------------------------------------------ */
/* The launcher                                                        */
/* ------------------------------------------------------------------ */

export function CallLauncher(props: CallLauncherProps) {
  const {
    providers,
    value,
    state,
    busy,
    error,
    toNumber,
    repNumber,
    onProvider,
    onToNumber,
    onRepNumber,
    onStart,
    onHangUp,
    onRefresh,
  } = props;

  const rootRef = useRef<HTMLDivElement | null>(null);
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const panelRef = useRef<HTMLDivElement | null>(null);
  const radiosRef = useRef<Array<HTMLButtonElement | null>>([]);
  const refreshRef = useRef(onRefresh);
  const [open, setOpen] = useState(false);

  const baseId = useId();
  const panelId = `${baseId}-panel`;
  const toId = `${baseId}-to`;
  const repId = `${baseId}-rep`;
  const repHintId = `${baseId}-rep-hint`;
  const reasonId = `${baseId}-reason`;
  const errorId = `${baseId}-error`;

  useEffect(() => {
    refreshRef.current = onRefresh;
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

  // Focus the panel on open, and take a fresh provider list while we are at it,
  // so a variable added to backend/.env two minutes ago shows up without a
  // reload of the whole page.
  useEffect(() => {
    if (!open) return;
    panelRef.current?.focus();
    refreshRef.current();
  }, [open]);

  /* A call is on the moment the backend leaves idle and stays on until it ends
     or fails. This is only "there is a call", not "the app is dialling": the
     backend starts all four options at "dialing" and only Twilio moves on. */
  const onNow = state === "dialing" || state === "ringing" || state === "live";

  /* Which option placed the call that is on right now.

     `value` is where the picker is pointing, and the rep is allowed to move that
     while a call they placed themselves is running, so `value` cannot be
     trusted to describe what is on the wire. This latches the option at the
     moment the call turns on and holds it until the call is over.

     Written during render, which is React's own way to fold a prop change into
     state: the assignment runs in the render that first sees the call, React
     drops that render and immediately runs another with the new value, so
     nothing stale is ever painted. */
  const [wasOn, setWasOn] = useState(onNow);
  const [callKey, setCallKey] = useState<CallProvider>(value);
  if (onNow !== wasOn) {
    setWasOn(onNow);
    if (onNow) setCallKey(value);
  }

  /* The app placed this call, so the picker goes away: changing the provider
     under a call the server is holding would be a lie about what is on the wire,
     and Hang up is the one way out. */
  const active = onNow && appDials(callKey);

  /* The rep placed this call, on their own phone or in their own WhatsApp. The
     app dialed nothing and can see nothing, so it has no business locking the
     panel. The picker stays, a quiet strip says the call is on, and the rep can
     move to another option without hunting for Hang up first. */
  const selfOn = onNow && !appDials(callKey);

  /* The three shapes the panel body can take. When this changes, the control the
     keyboard was standing on can stop existing and focus lands on the document.
     Put it back on the panel, which is where the panel puts it when it opens.
     Focus is only ever inside this popover or on the body while it is open,
     because leaving closes it, so this can never steal focus from elsewhere. */
  const shape = active ? "held" : selfOn ? "picker-on" : "picker";
  const wasShape = useRef(shape);
  useEffect(() => {
    if (wasShape.current === shape) return;
    wasShape.current = shape;
    if (open) panelRef.current?.focus();
  }, [shape, open]);

  const selectedIndex = providers.findIndex((p) => p.key === value);
  const selected = selectedIndex >= 0 ? providers[selectedIndex] : null;

  /* Which row holds the group's one tab stop. A value that matches no row is bad
     data, not a reason to make the whole group unreachable, so the stop falls
     back to the first row. */
  const tabStop = selectedIndex >= 0 ? selectedIndex : 0;

  function move(index: number) {
    const info = providers[index];
    if (!info) return;
    radiosRef.current[index]?.focus();
    /* Selection follows focus, which is the standard radio group behaviour, but
       never onto a row that cannot place a call. Focus still lands there so the
       missing variables can be read. */
    if (info.ready) onProvider(info.key);
  }

  function onRadioKeyDown(event: ReactKeyboardEvent<HTMLButtonElement>, index: number) {
    if (event.ctrlKey || event.metaKey || event.altKey) return;

    let next = -1;
    switch (event.key) {
      case "ArrowDown":
      case "ArrowRight":
        next = (index + 1) % providers.length;
        break;
      case "ArrowUp":
      case "ArrowLeft":
        next = (index - 1 + providers.length) % providers.length;
        break;
      case "Home":
        next = 0;
        break;
      case "End":
        next = providers.length - 1;
        break;
      default:
        return;
    }

    event.preventDefault();
    move(next);
  }

  const clientMissing = needsClientNumber(value) && toNumber.trim() === "";
  const repMissing = needsRepNumber(value) && repNumber.trim() === "";
  const ready = selected !== null && selected.ready;
  const canStart = ready && !busy && !active && !clientMissing && !repMissing;

  /* Why the button is dead, in one line, so nobody presses a grey rectangle
     twice and gives up. */
  let reason: string | null = null;
  if (selected === null) reason = "Pick how you want to call.";
  else if (!selected.ready) reason = missingLine(selected);
  else if (clientMissing) reason = "Type the client number first.";
  else if (repMissing) reason = "Type your own number too.";

  const startWord = busy ? "Wait" : startLabelFor(value);

  /* The call on the wire is described by the option that placed it. Everything
     else, the picker and the button, is described by the option in hand. */
  const spokenKey = onNow ? callKey : value;

  const triggerDot =
    state === "live"
      ? "bg-accent"
      : state === "failed"
        ? "bg-danger"
        : onNow
          ? `bg-accent${pulsesFor(state, callKey) ? " animate-mark" : ""}`
          : "bg-dim";

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
        aria-label={`Place the call. ${selected ? selected.label : "Nothing picked"}. ${stateLine(
          state,
          spokenKey,
        )}`}
        onClick={() => (open ? close(true) : setOpen(true))}
        className="flex h-[26px] items-center gap-2 rounded-hair border border-line-strong px-2.5 font-mono text-micro uppercase text-muted transition-colors duration-[120ms] ease-out hover:bg-surface-2 hover:text-text"
      >
        <span aria-hidden="true" className={`h-[5px] w-[5px] shrink-0 rounded-full ${triggerDot}`} />
        <span aria-hidden="true">Call</span>
      </button>

      {open ? (
        <div
          ref={panelRef}
          id={panelId}
          role="dialog"
          aria-modal="false"
          aria-label="Place the call"
          tabIndex={-1}
          className="absolute right-0 top-[34px] z-50 w-[360px] max-w-[calc(100vw-24px)] rounded-pop border border-line-strong bg-surface-2 p-3 shadow-pop"
        >
          <div className="flex items-center justify-between gap-2 pb-1">
            <span className="font-mono text-micro uppercase text-muted">Place the call</span>
            <button
              type="button"
              onClick={() => onRefresh()}
              className="flex h-6 items-center gap-1.5 rounded-hair border border-line-strong px-2 font-mono text-micro uppercase text-muted transition-colors duration-[120ms] ease-out hover:bg-surface hover:text-text"
            >
              <RefreshCw aria-hidden="true" className="h-3 w-3" />
              <span>Look again</span>
            </button>
          </div>

          {active ? (
            /* The app is holding this call open, so the picker is gone. Changing
               the provider under it would be a lie about what is on the wire.
               One state, one way out. */
            <div className="flex flex-col gap-3 pt-2">
              <div className="flex flex-col gap-1.5">
                <div className="flex items-center gap-2">
                  <span
                    aria-hidden="true"
                    className={`h-[5px] w-[5px] shrink-0 rounded-full bg-accent ${
                      pulsesFor(state, callKey) ? "animate-mark" : ""
                    }`}
                  />
                  <span
                    aria-live="polite"
                    className="font-mono text-status uppercase tabnum text-text"
                  >
                    {stateWord(state, callKey)}
                  </span>
                </div>
                <p className={HINT_CLASS}>{stateLine(state, callKey)}</p>
                <p className={HINT_CLASS}>{warningFor(callKey)}</p>
              </div>

              <button
                type="button"
                onClick={onHangUp}
                disabled={busy}
                aria-busy={busy}
                className={`${ACTION_CLASS} text-danger`}
              >
                <PhoneOff aria-hidden="true" className="h-4 w-4" />
                <span>{busy ? "Wait" : "Hang up"}</span>
              </button>

              {error ? (
                <p aria-live="polite" className="font-sans text-[12px] leading-[18px] text-danger">
                  {error}
                </p>
              ) : null}
            </div>
          ) : providers.length === 0 ? (
            /* Nothing to pick, so nothing to press. Showing a number field and a
               dead button here would be a Call button that cannot call, which is
               the one thing this panel is not allowed to draw. */
            <div className="flex flex-col gap-2 pt-2">
              <p className={HINT_CLASS}>
                The call options did not load. Press Look again, and check that the backend is
                running.
              </p>
              {error ? (
                <p aria-live="polite" className="font-sans text-[12px] leading-[18px] text-danger">
                  {error}
                </p>
              ) : null}
            </div>
          ) : (
            <div className="flex flex-col gap-3">
              {selfOn ? (
                /* A call the rep placed themselves. It is marked here so the top
                   bar dot has a meaning on this panel, and so there is a way to
                   tell the app it is over. The dot is still, because nothing is
                   moving that the app can see. */
                <div className="flex flex-col gap-1.5 rounded-hair border border-line-strong bg-surface p-2.5">
                  <div className="flex items-center gap-2">
                    <span
                      aria-hidden="true"
                      className="h-[5px] w-[5px] shrink-0 rounded-full bg-accent"
                    />
                    <span
                      aria-live="polite"
                      className="font-mono text-status uppercase tabnum text-text"
                    >
                      {stateWord(state, callKey)}
                    </span>
                    <button
                      type="button"
                      onClick={onHangUp}
                      disabled={busy}
                      aria-busy={busy}
                      className={`${SMALL_ACTION_CLASS} ml-auto text-danger`}
                    >
                      <PhoneOff aria-hidden="true" className="h-3 w-3" />
                      <span>{busy ? "Wait" : "Hang up"}</span>
                    </button>
                  </div>
                  <p className={HINT_CLASS}>{stateLine(state, callKey)}</p>
                </div>
              ) : null}

              {state === "ended" || state === "failed" ? (
                <p
                  aria-live="polite"
                  className={`font-sans text-[12px] leading-[18px] ${
                    state === "failed" ? "text-danger" : "text-muted"
                  }`}
                >
                  {stateLine(state, spokenKey)}
                </p>
              ) : null}

              <div
                role="radiogroup"
                aria-label="How to place the call"
                className="flex flex-col divide-y divide-line border-y border-line"
              >
                {providers.map((info, index) => (
                  <ProviderRow
                    key={info.key}
                    info={info}
                    Icon={iconFor(info.key)}
                    selected={info.key === value}
                    tabStop={index === tabStop}
                    descId={`${baseId}-desc-${info.key}`}
                    onPick={() => {
                      if (info.ready) onProvider(info.key);
                    }}
                    onKeyDown={(event) => onRadioKeyDown(event, index)}
                    buttonRef={(node) => {
                      radiosRef.current[index] = node;
                    }}
                  />
                ))}
              </div>

              {/* The client number is always here, even for manual, so the rep
                  never has to hunt for a field that moved. */}
              <div className="flex flex-col gap-1.5">
                <label htmlFor={toId} className="font-mono text-micro uppercase text-muted">
                  Client number
                </label>
                <input
                  id={toId}
                  type="tel"
                  inputMode="tel"
                  autoComplete="tel"
                  spellCheck={false}
                  placeholder="+923001234567"
                  value={toNumber}
                  onChange={(e) => onToNumber(e.target.value)}
                  className={FIELD_CLASS}
                />
                {needsClientNumber(value) ? null : (
                  <p className={HINT_CLASS}>Not needed for this one. You dial it yourself.</p>
                )}
              </div>

              {needsRepNumber(value) ? (
                <div className="flex flex-col gap-1.5">
                  <label htmlFor={repId} className="font-mono text-micro uppercase text-muted">
                    Your number
                  </label>
                  <input
                    id={repId}
                    type="tel"
                    inputMode="tel"
                    autoComplete="tel"
                    spellCheck={false}
                    placeholder="+923009876543"
                    value={repNumber}
                    aria-describedby={repHintId}
                    onChange={(e) => onRepNumber(e.target.value)}
                    className={FIELD_CLASS}
                  />
                  <p id={repHintId} className={HINT_CLASS}>
                    We ring this phone first, then we dial the client.
                  </p>
                </div>
              ) : null}

              <button
                type="button"
                onClick={onStart}
                disabled={!canStart}
                aria-busy={busy}
                aria-describedby={
                  [error ? errorId : null, reason ? reasonId : null].filter(Boolean).join(" ") ||
                  undefined
                }
                className={`${ACTION_CLASS} text-accent`}
              >
                <Phone aria-hidden="true" className="h-4 w-4" />
                <span>{startWord}</span>
              </button>

              {reason ? (
                <p id={reasonId} className={`${HINT_CLASS} break-words`}>
                  {reason}
                </p>
              ) : null}

              {error ? (
                <p
                  id={errorId}
                  aria-live="polite"
                  className="font-sans text-[12px] leading-[18px] text-danger"
                >
                  {error}
                </p>
              ) : null}

              <p className={HINT_CLASS}>{warningFor(value)}</p>
            </div>
          )}
        </div>
      ) : null}
    </div>
  );
}
