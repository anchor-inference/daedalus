// @vitest-environment jsdom
// Chat kinds: which sessions each tab shows (a helper and a fork follow the chat they hang under), the
// counts, the remembered tab read back safely, the new chat's default mode following the tab only
// where that kind can be started, the reasons a kind is not offered, the mark drawn only on a computer
// or Chrome chat, and the computer sheet asked for once.
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import type { BrowserUserStatus, SessionSummary } from "./api";
import type { ComputerState } from "./computer";
import { setLang } from "./i18n";
import { KindMark, KindTabs, byTab, defaultKind, kindIcon, kindOffers, kindTab, parseTab, requestComputerSheet, sessionKind, setKindTab, tabCounts, takeComputerSheet } from "./chatkind";

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
  });

  it("survives a loop in the parent links", () => {
    const loop = [row("x", { kind: "chrome", metadata: { subagent_of: "y" } }), row("y", { metadata: { subagent_of: "x" } })];
    expect(byTab(loop, "chrome").length + byTab(loop, "chat").length).toBe(2);
  });

  it("counts top-level, unarchived chats per tab", () => {
    expect(tabCounts(LIST)).toEqual({ all: 5, chat: 3, computer: 1, chrome: 1 });
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

  it("draws a mark only on a computer or Chrome chat, with its tooltip", () => {
    act(() => root.render(<>{(["chat", "computer", "chrome"] as const).map((k) => <KindMark key={k} kind={k} />)}</>));
    const marks = [...host.querySelectorAll(".kind-mark")];
    expect(marks.map((m) => m.className)).toEqual(["kind-mark kind-computer", "kind-mark kind-chrome"]);
    expect(marks[0].getAttribute("title")).toMatch(/компьютер/i);
    expect(kindIcon("chat")).toBeNull();
  });

  it("names the four tabs in Russian, marks the chosen one and walks them with the arrows", () => {
    const chosen: string[] = [];
    act(() => root.render(<KindTabs tab="computer" onChange={(t) => chosen.push(t)} counts={{ all: 5, chat: 3, computer: 1, chrome: 1 }} />));
    const tabs = [...host.querySelectorAll<HTMLButtonElement>("[role=tab]")];
    expect(tabs.map((b) => b.querySelector("span")?.textContent)).toEqual(["Все", "Обычные", "Компьютер", "Браузер"]);
    expect(tabs.map((b) => b.getAttribute("aria-selected"))).toEqual(["false", "false", "true", "false"]);
    // A count only on the two kinds a list holds few of; All and Chats would be one more number to read.
    expect(tabs.map((b) => b.querySelector("b")?.textContent ?? "")).toEqual(["", "", "1", "1"]);
    act(() => tabs[2].dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true })));
    act(() => tabs[0].click());
    expect(chosen).toEqual(["chrome", "all"]);
  });
});
