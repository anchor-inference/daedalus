// The service worker's doors: what a message may ask for depends on who sent it, the toolbar button
// opens the side panel, and a notification's click opens the panel on the conversation it named.

import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { flush } from "./harness.js";
import { installFakeChrome, type FakeChrome } from "./fake-chrome.js";

let fake: FakeChrome;

beforeAll(async () => {
  vi.useFakeTimers();
  fake = installFakeChrome();
  await import("../src/worker.js");
  await flush();
});

afterEach(() => {
  fake.runtimeMessages.length = 0;
});

function send(message: unknown, sender: chrome.runtime.MessageSender): Promise<unknown> {
  return new Promise((resolve) => {
    let answered = false;
    const listener = fake.events.runtimeMessage.listeners[0];
    const kept = listener(message, sender, (response: unknown) => {
      answered = true;
      resolve(response);
    });
    if (kept !== true && !answered) resolve("no answer");
  });
}

const PANEL = { id: "lkhhklicnjlplokpadpccndipomikoaj", url: "chrome-extension://lkhhklicnjlplokpadpccndipomikoaj/panel.html" };

describe("the worker", () => {
  it("lets the toolbar button open the side panel", () => {
    expect(fake.sidePanel.behavior).toEqual({ openPanelOnActionClick: true });
  });

  it("answers the panel's questions from an extension page", async () => {
    const answer = (await send({ kind: "panel-state" }, PANEL)) as { status: { phase: string }; grant: unknown };
    expect(answer.grant).toBeNull();
    expect(answer.status.phase).toBeDefined();
  });

  it.each([
    ["a content script in a page", { id: PANEL.id, url: "https://evil.example/", tab: { id: 5 } as chrome.tabs.Tab }],
    ["another extension", { id: "abcdefghijklmnopabcdefghijklmnop", url: "chrome-extension://abcdefghijklmnopabcdefghijklmnop/x.html" }],
  ])("refuses the panel's kinds from %s", async (_, sender) => {
    for (const kind of ["panel-state", "panel-adopt", "panel-focus", "panel-tabs", "pair", "disconnect"]) {
      expect(await send({ kind, tab: 1, group: "g", session: "s" }, sender as chrome.runtime.MessageSender)).toBe("no answer");
    }
  });

  it("opens the panel on a notification's click, inside the gesture, and remembers which conversation", async () => {
    fake.events.notificationClicked.emit("daedalus-panel:42:");
    expect(fake.sidePanel.opened).toEqual([]);
    fake.events.tabsActivated.emit({ tabId: 1, windowId: fake.operatorWindow });
    fake.events.notificationClicked.emit("daedalus-panel:42:");
    expect(fake.sidePanel.opened).toEqual([{ windowId: fake.operatorWindow }]);
    await flush();
    expect(fake.runtimeMessages).toContainEqual({ kind: "panel-show", session: "42" });
    expect(await send({ kind: "panel-show" }, PANEL)).toBe("42");
    expect(await send({ kind: "panel-show" }, PANEL)).toBeNull();
    fake.events.notificationClicked.emit("someone-else:1");
    expect(fake.sidePanel.opened).toHaveLength(1);
  });
});
