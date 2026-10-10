// @vitest-environment jsdom
// The event stream without a host: the parser, the replay flag, the hand-over between tabs, the
// backoff, and what the cache does with the events.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AppEvent } from "./api";
import {
  BACKOFF_FIRST_MS,
  DOWN_GRACE_MS,
  EventHub,
  LIST_DEBOUNCE_MS,
  NOT_A_STREAM_MS,
  SUMMARY_KEY,
  matches,
  readEventStream,
  sseParser,
  wireCache,
  type ChannelLike,
  type EventMeta,
  type HubSink,
  type LockManagerLike,
  type SseFrame,
  type StreamEnd,
} from "./events";

describe("the frame parser", () => {
  it("joins a frame split across pieces, anywhere", () => {
    const parse = sseParser();
    const text = 'id: 7\nevent: run.finished\ndata: {"seq":7}\n\n';
    const frames: SseFrame[] = [];
    for (const piece of [text.slice(0, 3), text.slice(3, 20), text.slice(20, text.length - 1), text.slice(text.length - 1)]) frames.push(...parse(piece));
    expect(frames).toEqual([{ event: "run.finished", data: '{"seq":7}', id: "7" }]);
  });

  it("skips comments, joins several data lines and takes CRLF split between pieces", () => {
    const parse = sseParser();
    const frames = [...parse(": keepalive\r"), ...parse("\n\r\nevent: hello\r\ndata: a\r\ndata: b\r"), ...parse("\n\r\n")];
    expect(frames).toEqual([{ event: "hello", data: "a\nb", id: null }]);
  });

  it("gives an event without an id line no id, and one without a name the default", () => {
    const parse = sseParser();
    expect(parse('id: 3\nevent: a\ndata: 1\n\nevent: terminal.progress\ndata: 2\n\ndata:3\n\n')).toEqual([
      { event: "a", data: "1", id: "3" },
      { event: "terminal.progress", data: "2", id: null },
      { event: "message", data: "3", id: null },
    ]);
  });
});

describe("types", () => {
  it("match by exact name or by a prefix ending in a dot", () => {
    expect(matches(["run."], "run.finished")).toBe(true);
    expect(matches(["run."], "runner")).toBe(false);
    expect(matches(["notify"], "notify.seen")).toBe(false);
    expect(matches(["notify", "notify.seen"], "notify.seen")).toBe(true);
    expect(matches([], "anything")).toBe(true);
  });
});

function event(seq: number, at: string, type = "run.finished"): AppEvent {
  return { seq, at, type, project_id: null, session_id: "s1", staff_id: null, terminal_id: null, payload: {} };
}

/** Frames as the host writes them. */
const hello = (head: number, serverTime: string): SseFrame => ({ event: "hello", data: JSON.stringify({ head, oldest: 1, server_time: serverTime, client: "tab" }), id: null });
const frameOf = (e: AppEvent): SseFrame => ({ event: e.type, data: JSON.stringify(e), id: e.seq ? String(e.seq) : null });

/** A connection the test drives: frames go in by hand, and it ends when the test says so. */
class FakeConnections {
  calls: { cursor: number | null; signal: AbortSignal; onFrame: (f: SseFrame) => void; end: (how: StreamEnd) => void }[] = [];
  connect = (cursor: number | null, signal: AbortSignal, onFrame: (f: SseFrame) => void): Promise<StreamEnd> =>
    new Promise((resolve) => {
      signal.addEventListener("abort", () => resolve("aborted"));
      this.calls.push({ cursor, signal, onFrame, end: resolve });
    });
  get last() {
    return this.calls[this.calls.length - 1];
  }
}

class Recorder implements HubSink {
  events: { event: AppEvent; meta: EventMeta }[] = [];
  ups: boolean[] = [];
  event = (event: AppEvent, meta: EventMeta) => {
    this.events.push({ event, meta });
  };
  up = (up: boolean) => {
    this.ups.push(up);
  };
}

/** One lock shared by every tab: the first request holds it until its callback's promise settles. */
class FakeLocks implements LockManagerLike {
  private held = false;
  private queue: { callback: () => Promise<void>; resolve: (v: unknown) => void; reject: (e: unknown) => void }[] = [];
  request(_name: string, options: { signal: AbortSignal }, callback: () => Promise<void>): Promise<unknown> {
    return new Promise((resolve, reject) => {
      const item = { callback, resolve, reject };
      options.signal.addEventListener("abort", () => {
        const i = this.queue.indexOf(item);
        if (i >= 0) {
          this.queue.splice(i, 1);
          reject(new Error("aborted"));
        }
      });
      this.queue.push(item);
      this.pump();
    });
  }
  private pump() {
    if (this.held) return;
    const item = this.queue.shift();
    if (!item) return;
    this.held = true;
    void item.callback().then((v) => {
      this.held = false;
      item.resolve(v);
      this.pump();
    });
  }
}

/** A broadcast channel: a message reaches every other end, a moment later, never the sender. */
class FakeBroadcast {
  ends = new Set<ChannelLike>();
  open(): ChannelLike {
    const end: ChannelLike = {
      onmessage: null,
      postMessage: (message) => {
        for (const other of this.ends) if (other !== end) queueMicrotask(() => other.onmessage?.({ data: structuredClone(message) }));
      },
      close: () => {
        this.ends.delete(end);
      },
    };
    this.ends.add(end);
    return end;
  }
}

const flush = async () => {
  for (let i = 0; i < 10; i++) await Promise.resolve();
};

describe("the hub", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("marks an event older than half a minute on the host's clock as replayed", async () => {
    // The browser's clock runs five minutes ahead of the host; the skew from `hello` corrects it.
    const clientNow = Date.parse("2026-09-24T12:05:00Z");
    const conn = new FakeConnections();
    const sink = new Recorder();
    const hub = new EventHub({ connect: conn.connect, now: () => clientNow }, sink);
    hub.start();
    conn.last.onFrame(hello(40, "2026-09-24T12:00:00Z"));
    expect(hub.lastSeq).toBe(40);
    conn.last.onFrame(frameOf(event(41, "2026-09-24T11:59:50Z")));
    conn.last.onFrame(frameOf(event(42, "2026-09-24T11:59:20Z")));
    expect(sink.events.map((e) => [e.event.seq, e.meta.replayed])).toEqual([[41, false], [42, true]]);
    expect(hub.lastSeq).toBe(42);
    expect(sink.ups).toEqual([true]);
    hub.stop();
  });

  it("resumes from the last sequence it saw, and a resync moves the cursor to the host's head", async () => {
    const conn = new FakeConnections();
    const sink = new Recorder();
    const hub = new EventHub({ connect: conn.connect }, sink);
    hub.start();
    conn.last.onFrame(hello(10, new Date().toISOString()));
    conn.last.onFrame(frameOf(event(11, new Date().toISOString())));
    conn.last.end("ended");
    await flush();
    await vi.advanceTimersByTimeAsync(BACKOFF_FIRST_MS);
    expect(conn.calls.map((c) => c.cursor)).toEqual([null, 11]);
    conn.last.onFrame(hello(3, new Date().toISOString()));
    conn.last.onFrame({ event: "resync", data: JSON.stringify({ reason: "cursor_ahead", head: 3 }), id: null });
    expect(hub.lastSeq).toBe(3);
    expect(sink.events.at(-1)?.event.type).toBe("resync");
    hub.stop();
  });

  it("waits a minute after an answer that is not an event stream, and a second after a dropped one", async () => {
    const conn = new FakeConnections();
    const hub = new EventHub({ connect: conn.connect }, new Recorder());
    hub.start();
    conn.last.end("not-a-stream");
    await flush();
    await vi.advanceTimersByTimeAsync(NOT_A_STREAM_MS - 1);
    expect(conn.calls.length).toBe(1);
    await vi.advanceTimersByTimeAsync(1);
    expect(conn.calls.length).toBe(2);
    conn.last.end("failed");
    await flush();
    await vi.advanceTimersByTimeAsync(BACKOFF_FIRST_MS);
    expect(conn.calls.length).toBe(3);
    conn.last.end("failed");
    await flush();
    await vi.advanceTimersByTimeAsync(BACKOFF_FIRST_MS);
    expect(conn.calls.length).toBe(3); // the second failure in a row waits twice as long
    await vi.advanceTimersByTimeAsync(BACKOFF_FIRST_MS);
    expect(conn.calls.length).toBe(4);
    hub.stop();
  });

  it("reconnects at once when poked during a backoff", async () => {
    const conn = new FakeConnections();
    const hub = new EventHub({ connect: conn.connect }, new Recorder());
    hub.start();
    conn.last.end("not-a-stream");
    await flush();
    hub.poke();
    await flush();
    expect(conn.calls.length).toBe(2);
    hub.stop();
  });

  it("keeps saying up for a moment after a drop, so a quick reconnect does not flip the polls", async () => {
    const conn = new FakeConnections();
    const sink = new Recorder();
    const hub = new EventHub({ connect: conn.connect }, sink);
    hub.start();
    conn.last.onFrame(hello(1, new Date().toISOString()));
    conn.last.end("ended");
    await flush();
    await vi.advanceTimersByTimeAsync(BACKOFF_FIRST_MS);
    conn.last.onFrame(hello(1, new Date().toISOString()));
    expect(sink.ups).toEqual([true]);
    conn.last.end("ended");
    await flush();
    await vi.advanceTimersByTimeAsync(DOWN_GRACE_MS);
    expect(sink.ups).toEqual([true, false]);
    hub.stop();
  });

  it("holds one stream for two tabs, and the tab that takes over resumes from the highest sequence", async () => {
    const locks = new FakeLocks();
    const air = new FakeBroadcast();
    const conn = new FakeConnections();
    const a = new Recorder();
    const b = new Recorder();
    const first = new EventHub({ connect: conn.connect, locks, channel: air.open() }, a);
    const second = new EventHub({ connect: conn.connect, locks, channel: air.open() }, b);
    first.start();
    second.start();
    await flush();
    expect(conn.calls.length).toBe(1);
    expect(first.isLeader).toBe(true);
    expect(second.isLeader).toBe(false);

    const now = new Date().toISOString();
    conn.last.onFrame(hello(20, now));
    conn.last.onFrame(frameOf(event(21, now)));
    conn.last.onFrame(frameOf(event(22, now, "session.unread_result")));
    await flush();
    // Both tabs saw both events, the follower through the channel, and both know the stream is up.
    expect(a.events.map((e) => e.event.seq)).toEqual([21, 22]);
    expect(b.events.map((e) => e.event.seq)).toEqual([21, 22]);
    expect(b.ups).toEqual([true]);
    expect(second.lastSeq).toBe(22);

    // The leading tab closes: the other one takes the lock and resumes past 22.
    first.stop();
    await flush();
    expect(conn.calls.length).toBe(2);
    expect(conn.last.cursor).toBe(22);
    expect(second.isLeader).toBe(true);
    expect(conn.calls[0].signal.aborted).toBe(true);
    second.stop();
  });

  it("answers a tab that opens later with whether the stream is up", async () => {
    const locks = new FakeLocks();
    const air = new FakeBroadcast();
    const conn = new FakeConnections();
    const leader = new EventHub({ connect: conn.connect, locks, channel: air.open() }, new Recorder());
    leader.start();
    await flush();
    conn.last.onFrame(hello(5, new Date().toISOString()));
    const late = new Recorder();
    const follower = new EventHub({ connect: conn.connect, locks, channel: air.open() }, late);
    follower.start();
    await flush();
    expect(late.ups).toEqual([true]);
    expect(follower.lastSeq).toBe(5);
    leader.stop();
    follower.stop();
  });
});

describe("reading a stream", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("counts an answer that is not an event stream as not a stream, whatever its status", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("{}", { status: 200, headers: { "content-type": "application/json" } })));
    expect(await readEventStream("/api/events", {}, new AbortController().signal, () => {})).toBe("not-a-stream");
    vi.stubGlobal("fetch", vi.fn(async () => new Response("<html>Bad gateway</html>", { status: 502, headers: { "content-type": "text/html" } })));
    expect(await readEventStream("/api/events", {}, new AbortController().signal, () => {})).toBe("not-a-stream");
  });

  it("hands every frame over and says the stream ended", async () => {
    const body = 'event: hello\ndata: {"head":1}\n\n: keepalive\n\nid: 2\nevent: run.started\ndata: {"seq":2}\n\n';
    vi.stubGlobal("fetch", vi.fn(async () => new Response(body, { status: 200, headers: { "content-type": "text/event-stream; charset=utf-8" } })));
    const frames: SseFrame[] = [];
    expect(await readEventStream("/api/events", {}, new AbortController().signal, (f) => frames.push(f))).toBe("ended");
    expect(frames.map((f) => f.event)).toEqual(["hello", "run.started"]);
  });

  it("says a refused connection failed", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => { throw new TypeError("network"); }));
    expect(await readEventStream("/api/events", {}, new AbortController().signal, () => {})).toBe("failed");
  });
});

describe("the cache", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  function wired() {
    const handlers: { types: readonly string[]; handler: (e: AppEvent, m: EventMeta) => void }[] = [];
    const primed: [string, unknown][] = [];
    const invalidated: string[] = [];
    const unwire = wireCache({
      on: (types, handler) => {
        const entry = { types, handler };
        handlers.push(entry);
        return () => handlers.splice(handlers.indexOf(entry), 1);
      },
      prime: (key, data) => void primed.push([key, data]),
      invalidate: (prefix) => void invalidated.push(prefix),
    });
    const send = (e: AppEvent) => {
      for (const h of [...handlers]) if (matches(h.types, e.type)) h.handler(e, { replayed: false });
    };
    return { handlers, primed, invalidated, unwire, send };
  }

  it("takes the badge from a notification event and reads the notification list again", () => {
    const { primed, invalidated, send, unwire } = wired();
    send({ ...event(5, new Date().toISOString(), "notify.seen"), payload: { ids: "all", summary: { unseen: 0, needs_you: 1 } } });
    expect(primed).toEqual([[SUMMARY_KEY, { unseen: 0, needs_you: 1 }]]);
    expect(invalidated).toEqual(["/api/notifications?"]);
    unwire();
  });

  it("reads the session lists once for a burst of run events, within the debounce", async () => {
    const { invalidated, send, unwire } = wired();
    const now = new Date().toISOString();
    send(event(1, now, "run.finished"));
    send(event(2, now, "session.status"));
    send(event(3, now, "session.unread_result"));
    expect(invalidated).toEqual([]);
    await vi.advanceTimersByTimeAsync(LIST_DEBOUNCE_MS);
    expect(invalidated).toEqual(["/api/sessions"]);
    send(event(4, now, "permission.pending"));
    await vi.advanceTimersByTimeAsync(LIST_DEBOUNCE_MS);
    expect(invalidated).toEqual(["/api/sessions", "/api/sessions"]);
    unwire();
  });

  it("reads the session lists again for a pin made elsewhere, and not for other project changes", async () => {
    const { invalidated, send, unwire } = wired();
    const now = new Date().toISOString();
    send({ ...event(1, now, "project.changed"), payload: { change: "settings" } });
    await vi.advanceTimersByTimeAsync(LIST_DEBOUNCE_MS);
    expect(invalidated).toEqual([]);
    send({ ...event(2, now, "project.changed"), payload: { change: "pinned" } });
    await vi.advanceTimersByTimeAsync(LIST_DEBOUNCE_MS);
    expect(invalidated).toEqual(["/api/sessions"]);
    unwire();
  });

  it("reads everything mounted again after a resync, and stops listening when unwired", () => {
    const { handlers, invalidated, send, unwire } = wired();
    send({ ...event(0, new Date().toISOString(), "resync"), payload: { reason: "expired", head: 9 } });
    expect(invalidated).toEqual([""]);
    unwire();
    expect(handlers.length).toBe(0);
  });
});
