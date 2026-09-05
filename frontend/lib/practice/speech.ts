"use client";

/**
 * The voice of the synthetic client, built on the browser speechSynthesis API.
 *
 * There is no TTS API call in this product. Groq's TTS model needs a manual
 * terms acceptance in their console, so the browser speaks instead. That is
 * free, offline and instant, and it costs us this file, because the Web Speech
 * API is one of the buggiest surfaces the browser exposes.
 *
 * Every workaround below is a real bug, not a precaution:
 *
 *   1. getVoices() returns an empty array on the first call in Chrome. The list
 *      loads later and fires `voiceschanged`. We cache and re read on that event.
 *   2. Chrome stops speaking after about 15 seconds, mid sentence, with no error
 *      and no `end` event. A pause() plus resume() pair every 10 seconds keeps
 *      the engine alive.
 *   3. cancel() often never fires `end`, so a caller waiting on onEnd would hang
 *      forever with the mic still muted. Every utterance carries a fired once
 *      flag and cancel finishes it by hand.
 *   4. Some browsers refuse to speak until the page has had a real user gesture.
 *      primeSpeech() is called from the practice Start button click for that.
 *   5. speak() straight after cancel() is dropped by Chrome, so the real speak
 *      call is deferred by a few milliseconds.
 *   6. An utterance that is not referenced anywhere can be garbage collected
 *      while it is still speaking, which kills the audio and the events. We hold
 *      a reference until it settles.
 *
 * When speechSynthesis is missing altogether, every function here is a no op and
 * reports through the caller's onError, so the practice call degrades to text
 * only instead of throwing. See CONTRACT_PRACTICE.md section 6.1.
 */

import type { Difficulty } from "@/lib/types";

/** One line to speak, plus the three callbacks the caller needs back. */
export interface SpeakOptions {
  text: string;
  /** BCP 47 tag or bare language code, for example "en" or "en-GB". */
  lang: string;
  /** 0.1 to 10, default 1. Use rateForDifficulty so the level is audible. */
  rate?: number;
  /** 0 to 2, default 1. */
  pitch?: number;
  /** Fired once, when the line is handed to the speech engine. */
  onStart(): void;
  /** Fired exactly once per call to speak, on end, on error, or on cancel. */
  onEnd(): void;
  /** A sentence a person can read. Never a raw event object. */
  onError(msg: string): void;
}

/**
 * How often we poke the engine while it is speaking.
 *
 * Chrome gives up at roughly 15 seconds, so the poke has to land before that,
 * and 10 seconds leaves room for a slow timer on a busy tab.
 */
const HEARTBEAT_MS = 10000;

/**
 * How long we wait after cancel() before handing over the next line.
 *
 * Chrome drops a speak() that lands in the same tick as a cancel(), and the
 * client line would then never be heard. A few milliseconds is under the ear's
 * notice and it is reliable.
 */
const START_AFTER_CANCEL_MS = 60;

const RATE_MIN = 0.1;
const RATE_MAX = 10;
const PITCH_MIN = 0;
const PITCH_MAX = 2;

/**
 * Speaking speed per difficulty, so the rep hears which client they picked.
 * A brutal client talks fast because they want the call over.
 */
export const RATE_BY_DIFFICULTY: Readonly<Record<Difficulty, number>> = {
  warm: 1.0,
  normal: 1.08,
  brutal: 1.15,
};

/** The speaking speed for one difficulty. Falls back to normal speed. */
export function rateForDifficulty(difficulty: Difficulty): number {
  return RATE_BY_DIFFICULTY[difficulty] ?? 1.0;
}

/** The message shown when the browser has no speech engine at all. */
const NO_SPEECH_MESSAGE =
  "This browser cannot talk out loud, so the practice client will only show text. Use a recent Chrome, Edge or Safari to hear it.";

/* ------------------------------------------------------------------ */
/* Engine access                                                       */
/* ------------------------------------------------------------------ */

/** The engine, or null during SSR and in a browser without the API. */
function engine(): SpeechSynthesis | null {
  if (typeof window === "undefined") return null;
  const synth: SpeechSynthesis | undefined = window.speechSynthesis;
  if (!synth) return null;
  if (typeof window.SpeechSynthesisUtterance !== "function") return null;
  return synth;
}

/**
 * True when this browser can speak. Both halves of the API are checked, because
 * a few browsers ship the SpeechSynthesis object with no utterance constructor.
 */
export function speechAvailable(): boolean {
  return engine() !== null;
}

/* ------------------------------------------------------------------ */
/* Voices                                                              */
/* ------------------------------------------------------------------ */

let cachedVoices: SpeechSynthesisVoice[] = [];
let voicesBound = false;

/** Read the list now and keep it. getVoices() throws in a few builds. */
function refreshVoices(synth: SpeechSynthesis): void {
  try {
    const voices = synth.getVoices();
    if (Array.isArray(voices) && voices.length > 0) {
      cachedVoices = voices;
    }
  } catch {
    // A browser that cannot list voices can still speak with its default one.
  }
}

/**
 * Subscribe to `voiceschanged` once.
 *
 * Chrome loads the voice list asynchronously, so the first getVoices() call on a
 * fresh page is empty. The event can also fire later when a system voice is
 * installed or a network voice becomes reachable, so the listener stays on.
 */
function bindVoicesChanged(synth: SpeechSynthesis): void {
  if (voicesBound) return;
  voicesBound = true;
  try {
    synth.addEventListener("voiceschanged", () => {
      refreshVoices(synth);
    });
  } catch {
    // Older Safari has no addEventListener here. The direct read still works.
  }
}

/**
 * Every voice this browser knows about.
 *
 * Safe to call at any time. It returns an empty array on the very first call in
 * Chrome, which is expected, and a later call returns the real list.
 */
export function listVoices(): SpeechSynthesisVoice[] {
  const synth = engine();
  if (!synth) return [];
  bindVoicesChanged(synth);
  refreshVoices(synth);
  return cachedVoices;
}

/** "en-GB" and "en_GB" and "EN" all become "en". */
function languagePrefix(lang: string): string {
  return lang.trim().toLowerCase().replace(/_/g, "-").split("-")[0] ?? "";
}

/**
 * The best voice for one language, or null to let the browser choose.
 *
 * A non local voice is preferred when there is one. Those are the network
 * voices, and they sound far more like a person than the built in robot, which
 * matters because the rep is meant to believe the client for a few minutes.
 */
export function pickVoice(lang: string): SpeechSynthesisVoice | null {
  const wanted = languagePrefix(lang);
  if (wanted.length === 0) return null;

  const voices = listVoices();
  if (voices.length === 0) return null;

  const matches = voices.filter((v) => languagePrefix(v.lang) === wanted);
  if (matches.length === 0) return null;

  const network = matches.find((v) => v.localService === false);
  return network ?? matches[0] ?? null;
}

/* ------------------------------------------------------------------ */
/* Speaking                                                            */
/* ------------------------------------------------------------------ */

interface ActiveSpeech {
  /**
   * Held here on purpose. An utterance with no live reference can be collected
   * by the garbage collector while it is still speaking, which cuts the audio
   * and kills the events, so the object stays reachable until it settles.
   */
  utterance: SpeechSynthesisUtterance;
  /** The fired once guard. onEnd runs exactly one time per speak call. */
  finished: boolean;
  onEnd(): void;
  /** The deferred speak, see START_AFTER_CANCEL_MS. */
  startTimer: ReturnType<typeof setTimeout> | null;
}

let active: ActiveSpeech | null = null;
let heartbeat: ReturnType<typeof setInterval> | null = null;

/**
 * Kept alive so the priming utterance is not collected before it settles.
 * Chrome garbage collects an utterance that nothing references while it is
 * still queued, which silently kills the speech. The variable is written and
 * never read on purpose, that reference is the whole job.
 */
// eslint-disable-next-line @typescript-eslint/no-unused-vars
let priming: SpeechSynthesisUtterance | null = null;

function stopHeartbeat(): void {
  if (heartbeat !== null) {
    clearInterval(heartbeat);
    heartbeat = null;
  }
}

/**
 * Poke the engine so Chrome does not stop at about 15 seconds.
 *
 * pause() followed straight by resume() resets that internal timer without any
 * audible gap. The guard on `speaking` keeps us from resuming an engine that is
 * idle, which would leave it in a paused state for the next line.
 */
function startHeartbeat(): void {
  stopHeartbeat();
  heartbeat = setInterval(() => {
    const synth = engine();
    if (!synth || !synth.speaking) return;
    try {
      synth.pause();
      synth.resume();
    } catch {
      // Some engines reject pause while a line is being handed over. Skip it.
    }
  }, HEARTBEAT_MS);
}

/** Settle one utterance. Runs its onEnd at most once, whatever ended it. */
function finish(entry: ActiveSpeech): void {
  if (entry.finished) return;
  entry.finished = true;

  if (entry.startTimer !== null) {
    clearTimeout(entry.startTimer);
    entry.startTimer = null;
  }

  if (active === entry) {
    active = null;
    stopHeartbeat();
  }

  entry.onEnd();
}

function clamp(value: number, min: number, max: number): number {
  if (!Number.isFinite(value)) return 1;
  if (value < min) return min;
  if (value > max) return max;
  return value;
}

/**
 * Say one line out loud.
 *
 * Any line already speaking is cancelled first, and its onEnd fires before this
 * one starts, so the caller never has two lines it believes are live. onEnd is
 * guaranteed for this call too: on the end event, on an error, or on a cancel.
 */
export function speak(opts: SpeakOptions): void {
  const synth = engine();
  if (!synth) {
    opts.onError(NO_SPEECH_MESSAGE);
    return;
  }

  const text = opts.text.trim();
  if (text.length === 0) {
    // Chrome fires no events at all for an empty line, so the caller would wait
    // forever. Report the start and the end by hand and speak nothing.
    opts.onStart();
    opts.onEnd();
    return;
  }

  cancelSpeech();

  let utterance: SpeechSynthesisUtterance;
  try {
    utterance = new window.SpeechSynthesisUtterance(text);
  } catch {
    opts.onError(NO_SPEECH_MESSAGE);
    return;
  }

  utterance.lang = opts.lang;
  utterance.rate = clamp(opts.rate ?? 1, RATE_MIN, RATE_MAX);
  utterance.pitch = clamp(opts.pitch ?? 1, PITCH_MIN, PITCH_MAX);
  utterance.volume = 1;

  const voice = pickVoice(opts.lang);
  if (voice) {
    utterance.voice = voice;
    // The voice wins over the tag. Leaving a mismatched lang on the utterance
    // makes some engines fall back to their default voice instead.
    utterance.lang = voice.lang;
  }

  const entry: ActiveSpeech = {
    utterance,
    finished: false,
    onEnd: opts.onEnd,
    startTimer: null,
  };
  active = entry;

  utterance.onend = () => {
    finish(entry);
  };

  utterance.onerror = (ev: SpeechSynthesisErrorEvent) => {
    const code = typeof ev.error === "string" ? ev.error : "";
    // "interrupted" and "canceled" are what a cut in looks like. That is the rep
    // doing their job, not a fault, so it is never shown to them.
    if (code !== "interrupted" && code !== "canceled") {
      opts.onError(`The browser could not speak that line. ${code || "Unknown reason"}.`);
    }
    finish(entry);
  };

  entry.startTimer = setTimeout(() => {
    entry.startTimer = null;
    if (active !== entry || entry.finished) return;

    try {
      // Some engines come back from a cancel in a paused state, and the next
      // line then sits silently in the queue.
      synth.resume();
      synth.speak(utterance);
    } catch (err) {
      const detail = err instanceof Error ? err.message : String(err);
      opts.onError(`The browser could not speak that line. ${detail}`);
      finish(entry);
      return;
    }

    // onStart is reported here rather than from utterance.onstart. Some engines
    // never fire onstart while the voice list is still loading, and the caller
    // uses this signal to mute the rep's mic. Muting a moment early is harmless,
    // muting late lets the client's own voice into the microphone.
    opts.onStart();
    startHeartbeat();
  }, START_AFTER_CANCEL_MS);
}

/**
 * Stop whatever is being said right now.
 *
 * This is the rep cutting in, so it has to be instant and it has to release the
 * mic. The pending onEnd is fired here by hand because cancel() often never
 * fires the end event, and the fired once flag makes the later duplicate event,
 * when it does arrive, a no op.
 */
export function cancelSpeech(): void {
  const entry = active;
  const synth = engine();

  stopHeartbeat();

  if (synth) {
    try {
      synth.cancel();
    } catch {
      // Cancelling an idle engine throws in some builds. Nothing to undo.
    }
  }

  if (entry) {
    finish(entry);
  }
}

/**
 * Unlock the speech engine from inside a real user gesture.
 *
 * Call this from the practice Start button click handler, nowhere else. Browsers
 * block audio that no one asked for, and the first speak() in a page can be
 * silently dropped when it does not descend from a click. A zero length
 * utterance at zero volume is enough to satisfy that rule, and it also kicks off
 * the voice list load so the first real line already has a good voice.
 *
 * A no op when the browser has no speech engine. There is no error callback here
 * because the caller finds out from speechAvailable().
 */
export function primeSpeech(): void {
  const synth = engine();
  if (!synth) return;

  listVoices();

  try {
    const warmup = new window.SpeechSynthesisUtterance("");
    warmup.volume = 0;
    warmup.rate = 1;
    priming = warmup;
    warmup.onend = () => {
      priming = null;
    };
    warmup.onerror = () => {
      priming = null;
    };
    synth.resume();
    synth.speak(warmup);
  } catch {
    // A refused warmup is not worth telling anyone about. The first real line
    // will still try, and speechAvailable() already covers the hard failure.
  }
}
