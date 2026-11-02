// @vitest-environment jsdom
// Chat kinds: which sessions each tab shows (a helper and a fork follow the chat they hang under), the
// counts, the remembered tab read back safely, the new chat's default mode following the tab only
// where that kind can be started, the reasons a kind is not offered, the mark drawn only on a computer,
// Chrome or remote chat, a remote chat never offered from the app, and the computer sheet asked for once.
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import type { BrowserUserStatus, SessionSummary } from "./api";
import type { ComputerState } from "./computer";
import { setLang } from "./i18n";
import { KindMark, KindTabs, byTab, defaultKind, kindIcon, kindOffers, kindPlate, kindTab, liveControls, parseTab, requestComputerSheet, sessionKind, setKindTab, shownKind, tabCounts, takeComputerSheet } from "./chatkind";

function row(id: string, extra: Partial<SessionSummary> = {}): SessionSummary {
  return { id, title: id, status: "idle", created_at: "2026-10-21T10:00:00Z", last_message_at: "2026-10-21T10:00:00Z", run_id: null, project_id: "p", project: "p", ...extra };
}

const LIST = [
  row("plain"),
  row("old"), // a host from before the column sends no kind
  row("desk", { kind: "computer" }),
  row("helper", { kind: "chat", metadata: { subagent_of: "desk" } }),
  row("deskfork", { metadata: { forked_from: { session_id: "desk", seq: 4 } } }),
  row("web", { kind: "chrome" }),
  row("gone", { kind: "computer", archived: true }),
  row("laptop", { kind: "remote", remote: { device_id: "d1", device_name: "laptop", cwd: "/home/you/site", connected: true } }),
];

describe("which chats a tab shows", () => {
  it("reads a missing or unknown kind as an ordinary chat", () => {
    expect(sessionKind(row("a"))).toBe("chat");
    expect(sessionKind(row("a", { kind: "nonsense" as never }))).toBe("chat");
    expect(sessionKind(row("a", { kind: "chrome" }))).toBe("chrome");
  });

  it("shows everything under All", () => {
    expect(byTab(LIST, "all")).toBe(LIST);
  });

  it("keeps a computer chat's helpers and forks under it", () => {
    expect(byTab(LIST, "computer").map((s) => s.id)).toEqual(["desk", "helper", "deskfork", "gone"]);
  });

  it("leaves a computer chat's helper out of the ordinary chats", () => {
    expect(byTab(LIST, "chat").map((s) => s.id)).toEqual(["plain", "old"]);
    expect(byTab(LIST, "chrome").map((s) => s.id)).toEqual(["web"]);
    expect(byTab(LIST, "remote").map((s) => s.id)).toEqual(["laptop"]);
  });

  it("survives a loop in the parent links", () => {
    const loop = [row("x", { kind: "chrome", metadata: { subagent_of: "y" } }), row("y", { metadata: { subagent_of: "x" } })];
    expect(byTab(loop, "chrome").length + byTab(loop, "chat").length).toBe(2);
  });

  it("counts top-level, unarchived chats per tab", () => {
    expect(tabCounts(LIST)).toEqual({ all: 6, chat: 3, computer: 1, chrome: 1, remote: 1 });
  });
});

// The origin is what a chat was started as and never changes; `controlling` is what it holds now. An
// ordinary chat that once used the operator's Chrome used to become a Browser chat for good.
describe("a control held now", () => {
  const driving = [
    row("plain"),
    row("borrow", { controlling: ["chrome"] }),
    row("web", { kind: "chrome" }),
    row("desk", { kind: "computer" }),
    row("lead"),
    row("lead-helper", { metadata: { subagent_of: "lead" }, controlling: ["chrome"] }),
  ];

  it("lists an ordinary chat under Browser while it controls Chrome, and under Chats as well", () => {
    expect(byTab(driving, "chrome").map((s) => s.id)).toEqual(["borrow", "web", "lead", "lead-helper"]);
    expect(byTab(driving, "chat").map((s) => s.id)).toEqual(["plain", "borrow", "lead", "lead-helper"]);
  });

  it("takes it out of Browser once control ends", () => {
    const released = driving.map((s) => ({ ...s, controlling: [] as ("chrome" | "computer")[] }));
    expect(byTab(released, "chrome").map((s) => s.id)).toEqual(["web"]);
    expect(byTab(released, "chat").map((s) => s.id)).toEqual(["plain", "borrow", "lead", "lead-helper"]);
  });

  it("keeps a computer chat under Computer without any control", () => {
    expect(byTab(driving, "computer").map((s) => s.id)).toEqual(["desk"]);
  });

  it("brings a helper's leader into Browser with it, so the helper is never an orphan", () => {
    expect(byTab(driving, "chrome")).toContainEqual(expect.objectContaining({ id: "lead" }));
  });

  it("counts a controlling chat under its own kind and under the control", () => {
    expect(tabCounts(driving)).toEqual({ all: 5, chat: 3, computer: 1, chrome: 3, remote: 0 });
  });

  it("reads only the controls it knows", () => {
    expect(liveControls(row("a", { controlling: ["remote", "computer", "printer" as never, "chrome"] }))).toEqual(["chrome", "computer", "remote"]);
    expect(liveControls(row("a"))).toEqual([]);
  });

  it("shows the origin, a borrowed control, or nothing", () => {
    expect(shownKind(row("a", { kind: "chrome" }))).toEqual({ kind: "chrome", live: false, borrowed: false });
    expect(shownKind(row("a", { kind: "chrome", controlling: ["chrome"] }))).toEqual({ kind: "chrome", live: true, borrowed: false });
    expect(shownKind(row("a", { kind: "computer", controlling: ["chrome"] }))).toEqual({ kind: "computer", live: false, borrowed: false });
    expect(shownKind(row("a", { controlling: ["computer"] }))).toEqual({ kind: "computer", live: true, borrowed: true });
    expect(shownKind(row("a", { controlling: ["computer", "chrome"] }))).toEqual({ kind: "chrome", live: true, borrowed: true });
    expect(shownKind(row("a"))).toEqual({ kind: "chat", live: false, borrowed: false });
    expect(kindPlate(row("a", { controlling: ["chrome"] })).className).toBe("kind-chrome kind-live kind-borrowed");
    expect(kindPlate(row("a")).className).toBe("");
  });

  // A terminal attached to a chat is a live mark like a browser or a computer held: an ordinary chat
  // with daedalus-cli connected to it is listed under Remote while it is, and a remote chat whose
  // terminal is connected is drawn live.
  it("lists a chat a terminal is attached to under Remote only while it is", () => {
    const attached = [row("plain"), row("watched", { controlling: ["remote"] }), row("born", { kind: "remote" })];
    expect(byTab(attached, "remote").map((s) => s.id)).toEqual(["watched", "born"]);
    expect(tabCounts(attached)).toEqual({ all: 3, chat: 2, computer: 0, chrome: 0, remote: 2 });
    expect(shownKind(row("a", { kind: "remote", controlling: ["remote"] }))).toEqual({ kind: "remote", live: true, borrowed: false });
    expect(shownKind(row("a", { controlling: ["remote"] }))).toEqual({ kind: "remote", live: true, borrowed: true });
    // The browser the chat drives outranks the terminal watching it.
    expect(shownKind(row("a", { controlling: ["remote", "chrome"] })).kind).toBe("chrome");
  });
});

describe("the remembered tab", () => {
  beforeEach(() => setKindTab("all"));

  it("reads back only a tab this build knows", () => {
    expect(parseTab("computer")).toBe("computer");
    expect(parseTab("printer")).toBe("all");
    expect(parseTab(null)).toBe("all");
  });

  it("is stored for the next visit", () => {
    setKindTab("chrome");
    expect(kindTab()).toBe("chrome");
    expect(localStorage.getItem("daedalus.chats.kind")).toBe("chrome");
  });
});

const NATIVE: ComputerState = { native: true, available: true };
const SERVER: ComputerState = { native: false, available: false, reason: "Only in the desktop app" };
const chrome = (extra: Partial<BrowserUserStatus>): BrowserUserStatus => ({
  configured: true, enabled: true, daemon: true, reason: "", detail: "", relay: { connected: true } as BrowserUserStatus["relay"], native: true,
  launcher: null, launcher_error: "", devices: [], server_url: "", consent: "", ...extra,
});

describe("what a new chat can be", () => {
  beforeEach(() => setLang("en"));

  it("offers the computer only in the native application, with the host's reason otherwise", () => {
    expect(kindOffers(NATIVE, undefined).computer.ok).toBe(true);
    const off = kindOffers(SERVER, undefined).computer;
    expect(off.ok).toBe(false);
    expect(off.reason).toBe("Only in the desktop app");
    expect(kindOffers(undefined, undefined).computer.reason).toMatch(/desktop app/);
  });

  it("offers Chrome where its daemon is configured and running, and says when it waits for the extension", () => {
    expect(kindOffers(undefined, chrome({ configured: false })).chrome).toMatchObject({ ok: false });
    expect(kindOffers(undefined, chrome({ daemon: false })).chrome.reason).toMatch(/not running/);
    const waiting = kindOffers(undefined, chrome({ relay: { connected: false } as BrowserUserStatus["relay"] }));
    expect(waiting.chrome.ok).toBe(true);
    expect(waiting.chromeConnected).toBe(false);
    expect(kindOffers(undefined, chrome({})).chromeConnected).toBe(true);
  });

  it("starts in the tab's kind only where that kind can be started", () => {
    const both = kindOffers(NATIVE, chrome({}));
    const none = kindOffers(SERVER, chrome({ configured: false }));
    expect(defaultKind("computer", both)).toBe("computer");
    expect(defaultKind("chrome", both)).toBe("chrome");
    expect(defaultKind("all", both)).toBe("chat");
    expect(defaultKind("chat", both)).toBe("chat");
    expect(defaultKind("computer", none)).toBe("chat");
    expect(defaultKind("chrome", none)).toBe("chat");
    expect(defaultKind("remote", both)).toBe("chat");
  });

  it("never offers a remote chat here, and says where one starts", () => {
    const offer = kindOffers(NATIVE, chrome({})).remote;
    expect(offer.ok).toBe(false);
    expect(offer.reason).toMatch(/daedalus-cli/);
  });

  it("opens the computer sheet once, for the chat it was asked for", () => {
    requestComputerSheet("s9");
    expect(takeComputerSheet("s1")).toBe(false);
    expect(takeComputerSheet("s9")).toBe(true);
    expect(takeComputerSheet("s9")).toBe(false);
  });
});

describe("the tabs and the mark", () => {
  let host: HTMLDivElement;
  let root: Root;
  beforeEach(() => {
    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    setLang("ru");
    host = document.createElement("div");
    document.body.appendChild(host);
    root = createRoot(host);
  });
  afterEach(() => {
    act(() => root.unmount());
    host.remove();
    setLang("en");
  });

  it("draws a mark only on a computer, Chrome or remote chat, with its tooltip", () => {
    act(() => root.render(<>{(["chat", "computer", "chrome", "remote"] as const).map((k) => <KindMark key={k} s={{ kind: k }} />)}</>));
    const marks = [...host.querySelectorAll(".kind-mark")];
    expect(marks.map((m) => m.className)).toEqual(["kind-mark kind-computer", "kind-mark kind-chrome", "kind-mark kind-remote"]);
    expect(marks[2].getAttribute("title")).toMatch(/daedalus-cli/);
    expect(marks[0].getAttribute("title")).toMatch(/компьютер/i);
    expect(kindIcon("chat")).toBeNull();
  });

  it("draws an ordinary chat's mark hollow and live only while it controls the browser", () => {
    act(() => root.render(<><KindMark s={{ kind: "chat", controlling: ["chrome"] }} /><KindMark s={{ kind: "chat", controlling: [] }} /></>));
    const marks = [...host.querySelectorAll(".kind-mark")];
    expect(marks.map((m) => m.className)).toEqual(["kind-mark kind-chrome kind-live kind-borrowed"]);
    expect(marks[0].getAttribute("title")).toMatch(/сейчас управляет вашим chrome/i);
  });

  it("names the five tabs in Russian, marks the chosen one and walks them with the arrows", () => {
    const chosen: string[] = [];
    act(() => root.render(<KindTabs tab="computer" onChange={(t) => chosen.push(t)} counts={{ all: 6, chat: 3, computer: 1, chrome: 1, remote: 1 }} />));
    const tabs = [...host.querySelectorAll<HTMLButtonElement>("[role=tab]")];
    expect(tabs.map((b) => b.querySelector("span")?.textContent)).toEqual(["Все", "Обычные", "Компьютер", "Браузер", "Удалённо"]);
    expect(tabs.map((b) => b.getAttribute("aria-selected"))).toEqual(["false", "false", "true", "false", "false"]);
    // A count only on the kinds a list holds few of; All and Chats would be one more number to read.
    expect(tabs.map((b) => b.querySelector("b")?.textContent ?? "")).toEqual(["", "", "1", "1", "1"]);
    act(() => tabs[2].dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true })));
    act(() => tabs[0].click());
    expect(chosen).toEqual(["chrome", "all"]);
  });
});
