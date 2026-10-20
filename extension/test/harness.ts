// A relay wired to the fake Chrome and to a transport the test plays the daemon through.

import { vi } from "vitest";
import { Relay } from "../src/relay.js";
import type { TransportConfig, TransportHandlers } from "../src/transport.js";
import { installFakeChrome } from "./fake-chrome.js";

export type FakeTransport = {
  config: TransportConfig;
  handlers: TransportHandlers;
  sent: any[];
  closed: boolean;
  send(frame: unknown): void;
  close(): void;
};

/** Lets every pending promise chain run; works under fake timers, where `setTimeout(0)` would not. */
export async function flush(): Promise<void> {
  for (let i = 0; i < 60; i++) await Promise.resolve();
}

export async function harness(options: { language?: string; settings?: Record<string, unknown> } = {}) {
  vi.useFakeTimers();
  vi.setSystemTime(new Date("2026-10-20T12:00:00Z"));
  const fake = installFakeChrome({ language: options.language });
  if (options.settings) await fake.chrome.storage.local.set(options.settings);
  const transports: FakeTransport[] = [];
  const relay = new Relay((config, handlers) => {
    const transport: FakeTransport = {
      config,
      handlers,
      sent: [],
      closed: false,
      send(frame) {
        this.sent.push(structuredClone(frame));
      },
      close() {
        this.closed = true;
      },
    };
    transports.push(transport);
    return transport;
  });
  const { events } = fake;
  events.tabsCreated.addListener((tab: any) => relay.onTabCreated(tab));
  events.tabsRemoved.addListener((id: number) => relay.onTabRemoved(id));
  events.tabsUpdated.addListener((id: number, info: any, tab: any) => relay.onTabUpdated(id, info, tab));
  events.groupRemoved.addListener((group: any) => relay.onGroupRemoved(group));
  events.debuggerEvent.addListener((source: any, method: string, params: any) => relay.onDebuggerEvent(source, method, params));
  events.debuggerDetach.addListener((source: any, reason: string) => relay.onDebuggerDetach(source, reason));
  events.menuClicked.addListener((info: any, tab: any) => relay.onMenuClicked(info, tab));
  await relay.start();
  await flush();

  const current = () => transports[transports.length - 1];
  const h = {
    fake,
    relay,
    transports,
    current,
    /** Frames the extension sent on the current transport since the last call. */
    take(): any[] {
      const transport = current();
      return transport.sent.splice(0);
    },
    async daemon(frame: unknown) {
      current().handlers.onFrame(structuredClone(frame));
      await flush();
    },
    async welcome(proto = 2) {
      await h.daemon({ t: "welcome", proto, daemon_version: "0.19.2", install_name: "Daedalus", install_kind: "native" });
    },
    /** Plays the daemon asking for a call and returns the extension's result frame. */
    async call(op: string, args: Record<string, unknown>, id = 900) {
      await h.daemon({ t: "call", id, op, args });
      await flush();
      const frames = current().sent;
      const index = frames.findIndex((frame) => frame.t === "result" && frame.id === id);
      return index >= 0 ? frames.splice(index, 1)[0] : undefined;
    },
    async cdp(session: string, method: string, params: Record<string, unknown> = {}, id = 901) {
      await h.daemon({ t: "cdp", id, session, method, params });
      await flush();
      const frames = current().sent;
      const index = frames.findIndex((frame) => frame.t === "result" && frame.id === id);
      return index >= 0 ? frames.splice(index, 1)[0] : undefined;
    },
    /** A connected relay with one agent tab in group `g1`. */
    async withAgentTab(title = "Flights to Lisbon") {
      await h.welcome();
      h.take();
      const result = await h.call("tab.create", { group: "g1", title, url: "about:blank" });
      return result.value.tab.id as number;
    },
  };
  return h;
}

/** Whether two frames have the same keys at every level of objects (values may differ). */
export function shape(value: unknown): unknown {
  if (Array.isArray(value)) return value.length ? [shape(value[0])] : [];
  if (value && typeof value === "object") {
    return Object.fromEntries(Object.keys(value).sort().map((key) => [key, shape((value as any)[key])]));
  }
  return typeof value;
}
