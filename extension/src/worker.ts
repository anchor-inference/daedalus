// The service worker. Chrome may stop it whenever it is idle and start it again for the next event,
// so every listener is registered here, synchronously, on each start: a listener added later, after
// an await, misses the event that woke the worker. Each one waits for the relay to have read its
// state back before acting.

import { Relay } from "./relay.js";

const relay = new Relay();
const ready = relay.start();

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

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (!message || typeof message.kind !== "string") return;
  if (message.kind === "daedalus-input") {
    after(() => relay.onContentInput(sender))();
    return;
  }
  // Only the extension's own pages (the popup, or the same page opened in a tab) may drive the relay;
  // a content script, whose sender is the page it runs in, is never trusted with more than input.
  if (sender.id !== chrome.runtime.id || !sender.url?.startsWith(chrome.runtime.getURL(""))) return;
  void ready
    .then(() => answer(message))
    .then(sendResponse, (error) => sendResponse({ error: String(error) }));
  return true;
});

async function answer(message: { kind: string; [key: string]: unknown }): Promise<unknown> {
  switch (message.kind) {
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
