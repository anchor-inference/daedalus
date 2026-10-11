// @vitest-environment jsdom
// The custom page bridge on both sides: the app's answers and the frame check (pagebridge.ts), and
// the page's client (kit/daedalus.js) driven against a stand-in parent, so the protocol both speak is
// proved without a browser.

import { afterEach, describe, expect, it, vi } from "vitest";
import { answer, appPath, isRequest, PROTOCOL, serveBridge, themeSnapshot, TOKEN_NAMES, type BridgeDeps } from "./pagebridge";
import kitSource from "../kit/daedalus.js?raw";

function deps(): BridgeDeps & { calls: string[] } {
  const calls: string[] = [];
  return {
    calls,
    theme: () => ({ tokens: { "--bg": "#000" }, scheme: "dark", theme: "claude", lang: "en", phone: false }),
    toast: (text) => calls.push(`toast:${text}`),
    navigate: (path) => calls.push(`navigate:${path}`),
    openFile: (id, path) => calls.push(`file:${id}:${path}`),
  };
}

describe("the app's side", () => {
  it("reads every token the stylesheet declares", () => {
    for (const name of ["--bg", "--fg", "--accent", "--fs-13", "--radius-md", "--row-h", "--space-4", "--font"]) expect(TOKEN_NAMES).toContain(name);
  });

  it("takes the theme from the computed style, inline values included", () => {
    const root = document.documentElement;
    root.style.setProperty("--bg", "#123456");
    root.dataset.scheme = "light";
    root.dataset.theme = "serebro";
    const snap = themeSnapshot(root, true, "ru");
    expect(snap.tokens["--bg"]).toBe("#123456");
    expect(snap).toMatchObject({ scheme: "light", theme: "serebro", lang: "ru", phone: true });
  });

  it("answers the requests a page may make and refuses the rest", () => {
    const d = deps();
    const req = (method: string, params: Record<string, unknown> = {}) => ({ daedalus: PROTOCOL, id: 1, method, params });
    expect(answer(req("ready"), d).result).toMatchObject({ protocol: PROTOCOL, theme: { theme: "claude" } });
    expect(answer(req("toast", { text: " saved " }), d)).toEqual({ result: true });
    expect(answer(req("toast", { text: "" }), d).error).toBeTruthy();
    expect(answer(req("navigate", { path: "/agents/abc" }), d)).toEqual({ result: true });
    expect(answer(req("navigate", { path: "https://elsewhere.example" }), d).error).toBeTruthy();
    expect(answer(req("openFile", { sessionId: "s1", path: "out/report.md" }), d)).toEqual({ result: true });
    expect(answer(req("openFile", { sessionId: "s1" }), d).error).toBeTruthy();
    expect(answer(req("rm -rf"), d).error).toMatch(/unknown method/);
    expect(d.calls).toEqual(["toast:saved", "navigate:/app/agents/abc", "file:s1:out/report.md"]);
  });

  it("keeps navigation inside the app", () => {
    expect(appPath("/app/settings")).toBe("/app/settings");
    expect(appPath("/inbox")).toBe("/app/inbox");
    expect(appPath("//evil.example/x")).toBeNull();
    expect(appPath("javascript:alert(1)")).toBeNull();
    expect(appPath(42)).toBeNull();
    expect(isRequest({ daedalus: PROTOCOL, id: 2, method: "toast" })).toBe(true);
    expect(isRequest({ daedalus: 99, id: 2, method: "toast" })).toBe(false);
  });

  it("listens only to its own frame, and stops listening when told", () => {
    const frame = document.createElement("iframe");
    document.body.appendChild(frame);
    const d = deps();
    const posted: unknown[] = [];
    const contentWindow = frame.contentWindow!;
    const spy = vi.spyOn(contentWindow, "postMessage").mockImplementation((message: unknown) => {
      posted.push(message);
    });
    const bridge = serveBridge(frame, d);
    const request = { daedalus: PROTOCOL, id: 7, method: "toast", params: { text: "hi" } };
    // Another window of the same origin is not the page.
    window.dispatchEvent(new MessageEvent("message", { data: request, source: window, origin: window.location.origin }));
    expect(d.calls).toEqual([]);
    window.dispatchEvent(new MessageEvent("message", { data: request, source: contentWindow, origin: window.location.origin }));
    expect(d.calls).toEqual(["toast:hi"]);
    expect(posted).toContainEqual({ daedalus: PROTOCOL, id: 7, result: true });
    // A change of look reaches the page without being asked for.
    window.dispatchEvent(new Event("daedalus-appearance"));
    expect(posted.some((m) => (m as { event?: string }).event === "theme")).toBe(true);
    bridge.stop();
    window.dispatchEvent(new MessageEvent("message", { data: request, source: contentWindow, origin: window.location.origin }));
    expect(d.calls).toEqual(["toast:hi"]);
    spy.mockRestore();
    frame.remove();
  });
});

// -- the page's side --------------------------------------------------------------------------------

type Listener = (e: { data: unknown; source: unknown; origin: string }) => void;

/** kit/daedalus.js run in a window of its own, framed by `parent`. */
function loadKit(parent: { postMessage: (m: unknown, origin: string) => void } | null) {
  const listeners: Listener[] = [];
  const page: Record<string, unknown> = {
    location: { origin: "https://daedalus.example", pathname: "/pages/demo/", href: "" },
    addEventListener: (type: string, fn: Listener) => {
      if (type === "message") listeners.push(fn);
    },
  };
  page.parent = parent ?? page;
  new Function("window", "document", kitSource)(page, document);
  const deliver = (data: unknown, source: unknown = parent, origin = "https://daedalus.example") => listeners.forEach((fn) => fn({ data, source, origin }));
  return { page, daedalus: page.Daedalus as any, deliver };
}

afterEach(() => {
  vi.unstubAllGlobals();
  document.documentElement.removeAttribute("style");
});

describe("the page's side", () => {
  it("announces itself, applies the theme it is sent, and ignores strangers", async () => {
    const sent: any[] = [];
    const { daedalus, deliver } = loadKit({ postMessage: (m) => sent.push(m) });
    expect(daedalus.version).toMatch(/^\d+\.\d+\.\d+$/);
    expect(sent[0]).toMatchObject({ daedalus: PROTOCOL, method: "ready" });
    const theme = { tokens: { "--accent": "#ff0000", color: "red" }, scheme: "light", theme: "zakat", lang: "ru", phone: true };
    // An answer from anyone but the parent is not an answer.
    deliver({ daedalus: PROTOCOL, id: sent[0].id, result: { theme: { ...theme, scheme: "dark" } } }, {}, "https://daedalus.example");
    deliver({ daedalus: PROTOCOL, id: sent[0].id, result: { theme } });
    const context = await daedalus.ready();
    expect(context).toMatchObject({ scheme: "light", theme: "zakat", lang: "ru", phone: true, slug: "demo", embedded: true });
    expect(document.documentElement.style.getPropertyValue("--accent")).toBe("#ff0000");
    expect(document.documentElement.style.getPropertyValue("color")).toBe("");
    expect(document.documentElement.dataset.scheme).toBe("light");
    const heard: unknown[] = [];
    const off = daedalus.on("agent.message", (data: unknown) => heard.push(data));
    deliver({ daedalus: PROTOCOL, event: "agent.message", data: { text: "done" } });
    off();
    deliver({ daedalus: PROTOCOL, event: "agent.message", data: { text: "again" } });
    expect(heard).toEqual([{ text: "done" }]);
  });

  it("asks the app for a toast and resolves with its answer", async () => {
    const sent: any[] = [];
    const { daedalus, deliver } = loadKit({ postMessage: (m) => sent.push(m) });
    const done = daedalus.ui.toast("saved");
    const request = sent.find((m) => m.method === "toast");
    expect(request.params).toEqual({ text: "saved" });
    deliver({ daedalus: PROTOCOL, id: request.id, result: true });
    await expect(done).resolves.toBe(true);
    const refused = daedalus.ui.navigate("https://elsewhere.example");
    const nav = sent.find((m) => m.method === "navigate");
    deliver({ daedalus: PROTOCOL, id: nav.id, error: "navigate takes a path inside the app" });
    await expect(refused).rejects.toThrow(/inside the app/);
  });

  it("keeps the page's storage through the API with the page's own slug", async () => {
    const calls: { url: string; init: RequestInit }[] = [];
    vi.stubGlobal("fetch", async (url: string, init: RequestInit) => {
      calls.push({ url, init });
      if (url.endsWith("/missing")) return new Response(JSON.stringify({ detail: "nothing" }), { status: 404, headers: { "Content-Type": "application/json" } });
      if (init.method === "PUT") return new Response(JSON.stringify({ key: "notes/a", updated_at: "now" }), { headers: { "Content-Type": "application/json" } });
      if (url.includes("?prefix=")) return new Response(JSON.stringify({ keys: [{ key: "notes/a" }] }), { headers: { "Content-Type": "application/json" } });
      return new Response(JSON.stringify({ key: "notes/a", value: { n: 1 } }), { headers: { "Content-Type": "application/json" } });
    });
    const { daedalus } = loadKit(null);
    await daedalus.storage.set("notes/a", { n: 1 });
    expect(calls[0].url).toBe("/api/pages/demo/storage/notes/a");
    expect(calls[0].init.method).toBe("PUT");
    expect(JSON.parse(String(calls[0].init.body))).toEqual({ value: { n: 1 } });
    expect(calls[0].init.credentials).toBe("same-origin");
    await expect(daedalus.storage.get("notes/a")).resolves.toEqual({ n: 1 });
    await expect(daedalus.storage.get("missing")).resolves.toBeNull();
    await expect(daedalus.storage.list("notes/")).resolves.toEqual([{ key: "notes/a" }]);
    expect(calls.at(-1)!.url).toBe("/api/pages/demo/storage?prefix=notes%2F");
    await daedalus.api("sessions");
    expect(calls.at(-1)!.url).toBe("/api/sessions");
  });

  it("works outside the app too", async () => {
    const { daedalus } = loadKit(null);
    await expect(daedalus.ready()).resolves.toMatchObject({ embedded: false });
    const info = vi.spyOn(console, "info").mockImplementation(() => undefined);
    await expect(daedalus.ui.toast("hello")).resolves.toBe(false);
    info.mockRestore();
  });
});
