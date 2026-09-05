/**
 * TeleprompterSocket: the one and only WebSocket wrapper for the call dashboard.
 *
 * Responsibilities, and nothing beyond them:
 *   - own a single WebSocket, in arraybuffer mode
 *   - frame outbound audio (1 stream byte, then the Int16 LE PCM payload)
 *   - drop audio frames instead of queueing them when the send buffer is full
 *   - validate every inbound text frame before it reaches React
 *   - reconnect on an unexpected close, on a fixed ladder, then give up
 *   - beat a heartbeat so the hook can measure round trip time
 *
 * Deliberately NOT here: latency math, transcript state, capture handling. The
 * hook owns all of that. This class holds no React state and never re renders
 * anything, so it stays testable in isolation.
 *
 * See CONTRACT.md section 2.2 for the wire format and section 4.3 for this API.
 */

import { isServerMessage } from "@/lib/types";
import type { ClientMessage, ConnState, ServerMessage } from "@/lib/types";

export interface TeleprompterSocketOptions {
  url: string;
  onMessage(m: ServerMessage): void;
  /** Every lifecycle change, collapsed into one signal for the UI chip. */
  onState(s: ConnState): void;
  /** A sentence the UI can show. Never a raw Event, the browser hides those. */
  onError(e: string): void;
  /**
   * Optional raw lifecycle taps, kept so a call site written against
   * CONTRACT.md section 4.3 compiles and still gets the close code (4404 says
   * the session is gone). onState carries the same transitions, these two just
   * hand over the DOM detail. A close we asked for ourselves fires neither,
   * because close() detaches the handlers first on purpose.
   */
  onOpen?(): void;
  onClose?(ev: CloseEvent): void;
}

/**
 * Above this many bytes still queued in the socket we throw audio away rather
 * than let it pile up. 512 KB of 16 kHz Int16 mono is roughly 16 seconds of
 * audio, so if we are that far behind the call is already lost and fresh audio
 * matters far more than old audio.
 */
const MAX_BUFFERED_BYTES = 512 * 1024;

/** Heartbeat period. The hook turns the matching pong into the RTT reading. */
const HEARTBEAT_MS = 5000;

/** Reconnect backoff in milliseconds. Six attempts, then we stop trying. */
const RECONNECT_LADDER: readonly number[] = [500, 1000, 2000, 4000, 8000, 8000];

/** A clean close asked for by either side. Never reconnect after this. */
const CLOSE_NORMAL = 1000;

/**
 * The backend closes with this when the session id is unknown or expired.
 * Reconnecting would fail identically forever, so we stop and tell the user.
 */
const CLOSE_UNKNOWN_SESSION = 4404;

export class TeleprompterSocket {
  private readonly opts: TeleprompterSocketOptions;

  private ws: WebSocket | null = null;

  private _state: ConnState = "idle";

  private _droppedFrames = 0;

  private _invalidMessages = 0;

  /** Index into RECONNECT_LADDER for the next retry. */
  private attempt = 0;

  /** Set by close(), so a close we asked for never restarts the ladder. */
  private intentional = false;

  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;

  private heartbeatTimer: ReturnType<typeof setInterval> | null = null;

  constructor(opts: TeleprompterSocketOptions) {
    this.opts = opts;
  }

  /** Current lifecycle state. Mirrors what was last passed to onState. */
  get state(): ConnState {
    return this._state;
  }

  /** Audio frames thrown away because the send buffer was too full. */
  get droppedFrames(): number {
    return this._droppedFrames;
  }

  /** Text frames that were not valid JSON or not a known server message. */
  get invalidMessages(): number {
    return this._invalidMessages;
  }

  /**
   * Open the socket. Safe to call twice: if one is already connecting or open
   * this is a no op, so a StrictMode double mount cannot produce two sockets.
   */
  connect(): void {
    if (typeof WebSocket === "undefined") {
      this.opts.onError("WebSocket is not available in this environment.");
      return;
    }

    const existing = this.ws;
    if (
      existing &&
      (existing.readyState === WebSocket.OPEN || existing.readyState === WebSocket.CONNECTING)
    ) {
      return;
    }

    this.intentional = false;
    this.clearReconnectTimer();
    this.setState(this.attempt > 0 ? "reconnecting" : "connecting");

    let ws: WebSocket;
    try {
      ws = new WebSocket(this.opts.url);
    } catch (err) {
      const detail = err instanceof Error ? err.message : String(err);
      this.opts.onError(`Could not open the call socket. ${detail}`);
      this.setState("closed");
      this.scheduleReconnect();
      return;
    }

    ws.binaryType = "arraybuffer";
    this.ws = ws;

    ws.onopen = () => {
      // A socket we already replaced must never touch our state again.
      if (this.ws !== ws) return;
      this.attempt = 0;
      this.setState("open");
      this.startHeartbeat();
      this.opts.onOpen?.();
    };

    ws.onmessage = (ev: MessageEvent) => {
      if (this.ws !== ws) return;
      this.handleFrame(ev.data);
    };

    ws.onerror = () => {
      if (this.ws !== ws) return;
      // The browser gives us no detail here on purpose (it would leak network
      // information), and an onclose always follows, so we only report and let
      // the close handler decide whether to reconnect.
      this.stopHeartbeat();
      this.opts.onError("The call socket hit a network error.");
    };

    ws.onclose = (ev: CloseEvent) => {
      if (this.ws !== ws) return;
      this.detach(ws);
      this.ws = null;
      this.stopHeartbeat();
      this.setState("closed");
      this.opts.onClose?.(ev);

      if (this.intentional) return;

      if (ev.code === CLOSE_UNKNOWN_SESSION) {
        this.opts.onError(
          "The server does not know this session any more. Go back and build the call context again.",
        );
        return;
      }

      if (ev.code === CLOSE_NORMAL) return;

      this.scheduleReconnect();
    };
  }

  /**
   * Close for good and stop every timer.
   *
   * The handlers are nulled BEFORE the close call. Without that, a late onclose
   * from this dead socket would arrive after the hook already moved on and would
   * restart the reconnect ladder against a socket nobody owns.
   */
  close(): void {
    this.intentional = true;
    this.clearReconnectTimer();
    this.stopHeartbeat();
    this.attempt = 0;

    const ws = this.ws;
    this.ws = null;

    if (ws) {
      this.detach(ws);
      try {
        if (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING) {
          ws.close(CLOSE_NORMAL, "client closed");
        }
      } catch {
        // Closing a socket that is already dead throws in some browsers. Ignore.
      }
    }

    this.setState("closed");
  }

  /**
   * Send one audio frame. Byte 0 is the stream id, the rest is the PCM payload.
   *
   * Exactly one allocation per frame (the prefixed copy). Anything the socket
   * cannot take right now is dropped and counted, never buffered by us.
   */
  sendAudio(streamId: 0 | 1, pcm: ArrayBuffer): void {
    const ws = this.ws;
    if (!ws || ws.readyState !== WebSocket.OPEN) return;

    if (ws.bufferedAmount > MAX_BUFFERED_BYTES) {
      this._droppedFrames += 1;
      return;
    }

    const frame = new Uint8Array(1 + pcm.byteLength);
    frame[0] = streamId;
    frame.set(new Uint8Array(pcm), 1);

    try {
      ws.send(frame);
    } catch {
      this._droppedFrames += 1;
    }
  }

  /** Send one JSON control message. Dropped silently when not open. */
  send(msg: ClientMessage): void {
    const ws = this.ws;
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    try {
      ws.send(JSON.stringify(msg));
    } catch (err) {
      const detail = err instanceof Error ? err.message : String(err);
      this.opts.onError(`Could not send a message to the server. ${detail}`);
    }
  }

  /**
   * Send a heartbeat now. The hook computes the round trip from the pong echo,
   * this class deliberately does no timing of its own.
   */
  ping(): void {
    this.send({ type: "ping", t: Date.now() });
  }

  /* ---------------------------------------------------------------- */
  /* internals                                                         */
  /* ---------------------------------------------------------------- */

  /** The single place that mutates _state, so onState fires once per change. */
  private setState(next: ConnState): void {
    if (this._state === next) return;
    this._state = next;
    this.opts.onState(next);
  }

  private handleFrame(data: unknown): void {
    // The backend never sends binary to us. Anything binary is noise.
    if (typeof data !== "string") return;

    let parsed: unknown;
    try {
      parsed = JSON.parse(data);
    } catch {
      this._invalidMessages += 1;
      return;
    }

    if (!isServerMessage(parsed)) {
      this._invalidMessages += 1;
      return;
    }

    this.opts.onMessage(parsed);
  }

  private scheduleReconnect(): void {
    if (this.intentional) return;

    if (this.attempt >= RECONNECT_LADDER.length) {
      this.opts.onError(
        "Lost the connection to the server and six retries did not get it back. Check that the backend is running, then reload this page.",
      );
      return;
    }

    const delay = RECONNECT_LADDER[this.attempt];
    this.attempt += 1;
    this.setState("reconnecting");

    this.clearReconnectTimer();
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      this.connect();
    }, delay);
  }

  private startHeartbeat(): void {
    this.stopHeartbeat();
    this.heartbeatTimer = setInterval(() => {
      this.ping();
    }, HEARTBEAT_MS);
  }

  private stopHeartbeat(): void {
    if (this.heartbeatTimer !== null) {
      clearInterval(this.heartbeatTimer);
      this.heartbeatTimer = null;
    }
  }

  private clearReconnectTimer(): void {
    if (this.reconnectTimer !== null) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
  }

  /** Cut every handler on a socket we are done with. */
  private detach(ws: WebSocket): void {
    ws.onopen = null;
    ws.onmessage = null;
    ws.onerror = null;
    ws.onclose = null;
  }
}
