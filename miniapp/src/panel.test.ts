// The right panel as a value: tabs, the Preview history, the route it writes and reads back, the
// width between its floor and its ceiling, the tab a session opens on.

import { describe, expect, it } from "vitest";
import {
  PANEL_CLOSED,
  PANEL_DEFAULT_PCT,
  PANEL_MAX_PCT,
  PANEL_OPEN_MIN,
  applyPanelQuery,
  canGoBack,
  canGoForward,
  clampPanelPct,
  closePanel,
  crumbsOf,
  currentEntry,
  defaultPanelTab,
  goBack,
  goForward,
  openFile,
  openTab,
  panelQuery,
  panelShortcut,
  readPanelPct,
  readPanelQuery,
  readPanelTab,
  rememberPanelPct,
  rememberPanelTab,
  routeTabsFor,
  tabsFor,
  toggleExpanded,
  togglePanel,
} from "./panel";

const base = "/api/sessions/s1";

describe("tabs", () => {
  it("opens, switches and closes", () => {
    const open = openTab(PANEL_CLOSED, "files");
    expect(open.tab).toBe("files");
    expect(openTab(open, "jobs").tab).toBe("jobs");
    expect(closePanel(open).tab).toBeNull();
    expect(closePanel(PANEL_CLOSED)).toBe(PANEL_CLOSED);
  });

  it("toggles to the last tab, Details when there was none, and back to closed", () => {
    expect(togglePanel(PANEL_CLOSED).tab).toBe("details");
    expect(togglePanel(PANEL_CLOSED, "jobs").tab).toBe("jobs");
    expect(togglePanel(openTab(PANEL_CLOSED, "files")).tab).toBeNull();
  });

  it("expands only while open, and closing drops the expansion", () => {
    expect(toggleExpanded(PANEL_CLOSED).expanded).toBe(false);
    const wide = toggleExpanded(openTab(PANEL_CLOSED, "preview"));
    expect(wide.expanded).toBe(true);
    expect(toggleExpanded(wide).expanded).toBe(false);
    expect(closePanel(wide).expanded).toBe(false);
  });
});

describe("the preview history", () => {
  const a = { base, path: "a.md" };
  const b = { base, path: "b.md" };
  const c = { base, path: "c.md" };

  it("walks back and forward over what was opened", () => {
    let s = openFile(PANEL_CLOSED, a);
    expect(s.tab).toBe("preview");
    expect(canGoBack(s)).toBe(false);
    s = openFile(openFile(s, b), c);
    expect(currentEntry(s)).toEqual(c);
    expect(canGoBack(s)).toBe(true);
    expect(canGoForward(s)).toBe(false);
    s = goBack(s);
    expect(currentEntry(s)).toEqual(b);
    expect(canGoForward(s)).toBe(true);
    s = goBack(s);
    expect(currentEntry(s)).toEqual(a);
    expect(goBack(s)).toBe(s);
    s = goForward(goForward(s));
    expect(currentEntry(s)).toEqual(c);
    expect(goForward(s)).toBe(s);
  });

  it("forgets the forward entries when a new file is opened mid-way", () => {
    let s = openFile(openFile(openFile(PANEL_CLOSED, a), b), c);
    s = goBack(goBack(s));
    s = openFile(s, { base, path: "d.md" });
    expect(s.stack.map((e) => e.path)).toEqual(["a.md", "d.md"]);
    expect(canGoForward(s)).toBe(false);
  });

  it("does not stack the same file twice in a row, but does stack a different cited range", () => {
    const s = openFile(openFile(PANEL_CLOSED, a), a);
    expect(s.stack).toHaveLength(1);
    const cited = openFile(s, { ...a, lines: "3-9" });
    expect(cited.stack).toHaveLength(2);
  });

  it("brings the Preview tab forward when walking the history from another tab", () => {
    const s = openTab(openFile(openFile(PANEL_CLOSED, a), b), "files");
    expect(goBack(s).tab).toBe("preview");
  });
});

describe("the route", () => {
  it("keeps cited lines when a link is copied or reloaded", () => {
    const source = openFile(PANEL_CLOSED, { base, path: "src/main.py", lines: "4-5" });
    const q = new URLSearchParams();
    for (const [key, value] of Object.entries(panelQuery(source))) if (value) q.set(key, value);
    expect(currentEntry(applyPanelQuery(PANEL_CLOSED, readPanelQuery(q), base))?.lines).toBe("4-5");
  });

  it("writes the tab and, on Preview, the path", () => {
    expect(panelQuery(PANEL_CLOSED)).toEqual({ panel: null, path: null, lines: null, tab: null });
    expect(panelQuery(openTab(PANEL_CLOSED, "files"))).toEqual({ panel: "files", path: null, lines: null, tab: null });
    const s = openFile(PANEL_CLOSED, { base, path: "reports/menu-check.md" });
    expect(panelQuery(s)).toEqual({ panel: "preview", path: "reports/menu-check.md", lines: null, tab: null });
    expect(panelQuery(openTab(s, "details")).path).toBeNull();
  });

  it("reads the tab from panel= or tab=, and ignores what it does not know", () => {
    expect(readPanelQuery(new URLSearchParams("panel=files"))).toEqual({ tab: "files", path: null });
    expect(readPanelQuery(new URLSearchParams("panel=files&tab=preview&path=reports/menu-check.md"))).toEqual({ tab: "preview", path: "reports/menu-check.md" });
    expect(readPanelQuery(new URLSearchParams("panel=sideways"))).toBeNull();
    expect(readPanelQuery(new URLSearchParams("with=x"))).toBeNull();
  });

  it("round-trips: what a state writes, a fresh pane reads back to the same view", () => {
    const s = openFile(PANEL_CLOSED, { base, path: "reports/menu-check.md" });
    const q = new URLSearchParams();
    for (const [k, v] of Object.entries(panelQuery(s))) if (v) q.set(k, v);
    const back = applyPanelQuery(PANEL_CLOSED, readPanelQuery(q), base);
    expect(back.tab).toBe("preview");
    expect(currentEntry(back)).toEqual({ base, path: "reports/menu-check.md" });
  });

  it("applies a route over an existing history without duplicating the file on screen", () => {
    const s = openFile(PANEL_CLOSED, { base, path: "a.md" });
    const same = applyPanelQuery(s, { tab: "preview", path: "a.md" }, base);
    expect(same.stack).toHaveLength(1);
    const other = applyPanelQuery(s, { tab: "preview", path: "b.md" }, base);
    expect(other.stack.map((e) => e.path)).toEqual(["a.md", "b.md"]);
    expect(applyPanelQuery(s, null, base).tab).toBeNull();
    expect(applyPanelQuery(s, { tab: "jobs", path: null }, base).tab).toBe("jobs");
  });
});

describe("the width", () => {
  it("stays between the pixel floor and the percentage ceiling", () => {
    expect(clampPanelPct(42, 1168)).toBe(42);
    expect(clampPanelPct(90, 1168)).toBe(PANEL_MAX_PCT);
    // 360 px of 1168 is 30.8 %: anything narrower is pulled up to it.
    expect(clampPanelPct(10, 1168)).toBe(30.8);
    // A pane too narrow for both keeps the ceiling rather than asking for more than 65 %.
    expect(clampPanelPct(10, 400)).toBe(PANEL_MAX_PCT);
    expect(clampPanelPct(Number.NaN, 1168)).toBe(PANEL_DEFAULT_PCT);
  });

  it("is remembered as a percentage and read back, with the default for nonsense", () => {
    const kept = new Map<string, string>();
    const storage = { getItem: (k: string) => kept.get(k) ?? null, setItem: (k: string, v: string) => void kept.set(k, v) };
    expect(readPanelPct(storage)).toBe(PANEL_DEFAULT_PCT);
    rememberPanelPct(55.5, storage);
    expect(kept.get("daedalus.width.panel")).toBe("55.5");
    expect(readPanelPct(storage)).toBe(55.5);
    kept.set("daedalus.width.panel", "900");
    expect(readPanelPct(storage)).toBe(PANEL_DEFAULT_PCT);
    const broken = { getItem: () => { throw new Error("private"); }, setItem: () => { throw new Error("private"); } };
    expect(() => rememberPanelPct(50, broken)).not.toThrow();
    expect(readPanelPct(broken)).toBe(PANEL_DEFAULT_PCT);
  });
});

describe("the tab a session opens on", () => {
  it("is Details on a wide window, nothing on a narrow one, until the operator chooses", () => {
    expect(defaultPanelTab(null, PANEL_OPEN_MIN)).toBe("details");
    expect(defaultPanelTab(null, PANEL_OPEN_MIN - 1)).toBeNull();
    expect(defaultPanelTab("files", 1440)).toBe("files");
    expect(defaultPanelTab("files", 1100)).toBeNull();
    expect(defaultPanelTab("0", 2560)).toBeNull();
    expect(defaultPanelTab("sideways", 1440)).toBe("details");
  });

  it("remembers the choice, closed included", () => {
    const kept = new Map<string, string>();
    const storage = { getItem: (k: string) => kept.get(k) ?? null, setItem: (k: string, v: string) => void kept.set(k, v) };
    rememberPanelTab("jobs", storage);
    expect(readPanelTab(1440, storage)).toBe("jobs");
    rememberPanelTab(null, storage);
    expect(readPanelTab(1440, storage)).toBeNull();
  });
});

describe("the keyboard", () => {
  const key = (over: Partial<KeyboardEvent>) => ({ key: ".", code: "Period", metaKey: false, ctrlKey: false, altKey: false, shiftKey: false, ...over });
  it("takes the period with a modifier, and Shift for the expansion", () => {
    expect(panelShortcut(key({ ctrlKey: true }))).toBe("toggle");
    expect(panelShortcut(key({ metaKey: true }))).toBe("toggle");
    expect(panelShortcut(key({ metaKey: true, shiftKey: true, key: ">" }))).toBe("expand");
    expect(panelShortcut(key({}))).toBeNull();
    expect(panelShortcut(key({ ctrlKey: true, altKey: true }))).toBeNull();
    expect(panelShortcut(key({ ctrlKey: true, key: "k", code: "KeyK" }))).toBeNull();
  });
});

describe("the breadcrumb", () => {
  it("is the path split on slashes, without empties", () => {
    expect(crumbsOf("reports/menu-check.md")).toEqual(["reports", "menu-check.md"]);
    expect(crumbsOf("/a//b/")).toEqual(["a", "b"]);
    expect(crumbsOf("")).toEqual([]);
  });
});

describe("a file opened beside the orchestrator", () => {
  it("keeps the panel open through the address it writes", () => {
    // The orchestrator has no Preview tab until a file is opened; the route must still know it,
    // or the address the panel writes reads back as "closed" and the first click is undone.
    const opened = openFile(PANEL_CLOSED, { base: "/api/files/0123456789ab", path: "fix.patch" });
    const written = new URLSearchParams(Object.entries(panelQuery(opened)).filter(([, v]) => v) as [string, string][]);
    const q = readPanelQuery(written, routeTabsFor("orchestrator"));
    expect(q).toEqual({ tab: "preview", path: "fix.patch" });
    const after = applyPanelQuery(opened, q, base);
    expect(after.tab).toBe("preview");
    expect(currentEntry(after)).toEqual({ base: "/api/files/0123456789ab", path: "fix.patch" });
  });

  it("offers Preview in the strip only once something was opened", () => {
    expect(tabsFor("orchestrator")).not.toContain("preview");
    expect(tabsFor("orchestrator", { preview: true })).toEqual(["questions", "details", "preview", "board", "brief", "wakeups", "folders"]);
    expect(routeTabsFor("orchestrator")).toContain("preview");
  });
});

describe("the Browser tab", () => {
  it("is offered only to a session that has had a browser, after the agent's own tabs", () => {
    expect(tabsFor("session")).not.toContain("browser");
    expect(tabsFor("session", { browser: true })).toEqual(["details", "files", "preview", "jobs", "browser"]);
    expect(tabsFor("member", { browser: true })).toEqual(["details", "files", "preview", "jobs", "browser", "board", "brief", "wakeups", "folders"]);
    expect(tabsFor("main", { browser: true })).toEqual(["questions", "details", "files", "preview", "jobs", "browser"]);
    expect(tabsFor("orchestrator", { browser: true })).toEqual(["questions", "details", "browser", "board", "brief", "wakeups", "folders"]);
  });

  it("gives the orchestrator its Details, and neither files nor jobs it does not have", () => {
    expect(tabsFor("orchestrator")).toEqual(["questions", "details", "board", "brief", "wakeups", "folders"]);
    expect(tabsFor("orchestrator")).not.toContain("files");
    expect(tabsFor("orchestrator")).not.toContain("jobs");
  });

  it("opens from a link before the listing says there is a browser", () => {
    const q = readPanelQuery(new URLSearchParams("panel=browser"), routeTabsFor("session"));
    expect(q).toEqual({ tab: "browser", path: null });
    expect(applyPanelQuery(PANEL_CLOSED, q, base).tab).toBe("browser");
    expect(panelQuery(openTab(PANEL_CLOSED, "browser"))).toEqual({ panel: "browser", path: null, lines: null, tab: null });
  });

  it("is never where a session opens by itself: the next session may have no browser", () => {
    const kept = new Map<string, string>([["daedalus.session.panel", "browser"]]);
    const storage = { getItem: (k: string) => kept.get(k) ?? null };
    expect(readPanelTab(1440, storage)).toBe("details");
  });
});
