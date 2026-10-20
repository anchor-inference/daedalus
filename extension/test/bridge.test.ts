// The side panel's bridge to the framed app: what it takes as the frame's handshake, where it sends
// the credential, and which requests it drops. A page in another frame, a frame from another origin,
// a stale load and a request with no click behind it must all get nothing.

import { describe, expect, it } from "vitest";
import { GESTURE_OPS, HELLO, PANEL_PROTO, READY, ShellBridge, type BridgeDeps } from "../src/bridge.js";

const ORIGIN = "https://daedalus.example.org";
const NONCE = "n0nce-n0nce-n0nce-1";

class FakePort {
  sent: any[] = [];
  closed = false;
  onmessage: ((event: { data: unknown }) => void) | null = null;
  postMessage(message: unknown) {
    if (!this.closed) this.sent.push(message);
  }
  close() {
    this.closed = true;
  }
}

function setup(over: Partial<BridgeDeps> = {}) {
  const frame = { name: "frame" } as unknown as MessageEventSource;
  const posts: { target: unknown; message: any; origin: string; transfer: unknown[] }[] = [];
  const channels: { port1: FakePort; port2: FakePort }[] = [];
  const handled: { op: string; args: unknown }[] = [];
  let clicked = true;
  const bridge = new ShellBridge({
    frame: () => frame,
    origin: ORIGIN,
    hello: () => ({ token: "pnl_secret", lang: "en" }),
    handle: async (op, args) => {
      handled.push({ op, args });
      return { done: op };
    },
    gesture: () => clicked,
    channel: () => {
      const pair = { port1: new FakePort(), port2: new FakePort() };
      channels.push(pair);
      return pair as unknown as MessageChannel;
    },
    post: (target, message, origin, transfer) => posts.push({ target, message, origin, transfer }),
    ...over,
  });
  const ready = (extra: Record<string, unknown> = {}) => ({ source: frame, origin: ORIGIN, data: { t: READY, proto: PANEL_PROTO, nonce: NONCE, ...extra } });
  const request = async (channel: { port1: FakePort }, data: unknown) => {
    channel.port1.onmessage!({ data });
    for (let i = 0; i < 10; i++) await Promise.resolve();
  };
  return { bridge, frame, posts, channels, handled, ready, request, unclick: () => (clicked = false) };
}

describe("the handshake", () => {
  it("answers the frame's READY with the nonce, the credential and a port, to the host's origin only", async () => {
    const s = setup();
    expect(await s.bridge.onWindowMessage(s.ready())).toBe(true);
    expect(s.posts).toHaveLength(1);
    const [post] = s.posts;
    expect(post.target).toBe(s.frame);
    expect(post.origin).toBe(ORIGIN);
    expect(post.message).toMatchObject({ t: HELLO, proto: PANEL_PROTO, nonce: NONCE, generation: 1, token: "pnl_secret" });
    expect(post.transfer).toEqual([s.channels[0].port2]);
  });

  it.each([
    ["another window", (s: ReturnType<typeof setup>) => ({ ...s.ready(), source: {} as MessageEventSource })],
    ["another origin", (s: ReturnType<typeof setup>) => ({ ...s.ready(), origin: "https://evil.example" })],
    ["the extension's own origin", (s: ReturnType<typeof setup>) => ({ ...s.ready(), origin: "chrome-extension://lkhhklicnjlplokpadpccndipomikoaj" })],
    ["no nonce", (s: ReturnType<typeof setup>) => s.ready({ nonce: undefined })],
    ["a short nonce", (s: ReturnType<typeof setup>) => s.ready({ nonce: "abc" })],
    ["another protocol", (s: ReturnType<typeof setup>) => s.ready({ proto: 99 })],
    ["another message", (s: ReturnType<typeof setup>) => s.ready({ t: HELLO })],
  ])("drops a READY from %s, and the credential goes nowhere", async (_, make) => {
    const s = setup();
    expect(await s.bridge.onWindowMessage(make(s))).toBe(false);
    expect(s.posts).toEqual([]);
  });

  it("drops everything while there is no frame", async () => {
    const s = setup({ frame: () => null });
    expect(await s.bridge.onWindowMessage(s.ready())).toBe(false);
    expect(s.posts).toEqual([]);
  });
});

describe("requests on the port", () => {
  it("runs a known operation and answers by its id", async () => {
    const s = setup();
    await s.bridge.onWindowMessage(s.ready());
    await s.request(s.channels[0], { id: 7, op: "tab.current" });
    expect(s.handled).toEqual([{ op: "tab.current", args: {} }]);
    expect(s.channels[0].port1.sent).toEqual([{ id: 7, ok: true, value: { done: "tab.current" } }]);
  });

  it("refuses an operation it does not know, without running anything", async () => {
    const s = setup();
    await s.bridge.onWindowMessage(s.ready());
    await s.request(s.channels[0], { id: 8, op: "cookies.read" });
    expect(s.handled).toEqual([]);
    expect(s.channels[0].port1.sent).toEqual([{ id: 8, ok: false, error: "unknown" }]);
  });

  it.each([...GESTURE_OPS])("needs the operator's click for %s", async (op) => {
    const s = setup();
    s.unclick();
    await s.bridge.onWindowMessage(s.ready());
    await s.request(s.channels[0], { id: 9, op, args: { group: "g", session: "s" } });
    expect(s.handled).toEqual([]);
    expect(s.channels[0].port1.sent).toEqual([{ id: 9, ok: false, error: "gesture" }]);
  });

  it("drops a request of a superseded load: the frame reloaded, and its old port is dead", async () => {
    const s = setup();
    await s.bridge.onWindowMessage(s.ready());
    await s.bridge.onWindowMessage(s.ready({ nonce: "second-nonce-second" }));
    expect(s.channels[0].port1.closed).toBe(true);
    await s.request(s.channels[0], { id: 10, op: "capture.visible" });
    expect(s.handled).toEqual([]);
    await s.request(s.channels[1], { id: 11, op: "tab.current" });
    expect(s.handled).toEqual([{ op: "tab.current", args: {} }]);
    expect(s.posts[1].message.generation).toBe(2);
  });

  it("tells only the current load of a change", async () => {
    const s = setup();
    await s.bridge.onWindowMessage(s.ready());
    await s.bridge.onWindowMessage(s.ready({ nonce: "second-nonce-second" }));
    s.bridge.emit("current", { tab: null });
    expect(s.channels[0].port1.sent).toEqual([]);
    expect(s.channels[1].port1.sent).toEqual([{ event: "current", value: { tab: null } }]);
  });

  it("drops malformed requests silently", async () => {
    const s = setup();
    await s.bridge.onWindowMessage(s.ready());
    for (const junk of [null, "tab.current", { op: "tab.current" }, { id: "1", op: "tab.current" }, { id: 1.5, op: "tab.current" }]) {
      await s.request(s.channels[0], junk);
    }
    expect(s.handled).toEqual([]);
    expect(s.channels[0].port1.sent).toEqual([]);
  });

  it("answers a handler's failure with its message", async () => {
    const s = setup({ handle: async () => { throw new Error("unreadable"); } });
    await s.bridge.onWindowMessage(s.ready());
    await s.request(s.channels[0], { id: 12, op: "tab.current" });
    expect(s.channels[0].port1.sent).toEqual([{ id: 12, ok: false, error: "unreadable" }]);
  });
});
