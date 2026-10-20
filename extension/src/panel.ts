// The side panel: one per window, following that window's active tab. It is a thin privileged shell
// around the host's own app, framed at `/app/panel`: the frame draws the conversations that use this
// Chrome, the transcript, the composer and the approvals; this page draws a strip with the
// connection's state and its settings (pairing, the name, the window mode), and lends the frame what
// only an extension can do through the bridge (`bridge.ts`).
//
// The app is framed rather than bundled here so that it always speaks the API of the server it talks
// to: an extension on a laptop is updated on its own schedule, and a copy of the app inside it would
// break on the server's next change. The frame is cookie-less by design; it signs in with the panel's
// own credential, which reaches it in the bridge's handshake and never in its address.

import { ShellBridge, type Op } from "./bridge.js";
import { captureVisible, pageText, readable, selection } from "./capture.js";
import { language, t } from "./i18n.js";
import type { PanelGrant } from "./protocol.js";
import type { Status, TabOwner } from "./relay.js";
import { ask, refreshSettings, renderSettings, translateStatic, wireSettings } from "./settings.js";

const $ = <T extends HTMLElement = HTMLElement>(id: string) => document.getElementById(id) as T;

/** How long the frame has to say READY before the panel offers to try again. */
const READY_MS = 15_000;

type Current = { tab: { id: number; url: string; title: string; readable: boolean } | null; owner: TabOwner | null };

let windowId = -1;
let grant: PanelGrant | null = null;
let status: Status | null = null;
let bridge: ShellBridge | null = null;
let current: Current = { tab: null, owner: null };
let readyTimer: ReturnType<typeof setTimeout> | undefined;
let showingSettings = false;

const frame = () => $<HTMLIFrameElement>("frame");

/** The operator's own click or key in the panel a moment ago. User activation spreads from the frame
 *  to this page, so a click on "attach" inside the app counts, and a script cannot fake one. */
function gesture(): boolean {
  const activation = (navigator as Navigator & { userActivation?: { isActive: boolean } }).userActivation;
  return !!activation?.isActive;
}

async function currentTab(): Promise<Current> {
  const [tab] = await chrome.tabs.query({ active: true, windowId });
  if (!tab || tab.id === undefined) return { tab: null, owner: null };
  const owner = await ask<TabOwner | null>({ kind: "panel-tab", tab: tab.id });
  const open = readable(tab.url);
  // A browser page's address says nothing the conversation needs, and it is not the operator's site.
  return { tab: { id: tab.id, url: open ? tab.url ?? "" : "", title: open ? tab.title ?? "" : "", readable: open }, owner };
}

async function followTab(): Promise<void> {
  const next = await currentTab();
  if (JSON.stringify(next) === JSON.stringify(current)) return;
  current = next;
  bridge?.emit("current", current);
}

/** A worker answer, or the error it answered with as a thrown one. */
async function must<T>(message: Record<string, unknown>): Promise<T> {
  const answer = await ask<T | { error: string }>(message);
  if (answer && typeof answer === "object" && "error" in answer && Object.keys(answer).length === 1) throw new Error(String(answer.error));
  return answer as T;
}

function requireString(args: Record<string, unknown>, key: string): string {
  const value = args[key];
  if (typeof value !== "string" || !value || value.length > 200) throw new Error(`bad_${key}`);
  return value;
}

async function handle(op: Op, args: Record<string, unknown>): Promise<unknown> {
  switch (op) {
    case "tab.current":
      current = await currentTab();
      return current;
    case "capture.visible":
      return captureVisible(windowId);
    case "page.text":
      return pageText(windowId);
    case "page.selection":
      return selection(windowId);
    case "tab.grant": {
      // Always the active tab of this window, whatever the frame would like: the operator is looking
      // at the tab they give.
      const [tab] = await chrome.tabs.query({ active: true, windowId });
      if (!tab || tab.id === undefined || !readable(tab.url)) throw new Error("unreadable");
      const given = await must<unknown>({
        kind: "panel-adopt",
        tab: tab.id,
        group: requireString(args, "group"),
        session: requireString(args, "session"),
        title: typeof args.title === "string" ? args.title.slice(0, 80) : "",
      });
      await followTab();
      return given;
    }
    case "tab.focus": {
      const tabs = await ask<number[]>({ kind: "panel-tabs", session: requireString(args, "session") });
      // The agent's newest tab of that conversation, unless the frame named one of its others.
      const wanted = typeof args.tab === "number" && tabs.includes(args.tab) ? args.tab : tabs[tabs.length - 1];
      if (wanted === undefined) throw new Error("no_tab");
      await must({ kind: "panel-focus", tab: wanted });
      return { tab: wanted };
    }
    case "group.pause": {
      const session = requireString(args, "session");
      const tabs = await ask<number[]>({ kind: "panel-tabs", session });
      const owner = tabs.length ? await ask<TabOwner | null>({ kind: "panel-tab", tab: tabs[0] }) : null;
      if (!owner) throw new Error("no_group");
      await ask({ kind: "pause", group: owner.group, paused: args.paused === true });
      return { group: owner.group, paused: args.paused === true };
    }
    case "open.app": {
      const path = typeof args.path === "string" ? args.path : "/app/";
      if (!grant || !/^\/app\/[A-Za-z0-9/_\-.?=&%]*$/.test(path) || path.includes("//")) throw new Error("bad_path");
      await chrome.tabs.create({ url: grant.base + path });
      return {};
    }
    case "token.refresh":
      grant = (await ask<{ grant: PanelGrant | null }>({ kind: "panel-state" })).grant;
      return grant ? { token: grant.token, expires: grant.expires } : null;
  }
}

async function hello(): Promise<Record<string, unknown>> {
  current = await currentTab();
  const show = await ask<string | null>({ kind: "panel-show" });
  return {
    lang: language(),
    token: grant?.token ?? "",
    expires: grant?.expires ?? "",
    current,
    show,
    profile: status?.profileName ?? "",
    connected: status?.phase === "connected",
  };
}

function mountFrame(): void {
  if (!grant) return;
  bridge?.close();
  const origin = grant.base;
  bridge = new ShellBridge({ frame: () => frame().contentWindow, origin, hello, handle, gesture });
  $("frame-error").hidden = true;
  frame().hidden = false;
  // Nothing in the address but the page: the credential travels in the handshake only.
  frame().src = `${origin}/app/panel`;
  clearTimeout(readyTimer);
  readyTimer = setTimeout(() => {
    $("frame-error").hidden = false;
    frame().hidden = true;
  }, READY_MS);
}

function show(): void {
  const framed = !!grant && !showingSettings;
  $("stage").hidden = !framed;
  $("settings").hidden = framed;
  $("lead").hidden = !!grant;
  $("toggle-settings").hidden = !grant;
  $("open-app").hidden = !grant;
  $("toggle-settings").setAttribute("aria-pressed", String(showingSettings));
}

async function load(): Promise<void> {
  const state = await ask<{ status: Status; grant: PanelGrant | null }>({ kind: "panel-state" });
  status = state.status;
  renderStrip(state.status);
  const changed = (state.grant?.base ?? "") !== (grant?.base ?? "");
  grant = state.grant;
  if (changed) {
    if (grant) mountFrame();
    else {
      bridge?.close();
      bridge = null;
      frame().removeAttribute("src");
    }
  } else if (grant) {
    bridge?.emit("token", { token: grant.token, expires: grant.expires });
  }
  show();
}

function renderStrip(next: Status): void {
  $("phase").dataset.phase = next.phase;
  $("phase").title = t(`phase.${next.phase}`);
}

function wire(): void {
  window.addEventListener("message", (event) => {
    if (!bridge) return;
    void bridge.onWindowMessage(event).then((taken) => {
      if (taken) clearTimeout(readyTimer);
    });
  });
  $("toggle-settings").onclick = () => {
    showingSettings = !showingSettings;
    if (showingSettings) void refreshSettings();
    show();
  };
  $("open-app").onclick = () => {
    if (grant) void chrome.tabs.create({ url: `${grant.base}/app/` });
  };
  $("retry").onclick = () => mountFrame();
  chrome.tabs.onActivated.addListener((info) => {
    if (info.windowId === windowId) void followTab();
  });
  chrome.tabs.onUpdated.addListener((tabId, info) => {
    if (tabId === current.tab?.id && (info.url !== undefined || info.title !== undefined || info.groupId !== undefined)) void followTab();
  });
  chrome.runtime.onMessage.addListener((message) => {
    if (message?.kind === "status") {
      void ask<Status>({ kind: "get-status" }).then((next) => {
        status = next;
        renderStrip(next);
        if (!$("settings").hidden) renderSettings(next);
      });
      void followTab();
    }
    if (message?.kind === "panel-grant") void load();
    if (message?.kind === "panel-show" && typeof message.session === "string") {
      showingSettings = false;
      show();
      bridge?.emit("show", message.session);
    }
    if (message?.kind === "panel-event") bridge?.emit("event", message.event);
  });
  setInterval(() => {
    if (!$("settings").hidden) void refreshSettings();
  }, 1000);
}

async function start(): Promise<void> {
  translateStatic();
  wireSettings();
  wire();
  const window = await chrome.windows.getCurrent();
  windowId = window.id ?? -1;
  chrome.runtime.connect({ name: "panel" }).postMessage({ window: windowId });
  await load();
  await refreshSettings();
}

void start();
