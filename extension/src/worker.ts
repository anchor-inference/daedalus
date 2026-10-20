// The service worker. Chrome may stop it whenever it is idle and start it again for the next event,
// so every listener is registered here, synchronously, on each start: a listener added later, after
// an await, misses the event that woke the worker. Each one waits for the relay to have read its
// state back before acting.

import { NOTICE_PREFIX, Relay } from "./relay.js";

const relay = new Relay();
const ready = relay.start();

// The toolbar button and its shortcut (`_execute_action`, Alt+Shift+D unless the operator changed it
// in chrome://extensions/shortcuts) open the side panel; there is no popup any more. Chrome opens a
// panel only on the operator's own gesture, so nothing here ever opens one by itself.
void chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true }).catch(() => undefined);

// The window the operator was last in, known without waiting: a notification's click must open the
// panel inside the gesture, and an await before `sidePanel.open` would spend it.
let lastWindow: number | undefined;
chrome.tabs.onActivated.addListener((info) => {
  lastWindow = info.windowId;
});

function after<A extends unknown[]>(handler: (...args: A) => unknown): (...args: A) => void {
  return (...args: A) => {
    void ready.then(() => handler(...args)).catch((error) => console.error(error));
  };
}

chrome.tabs.onCreated.addListener(after((tab) => relay.onTabCreated(tab)));
chrome.tabs.onRemoved.addListener(after((tabId) => relay.onTabRemoved(tabId)));
chrome.tabs.onUpdated.addListener(after((tabId, info, tab) => relay.onTabUpdated(tabId, info, tab)));
chrome.tabGroups.onRemoved.addListener(after((group) => relay.onGroupRemoved(group)));
chrome.debugger.onEvent.addListener(after((source, method, params) => relay.onDebuggerEvent(source, method, params)));
chrome.debugger.onDetach.addListener(after((source, reason) => relay.onDebuggerDetach(source, reason)));
chrome.contextMenus.onClicked.addListener(after((info, tab) => relay.onMenuClicked(info, tab)));

// An open native port or a live WebSocket keeps the worker running; the alarm is for the time in
// between, when a reconnect timer died with a stopped worker and nothing else would wake it.
void chrome.alarms.create("daedalus-wake", { periodInMinutes: 1 });
chrome.alarms.onAlarm.addListener(after(() => relay.wake()));

// Each open side panel holds a port named for its window, so the relay knows where the operator is
// already looking and does not notify them about what that panel shows anyway.
chrome.runtime.onConnect.addListener((port) => {
  if (port.name !== "panel" || !isExtensionPage(port.sender)) return;
  let windowId: number | undefined;
  port.onMessage.addListener((message) => {
    if (typeof message?.window !== "number") return;
    windowId = message.window;
    relay.openPanels.add(message.window);
  });
  port.onDisconnect.addListener(() => {
    if (windowId !== undefined) relay.openPanels.delete(windowId);
  });
});

chrome.notifications.onClicked.addListener((id) => {
  if (!id.startsWith(`${NOTICE_PREFIX}:`)) return;
  const [, session = "", tab = ""] = id.split(":");
  const tabId = /^\d+$/.test(tab) ? Number(tab) : undefined;
  // First, while the click still counts as the operator's gesture.
  const target = tabId !== undefined ? { tabId } : lastWindow !== undefined ? { windowId: lastWindow } : null;
  if (target) void chrome.sidePanel.open(target).catch(() => undefined);
  void chrome.notifications.clear(id).catch(() => undefined);
  void chrome.storage.session.set({ panelShow: session });
  void chrome.runtime.sendMessage({ kind: "panel-show", session }).catch(() => undefined);
  if (tabId !== undefined) after(() => relay.focusTab(tabId))();
});

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (!message || typeof message.kind !== "string") return;
  if (message.kind === "daedalus-input") {
    after(() => relay.onContentInput(sender))();
    return;
  }
  // Only the extension's own pages (the side panel, or the same page opened in a tab) may drive the
  // relay; a content script, whose sender is the page it runs in, is never trusted with more than input.
  if (!isExtensionPage(sender)) return;
  void ready
    .then(() => answer(message))
    .then(sendResponse, (error) => sendResponse({ error: String(error) }));
  return true;
});

function isExtensionPage(sender: chrome.runtime.MessageSender | undefined): boolean {
  return !!sender && sender.id === chrome.runtime.id && !!sender.url?.startsWith(chrome.runtime.getURL(""));
}

async function answer(message: { kind: string; [key: string]: unknown }): Promise<unknown> {
  switch (message.kind) {
    case "panel-state":
      return { status: relay.status(), grant: await relay.grant() };
    case "panel-tab":
      return typeof message.tab === "number" ? relay.tabOwner(message.tab) : null;
    case "panel-tabs":
      return relay.sessionTabs(String(message.session ?? ""));
    case "panel-adopt":
      return relay.adoptTab(Number(message.tab), String(message.group ?? ""), String(message.session ?? ""), String(message.title ?? ""));
    case "panel-focus":
      return relay.focusTab(Number(message.tab));
    case "panel-show": {
      // The conversation a notification's click asked for, taken once by the panel that opened.
      const shown = (await chrome.storage.session.get("panelShow")).panelShow;
      await chrome.storage.session.remove("panelShow");
      return typeof shown === "string" ? shown : null;
    }
    case "get-status":
      return relay.status();
    case "check":
      return relay.check();
    case "pause":
      return relay.pause(String(message.group), message.paused === true);
    case "stop":
      return relay.stop(String(message.group));
    case "pair":
      return relay.pair(String(message.server ?? ""), String(message.code ?? ""));
    case "use-native":
      return relay.useNative();
    case "disconnect":
      return relay.disconnect();
    case "set-name":
      return relay.setProfileName(String(message.name ?? ""));
    case "set-window-mode":
      return relay.setWindowMode(message.mode === "current" ? "current" : "own");
    default:
      return undefined;
  }
}
