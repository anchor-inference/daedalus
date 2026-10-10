// The host's event stream, one connection per browser.
//
// Everything that happens to sessions, notifications, terminals and staff arrives on
// `/api/events`, and the lists and badges follow it instead of polling every few seconds. The
// polls stay as a safety net: `useStreamUp` says whether the stream is really there, and a screen
// polls as it always did while it is not.
//
// One connection per browser rather than per tab: over plain HTTP/1.1 (the desktop launcher and
// native mode serve 127.0.0.1) a browser opens at most six connections to an origin, an open
// session already holds one and the split view two, and a stream per tab would leave the next
// ordinary request hanging. So the tabs elect a leader with the Web Locks API; the leader holds the
// stream and repeats every frame on a BroadcastChannel; every tab remembers the last sequence
// number it saw, so whichever takes over resumes where the last one stopped. A WebView without
// either API gets a stream per tab, which is always correct, only more expensive.
//
// Read with `fetch` rather than `EventSource` because the request must carry the auth header, so
// `Last-Event-ID` is sent by hand.

import { useEffect, useRef, useSyncExternalStore } from "react";
import { api, type AppEvent } from "./api";
import { clientId, currentKind } from "./presence";
import { invalidate, prime } from "./store";

export type EventMeta = {
  /** The event is older than half a minute on arrival: a replay after a reconnect. Update state from it, never announce it. */
  replayed: boolean;
};
export type EventHandler = (event: AppEvent, meta: EventMeta) => void;

export const REPLAYED_AFTER_MS = 30000;
export const BACKOFF_FIRST_MS = 1000;
export const BACKOFF_MOST_MS = 15000;
/** A proxy's error page or a host that answers something else is not coming back in a second. */
export const NOT_A_STREAM_MS = 60000;
/** A connection that lasted this long was a healthy one: the next reconnect starts the backoff over. */
export const HEALTHY_MS = 15000;
/** A dropped stream still counts as up this long, so a reconnect does not flip every poll twice. */
export const DOWN_GRACE_MS = 5000;
const LOCK = "daedalus.events";
const CHANNEL = "daedalus.events";

/** One server-sent event as it was on the wire. `id` is null for an event the host does not keep. */
export type SseFrame = { event: string; data: string; id: string | null };

/**
 * A parser for the text of an event stream, fed in whatever pieces the network delivers.
 *
 * It follows the format rather than splitting on blank lines: a comment line (the keepalive) is
 * skipped, several `data:` lines join with a newline, and any of CR, LF or CRLF ends a line. A
 * lone CR at the end of a piece is held back, because it may be the first half of a CRLF.
 */
export function sseParser(): (chunk: string) => SseFrame[] {
  let buffer = "";
  let event = "";
  let data: string[] = [];
  let id: string | null = null;
  return (chunk) => {
    buffer += chunk;
    const frames: SseFrame[] = [];
    const ends = /\r\n|\r|\n/g;
    let start = 0;
    let match: RegExpExecArray | null;
    while ((match = ends.exec(buffer))) {
      if (match[0] === "\r" && match.index === buffer.length - 1) break;
      const line = buffer.slice(start, match.index);
      start = ends.lastIndex;
      if (line === "") {
        if (data.length) frames.push({ event: event || "message", data: data.join("\n"), id });
        event = "";
        data = [];
        id = null;
        continue;
      }
      if (line.startsWith(":")) continue;
      const colon = line.indexOf(":");
      const field = colon < 0 ? line : line.slice(0, colon);
      let value = colon < 0 ? "" : line.slice(colon + 1);
      if (value.startsWith(" ")) value = value.slice(1);
      if (field === "event") event = value;
      else if (field === "data") data.push(value);
      else if (field === "id" && !value.includes("\0")) id = value;
    }
    buffer = buffer.slice(start);
    return frames;
  };
}

/** How a connection ended: the stream closed, it never opened, or what answered was not a stream. */
export type StreamEnd = "ended" | "failed" | "not-a-stream" | "aborted";

/** Read one event stream until it ends, handing every frame to `onFrame`. Never throws. */
export async function readEventStream(url: string, headers: Record<string, string>, signal: AbortSignal, onFrame: (frame: SseFrame) => void): Promise<StreamEnd> {
  let response: Response;
  try {
    response = await fetch(url, { headers, signal, cache: "no-store" });
  } catch {
    return signal.aborted ? "aborted" : "failed";
  }
  // Checked before the status: a proxy's 502 page and a stub's `{}` are both "not a stream", and
  // retrying either every second would only add load to whatever is already wrong.
  const type = response.headers.get("content-type") ?? "";
  if (!type.toLowerCase().startsWith("text/event-stream")) {
    response.body?.cancel().catch(() => {});
    return "not-a-stream";
  }
  if (!response.ok || !response.body) return "failed";
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  const parse = sseParser();
  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) return "ended";
      for (const frame of parse(decoder.decode(value, { stream: true }))) {
        try {
          onFrame(frame);
        } catch {
          /* one bad frame must not end the stream */
        }
      }
    }
  } catch {
    return signal.aborted ? "aborted" : "ended";
  }
}

export interface LockManagerLike {
  request(name: string, options: { signal: AbortSignal }, callback: () => Promise<void>): Promise<unknown>;
}

export interface ChannelLike {
  postMessage(message: unknown): void;
  onmessage: ((event: { data: unknown }) => void) | null;
  close(): void;
}

export interface HubDeps {
  /** Open the stream past `cursor` (null: live from now) and read it to the end. */
  connect: (cursor: number | null, signal: AbortSignal, onFrame: (frame: SseFrame) => void) => Promise<StreamEnd>;
  /** Both present: one tab of the browser leads. Either missing: this tab holds its own stream. */
  locks?: LockManagerLike | null;
  channel?: ChannelLike | null;
  now?: () => number;
}

export interface HubSink {
  event: (event: AppEvent, meta: EventMeta) => void;
  up: (up: boolean) => void;
}

type Message =
  | { t: "event"; event: AppEvent; replayed: boolean; cursor: number | null }
  | { t: "state"; up: boolean; cursor: number | null }
  | { t: "ask" };

/**
 * The connection and the election. Separate from the module's handler registry so a test can run
 * two tabs against one fake lock and channel.
 */
export class EventHub {
  private cursor: number | null = null;
  private skew = 0;
  private live = false;
  private shownUp = false;
  private grace: ReturnType<typeof setTimeout> | null = null;
  private stopped = false;
  private leading = false;
  private connection: AbortController | null = null;
  private election: AbortController | null = null;
  private wake: (() => void) | null = null;
  private readonly now: () => number;

  constructor(private readonly deps: HubDeps, private readonly sink: HubSink) {
    this.now = deps.now ?? Date.now;
  }

  /** The highest sequence number this tab knows was delivered: where a new leader resumes. */
  get lastSeq(): number | null {
    return this.cursor;
  }

  /** Start from a cursor this page already had: a page brought back from the back-forward cache. */
  resumeFrom(cursor: number | null): void {
    this.cursor = cursor;
  }

  get isLeader(): boolean {
    return this.leading;
  }

  start(): void {
    const { locks, channel } = this.deps;
    if (locks && channel) {
      channel.onmessage = (e) => this.heard(e.data as Message);
      this.election = new AbortController();
      locks.request(LOCK, { signal: this.election.signal }, () => this.lead()).catch(() => {
        /* stopped while waiting for the lock */
      });
      channel.postMessage({ t: "ask" } satisfies Message);
    } else {
      void this.lead();
    }
  }

  /** Reconnect now if the stream is waiting out a backoff (the page became visible, the network came back). */
  poke(): void {
    this.wake?.();
  }

  stop(): void {
    if (this.stopped) return;
    this.stopped = true;
    this.election?.abort();
    this.connection?.abort();
    this.wake?.();
    if (this.leading) this.post({ t: "state", up: false, cursor: this.cursor });
    if (this.grace) clearTimeout(this.grace);
    this.live = false;
    this.show(false);
    this.deps.channel?.close();
  }

  private async lead(): Promise<void> {
    this.leading = true;
    let backoff = BACKOFF_FIRST_MS;
    while (!this.stopped) {
      this.connection = new AbortController();
      const began = this.now();
      const end = await this.deps.connect(this.cursor, this.connection.signal, (frame) => this.frame(frame));
      this.connected(false);
      if (this.stopped) break;
      let delay: number;
      if (end === "not-a-stream") delay = NOT_A_STREAM_MS;
      else {
        if (this.now() - began >= HEALTHY_MS) backoff = BACKOFF_FIRST_MS;
        delay = backoff;
        backoff = Math.min(backoff * 2, BACKOFF_MOST_MS);
      }
      await this.pause(delay);
    }
    // Returning releases the lock, and the next tab in line takes the stream over.
    this.leading = false;
  }

  private pause(ms: number): Promise<void> {
    return new Promise((resolve) => {
      const done = () => {
        clearTimeout(timer);
        this.wake = null;
        resolve();
      };
      const timer = setTimeout(done, ms);
      this.wake = done;
    });
  }

  private frame(frame: SseFrame): void {
    let data: any;
    try {
      data = JSON.parse(frame.data);
    } catch {
      return;
    }
    if (!data || typeof data !== "object") return;
    if (frame.event === "hello") {
      const server = Date.parse(String(data.server_time ?? ""));
      if (!Number.isNaN(server)) this.skew = this.now() - server;
      // Without a cursor the stream is live from the head; remembering it means a drop a second
      // later resumes from there instead of losing what happened in between.
      if (this.cursor === null && typeof data.head === "number") this.cursor = data.head;
      this.connected(true);
      return;
    }
    if (frame.event === "resync") {
      // The host could not replay from the cursor (too old, too far behind, or ahead of a host that
      // lost its table): it continues live from its head, and the lists are read again.
      if (typeof data.head === "number") this.cursor = data.head;
      const event: AppEvent = { seq: 0, at: new Date(this.now() - this.skew).toISOString(), type: "resync", project_id: null, session_id: null, staff_id: null, terminal_id: null, payload: data };
      this.deliver(event, false);
      return;
    }
    if (typeof data.type !== "string" || typeof data.payload !== "object" || data.payload === null) return;
    const event = data as AppEvent;
    if (typeof event.seq === "number" && event.seq > 0) this.cursor = event.seq;
    const at = Date.parse(event.at);
    const replayed = !Number.isNaN(at) && this.now() - this.skew - at > REPLAYED_AFTER_MS;
    this.deliver(event, replayed);
  }

  private deliver(event: AppEvent, replayed: boolean): void {
    this.post({ t: "event", event, replayed, cursor: this.cursor });
    this.sink.event(event, { replayed });
  }

  private heard(message: Message): void {
    if (this.stopped || !message || typeof message !== "object") return;
    if (message.t === "ask") {
      if (this.leading) this.post({ t: "state", up: this.live, cursor: this.cursor });
      return;
    }
    // A follower takes the leader's cursor as it is, not the larger of the two: after a resync from
    // a host that lost its table, the old, larger number is the wrong one.
    if (this.leading) return;
    this.cursor = message.cursor;
    if (message.t === "state") this.connected(message.up);
    else if (message.t === "event") this.sink.event(message.event, { replayed: message.replayed });
  }

  private post(message: Message): void {
    try {
      this.deps.channel?.postMessage(message);
    } catch {
      /* a closed channel: this tab is going away */
    }
  }

  private connected(live: boolean): void {
    if (this.stopped) return;
    const changed = this.live !== live;
    this.live = live;
    if (this.leading && changed) this.post({ t: "state", up: live, cursor: this.cursor });
    if (live) {
      if (this.grace) clearTimeout(this.grace);
      this.grace = null;
      this.show(true);
    } else if (this.shownUp && !this.grace) {
      this.grace = setTimeout(() => {
        this.grace = null;
        if (!this.live) this.show(false);
      }, DOWN_GRACE_MS);
    }
  }

  private show(up: boolean): void {
    if (this.shownUp === up) return;
    this.shownUp = up;
    this.sink.up(up);
  }
}

/** Whether the event type is one of `types`: an exact name, or a prefix ending in "." (`"run."`). No types: every event. */
export function matches(types: readonly string[], type: string): boolean {
  if (types.length === 0) return true;
  return types.some((t) => (t.endsWith(".") ? type.startsWith(t) : type === t));
}

type Entry = { types: readonly string[]; handler: EventHandler };
const handlers = new Set<Entry>();
let up = false;
const upListeners = new Set<() => void>();

function dispatch(event: AppEvent, meta: EventMeta): void {
  for (const entry of [...handlers]) {
    if (!matches(entry.types, event.type)) continue;
    try {
      entry.handler(event, meta);
    } catch (error) {
      console.error("event handler failed", event.type, error);
    }
  }
}

function setUp(value: boolean): void {
  if (up === value) return;
  up = value;
  for (const listener of [...upListeners]) listener();
}

/** Call `handler` for every event of the given types (see `matches`); returns the function that stops it. */
export function onEvent(types: readonly string[], handler: EventHandler): () => void {
  const entry = { types, handler };
  handlers.add(entry);
  return () => {
    handlers.delete(entry);
  };
}

/** `onEvent` for as long as the component is mounted. The handler may change on every render; `deps` re-subscribe. */
export function useEvent(types: readonly string[], handler: EventHandler, deps: readonly unknown[] = []): void {
  const latest = useRef(handler);
  latest.current = handler;
  const key = types.join(",");
  useEffect(
    () => onEvent(key ? key.split(",") : [], (event, meta) => latest.current(event, meta)),
    [key, ...deps],
  );
}

export function streamUp(): boolean {
  return up;
}

function subscribeUp(listener: () => void): () => void {
  upListeners.add(listener);
  return () => {
    upListeners.delete(listener);
  };
}

/** Whether the stream is connected: a poll can relax while this is true and must not while it is false. */
export function useStreamUp(): boolean {
  return useSyncExternalStore(subscribeUp, streamUp, streamUp);
}

export const SUMMARY_KEY = "/api/notifications/summary";
/** Several events of one run arrive together; the lists are read once for the burst, not once per event. */
export const LIST_DEBOUNCE_MS = 300;

/** What the cached reads do with the stream: the badge comes with the event, the lists are read again. */
export function wireCache(to: { on: typeof onEvent; prime: typeof prime; invalidate: typeof invalidate } = { on: onEvent, prime, invalidate }): () => void {
  let timer: ReturnType<typeof setTimeout> | null = null;
  const sessionsSoon = () => {
    if (timer) return;
    timer = setTimeout(() => {
      timer = null;
      to.invalidate("/api/sessions");
    }, LIST_DEBOUNCE_MS);
  };
  const offs = [
    to.on(["notify", "notify.seen", "notify.resolved"], (event) => {
      const summary = event.payload.summary;
      if (summary && typeof summary.unseen === "number" && typeof summary.needs_you === "number") to.prime(SUMMARY_KEY, { unseen: summary.unseen, needs_you: summary.needs_you });
      to.invalidate("/api/notifications?");
    }),
    to.on(["run.", "session.", "ask.", "permission."], sessionsSoon),
    // A pin made on another screen moves a row into or out of the pinned block. Only the pin: the
    // other project changes are the project screens' to read, and most of them come in bursts.
    to.on(["project.changed"], (event) => { if (event.payload.change === "pinned") sessionsSoon(); }),
    // Whatever the stream could not replay, every mounted read fetches again.
    to.on(["resync"], () => to.invalidate("")),
  ];
  return () => {
    for (const off of offs) off();
    if (timer) clearTimeout(timer);
  };
}

function browserDeps(): HubDeps {
  const locks = typeof navigator !== "undefined" && navigator.locks ? (navigator.locks as unknown as LockManagerLike) : null;
  let channel: ChannelLike | null = null;
  try {
    channel = typeof BroadcastChannel !== "undefined" ? (new BroadcastChannel(CHANNEL) as unknown as ChannelLike) : null;
  } catch {
    channel = null;
  }
  return {
    locks: channel ? locks : null,
    channel: locks ? channel : null,
    connect: (cursor, signal, onFrame) => {
      // The tab's id ties this window's presence to the connection: the host forgets it a few
      // seconds after the stream closes rather than a minute later when its report expires.
      const url = `/api/events?client=${encodeURIComponent(clientId())}&kind=${currentKind()}`;
      const headers: Record<string, string> = { ...api.authHeaders() };
      if (cursor !== null) headers["Last-Event-ID"] = String(cursor);
      return readEventStream(url, headers, signal, onFrame);
    },
  };
}

/** Open the stream (or follow the tab that holds it) and wire the cache to it. Called once the app is signed in. */
export function startEvents(): () => void {
  let hub = new EventHub(browserDeps(), { event: dispatch, up: setUp });
  const unwire = wireCache();
  hub.start();
  const poke = () => {
    if (document.visibilityState === "visible") hub.poke();
  };
  // Leaving the page stops the stream and hands the lock on at once; a page brought back from the
  // back-forward cache starts again from where it was.
  const leave = () => hub.stop();
  const back = (e: PageTransitionEvent) => {
    if (!e.persisted) return;
    const cursor = hub.lastSeq;
    hub = new EventHub(browserDeps(), { event: dispatch, up: setUp });
    hub.resumeFrom(cursor);
    hub.start();
  };
  document.addEventListener("visibilitychange", poke);
  window.addEventListener("online", poke);
  window.addEventListener("pagehide", leave);
  window.addEventListener("pageshow", back);
  return () => {
    document.removeEventListener("visibilitychange", poke);
    window.removeEventListener("online", poke);
    window.removeEventListener("pagehide", leave);
    window.removeEventListener("pageshow", back);
    hub.stop();
    unwire();
  };
}
