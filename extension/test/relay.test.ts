import { readFileSync } from "node:fs";
import { afterEach, describe, expect, it, vi } from "vitest";
import { CHECK_TIMEOUT_MS, HEARTBEAT_TIMEOUT_MS, OWN_INPUT_MS, PAUSE_RENEW_MS, REATTACH_MS } from "../src/relay.js";
import { PRIVATE_ADDRESS } from "../src/guard.js";
import { flush, harness, shape } from "./harness.js";

const golden = JSON.parse(readFileSync(new URL("./frames.json", import.meta.url), "utf8"));
const goldenFrame = (name: string) => golden.frames.find((f: any) => f.name === name).frame;

afterEach(() => {
  vi.useRealTimers();
});

describe("connecting", () => {
  it("says hello over native messaging with the golden frame's fields and no token", async () => {
    const h = await harness();
    expect(h.current().config).toEqual({ kind: "native" });
    const [hello] = h.take();
    expect(shape(hello)).toEqual(shape(goldenFrame("hello")));
    expect(hello).toMatchObject({ t: "hello", proto: 2, ext_version: "0.1.0", platform: "linux", profile_name: "Chrome" });
    expect(hello.profile_id).toMatch(/^[0-9a-f-]{36}$/);
    // The profile id is kept, so the same Chrome is the same Chrome after a restart.
    expect((await h.fake.chrome.storage.local.get("profileId")).profileId).toBe(hello.profile_id);
  });

  it("carries the device token in hello on the server transport", async () => {
    const h = await harness({ settings: { mode: "server", server: "https://daedalus.example.org", deviceToken: "tok" } });
    expect(h.current().config).toEqual({ kind: "server", relayUrl: "wss://daedalus.example.org/api/browser/relay" });
    const [hello] = h.take();
    expect(shape(hello)).toEqual(shape(goldenFrame("hello over the server transport")));
    expect(hello.token).toBe("tok");
  });

  it("is green once welcomed, and shows the installation", async () => {
    const h = await harness();
    expect(h.relay.status().phase).toBe("connecting");
    await h.welcome();
    const status = h.relay.status();
    expect(status).toMatchObject({ phase: "connected", installName: "Daedalus", daemonVersion: "0.19.2", installKind: "native" });
    expect(h.fake.badge.color).toBe("#1a7f37");
  });

  it("answers a ping and records the heartbeat", async () => {
    const h = await harness();
    await h.welcome();
    h.take();
    vi.advanceTimersByTime(15_000);
    await h.daemon(goldenFrame("ping"));
    expect(h.take()).toEqual([goldenFrame("pong")]);
    expect(h.relay.status().lastHeartbeat).toBe(Date.now());
  });

  it("calls a lost heartbeat lost and reconnects", async () => {
    const h = await harness();
    await h.welcome();
    vi.advanceTimersByTime(HEARTBEAT_TIMEOUT_MS + 5_000);
    await flush();
    expect(h.relay.status()).toMatchObject({ phase: "error", error: "lost" });
    expect(h.transports[0].closed).toBe(true);
    expect(h.fake.badge.color).toBe("#cf222e");
    vi.advanceTimersByTime(1_000);
    await flush();
    expect(h.transports).toHaveLength(2);
  });

  it.each([
    [3, "version_extension"],
    [1, "version_daemon"],
  ])("refuses a daemon speaking protocol %i: %s", async (proto, error) => {
    const h = await harness();
    await h.welcome(proto);
    expect(h.relay.status()).toMatchObject({ phase: "error", error });
  });

  it("names the other Chrome when the relay is busy, and waits a minute before trying again", async () => {
    const h = await harness();
    await h.daemon(goldenFrame("bye: another Chrome"));
    expect(h.relay.status()).toMatchObject({ phase: "error", error: "busy", errorName: "Chrome" });
    vi.advanceTimersByTime(59_000);
    await flush();
    expect(h.transports).toHaveLength(1);
    vi.advanceTimersByTime(1_000);
    await flush();
    expect(h.transports).toHaveLength(2);
  });

  it.each([
    ["bye: stopping", "shutdown"],
    ["bye: replaced", "replaced"],
    ["bye: no answer to pings", "lost"],
    ["bye: a broken frame", "lost"],
    ["bye from the bridge: no daemon", "not_running"],
    ["bye from the bridge: the daemon refused", "unavailable"],
    ["bye: versions differ", "version_daemon"],
  ])("explains %s in its own words", async (name, error) => {
    const h = await harness();
    await h.daemon(goldenFrame(name));
    expect(h.relay.status()).toMatchObject({ phase: "error", error });
    expect(h.transports[0].closed).toBe(true);
  });

  it("explains a missing registration and a stopped Daedalus apart", async () => {
    const h = await harness();
    h.current().handlers.onClose("not_registered");
    expect(h.relay.status().error).toBe("not_registered");
    vi.advanceTimersByTime(30_000);
    await flush();
    h.current().handlers.onClose("not_running");
    expect(h.relay.status().error).toBe("not_running");
  });

  it("stops retrying when the pairing has expired", async () => {
    const h = await harness({ settings: { mode: "server", server: "https://daedalus.example.org", deviceToken: "tok" } });
    await h.daemon({ t: "bye", reason: "unauthorized" });
    expect(h.relay.status().error).toBe("pairing_expired");
    vi.advanceTimersByTime(600_000);
    await flush();
    h.relay.wake();
    expect(h.transports).toHaveLength(1);
  });

  it("is grey and idle after Disconnect, and forgets the device token", async () => {
    const h = await harness({ settings: { mode: "server", server: "https://daedalus.example.org", deviceToken: "tok" } });
    await h.relay.disconnect();
    expect(h.relay.status()).toMatchObject({ phase: "idle", mode: "off" });
    expect((await h.fake.chrome.storage.local.get("deviceToken")).deviceToken).toBeUndefined();
    expect(h.fake.badge.color).toBe("#6e7781");
    expect(h.transports[0].closed).toBe(true);
  });

  it("pairs with a one-time code and connects to the server", async () => {
    const h = await harness({ settings: { mode: "off" } });
    const fetch = vi.fn(async () => new Response(JSON.stringify({ token: "device-token" }), { status: 200 }));
    vi.stubGlobal("fetch", fetch);
    expect(await h.relay.pair("daedalus.example.org", " 123456 ")).toEqual({ ok: true });
    const [url, init] = fetch.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("https://daedalus.example.org/api/browser/pair");
    expect(JSON.parse(String(init.body))).toMatchObject({ code: "123456", profile_name: "Chrome" });
    expect(h.current().config).toEqual({ kind: "server", relayUrl: "wss://daedalus.example.org/api/browser/relay" });
    expect(h.take()[0].token).toBe("device-token");
    vi.unstubAllGlobals();
  });

  it("refuses a code the server does not accept, and an address that is not HTTPS", async () => {
    const h = await harness({ settings: { mode: "off" } });
    vi.stubGlobal("fetch", vi.fn(async () => new Response("{}", { status: 403 })));
    expect(await h.relay.pair("daedalus.example.org", "1")).toEqual({ ok: false, error: "pair_failed" });
    expect(await h.relay.pair("http://daedalus.example.org", "1")).toEqual({ ok: false, error: "bad_server" });
    vi.unstubAllGlobals();
  });
});

describe("the connection check", () => {
  it("confirms when check_ok returns within 5 s", async () => {
    const h = await harness();
    await h.welcome();
    h.take();
    const outcome = h.relay.check();
    const [check] = h.take();
    expect(shape(check)).toEqual(shape(goldenFrame("check")));
    vi.advanceTimersByTime(40);
    await h.daemon({ t: "check_ok", n: check.n, host_seen: true });
    expect(await outcome).toEqual({ ok: true, ms: 40 });
  });

  it("times out after 5 s without an answer", async () => {
    const h = await harness();
    await h.welcome();
    const outcome = h.relay.check();
    vi.advanceTimersByTime(CHECK_TIMEOUT_MS);
    expect(await outcome).toEqual({ ok: false, reason: "timeout" });
  });

  it("does not pretend to check while disconnected", async () => {
    const h = await harness();
    expect(await h.relay.check()).toEqual({ ok: false, reason: "offline" });
  });

  it("flashes when the app runs its own check", async () => {
    const h = await harness();
    await h.welcome();
    const result = await h.call("notify", { text: "12 ms" }, 29);
    expect(result).toEqual(goldenFrame("a call's empty value"));
    expect(h.fake.badge).toMatchObject({ color: "#1f6feb", text: "✓" });
    expect(h.fake.runtimeMessages).toContainEqual({ kind: "notice", notice: { at: Date.now(), text: "12 ms" } });
    expect(h.relay.status().notice?.text).toBe("12 ms");
    vi.advanceTimersByTime(3_000);
    expect(h.fake.badge.color).toBe("#1a7f37");
  });
});

describe("the agent's tabs", () => {
  it("opens the agent's first tab blank, in its own unfocused window, in a titled group", async () => {
    const h = await harness();
    await h.welcome();
    const result = await h.call("tab.create", { group: "g1", title: "Flights to Lisbon", url: "https://example.org/" }, 21);
    const tabId = result.value.tab.id;
    expect(result).toEqual({ ...goldenFrame("tab.create's value"), value: { tab: { id: tabId, group: "g1", url: "about:blank" } } });
    const tab = h.fake.tabs.get(tabId)!;
    // The daemon navigates after it attached, so the first page is judged like every other.
    expect(tab.url).toBe("about:blank");
    expect(tab.windowId).not.toBe(h.fake.operatorWindow);
    expect(h.fake.windows.get(tab.windowId)!.focused).toBe(false);
    // The window's own blank tab is gone; only the agent's tab is left in it.
    expect([...h.fake.tabs.values()].filter((t) => t.windowId === tab.windowId).map((t) => t.id)).toEqual([tabId]);
    expect(h.fake.groups.get(tab.groupId)!.title).toBe("Daedalus · Flights to Lisbon");
    expect(h.fake.attached.has(tabId)).toBe(true);
    expect(h.fake.commands).toContainEqual({ target: { tabId }, method: "Emulation.setFocusEmulationEnabled", params: { enabled: true } });
    expect(h.fake.injected).toContain(tabId);
    expect(h.fake.dnrRules).toEqual([expect.objectContaining({ condition: { regexFilter: PRIVATE_ADDRESS, tabIds: [tabId] } })]);
    // Nothing the daemon itself did is reported back to it as the operator's doing.
    expect(h.take().filter((f) => f.t === "tabs")).toEqual([]);
    expect(h.fake.badge.text).toBe("1");
  });

  it("puts the agent's tab in the operator's window when asked to", async () => {
    const h = await harness({ settings: { windowMode: "current" } });
    const tabId = await h.withAgentTab();
    const tab = h.fake.tabs.get(tabId)!;
    expect(tab.windowId).toBe(h.fake.operatorWindow);
    expect(tab.active).toBe(false);
    expect((await h.call("window.bounds", { group: "g1", bounds: { width: 10 } })).error.code).toBe("refused");
  });

  it("sizes the agent's own window", async () => {
    const h = await harness();
    await h.withAgentTab();
    const result = await h.call("window.bounds", { group: "g1", bounds: { width: 1296, height: 939 } }, 25);
    expect(result).toEqual({ ...goldenFrame("window.bounds's value"), value: { bounds: { left: 0, top: 0, width: 1296, height: 939 } } });
    expect((await h.call("window.bounds", { group: "g1" })).value.bounds.width).toBe(1296);
  });

  it("names the group's window and the tabs already in it", async () => {
    const h = await harness();
    const tab = await h.withAgentTab();
    Object.assign(h.fake.tabs.get(tab)!, { url: "https://example.com/", title: "Example" });
    const result = await h.call("window.ensure", { group: "g1" }, 20);
    expect(shape(result)).toEqual(shape(goldenFrame("window.ensure's value")));
    expect(result.value).toEqual({ window: h.fake.tabs.get(tab)!.windowId, tabs: [{ id: tab, group: "g1", url: "https://example.com/", title: "Example" }] });
  });

  it("reuses the group's window and group for its next tab", async () => {
    const h = await harness();
    const first = await h.withAgentTab();
    const second = (await h.call("tab.create", { group: "g1" })).value.tab.id;
    expect(h.fake.tabs.get(second)!.windowId).toBe(h.fake.tabs.get(first)!.windowId);
    expect(h.fake.tabs.get(second)!.groupId).toBe(h.fake.tabs.get(first)!.groupId);
    expect(h.relay.status().groups).toEqual([{ id: "g1", title: "Flights to Lisbon", tabs: 2, paused: false }]);
  });

  it("takes a page the agent's tab opened into the group", async () => {
    const h = await harness();
    const opener = await h.withAgentTab();
    h.take();
    const popup = await h.fake.chrome.tabs.create({ windowId: h.fake.tabs.get(opener)!.windowId, openerTabId: opener });
    await flush();
    const frames = h.take().filter((f) => f.t === "tabs");
    expect(frames).toHaveLength(1);
    expect(shape(frames[0])).toEqual(shape(goldenFrame("tabs: a popup of an agent tab")));
    expect(frames[0]).toEqual({ t: "tabs", op: "created", tab: { id: popup.id, group: "g1", opener, url: "about:blank" } });
    expect(h.fake.tabs.get(popup.id)!.groupId).toBe(h.fake.tabs.get(opener)!.groupId);
    expect(h.fake.attached.has(popup.id)).toBe(true);
  });

  it("leaves alone a new tab page the operator opens beside an agent tab", async () => {
    const h = await harness();
    const opener = await h.withAgentTab();
    h.take();
    const page = await h.fake.chrome.tabs.create({ windowId: h.fake.tabs.get(opener)!.windowId, openerTabId: opener, url: "chrome://newtab/" });
    await flush();
    expect(h.take()).toEqual([]);
    expect(h.fake.attached.has(page.id)).toBe(false);
  });

  it("reports an agent tab's new address and title", async () => {
    const h = await harness();
    const tab = await h.withAgentTab();
    h.take();
    const fakeTab = h.fake.tabs.get(tab)!;
    Object.assign(fakeTab, { url: "https://example.com/next", title: "Next" });
    h.fake.events.tabsUpdated.emit(tab, { url: "https://example.com/next" }, { ...fakeTab });
    await flush();
    expect(h.take()).toEqual([{ ...goldenFrame("tabs: address or title changed"), tab: { id: tab, url: "https://example.com/next", title: "Next" } }]);
    const own = h.fake.addTab(h.fake.operatorWindow, "https://example.org/");
    h.fake.events.tabsUpdated.emit(own.id, { url: "https://example.org/x" }, { ...own });
    await flush();
    expect(h.take()).toEqual([]);
  });

  it("leaves alone a tab opened by one of the operator's own tabs", async () => {
    const h = await harness();
    await h.withAgentTab();
    const own = h.fake.addTab(h.fake.operatorWindow, "https://example.org/");
    h.take();
    await h.fake.chrome.tabs.create({ windowId: h.fake.operatorWindow, openerTabId: own.id });
    await flush();
    expect(h.take()).toEqual([]);
    expect(h.fake.attached.size).toBe(1);
  });

  it("joins a tab the operator drags into the group, and lets it go when dragged out", async () => {
    const h = await harness();
    const agent = await h.withAgentTab();
    const groupId = h.fake.tabs.get(agent)!.groupId;
    const own = h.fake.addTab(h.fake.operatorWindow, "https://example.org/", { title: "Example" });
    h.take();

    h.fake.setGroup(own.id, groupId);
    await flush();
    let frames = h.take();
    expect(frames).toEqual([{ t: "tabs", op: "joined", tab: { id: own.id, group: "g1", url: "https://example.org/", title: "Example" } }]);
    expect(shape(frames[0])).toEqual(shape(goldenFrame("tabs: the operator dragged a tab in")));
    expect(h.fake.attached.has(own.id)).toBe(true);

    h.fake.setGroup(own.id, -1);
    await flush();
    frames = h.take();
    expect(frames).toEqual([
      { t: "event", session: `x${own.id}`, method: "Inspector.detached", params: { reason: "left" } },
      { ...goldenFrame("tabs: the operator dragged a tab out"), tab: { id: own.id } },
    ]);
    expect(h.fake.attached.has(own.id)).toBe(false);
    expect(h.fake.tabMessages).toContainEqual({ tabId: own.id, message: { kind: "daedalus-border", visible: null } });
    expect(h.fake.dnrRules[0].condition.tabIds).toEqual([agent]);
  });

  it("gives a tab through the context menu, in the operator's language", async () => {
    const h = await harness({ language: "ru" });
    await h.withAgentTab();
    expect(h.fake.menus.map((m) => m.title)).toEqual(["Спросить Daedalus об этой странице", "Разрешить Daedalus эту вкладку", "Отдать: Flights to Lisbon"]);
    const own = h.fake.addTab(h.fake.operatorWindow, "https://example.org/");
    h.take();
    h.fake.events.menuClicked.emit({ menuItemId: "daedalus-grant:g1" }, { ...own });
    await flush();
    const frames = h.take().filter((f) => f.t === "tabs");
    expect(frames).toEqual([expect.objectContaining({ op: "joined", tab: expect.objectContaining({ id: own.id }) })]);
  });

  it("disables the context menu while no agent is working", async () => {
    const h = await harness();
    await h.welcome();
    await h.call("tab.create", { group: "g1" });
    await h.call("group.close", { group: "g1" });
    // "Ask Daedalus about this page" stays: it starts a conversation, which needs no agent at work.
    expect(h.fake.menus).toEqual([expect.objectContaining({ id: "daedalus-ask" }), expect.objectContaining({ id: "daedalus-grant", enabled: false })]);
  });

  it("reports a tab the operator closed, and the group when its last tab goes", async () => {
    const h = await harness();
    const agent = await h.withAgentTab();
    h.take();
    await h.fake.chrome.tabs.remove(agent);
    await flush();
    const frames = h.take();
    expect(frames).toEqual([
      { t: "event", session: `x${agent}`, method: "Inspector.detached", params: { reason: "target_closed" } },
      { ...goldenFrame("tabs: closed"), tab: { id: agent } },
      { ...goldenFrame("tabs: the operator closed the group"), group: "g1" },
    ]);
    expect(h.relay.status().groups).toEqual([]);
  });

  it("closes a tab and a group the daemon asks to close without reporting them back", async () => {
    const h = await harness();
    const first = await h.withAgentTab();
    const second = (await h.call("tab.create", { group: "g1" })).value.tab.id;
    h.take();
    expect((await h.call("tab.close", { tab: first })).ok).toBe(true);
    expect(h.fake.tabs.has(first)).toBe(false);
    expect((await h.call("group.close", { group: "g1" })).ok).toBe(true);
    expect(h.fake.tabs.has(second)).toBe(false);
    expect(h.take().filter((f) => f.t === "tabs")).toEqual([]);
  });

  it("refuses to close or activate a tab that is not the agent's", async () => {
    const h = await harness();
    await h.withAgentTab();
    const own = h.fake.addTab(h.fake.operatorWindow, "https://example.org/");
    expect((await h.call("tab.close", { tab: own.id })).error.code).toBe("no_tab");
    expect((await h.call("tab.activate", { tab: own.id })).error.code).toBe("no_tab");
    expect(h.fake.tabs.has(own.id)).toBe(true);
  });

  it("answers an unknown operation without doing anything", async () => {
    const h = await harness();
    await h.welcome();
    expect((await h.call("profile.clear", {})).error.code).toBe("failed");
  });
});

describe("the operator's controls", () => {
  it("pauses a group: its tabs are the operator's for as long as the pause lasts", async () => {
    const h = await harness();
    const tab = await h.withAgentTab();
    h.take();
    await h.relay.pause("g1", true);
    expect(h.take()).toEqual([{ t: "input", tab }]);
    vi.advanceTimersByTime(PAUSE_RENEW_MS);
    expect(h.take()).toEqual([{ t: "input", tab }]);
    const refused = await h.cdp(`x${tab}`, "Input.dispatchMouseEvent", { type: "mousePressed", x: 1, y: 1 });
    expect(refused.error).toEqual({ code: "refused", message: "the operator paused this group" });
    expect((await h.cdp(`x${tab}`, "Runtime.evaluate", { expression: "1" })).ok).toBe(true);

    await h.relay.pause("g1", false);
    h.take();
    vi.advanceTimersByTime(PAUSE_RENEW_MS * 3);
    expect(h.take()).toEqual([]);
    expect((await h.cdp(`x${tab}`, "Input.dispatchMouseEvent", { type: "mousePressed", x: 1, y: 1 })).ok).toBe(true);
  });

  it("stops and closes a group: the tabs close and the daemon hears it once", async () => {
    const h = await harness();
    const tab = await h.withAgentTab();
    h.take();
    await h.relay.stop("g1");
    await flush();
    expect(h.fake.tabs.has(tab)).toBe(false);
    const frames = h.take().filter((f) => f.t === "tabs");
    expect(frames).toEqual([{ ...goldenFrame("tabs: the operator closed the group"), group: "g1" }]);
  });

  it("reports the operator's own input, not the daemon's, once a second at most", async () => {
    const h = await harness();
    const tab = await h.withAgentTab();
    h.take();
    const sender = { tab: { id: tab } as any };
    h.relay.onContentInput(sender);
    expect(h.take()).toEqual([{ t: "input", tab }]);
    h.relay.onContentInput(sender);
    expect(h.take()).toEqual([]);

    vi.advanceTimersByTime(2_000);
    await h.cdp(`x${tab}`, "Input.dispatchKeyEvent", { type: "keyDown" });
    h.take();
    h.relay.onContentInput(sender);
    expect(h.take()).toEqual([]);
    vi.advanceTimersByTime(OWN_INPUT_MS);
    h.relay.onContentInput(sender);
    expect(h.take()).toEqual([{ t: "input", tab }]);

    h.relay.onContentInput({ tab: { id: 99999 } as any });
    expect(h.take()).toEqual([]);
  });
});

describe("the DevTools proxy", () => {
  it("runs an allowed command on an agent tab and returns its answer", async () => {
    const h = await harness();
    const tab = await h.withAgentTab();
    const golden = goldenFrame("result with a value");
    h.fake.onCommand((method) => (method === "Page.navigate" ? golden.value : {}));
    const result = await h.cdp(`x${tab}`, "Page.navigate", { url: "https://example.com/" }, golden.id);
    expect(result).toEqual(golden);
  });

  it.each([
    ["Network.getCookies", "refused"],
    ["Storage.getCookies", "refused"],
    ["Browser.getVersion", "refused"],
    ["Target.createTarget", "refused"],
  ])("refuses %s before Chrome ever sees it", async (method, code) => {
    const h = await harness();
    const tab = await h.withAgentTab();
    const before = h.fake.commands.length;
    const result = await h.cdp(`x${tab}`, method, {}, 43);
    expect(result).toMatchObject({ t: "result", id: 43, ok: false, error: { code } });
    expect(h.fake.commands.length).toBe(before);
  });

  it("refuses a tab outside the agent's groups and a session that names no tab", async () => {
    const h = await harness();
    await h.withAgentTab();
    const own = h.fake.addTab(h.fake.operatorWindow, "https://example.org/");
    expect((await h.cdp(`x${own.id}`, "Runtime.evaluate")).error.code).toBe("refused");
    expect((await h.cdp("", "Runtime.evaluate")).error.code).toBe("refused");
    expect((await h.cdp("BROWSER", "Page.enable")).error.code).toBe("refused");
  });

  it("follows flat child sessions: their events are named under the tab, and only known ones are addressed", async () => {
    const h = await harness();
    const tab = await h.withAgentTab();
    const setAuto = await h.cdp(`x${tab}`, "Target.setAutoAttach", { autoAttach: true, waitForDebuggerOnStart: false });
    expect(setAuto.ok).toBe(true);
    // Flat mode is forced: a nested session would never reach the extension.
    expect(h.fake.commands.at(-1)!.params).toMatchObject({ flatten: true });
    h.take();
    expect((await h.cdp(`x${tab}/CHILD1`, "DOM.getDocument")).error.code).toBe("refused");

    h.fake.events.debuggerEvent.emit({ tabId: tab }, "Target.attachedToTarget", { sessionId: "CHILD1", targetInfo: { type: "iframe" } });
    h.fake.events.debuggerEvent.emit({ tabId: tab, sessionId: "CHILD1" }, "Runtime.consoleAPICalled", { type: "log", args: [], executionContextId: 3, timestamp: 1 });
    await flush();
    const events = h.take();
    expect(events[1]).toEqual({
      t: "event",
      session: `x${tab}/CHILD1`,
      method: "Runtime.consoleAPICalled",
      params: { type: "log", args: [], executionContextId: 3, timestamp: 1 },
    });
    expect(shape(events[1])).toEqual(shape({ ...goldenFrame("event of a child session"), params: events[1].params }));
    expect((await h.cdp(`x${tab}/CHILD1`, "DOM.getDocument")).ok).toBe(true);
    expect(h.fake.commands.at(-1)!.target).toEqual({ tabId: tab, sessionId: "CHILD1" });
  });

  it("drops events from tabs that are not the agent's", async () => {
    const h = await harness();
    await h.withAgentTab();
    h.take();
    h.fake.events.debuggerEvent.emit({ tabId: 4242 }, "Page.loadEventFired", {});
    expect(h.take()).toEqual([]);
  });

  it("hides the border for a screenshot and shows it again", async () => {
    const h = await harness();
    const tab = await h.withAgentTab();
    await h.cdp(`x${tab}`, "Page.captureScreenshot", { format: "png" });
    const border = h.fake.tabMessages.filter((m) => m.tabId === tab).map((m) => m.message.visible);
    expect(border).toEqual([false, true]);
  });
});

describe("detach and reattach", () => {
  it("tells the daemon a detach in its own events and reattaches after a cooldown", async () => {
    const h = await harness();
    const tab = await h.withAgentTab();
    h.fake.events.debuggerEvent.emit({ tabId: tab }, "Target.attachedToTarget", { sessionId: "CHILD1" });
    await flush();
    h.take();

    h.fake.attached.delete(tab);
    h.fake.events.debuggerDetach.emit({ tabId: tab }, "canceled_by_user");
    await flush();
    const frames = h.take();
    expect(frames.slice(0, 2)).toEqual([
      { t: "event", session: `x${tab}`, method: "Target.detachedFromTarget", params: { sessionId: "CHILD1" } },
      { t: "event", session: `x${tab}`, method: "Inspector.detached", params: { reason: "canceled_by_user" } },
    ]);
    expect(frames).toHaveLength(2);
    expect((await h.cdp(`x${tab}`, "Runtime.evaluate")).error.code).toBe("detached");

    vi.advanceTimersByTime(REATTACH_MS[0] - 1);
    await flush();
    expect(h.fake.attached.has(tab)).toBe(false);
    vi.advanceTimersByTime(1);
    await flush();
    expect(h.fake.attached.has(tab)).toBe(true);
    // A fresh session is a tab joining again, for the daemon to adopt and enable its domains on.
    expect(h.take()).toEqual([{ t: "tabs", op: "joined", tab: { id: tab, group: "g1", url: "about:blank" } }]);
  });

  it("lets a tab go after the reattach attempts run out", async () => {
    const h = await harness();
    const tab = await h.withAgentTab();
    h.fake.failAttach("Another debugger is attached");
    h.fake.attached.delete(tab);
    h.fake.events.debuggerDetach.emit({ tabId: tab }, "canceled_by_user");
    await flush();
    for (const delay of REATTACH_MS) {
      vi.advanceTimersByTime(delay);
      await flush();
    }
    expect(h.take()).toContainEqual(expect.objectContaining({ t: "tabs", op: "left", tab: expect.objectContaining({ id: tab }) }));
    expect(h.relay.status().tabs).toBe(0);
  });

  it("does not reattach to a tab that closed", async () => {
    const h = await harness();
    const tab = await h.withAgentTab();
    h.fake.attached.delete(tab);
    h.fake.events.debuggerDetach.emit({ tabId: tab }, "target_closed");
    await flush();
    vi.advanceTimersByTime(REATTACH_MS[0]);
    await flush();
    expect(h.fake.attached.has(tab)).toBe(false);
  });
});

describe("a restart", () => {
  it("replays the tabs it still holds to a daemon it reconnects to", async () => {
    const h = await harness();
    const tab = await h.withAgentTab();
    h.current().handlers.onClose("not_running");
    vi.advanceTimersByTime(1_000);
    await flush();
    h.take();
    await h.welcome();
    expect(h.take()).toEqual([{ t: "tabs", op: "joined", tab: { id: tab, group: "g1", url: "about:blank" } }]);
  });
});

describe("browser-level calls", () => {
  it("gives cookie names and flags, never values", async () => {
    const h = await harness();
    await h.welcome();
    const result = await h.call("cookies.names", { url: "https://example.org/" }, 27);
    expect(shape(result)).toEqual(shape(goldenFrame("cookies.names's value")));
    expect(result.value.cookies).toEqual([
      { name: "sid", domain: "example.org", path: "/", expires: 1893456000, secure: true, http_only: true, same_site: "lax", session: false },
    ]);
    expect(JSON.stringify(result)).not.toContain("secret-value");
  });

  it("finds the download behind a DevTools guid an agent tab announced, and nothing else", async () => {
    const h = await harness();
    const tab = await h.withAgentTab();
    const guid = "c5d3f6a2-8b1e-4f0a-9d7c-3e2b1a0f9e8d";
    const item = { id: 12, url: "https://example.org/report.txt", filename: "C:\\Users\\someone\\Downloads\\report.txt", mime: "text/plain", state: "complete", bytesReceived: 21, totalBytes: 21, exists: true };
    h.fake.chrome.downloads.items.push(
      { ...item, id: 3, startTime: "2026-01-01T00:00:00Z" },
      { ...item, startTime: new Date(Date.now() + 500).toISOString() },
    );
    // Before the tab announced it, the guid names nothing; the operator's older download of the
    // same address is never the answer.
    expect((await h.call("downloads.search", { id: guid })).error.code).toBe("failed");
    h.fake.events.debuggerEvent.emit({ tabId: tab }, "Page.downloadWillBegin", { frameId: "F", guid, url: item.url, suggestedFilename: "report.txt" });
    await flush();
    expect(await h.call("downloads.search", { id: guid }, 26)).toEqual(goldenFrame("downloads.search's value"));
  });
});
