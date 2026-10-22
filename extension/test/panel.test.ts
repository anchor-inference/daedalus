// The relay's side of the side panel: the credential the host hands over, the conversation that owns
// a tab, giving the operator's tab to a conversation, and the news that becomes a notification.

import { readFileSync } from "node:fs";
import { afterEach, describe, expect, it, vi } from "vitest";
import { captureVisible, pageText, selection } from "../src/capture.js";
import { harness } from "./harness.js";

const golden = JSON.parse(readFileSync(new URL("./frames.json", import.meta.url), "utf8"));
const goldenFrame = (name: string) => golden.frames.find((f: any) => f.name === name).frame;

afterEach(() => {
  vi.useRealTimers();
});

const SERVER = { mode: "server", server: "https://daedalus.example.org", deviceToken: "tok" };

describe("the panel's credential", () => {
  it("is kept in session storage when the host's address is the paired server", async () => {
    const h = await harness({ settings: SERVER });
    await h.welcome();
    const frame = goldenFrame("call panel.grant");
    await h.daemon(frame);
    expect(h.take().find((f) => f.t === "result" && f.id === frame.id)).toMatchObject({ ok: true });
    expect((await h.fake.chrome.storage.session.get("panelGrant")).panelGrant).toEqual(frame.args);
    expect(await h.fake.chrome.storage.local.get("panelGrant")).toEqual({});
    expect(await h.relay.grant()).toEqual(frame.args);
    expect(h.fake.runtimeMessages).toContainEqual({ kind: "panel-grant" });
  });

  it("frames the server this Chrome was paired with, whatever address the host gives", async () => {
    const h = await harness({ settings: SERVER });
    await h.welcome();
    expect(await h.call("panel.grant", { base: "https://evil.example", token: "t", expires: "" })).toMatchObject({ ok: true });
    expect((await h.relay.grant())?.base).toBe(SERVER.server);
  });

  it("natively names this computer only", async () => {
    const h = await harness();
    await h.welcome();
    expect(await h.call("panel.grant", { base: "https://daedalus.example.org", token: "t", expires: "" })).toMatchObject({ ok: false });
    expect(await h.call("panel.grant", { base: "http://127.0.0.1:48765", token: "t", expires: "" }, 901)).toMatchObject({ ok: true });
    expect((await h.relay.grant())?.base).toBe("http://127.0.0.1:48765");
  });

  it("lapses at its expiry and is forgotten when the operator disconnects or changes installation", async () => {
    const h = await harness({ settings: SERVER });
    await h.welcome();
    await h.call("panel.grant", { base: SERVER.server, token: "t", expires: "2026-10-20T13:00:00Z" });
    expect(await h.relay.grant()).not.toBeNull();
    vi.setSystemTime(new Date("2026-10-20T13:00:01Z"));
    expect(await h.relay.grant()).toBeNull();
    await h.call("panel.grant", { base: SERVER.server, token: "t2", expires: "" }, 902);
    expect((await h.relay.grant())?.token).toBe("t2");
    await h.relay.setProfileName("Work");
    expect((await h.relay.grant())?.token).toBe("t2");
    await h.relay.disconnect();
    expect(await h.relay.grant()).toBeNull();
  });
});

describe("the conversation that owns a tab", () => {
  it("is the session window.ensure and tab.create named", async () => {
    const h = await harness();
    await h.welcome();
    await h.call("window.ensure", { group: "s-42", session: "42", title: "Flights to Lisbon" });
    const created = await h.call("tab.create", { group: "s-42", url: "about:blank" }, 901);
    const tab = created.value.tab.id;
    expect(h.relay.tabOwner(tab)).toEqual({ group: "s-42", session: "42", title: "Flights to Lisbon", paused: false });
    expect(h.relay.sessionTabs("42")).toEqual([tab]);
    expect(h.relay.tabOwner(h.fake.addTab(h.fake.operatorWindow, "https://mail.example/").id)).toBeNull();
    // The Chrome tab group says whose it is.
    expect([...h.fake.groups.values()][0].title).toBe("Daedalus · Flights to Lisbon");
  });
});

describe("giving the operator's tab to a conversation", () => {
  it("makes the group in the tab's window, tells the daemon, and window.ensure then finds the tab without a window of its own", async () => {
    const h = await harness();
    await h.welcome();
    h.take();
    const page = h.fake.addTab(h.fake.operatorWindow, "https://shop.example/cart", { active: true, title: "Cart" });
    const given = await h.relay.adoptTab(page.id, "s-7-u", "7", "Compare prices");
    expect(given).toMatchObject({ id: page.id, group: "s-7-u", url: "https://shop.example/cart" });
    expect(h.fake.attached.has(page.id)).toBe(true);
    expect(h.take()).toContainEqual({ t: "tabs", op: "joined", tab: { id: page.id, group: "s-7-u", url: "https://shop.example/cart", title: "Cart", given: true } });
    expect(h.relay.tabOwner(page.id)?.session).toBe("7");
    const windows = h.fake.windows.size;
    const ensured = await h.call("window.ensure", { group: "s-7-u", session: "7" });
    expect(ensured.value).toEqual({ window: h.fake.operatorWindow, tabs: [{ id: page.id, group: "s-7-u", url: "https://shop.example/cart", title: "Cart", given: true }] });
    expect(h.fake.windows.size).toBe(windows);
  });

  it("refuses a browser page and a tab another conversation already holds", async () => {
    const h = await harness();
    await h.welcome();
    const settings = h.fake.addTab(h.fake.operatorWindow, "chrome://settings/");
    await expect(h.relay.adoptTab(settings.id, "s-7-u", "7", "")).rejects.toThrow(/browser page/);
    const page = h.fake.addTab(h.fake.operatorWindow, "https://shop.example/");
    await h.relay.adoptTab(page.id, "s-7-u", "7", "");
    await expect(h.relay.adoptTab(page.id, "s-8-u", "8", "")).rejects.toThrow(/another conversation/);
  });

  it("focuses only the agent's own tabs", async () => {
    const h = await harness();
    const tab = await h.withAgentTab();
    await h.relay.focusTab(tab);
    expect(h.fake.tabs.get(tab)?.active).toBe(true);
    const other = h.fake.addTab(h.fake.operatorWindow, "https://mail.example/");
    await expect(h.relay.focusTab(other.id)).rejects.toThrow();
  });
});

describe("a conversation's news", () => {
  it("is a notification naming the conversation's own tab, and the badge counts who waits", async () => {
    const h = await harness();
    await h.welcome();
    await h.call("window.ensure", { group: "s-42", session: "42", title: "Flights" });
    const tab = (await h.call("tab.create", { group: "s-42", url: "about:blank" }, 901)).value.tab.id;
    await h.call("panel.event", { session: "42", kind: "needs_you", title: "Flights", waiting: 2, tab }, 902);
    expect(h.fake.notifications).toEqual([
      { id: `daedalus-panel:42:${tab}`, options: expect.objectContaining({ title: "Flights", message: "Waiting for you in your Chrome", iconUrl: expect.stringContaining("icon-128.png") }) },
    ]);
    expect(h.fake.badge).toMatchObject({ text: "2", color: "#bf8700" });
    expect(h.relay.status().waiting).toBe(2);
    await h.call("panel.event", { session: "42", kind: "done", title: "Flights", waiting: 0 }, 903);
    expect(h.fake.badge.color).toBe("#1a7f37");
  });

  it("never names a tab of another conversation", async () => {
    const h = await harness();
    const foreign = await h.withAgentTab();
    await h.call("panel.event", { session: "99", kind: "done", title: "", waiting: 0, tab: foreign }, 904);
    expect(h.fake.notifications[0].id).toBe("daedalus-panel:99:");
  });

  it("is not a notification while the operator looks at the panel of the window they are in", async () => {
    const h = await harness();
    await h.welcome();
    h.relay.openPanels.add(h.fake.operatorWindow);
    await h.call("panel.event", { session: "42", kind: "done", title: "Flights", waiting: 0 });
    expect(h.fake.notifications).toEqual([]);
    expect(h.fake.runtimeMessages).toContainEqual({ kind: "panel-event", event: { session: "42", kind: "done", title: "Flights", waiting: 0 } });
  });
});

describe("what the panel attaches", () => {
  it("reads the active tab of the panel's window, and never a browser page", async () => {
    const h = await harness();
    const page = h.fake.addTab(h.fake.operatorWindow, "https://news.example/a", { active: true, title: "A" });
    h.fake.onPageScript((tabId, func) => (String(func).includes("getSelection") ? `picked in ${tabId}` : "the article"));
    expect(await captureVisible(h.fake.operatorWindow)).toEqual({ tab: page.id, url: "https://news.example/a", title: "A", image: "data:image/png;base64,AAAA" });
    expect(await pageText(h.fake.operatorWindow)).toMatchObject({ text: "the article" });
    expect(await selection(h.fake.operatorWindow)).toMatchObject({ text: `picked in ${page.id}` });
    h.fake.tabs.get(page.id)!.active = false;
    h.fake.addTab(h.fake.operatorWindow, "chrome://settings/", { active: true });
    await expect(captureVisible(h.fake.operatorWindow)).rejects.toThrow("unreadable");
  });
});
