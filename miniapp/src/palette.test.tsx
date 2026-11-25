// @vitest-environment jsdom
// The command palette: its groups, what typing does to them, the host's search for chats by meaning,
// and the two keys that open a row (Enter, and Ctrl Enter beside the open chat).

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setLang } from "./i18n";
import { Palette } from "./palette";
import type { PaletteItem } from "./shell";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let host: HTMLDivElement;
let root: Root;
const run = vi.fn();
const beside = vi.fn();
const close = vi.fn();

const ITEMS: PaletteItem[] = [
  { id: "s-1", group: "chats", label: "Menu page", hint: "Bakery", icon: "bots", right: "working", tone: "live", run: () => run("chat"), beside: () => beside("chat") },
  { id: "s-2", group: "chats", label: "Courier rates", hint: "Delivery", icon: "bots", right: "1d", run: () => run("rates") },
  { id: "a-1", group: "actions", label: "New chat", icon: "compose", right: "Ctrl ⇧ O", run: () => run("new") },
  { id: "g-1", group: "go", label: "Changes", icon: "changes", right: "G C", run: () => run("changes") },
  { id: "g-2", group: "go", label: "Menu of the week", icon: "file", right: "Page", run: () => run("page") },
];

beforeEach(() => {
  // The palette waits for the typing to pause before it asks the host. On the clock's own time that is
  // a quarter of a second of waiting in every test, and under load a test long enough to be cut off;
  // on a fake one the pause is stepped over, and what it does at the end of it is the same.
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
  setLang("en");
  run.mockReset();
  beside.mockReset();
  close.mockReset();
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
  vi.stubGlobal("fetch", vi.fn(async () => answer([])));
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
});
afterEach(async () => {
  await act(async () => root.unmount());
  host.remove();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

/** The host's search answer: the chats it found, by words and by meaning. */
const answer = (sessions: object[]) => new Response(JSON.stringify({ sessions, projects: [], semantic: true }), { status: 200 });
const QUARTERLY = { id: "s-9", title: "Quarterly summary", project: "Ops", status: "idle", last_message_at: new Date().toISOString(), metadata: {}, match: { score: 1, snippet: "the sales figures" } };

const settle = (ms = 40) => act(async () => { await vi.advanceTimersByTimeAsync(ms); });
const field = () => document.querySelector<HTMLInputElement>(".palette-sheet .combo-input")!;
const rows = () => [...document.querySelectorAll(".palette-row")].map((row) => row.querySelector(".palette-title")?.textContent);
const labels = () => [...document.querySelectorAll(".palette-box .combo-group-label")].map((el) => el.firstChild?.textContent);
async function type(text: string) {
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(field(), text);
    field().dispatchEvent(new Event("input", { bubbles: true }));
  });
}
const key = (name: string, extra: KeyboardEventInit = {}) => act(async () => {
  field().dispatchEvent(new KeyboardEvent("keydown", { key: name, bubbles: true, cancelable: true, ...extra }));
});

async function open(initialQuery = "") {
  await act(async () => root.render(<Palette items={() => ITEMS} initialQuery={initialQuery} onClose={close} />));
  await settle();
}

// Each test draws the palette and its combobox in jsdom, the first one with the start-up of React and
// jsdom besides, which a busy machine stretches past the default five seconds.
describe("the groups", { timeout: 30_000 }, () => {
  it("draws the chats, actions and places a visit starts from, each under its label", async () => {
    await open();
    expect(labels()).toEqual(["Chats", "Actions", "Go to"]);
    expect(rows()).toEqual(["Menu page", "Courier rates", "New chat", "Changes", "Menu of the week"]);
    expect(document.querySelector(".palette-foot")?.textContent).toContain("5 results");
  });

  it("narrows every group to what holds the words, marking them in the names", async () => {
    await open();
    await type("menu");
    await settle(300);
    // The two names that hold it, in their groups; the settings index adds its own rows after them.
    expect(rows().slice(0, 2)).toEqual(["Menu page", "Menu of the week"]);
    expect([...document.querySelectorAll(".palette-hit")].map((el) => el.textContent?.toLowerCase())).toContain("menu");
    expect(labels()).toContain("Chats");
    expect(labels()).not.toContain("Actions");
  });

  it("opens on the words it was given", async () => {
    await open("rates");
    expect(field().value).toBe("rates");
    expect(rows()).toContain("Courier rates");
  });

  it("says when nothing matches, and counts zero", async () => {
    await open();
    await type("zzqqxx");
    await settle(300);
    expect(document.querySelector(".palette-box")?.textContent).toContain("Nothing matches.");
    expect(document.querySelector(".palette-foot")?.textContent).toContain("0 results");
  });
});

describe("the host's search by meaning", { timeout: 30_000 }, () => {
  it("adds a chat whose name does not hold the words, after the ones whose names do", async () => {
    vi.mocked(fetch).mockImplementation(async () => answer([QUARTERLY]));
    await open();
    await type("sales");
    await settle(400);
    expect(vi.mocked(fetch).mock.calls[0][0]).toContain("/api/sessions/search?q=sales");
    expect(rows()).toContain("Quarterly summary");
    const hint = [...document.querySelectorAll(".palette-row")].find((row) => row.textContent?.includes("Quarterly"))?.querySelector(".palette-hint")?.textContent;
    expect(hint).toContain("Ops");
    expect(hint).toContain("the sales figures");
  });

  it("is asked once the typing pauses, not on every key", async () => {
    await open();
    await type("s");
    await type("sa");
    await type("sal");
    await settle(400);
    expect(vi.mocked(fetch)).toHaveBeenCalledTimes(1);
  });
});

describe("opening a row", { timeout: 30_000 }, () => {
  it("opens the highlighted row on Enter and closes", async () => {
    await open();
    await type("changes");
    await settle(300);
    await key("ArrowDown");
    await key("Enter");
    expect(run).toHaveBeenCalledWith("changes");
    expect(close).toHaveBeenCalled();
  });

  it("opens a chat beside the open one on Ctrl Enter, and just opens a row that has no beside", async () => {
    await open();
    await type("menu page");
    await settle(300);
    await key("ArrowDown");
    await key("Enter", { ctrlKey: true });
    expect(beside).toHaveBeenCalledWith("chat");
    expect(run).not.toHaveBeenCalled();
    await open();
    await type("changes");
    await settle(300);
    await key("ArrowDown");
    await key("Enter", { ctrlKey: true });
    expect(run).toHaveBeenCalledWith("changes");
  });
});
