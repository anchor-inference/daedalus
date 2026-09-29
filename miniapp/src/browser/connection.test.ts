// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../api";
import type { SocketLike } from "../terminal/connection";
import { BrowserConnection, type FrameSink, type ViewDeps, type ViewState } from "./connection";
import { COPY_TIMEOUT_MS, LiveView } from "./live";
import { VIEW, type FrameMeta } from "./protocol";

class FakeSocket implements SocketLike {
  binaryType = "blob";
  readyState = 0;
  sent: Uint8Array[] = [];
  onopen: SocketLike["onopen"] = null;
  onmessage: SocketLike["onmessage"] = null;
  onclose: SocketLike["onclose"] = null;
  onerror: SocketLike["onerror"] = null;
  constructor(public url: string) {}
  send(data: Uint8Array) {
    this.sent.push(data);
  }
  close() {
    this.readyState = 3;
  }
  open() {
    this.readyState = 1;
    this.onopen?.({});
  }
  receive(bytes: Uint8Array) {
    this.onmessage?.({ data: bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength) });
  }
  drop(code = 1006) {
    this.readyState = 3;
    this.onclose?.({ code });
  }
  of(type: number) {
    return this.sent.filter((f) => f[0] === type);
  }
  json(type: number) {
    return this.of(type).map((f) => JSON.parse(new TextDecoder().decode(f.subarray(1))));
  }
  acks() {
    return this.of(VIEW.ACK).map((f) => new DataView(f.buffer, f.byteOffset).getUint32(1));
  }
}

/** A sink whose draws finish only when the test says so, as a slow phone's decoder would. */
class FakeSink implements FrameSink {
  drawn: number[] = [];
  pending: { no: number; done: () => void }[] = [];
  auto = true;
  draw(frame: { frameNo: number }) {
    return new Promise<void>((resolve) => {
      const done = () => {
        this.drawn.push(frame.frameNo);
        resolve();
      };
      if (this.auto) done();
      else this.pending.push({ no: frame.frameNo, done });
    });
  }
  finish() {
    const p = this.pending.shift();
    p?.done();
  }
}

const enc = new TextEncoder();
const META: FrameMeta = { tab: "t1", tier: "live", w: 1280, h: 800, vw: 1280, vh: 800, scroll_x: 0, scroll_y: 0, offset_top: 0, page_scale: 1, ts: 1 };
function frame(no: number): Uint8Array {
  const meta = enc.encode(JSON.stringify(META));
  const out = new Uint8Array(7 + meta.length + 4);
  out[0] = VIEW.FRAME;
  new DataView(out.buffer).setUint32(1, no);
  new DataView(out.buffer).setUint16(5, meta.length);
  out.set(meta, 7);
  out.set([0xff, 0xd8, 0xff, 0xd9], 7 + meta.length);
  return out;
}
const event = (body: object) => new Uint8Array([VIEW.EVENT, ...enc.encode(JSON.stringify(body))]);
const hello = event({ type: "hello", client_id: "c7", read_only: false, tier: "live", group: { id: "g1", profile: "p", viewport: { w: 1280, h: 800 } }, tab_id: "t1", control: { owner: "agent", holder: null, until: null, reason: "" }, fps_cap: 15 });
const flush = () => vi.advanceTimersByTimeAsync(0);

let sockets: FakeSocket[];
let tickets: { group: string; tier: string; readOnly: boolean }[];
let deps: ViewDeps;
let ticketError: Error | null;

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "setInterval", "clearInterval", "Date"] });
  sockets = [];
  tickets = [];
  ticketError = null;
  deps = {
    ticket: async (group, tier, readOnly) => {
      tickets.push({ group, tier, readOnly });
      if (ticketError) throw ticketError;
      return { ticket: `tk${tickets.length}` };
    },
    socket: (url) => {
      const s = new FakeSocket(url);
      sockets.push(s);
      return s;
    },
    base: () => "ws://h",
    wake: () => () => undefined,
    random: () => 0.5,
  };
});
afterEach(() => vi.useRealTimers());

async function connected(opts: { readOnly?: boolean; sink?: FakeSink } = {}) {
  const sink = opts.sink ?? new FakeSink();
  const states: ViewState[] = [];
  const events: string[] = [];
  const conn = new BrowserConnection({ group: "g1", tier: "live", tab: "t1", readOnly: opts.readOnly, box: () => ({ max_w: 1600, max_h: 1000, dpr: 2 }), sink, onState: (s) => states.push(s), onEvent: (e) => events.push(e.type) }, deps);
  await vi.advanceTimersByTimeAsync(0);
  const s = sockets.at(-1)!;
  s.open();
  s.receive(hello);
  return { conn, sink, states, events, s };
}

describe("a live view", () => {
  it("asks for a ticket, opens the group's socket and attaches with its box", async () => {
    const { conn, s, states } = await connected();
    expect(tickets).toEqual([{ group: "g1", tier: "live", readOnly: false }]);
    expect(s.url).toBe("ws://h/ws/browsers/g1?ticket=tk1");
    expect(s.json(VIEW.ATTACH)).toEqual([{ tier: "live", tab: "t1", max_w: 1600, max_h: 1000, dpr: 2 }]);
    expect(states.at(-1)).toEqual({ kind: "live" });
    expect(conn.clientId).toBe("c7");
  });

  it("acknowledges a frame once it is drawn, not when it arrives", async () => {
    const sink = new FakeSink();
    sink.auto = false;
    const { s } = await connected({ sink });
    s.receive(frame(1));
    await flush();
    expect(s.acks()).toEqual([]);
    sink.finish();
    await flush();
    expect(s.acks()).toEqual([1]);
  });

  it("draws only the newest of the frames that arrived during a slow draw", async () => {
    const sink = new FakeSink();
    sink.auto = false;
    const { conn, s } = await connected({ sink });
    s.receive(frame(1));
    s.receive(frame(2));
    s.receive(frame(3));
    s.receive(frame(4));
    sink.finish();
    await flush();
    sink.finish();
    await flush();
    expect(sink.drawn).toEqual([1, 4]);
    expect(s.acks()).toEqual([1, 4]);
    expect(conn.dropped).toBe(2);
  });

  it("ignores a frame it cannot read and keeps drawing the next", async () => {
    const { s, sink } = await connected();
    s.receive(new Uint8Array([VIEW.FRAME, 0, 0]));
    s.receive(frame(9));
    await flush();
    expect(sink.drawn).toEqual([9]);
  });

  it("sends input only while live and never from a read-only view", async () => {
    const { conn, s } = await connected();
    expect(conn.input({ t: "mouse", type: "down", x: 1, y: 2, button: "left", clicks: 1, mods: 0 })).toBe(true);
    expect(s.json(VIEW.INPUT)).toEqual([{ t: "mouse", type: "down", x: 1, y: 2, button: "left", clicks: 1, mods: 0 }]);
    const ro = await connected({ readOnly: true });
    expect(ro.conn.input({ t: "text", text: "hi" })).toBe(false);
    expect(ro.s.of(VIEW.INPUT)).toEqual([]);
  });

  it("splits a long text into several inputs", async () => {
    const { conn, s } = await connected();
    conn.input({ t: "text", text: "y".repeat(2500) });
    expect(s.json(VIEW.INPUT).map((i) => i.text.length)).toEqual([1000, 1000, 500]);
  });

  it("sends a view change on the same socket, and attaches with it after a reconnect", async () => {
    const { conn, s } = await connected();
    conn.view({ tier: "thumb", max_w: 320, max_h: 200 });
    expect(s.json(VIEW.VIEW)).toEqual([{ tier: "thumb", max_w: 320, max_h: 200 }]);
    s.drop(1012);
    await vi.advanceTimersByTimeAsync(1000);
    const again = sockets.at(-1)!;
    again.open();
    expect(again.json(VIEW.ATTACH)[0].tier).toBe("thumb");
    expect(tickets.at(-1)!.tier).toBe("thumb");
  });

  it("reconnects after the host restarts (1012) with a new ticket", async () => {
    const { s, states } = await connected();
    s.drop(1012);
    expect(states.at(-1)).toMatchObject({ kind: "reconnecting", attempt: 1 });
    await vi.advanceTimersByTimeAsync(600);
    expect(tickets.length).toBe(2);
    const again = sockets.at(-1)!;
    again.open();
    again.receive(hello);
    expect(states.at(-1)).toEqual({ kind: "live" });
  });

  it("gives up on a group that is gone and on a page that is not allowed", async () => {
    const a = await connected();
    a.s.drop(4404);
    expect(a.states.at(-1)).toEqual({ kind: "unavailable", reason: "gone" });
    await vi.advanceTimersByTimeAsync(30_000);
    expect(sockets.length).toBe(1);
    const b = await connected();
    b.s.drop(4403);
    expect(b.states.at(-1)).toEqual({ kind: "unavailable", reason: "origin" });
  });

  it("says the group is gone when the ticket is refused with 404", async () => {
    ticketError = new ApiError(404, "no such browser");
    const states: ViewState[] = [];
    new BrowserConnection({ group: "g9", tier: "thumb", box: () => ({ max_w: 320, max_h: 200 }), sink: new FakeSink(), onState: (s) => states.push(s) }, deps);
    await vi.advanceTimersByTimeAsync(0);
    expect(states.at(-1)).toEqual({ kind: "unavailable", reason: "gone" });
  });

  it("treats 45 s of silence as a dead socket", async () => {
    const { s } = await connected();
    await vi.advanceTimersByTimeAsync(50_000);
    expect(sockets.length).toBe(2);
    expect(s.readyState).toBe(3);
  });

  it("passes events on in order", async () => {
    const { s, events } = await connected();
    s.receive(event({ type: "action", id: "a1", group: "g1", tab: "t1", actor: "agent", kind: "click", name: "Go", element: "the Go button", at: 1 }));
    s.receive(event({ type: "control", owner: "human", holder: "you", until: null, reason: "" }));
    expect(events).toEqual(["hello", "action", "control"]);
  });

  it("stops everything when closed", async () => {
    const { conn, s } = await connected();
    conn.close();
    expect(s.readyState).toBe(3);
    s.receive(frame(1));
    await vi.advanceTimersByTimeAsync(60_000);
    expect(sockets.length).toBe(1);
  });
});

describe("a view out of sight", () => {
  it("tells the daemon when its page is hidden and when it is back, and says so at attach", async () => {
    let hidden = true;
    let told: ((hidden: boolean) => void) | null = null;
    deps.hidden = () => hidden;
    deps.visibility = (callback) => {
      told = callback;
      return () => {
        told = null;
      };
    };
    const { conn, s } = await connected();
    // Opened in a background tab: the ATTACH is followed at once by the VIEW that says so.
    expect(s.json(VIEW.VIEW)).toEqual([{ hidden: true }]);
    hidden = false;
    told!(false);
    told!(false);
    expect(s.json(VIEW.VIEW)).toEqual([{ hidden: true }, { hidden: false }]);
    conn.close();
    expect(told).toBeNull();
  });
});

describe("a copy from the page", () => {
  async function live() {
    const view = new LiveView({ group: "g1", tier: "live", box: () => ({ max_w: 1600, max_h: 1000 }), deps });
    await vi.advanceTimersByTimeAsync(0);
    const s = sockets.at(-1)!;
    s.open();
    s.receive(hello);
    return { view, s };
  }

  it("asks the daemon by id and takes the answer that names it, not another", async () => {
    const { view, s } = await live();
    const first = view.copy();
    const second = view.copy();
    const asked = s.json(VIEW.INPUT);
    expect(asked).toEqual([{ t: "copy", id: "k1" }, { t: "copy", id: "k2" }]);
    s.receive(event({ type: "copied", id: "k2", text: "two", truncated: false, withheld: false }));
    s.receive(event({ type: "copied", id: "k1", text: "", truncated: false, withheld: true }));
    s.receive(event({ type: "copied", id: "k9", text: "nobody asked" }));
    await expect(second).resolves.toEqual({ text: "two", truncated: false, withheld: false });
    await expect(first).resolves.toEqual({ text: "", truncated: false, withheld: true });
    view.close();
  });

  it("fails on the page's error, on silence and on a closed view", async () => {
    const { view, s } = await live();
    const failed = view.copy();
    s.receive(event({ type: "copied", id: "k1", text: "", error: "a dialog is open" }));
    await expect(failed).rejects.toThrow("a dialog is open");
    const silent = view.copy();
    const caught = expect(silent).rejects.toThrow("timeout");
    await vi.advanceTimersByTimeAsync(COPY_TIMEOUT_MS + 1);
    await caught;
    const left = view.copy();
    view.close();
    await expect(left).rejects.toThrow("closed");
  });
});
