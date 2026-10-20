// @vitest-environment jsdom
// The card "Your Chrome" in each of its states, and the panel's switch: what Daedalus sees of the
// operator's Chrome is said plainly (connected as which Chrome, or not and why), the connector and the
// extension steps appear only on this computer, pairing only on a server, and the first switch of a
// conversation to the operator's Chrome asks before it is sent.
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { BrowserUserStatus } from "../api";
import { setLang } from "../i18n";
import { prime } from "../store";
import { BrowserSwitch, chromeState, connectorState, DEFAULT_USER, notConnectedReason, USER_STATUS, YourChrome } from "./yourchrome";

const RELAY = { connected: true, profile_name: "Work", ext_version: "0.1.0", chrome_version: "141.0.7390.54", transport: "native", since: new Date().toISOString(), last_seen: new Date().toISOString() };

function status(extra: Partial<BrowserUserStatus> = {}): BrowserUserStatus {
  return {
    configured: true, enabled: true, daemon: true, reason: "", detail: "", relay: RELAY, native: true,
    launcher: { connector: { registered: true }, extension: { path: "/data/browser-extension", ready: true, version: "0.1.0" } },
    launcher_error: "", devices: [], server_url: "", consent: "The agent will work in your own Chrome.", ...extra,
  };
}

describe("the card's state", () => {
  it("reads the status and the connector", () => {
    expect(chromeState(undefined)).toBe("none");
    expect(chromeState(status({ configured: false }))).toBe("none");
    expect(chromeState(status({ daemon: false }))).toBe("down");
    expect(chromeState(status())).toBe("connected");
    expect(chromeState(status({ relay: { connected: false } }))).toBe("waiting");
    expect(connectorState(null)).toBe("unknown");
    expect(connectorState({ connector: { registered: true, restart_chrome: true } })).toBe("restart");
    expect(connectorState({ connector: { registered: false, stale: true } })).toBe("stale");
    expect(connectorState({ connector: { registered: false } })).toBe("absent");
    setLang("en");
    expect(notConnectedReason(status({ relay: { connected: false, last_error: "Chrome stopped answering" } }))).toBe("Chrome stopped answering");
    expect(notConnectedReason(status({ relay: null }))).toBe("The extension has not connected yet");
  });
});

describe("the card", () => {
  let host: HTMLDivElement;
  let root: Root;
  let fetched: { url: string; method: string; body: string }[];
  let current: BrowserUserStatus = status();
  beforeEach(() => {
    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    setLang("en");
    fetched = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      fetched.push({ url: String(url), method: init?.method ?? "GET", body: String(init?.body ?? "") });
      const reply = String(url).endsWith(USER_STATUS) ? current : String(url).endsWith("/check") ? { ok: true, rtt_ms: 14 } : String(url).endsWith("/pairing") ? { code: "ABCD-EFGH", expires_at: "2026-10-20T12:10:00Z", server_url: "https://daedalus.example" } : {};
      return new Response(JSON.stringify(reply), { status: 200, headers: { "Content-Type": "application/json" } });
    }));
    host = document.createElement("div");
    document.body.append(host);
    root = createRoot(host);
  });
  afterEach(async () => {
    await act(async () => root.unmount());
    host.remove();
    vi.unstubAllGlobals();
  });

  async function show(s: BrowserUserStatus): Promise<HTMLElement | null> {
    current = s;
    prime(USER_STATUS, s);
    await act(async () => root.render(<YourChrome user={DEFAULT_USER} setUser={() => undefined} toast={() => undefined} />));
    return host.querySelector<HTMLElement>(".bs-chrome");
  }

  it("is not there where the installation has no daemon for it", async () => {
    expect(await show(status({ configured: false }))).toBeNull();
  });

  it("shows the connected Chrome and checks the connection with its round trip", async () => {
    const card = (await show(status()))!;
    expect(card.dataset.state).toBe("connected");
    expect(card.textContent).toContain("Connected: Work");
    expect(card.textContent).toContain("141.0.7390.54");
    expect(card.textContent).toContain("Daedalus on this computer");
    const check = [...card.querySelectorAll("button")].find((b) => b.textContent === "Check connection")!;
    await act(async () => check.click());
    expect(fetched.some((f) => f.url.endsWith("/api/browser/user/check") && f.method === "POST")).toBe(true);
    expect(card.querySelector<HTMLElement>(".bs-chrome-rtt")?.textContent).toBe("Chrome answered in 14 ms");
  });

  it("says why no Chrome is connected, and on this computer how to install and register", async () => {
    const card = (await show(status({ relay: { connected: false, last_error: "Chrome closed the connection" }, launcher: { connector: { registered: false }, extension: { path: "/data/browser-extension", ready: true } } })))!;
    expect(card.dataset.state).toBe("waiting");
    expect(card.textContent).toContain("Not connected");
    expect(card.textContent).toContain("Chrome closed the connection");
    expect(card.querySelector<HTMLElement>("[data-step=connector]")?.dataset.connector).toBe("absent");
    expect(card.textContent).toContain("chrome://extensions");
    expect(card.textContent).toContain("/data/browser-extension");
    const register = [...card.querySelectorAll("button")].find((b) => b.textContent === "Register")!;
    await act(async () => register.click());
    expect(fetched.some((f) => f.url.endsWith("/api/browser/user/register") && f.method === "POST")).toBe(true);
    expect(card.textContent).not.toContain("Pair a browser");
  });

  it("on a server pairs a browser with a code and lists the paired ones", async () => {
    const device = { id: "d1", name: "Laptop", profile_id: "p", ext_version: "0.1.0", chrome_version: "141", paired_at: "2026-10-01T00:00:00Z", last_seen: "2026-10-20T00:00:00Z", connected: false };
    const card = (await show(status({ native: false, launcher: null, relay: { connected: false }, devices: [device], server_url: "https://daedalus.example" })))!;
    expect(card.querySelector("[data-step=connector]")).toBeNull();
    expect(card.querySelector("[data-device=d1]")?.textContent).toContain("Laptop");
    const pair = [...card.querySelectorAll("button")].find((b) => b.textContent === "Pair a browser")!;
    await act(async () => pair.click());
    expect(card.querySelector<HTMLElement>(".bs-chrome-code")?.dataset.code).toBe("ABCD-EFGH");
    expect(card.textContent).toContain("https://daedalus.example");
  });

  it("says when its daemon is down, in Russian too", async () => {
    setLang("ru");
    const card = (await show(status({ daemon: false, detail: "the browser service does not answer" })))!;
    expect(card.dataset.state).toBe("down");
    expect(card.textContent).toContain("Служба браузера для вашего Chrome не запущена");
    expect(card.textContent).toContain(status().consent);
  });
});

describe("the switch", () => {
  let host: HTMLDivElement;
  let root: Root;
  beforeEach(() => {
    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    setLang("en");
    host = document.createElement("div");
    document.body.append(host);
    root = createRoot(host);
  });
  afterEach(async () => {
    await act(async () => root.unmount());
    host.remove();
    vi.unstubAllGlobals();
  });

  it("names the operator's Chrome and sends the switch once the operator agreed", async () => {
    const sent: string[] = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      sent.push(`${init?.method} ${url} ${init?.body}`);
      return new Response("{}", { status: 200, headers: { "Content-Type": "application/json" } });
    }));
    await act(async () => root.render(<BrowserSwitch sessionId="s1" user={{ env: "agent", consented: true, connected: true, name: "Work" }} toast={() => undefined} />));
    const options = [...host.querySelectorAll("button")];
    expect(options.map((b) => b.textContent)).toEqual(["Agent's browser", "Your Chrome · Work"]);
    await act(async () => options[1].click());
    expect(sent).toEqual([`PUT /api/browser/user/sessions/s1 ${JSON.stringify({ env: "user", consent: false })}`]);
  });
});
