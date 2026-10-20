// @vitest-environment jsdom
// The side panel's route (/app/panel): its half of the bridge's handshake, what it decides on its
// own, the panel mode of the API client, and the screen itself against a stubbed shell and host —
// the conversation of the active tab, the approval card answered from the keyboard, and a new
// conversation that gives the tab before its first message.

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, panelPath, setPanelCredential } from "../api";
import { setLang } from "../i18n";
import { connectShell, EXTENSION_ORIGIN, HELLO, PANEL_PROTO, READY, type Current, type Shell } from "./bridge";
import { cardKey, cropRect, pageFile, pickSession, selectionQuote, stopUnconfirmed, type PanelSession } from "./model";
import { SidePanel } from "./SidePanel";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const fakeShell: { current: Shell | null } = { current: null };
vi.mock("./bridge", async (original) => {
  const real = await original<typeof import("./bridge")>();
  return { ...real, connectShell: vi.fn((...args: Parameters<typeof real.connectShell>) => (args.length ? real.connectShell(...args) : Promise.resolve(fakeShell.current!))) };
});

function session(id: string, over: Partial<PanelSession> = {}): PanelSession {
  return { id, title: id, status: "idle", last_message_at: "2026-10-20T10:00:00Z", needs_you: false, group: `s-${id}-u`, ...over };
}

describe("the frame's half of the handshake", () => {
  function fakeWindow() {
    const listeners = new Set<(e: MessageEvent) => void>();
    const parent = { posted: [] as { message: any; origin: string }[], postMessage(message: unknown, origin: string) { this.posted.push({ message, origin }); } };
    return {
      parent,
      addEventListener: (_: string, l: (e: MessageEvent) => void) => listeners.add(l),
      removeEventListener: (_: string, l: (e: MessageEvent) => void) => listeners.delete(l),
      deliver: (event: Partial<MessageEvent>) => { for (const l of [...listeners]) l(event as MessageEvent); },
    };
  }

  it("says READY to the extension's origin only, and takes HELLO only from its parent, from that origin, with its nonce", async () => {
    const win = fakeWindow();
    const connecting = connectShell(win as never, 1000);
    expect(win.parent.posted).toHaveLength(1);
    const [{ message, origin }] = win.parent.posted;
    expect(origin).toBe(EXTENSION_ORIGIN);
    expect(message).toMatchObject({ t: READY, proto: PANEL_PROTO });
    const channel = new MessageChannel();
    const hello = { t: HELLO, proto: PANEL_PROTO, nonce: message.nonce, token: "pnl_x", lang: "en", current: { tab: null, owner: null }, show: null, generation: 1 };
    win.deliver({ source: {} as never, origin: EXTENSION_ORIGIN, data: hello, ports: [channel.port2] as never });
    win.deliver({ source: win.parent as never, origin: "https://evil.example", data: hello, ports: [channel.port2] as never });
    win.deliver({ source: win.parent as never, origin: EXTENSION_ORIGIN, data: { ...hello, nonce: "guessed-guessed-guessed" }, ports: [channel.port2] as never });
    win.deliver({ source: win.parent as never, origin: EXTENSION_ORIGIN, data: hello, ports: [channel.port2] as never });
    const shell = await connecting;
    expect(shell.hello.token).toBe("pnl_x");
    // Requests go out on the port and come back by id.
    channel.port1.onmessage = (e) => channel.port1.postMessage({ id: e.data.id, ok: true, value: { op: e.data.op } });
    await expect(shell.request("tab.current")).resolves.toEqual({ op: "tab.current" });
    channel.port1.onmessage = (e) => channel.port1.postMessage({ id: e.data.id, ok: false, error: "gesture" });
    await expect(shell.request("capture.visible")).rejects.toThrow("gesture");
    channel.port1.close();
  });

  it("gives up when nobody answers: the page was opened outside the panel", async () => {
    vi.useFakeTimers();
    const connecting = connectShell(fakeWindow() as never, 50);
    vi.advanceTimersByTime(60);
    await expect(connecting).rejects.toThrow("no extension");
    vi.useRealTimers();
  });
});

describe("what the panel decides", () => {
  const sessions = [session("new"), session("old")];
  const on = (id: string): Current => ({ tab: { id: 1, url: "https://a.example/", title: "A", readable: true }, owner: { group: `s-${id}-u`, session: id, title: id, paused: false } });

  it("follows the active tab's conversation unless pinned, and a notification's click wins", () => {
    expect(pickSession({ current: on("old"), sessions, shown: "new", pinned: false })).toBe("old");
    expect(pickSession({ current: on("old"), sessions, shown: "new", pinned: true })).toBe("new");
    expect(pickSession({ show: "new", current: on("old"), sessions, shown: "old", pinned: false })).toBe("new");
    expect(pickSession({ current: { tab: null, owner: null }, sessions, shown: "old", pinned: false })).toBe("old");
    expect(pickSession({ current: on("gone"), sessions, shown: null, pinned: false })).toBe("new");
    expect(pickSession({ current: null, sessions: [], shown: null, pinned: false })).toBeNull();
  });

  it("reads the approval card's keys", () => {
    expect(cardKey({ key: "Enter" })).toBe("allow");
    expect(cardKey({ key: "Enter", ctrlKey: true })).toBe("similar");
    expect(cardKey({ key: "Enter", metaKey: true })).toBe("similar");
    expect(cardKey({ key: "Escape" })).toBe("deny");
    expect(cardKey({ key: "Enter", isComposing: true })).toBeNull();
    expect(cardKey({ key: "a" })).toBeNull();
  });

  it("calls a stop unconfirmed only after three seconds of the run going on", () => {
    expect(stopUnconfirmed(null, "running", 10_000)).toBe(false);
    expect(stopUnconfirmed(1_000, "running", 3_999)).toBe(false);
    expect(stopUnconfirmed(1_000, "running", 4_000)).toBe(true);
    expect(stopUnconfirmed(1_000, "idle", 9_000)).toBe(false);
  });

  it("turns a page into attachments", async () => {
    const file = pageFile({ url: "https://a.example/", title: "A", text: "body" });
    expect(file.name).toBe("page.txt");
    expect(await file.text()).toBe("A\nhttps://a.example/\n\nbody");
    expect(selectionQuote({ url: "https://a.example/", text: "one\ntwo" })).toBe("> one\n> two\n— https://a.example/\n\n");
    expect(cropRect({ x: 50, y: 40, w: -20, h: 10 }, { w: 200, h: 100 }, { w: 400, h: 200 })).toEqual({ x: 60, y: 80, w: 40, h: 20 });
  });
});

describe("the API client in the panel", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("sends the panel's credential to the panel's door, without cookies, and renews it once on a 401", async () => {
    let refreshed = 0;
    setPanelCredential("pnl_old", async () => {
      refreshed += 1;
      return "pnl_new";
    });
    expect(panelPath("/api/sessions/s1/messages")).toBe("/api/panel/sessions/s1/messages");
    expect(panelPath("/api/settings")).toBe("/api/panel/settings");
    expect(panelPath("/api/panel/sessions")).toBe("/api/panel/sessions");
    expect(panelPath("/api/terminals")).toBe("/api/terminals");
    expect(api.streamUrl("s1")).toBe("/api/panel/sessions/s1/stream");
    const seen: { url: string; auth: string; credentials?: string }[] = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string, init: RequestInit) => {
      const auth = (init.headers as Record<string, string>).Authorization;
      seen.push({ url, auth, credentials: init.credentials });
      return new Response(JSON.stringify({ ok: auth === "Panel pnl_new" }), { status: auth === "Panel pnl_new" ? 200 : 401 });
    }));
    await expect(api.post("/api/sessions/s1/stop")).resolves.toEqual({ ok: true });
    expect(refreshed).toBe(1);
    expect(seen).toEqual([
      { url: "/api/panel/sessions/s1/stop", auth: "Panel pnl_old", credentials: "omit" },
      { url: "/api/panel/sessions/s1/stop", auth: "Panel pnl_new", credentials: "omit" },
    ]);
  });
});

describe("the screen", { timeout: 20_000 }, () => {
  let host: HTMLDivElement;
  let root: Root;
  let posted: { url: string; body: unknown }[];
  let requests: { op: string; args: unknown }[];
  let listeners: Map<string, (value: unknown) => void>;
  let detail: Record<string, unknown>;
  let listing: Record<string, unknown>;

  const refusal = (key: string) => [
    { role: "user", text: "pay the bill", thinking: "", created_at: "2026-10-20T10:00:00Z", seq: 1, tool_calls: [], tool_results: [] },
    { role: "assistant", text: "", thinking: "", created_at: "2026-10-20T10:00:01Z", seq: 2, tool_calls: [{ id: "c1", name: "BrowserNavigate", arguments: { url: "https://bank.example/" } }], tool_results: [] },
    { role: "tool", text: "", thinking: "", created_at: "2026-10-20T10:00:02Z", seq: 3, tool_calls: [], tool_results: [{ id: "c1", content: `needs the operator's approval: a new site. Approval key: ${key}`, is_error: true }] },
  ];

  beforeEach(() => {
    setLang("en");
    vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
    posted = [];
    requests = [];
    listeners = new Map();
    listing = { sessions: [session("s1", { title: "Pay the bill" }), session("s2", { title: "Flights" })], consented: true, consent: "CONSENT", chrome: { connected: true, name: "Work" } };
    detail = { id: "s1", title: "Pay the bill", status: "idle", messages: refusal("0123456789ab"), model: "m", mode: "", workspace: "/w", folders: [], pending: null };
    fakeShell.current = {
      hello: { lang: "en", token: "pnl_x", expires: "", current: { tab: { id: 7, url: "https://bank.example/", title: "Bank", readable: true }, owner: { group: "s-s1-u", session: "s1", title: "Pay the bill", paused: false } }, show: null, profile: "Work", connected: true, generation: 1 },
      request: async (op: string, args: Record<string, unknown> = {}) => {
        requests.push({ op, args });
        return {} as never;
      },
      on: (event: string, listener: (value: unknown) => void) => {
        listeners.set(event, listener);
        return () => listeners.delete(event);
      },
    } as Shell;
    vi.stubGlobal("fetch", vi.fn(async (url: string, init: RequestInit = {}) => {
      if (init.method === "POST") posted.push({ url, body: init.body ? JSON.parse(String(init.body)) : null });
      if (url === "/api/panel/sessions" && init.method === "POST") return new Response(JSON.stringify({ id: "s9", group: "s-s9-u", title: "find a gift" }));
      if (url === "/api/panel/sessions") return new Response(JSON.stringify(listing));
      if (url.startsWith("/api/panel/sessions/s1?")) return new Response(JSON.stringify(detail));
      if (url.includes("/policy/similar/")) return new Response(JSON.stringify({ similar: null }));
      if (url.includes("/stream")) return new Response(new ReadableStream({ start() {} }));
      if (url === "/api/panel/modes") return new Response(JSON.stringify({}));
      return new Response(JSON.stringify({ ok: true, receipt: { status: "consumed" } }));
    }));
    host = document.createElement("div");
    document.body.appendChild(host);
    root = createRoot(host);
  });

  afterEach(() => {
    act(() => root.unmount());
    host.remove();
    vi.unstubAllGlobals();
  });

  async function settle() {
    for (let i = 0; i < 8; i++) await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
  }

  it("shows the active tab's conversation and answers its pending action from the keyboard", async () => {
    act(() => root.render(<SidePanel />));
    await settle();
    expect(host.querySelector(".sp-title")?.textContent).toContain("Pay the bill");
    const card = host.querySelector<HTMLElement>(".sp-card");
    expect(card).not.toBeNull();
    expect(card!.textContent).toContain("https://bank.example/");
    expect(document.activeElement).toBe(card);
    await act(async () => {
      card!.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
      await new Promise((r) => setTimeout(r, 200));
    });
    expect(posted).toContainEqual({ url: "/api/panel/sessions/s1/policy/grant", body: { key: "0123456789ab" } });
  });

  it("refuses with Esc", async () => {
    act(() => root.render(<SidePanel />));
    await settle();
    await act(async () => {
      host.querySelector(".sp-card")!.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
      await new Promise((r) => setTimeout(r, 200));
    });
    expect(posted).toContainEqual({ url: "/api/panel/sessions/s1/policy/refuse", body: { key: "0123456789ab" } });
  });

  it("follows the tab to another conversation", async () => {
    detail = { ...detail, id: "s2", title: "Flights", messages: [] };
    act(() => root.render(<SidePanel />));
    await settle();
    await act(async () => {
      listeners.get("current")!({ tab: { id: 8, url: "https://air.example/", title: "Air", readable: true }, owner: { group: "s-s2-u", session: "s2", title: "Flights", paused: false } });
    });
    await settle();
    expect(host.querySelector(".sp-title")?.textContent).toContain("Flights");
  });

  it("starts a new conversation that takes the tab before its first message", async () => {
    listing = { ...listing, sessions: [], consented: false };
    act(() => root.render(<SidePanel />));
    await settle();
    expect(host.querySelector(".sp-consent")?.textContent).toContain("CONSENT");
    const field = host.querySelector<HTMLTextAreaElement>(".sp-new-field")!;
    await act(async () => {
      const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")!.set!;
      setter.call(field, "find a gift");
      field.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await act(async () => {
      host.querySelector<HTMLButtonElement>(".sp-new-actions .primary")!.click();
    });
    await settle();
    expect(posted[0]).toEqual({ url: "/api/panel/sessions", body: { title: "find a gift", consent: true } });
    expect(requests).toContainEqual({ op: "tab.grant", args: { group: "s-s9-u", session: "s9", title: "find a gift" } });
    const order = posted.map((p) => p.url);
    expect(order.indexOf("/api/panel/sessions/s9/tab")).toBeGreaterThan(0);
    expect(order.indexOf("/api/panel/sessions/s9/messages")).toBeGreaterThan(order.indexOf("/api/panel/sessions/s9/tab"));
    const message = posted.find((p) => p.url === "/api/panel/sessions/s9/messages")!.body as { text: string };
    expect(message.text).toContain("find a gift");
    expect(message.text).toContain("https://bank.example/");
  });

  it("opens on a new conversation when the page's menu asked for one", async () => {
    fakeShell.current = { ...fakeShell.current!, hello: { ...fakeShell.current!.hello, show: "new" } };
    act(() => root.render(<SidePanel />));
    await settle();
    expect(host.querySelector(".sp-new-field")).not.toBeNull();
    expect(host.querySelector(".sp-new-tab")?.textContent).toContain("Bank");
  });
});
