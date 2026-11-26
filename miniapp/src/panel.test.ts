// The right panel as a value: tabs, the Preview history, the route it writes and reads back, the
// width between its floor and its ceiling, the tab a session opens on.

import { describe, expect, it } from "vitest";
import {
  TAB_FITS,
  widestFit,
  PANEL_CLOSED,
  PANEL_MAX_PCT,
  PANEL_MIN_PX,
  PANEL_OPEN_MIN,
  clampPanelPx,
  closeTool,
  groupTools,
  parseStoredPanel,
  readPanelState,
  storedPanel,
  applyPanelQuery,
  canGoBack,
  canGoForward,
  closeEntry,
  openBeside,
  openEntries,
  readPanelPinned,
  rememberPanelPinned,
  showEntry,
  closePanel,
  crumbsOf,
  currentEntry,
  defaultPanelTab,
  goBack,
  goForward,
  openFile,
  openSubagent,
  openTab,
  openTask,
  panelQuery,
  panelShortcut,
  readPanelPx,
  readPanelQuery,
  readPanelTab,
  rememberPanel,
  rememberPanelPx,
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

  it("keeps every tool it opened as a tab, in order, and the Overview out of the list", () => {
    const s = openTab(openTab(openTab(PANEL_CLOSED, "changes"), "jobs"), "overview");
    expect(s.open).toEqual(["changes", "jobs"]);
    expect(s.tab).toBe("overview");
    expect(openTab(s, "changes").open).toEqual(["changes", "jobs"]);
    // Closing the panel keeps the tools: it reopens with the tabs the operator was working with.
    expect(closePanel(s).open).toEqual(["changes", "jobs"]);
  });

  it("closes a tool onto the tab before it, the Overview when it was the first", () => {
    const s = openTab(openTab(openTab(PANEL_CLOSED, "changes"), "jobs"), "notes");
    expect(closeTool(s, "notes")).toMatchObject({ tab: "jobs", open: ["changes", "jobs"] });
    expect(closeTool(openTab(s, "changes"), "changes")).toMatchObject({ tab: "overview", open: ["jobs", "notes"] });
    // A tool that is not on screen goes without moving the reader.
    expect(closeTool(s, "changes")).toMatchObject({ tab: "notes", open: ["jobs", "notes"] });
    expect(closeTool(s, "files")).toBe(s);
    // Preview takes its history with it, Subagents the helper it showed.
    const previewed = openFile(PANEL_CLOSED, { base, path: "a.md" });
    expect(closeTool(previewed, "preview")).toMatchObject({ tab: "overview", stack: [], at: -1 });
    expect(closeTool(openSubagent(PANEL_CLOSED, "c1"), "subagents").sub).toBeNull();
  });

  it("toggles to the last tab, the Overview when there was none, and back to closed", () => {
    expect(togglePanel(PANEL_CLOSED).tab).toBe("overview");
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
    expect(panelQuery(PANEL_CLOSED)).toEqual({ panel: null, path: null, lines: null, sub: null, task: null, tab: null });
    expect(panelQuery(openTab(PANEL_CLOSED, "files"))).toEqual({ panel: "files", path: null, lines: null, sub: null, task: null, tab: null });
    const s = openFile(PANEL_CLOSED, { base, path: "reports/menu-check.md" });
    expect(panelQuery(s)).toEqual({ panel: "preview", path: "reports/menu-check.md", lines: null, sub: null, task: null, tab: null });
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
  it("stays between the pixel floor and the share of the chat it may take", () => {
    expect(clampPanelPx(460, 1620)).toBe(460);
    expect(clampPanelPx(1400, 1620)).toBe(Math.floor((1620 * PANEL_MAX_PCT) / 100));
    expect(clampPanelPx(200, 1620)).toBe(PANEL_MIN_PX);
    // A pane too narrow for both keeps the floor: a panel too narrow to read is no panel.
    expect(clampPanelPx(700, 500)).toBe(PANEL_MIN_PX);
    expect(clampPanelPx(Number.NaN, 1620)).toBeNull();
  });

  it("is remembered in pixels, and nothing stored or a width under the floor is the stylesheet's own", () => {
    const kept = new Map<string, string>();
    const storage = { getItem: (k: string) => kept.get(k) ?? null, setItem: (k: string, v: string) => void kept.set(k, v) };
    expect(readPanelPx(storage)).toBeNull();
    rememberPanelPx(455.6, storage);
    expect(kept.get("daedalus.width.panel")).toBe("456");
    expect(readPanelPx(storage)).toBe(456);
    // The share of the chat an older build kept here reads as "not dragged".
    kept.set("daedalus.width.panel", "42");
    expect(readPanelPx(storage)).toBeNull();
    const broken = { getItem: () => { throw new Error("private"); }, setItem: () => { throw new Error("private"); } };
    expect(() => rememberPanelPx(500, broken)).not.toThrow();
    expect(readPanelPx(broken)).toBeNull();
  });
});

describe("the tab a session opens on", () => {
  it("is the Overview on a wide window, nothing on a narrow one, until the operator chooses", () => {
    expect(defaultPanelTab(null, PANEL_OPEN_MIN)).toBe("overview");
    expect(defaultPanelTab(null, PANEL_OPEN_MIN - 1)).toBeNull();
    expect(defaultPanelTab("files", 1440)).toBe("files");
    expect(defaultPanelTab("files|files,notes", 1440)).toBe("files");
    expect(defaultPanelTab("files", 1100)).toBeNull();
    expect(defaultPanelTab("0", 2560)).toBeNull();
    expect(defaultPanelTab("sideways", 1440)).toBe("overview");
  });

  it("remembers the tab and the open tools, closed included", () => {
    const kept = new Map<string, string>();
    const storage = { getItem: (k: string) => kept.get(k) ?? null, setItem: (k: string, v: string) => void kept.set(k, v) };
    rememberPanel({ tab: "jobs", open: ["changes", "jobs"] }, storage);
    expect(kept.get("daedalus.session.panel")).toBe("jobs|changes,jobs");
    expect(readPanelState(1440, storage)).toEqual({ tab: "jobs", open: ["changes", "jobs"] });
    rememberPanel({ tab: null, open: ["changes", "jobs"] }, storage);
    expect(readPanelState(1440, storage)).toEqual({ tab: null, open: ["changes", "jobs"] });
  });

  it("reads a bare tab as that one tool open, and drops what is not a tool", () => {
    expect(parseStoredPanel("details")).toEqual({ tab: "details", open: ["details"] });
    expect(parseStoredPanel("overview|changes,nonsense,changes")).toEqual({ tab: "overview", open: ["changes"] });
    expect(parseStoredPanel("0|notes")).toEqual({ tab: "0", open: ["notes"] });
    expect(storedPanel({ tab: "overview", open: [] })).toBe("overview");
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

  it("offers Preview only once something was opened", () => {
    expect(tabsFor("orchestrator")).not.toContain("preview");
    expect(tabsFor("orchestrator", { preview: true })).toEqual(["overview", "questions", "files", "preview", "context", "notes", "board", "brief", "wakeups", "folders", "details", "usage"]);
    expect(routeTabsFor("orchestrator")).toContain("preview");
  });
});

describe("the Questions tab of an ordinary session", () => {
  it("is there only while side questions wait, first, and a link to it always opens it", () => {
    expect(tabsFor("session")).not.toContain("questions");
    expect(tabsFor("session", { questions: true }).slice(0, 3)).toEqual(["overview", "questions", "files"]);
    expect(tabsFor("member", { questions: true })).not.toContain("questions");
    expect(routeTabsFor("session")).toContain("questions");
  });
});

describe("the Agents tab", () => {
  it("is there only for a chat that talks to another agent, before Notes, and a link to it always opens it", () => {
    expect(tabsFor("session")).not.toContain("agents");
    expect(tabsFor("session", { agents: true })).toEqual(["overview", "files", "preview", "jobs", "agents", "context", "memory", "notes", "canvas", "attachments", "details", "usage"]);
    expect(tabsFor("session", { subagents: true, agents: true })).toContain("subagents");
    expect(routeTabsFor("session")).toContain("agents");
  });
});

describe("the Browser tab", () => {
  it("is offered only to a session that has had a browser, after the agent's own tabs", () => {
    expect(tabsFor("session")).not.toContain("browser");
    expect(tabsFor("session", { browser: true }).slice(0, 6)).toEqual(["overview", "files", "preview", "jobs", "browser", "context"]);
    expect(tabsFor("member", { browser: true })).toContain("browser");
    expect(tabsFor("main", { browser: true }).slice(0, 6)).toEqual(["overview", "questions", "files", "preview", "jobs", "browser"]);
    expect(tabsFor("orchestrator", { browser: true })).toEqual(["overview", "questions", "files", "browser", "context", "notes", "board", "brief", "wakeups", "folders", "details", "usage"]);
  });

  it("gives the orchestrator its Details and a Files tab to read the project by, and no jobs it does not have", () => {
    expect(tabsFor("orchestrator")).toEqual(["overview", "questions", "files", "context", "notes", "board", "brief", "wakeups", "folders", "details", "usage"]);
    expect(tabsFor("orchestrator")).not.toContain("jobs");
  });

  it("opens from a link before the listing says there is a browser", () => {
    const q = readPanelQuery(new URLSearchParams("panel=browser"), routeTabsFor("session"));
    expect(q).toEqual({ tab: "browser", path: null });
    expect(applyPanelQuery(PANEL_CLOSED, q, base).tab).toBe("browser");
    expect(panelQuery(openTab(PANEL_CLOSED, "browser"))).toEqual({ panel: "browser", path: null, lines: null, sub: null, task: null, tab: null });
  });

  it("is never where a session opens by itself: the next session may have no browser", () => {
    const kept = new Map<string, string>([["daedalus.session.panel", "browser"]]);
    const storage = { getItem: (k: string) => kept.get(k) ?? null };
    expect(readPanelTab(1440, storage)).toBe("overview");
  });
});

describe("the Subagents tab", () => {
  const base = "/api/files/0123456789ab";
  it("is offered only to a session that has helpers, first among the agent's tools", () => {
    expect(tabsFor("session")).not.toContain("subagents");
    expect(tabsFor("session", { subagents: true }).slice(4, 7)).toEqual(["subagents", "context", "memory"]);
    expect(tabsFor("orchestrator", { subagents: true })).not.toContain("subagents");
  });
  it("opens from a link on one helper, before the listing says there are helpers", () => {
    const q = readPanelQuery(new URLSearchParams("panel=subagents&sub=child1"), routeTabsFor("session"));
    expect(q).toEqual({ tab: "subagents", path: null, sub: "child1" });
    const s = applyPanelQuery(PANEL_CLOSED, q, base);
    expect(s.tab).toBe("subagents");
    expect(s.sub).toBe("child1");
  });
  it("writes the helper it shows, and drops it on any other tab", () => {
    const s = openSubagent(PANEL_CLOSED, "child1");
    expect(panelQuery(s)).toEqual({ panel: "subagents", path: null, lines: null, sub: "child1", task: null, tab: null });
    expect(panelQuery(openSubagent(s, null)).sub).toBeNull();
    expect(panelQuery(openTab(s, "files")).sub).toBeNull();
  });
  it("goes back to the list when the link names no helper", () => {
    const s = openSubagent(PANEL_CLOSED, "child1");
    const back = applyPanelQuery(s, readPanelQuery(new URLSearchParams("panel=subagents"), routeTabsFor("session")), base);
    expect(back.sub).toBeNull();
  });
});

describe("how much of each tab the strip draws", () => {
  it("steps from the icon and the word to the icons alone", () => {
    expect(TAB_FITS).toEqual(["full", "active", "icons"]);
    expect(widestFit(TAB_FITS, (fit) => fit !== "full")).toBe("active");
    expect(widestFit(TAB_FITS, () => false)).toBe("icons");
  });
});

describe("the + menu", () => {
  it("lists the tools under their groups, the Overview in none, a project's board with the project", () => {
    expect(groupTools(tabsFor("session", { changes: true, board: true }), "session")).toEqual([
      { group: "work", tabs: ["changes", "files", "preview", "jobs"] },
      { group: "agent", tabs: ["board", "context", "memory"] },
      { group: "materials", tabs: ["notes", "canvas", "attachments"] },
      { group: "info", tabs: ["details", "usage"] },
    ]);
    expect(groupTools(tabsFor("orchestrator"), "orchestrator").find((g) => g.group === "project")?.tabs).toEqual(["board", "brief", "wakeups", "folders"]);
  });

  it("offers Changes only to a chat whose folder is a repository", () => {
    expect(tabsFor("session")).not.toContain("changes");
    expect(tabsFor("session", { changes: true })[1]).toBe("changes");
  });
});

describe("open files as tabs", () => {
  const a = { base, path: "a.md" };
  const b = { base, path: "b.md" };
  const c = { base, path: "c.md" };
  it("lists each open file once, in the order it was first opened", () => {
    const s = openFile(openFile(openFile(openFile(PANEL_CLOSED, a), b), a), c);
    expect(openEntries(s).map((e) => e.path)).toEqual(["a.md", "b.md", "c.md"]);
  });
  it("shows a tab's file without growing the history", () => {
    const s = openFile(openFile(PANEL_CLOSED, a), b);
    const shown = showEntry(s, a);
    expect(currentEntry(shown)?.path).toBe("a.md");
    expect(shown.stack).toHaveLength(2);
  });
  it("closing the file on screen moves to the one before it, or the next when it was first", () => {
    const s = openFile(openFile(openFile(PANEL_CLOSED, a), b), c);
    expect(currentEntry(closeEntry(s, c))?.path).toBe("b.md");
    const first = showEntry(s, a);
    expect(currentEntry(closeEntry(first, a))?.path).toBe("b.md");
  });
  it("closing another file keeps the one on screen", () => {
    const s = openFile(openFile(openFile(PANEL_CLOSED, a), b), c);
    const closed = closeEntry(s, a);
    expect(currentEntry(closed)?.path).toBe("c.md");
    expect(openEntries(closed).map((e) => e.path)).toEqual(["b.md", "c.md"]);
  });
  it("closing the last file leaves nothing on screen", () => {
    const closed = closeEntry(openFile(PANEL_CLOSED, a), a);
    expect(closed.at).toBe(-1);
    expect(currentEntry(closed)).toBeNull();
  });
  it("opens a file beside the current one and stays on the current one", () => {
    const s = openBeside(openFile(PANEL_CLOSED, a), b);
    expect(currentEntry(s)?.path).toBe("a.md");
    expect(openEntries(s).map((e) => e.path)).toEqual(["a.md", "b.md"]);
    expect(openBeside(s, b)).toBe(s);
  });
});

describe("the pin", () => {
  const store = () => {
    const values = new Map<string, string>();
    return { getItem: (k: string) => values.get(k) ?? null, setItem: (k: string, v: string) => void values.set(k, v) };
  };
  it("opens a pinned panel in every chat on any window that draws it as a column", () => {
    expect(defaultPanelTab("0", 1100, undefined, true)).toBe("overview");
    expect(defaultPanelTab("files", 1100, undefined, true)).toBe("files");
    expect(defaultPanelTab("files", 900, undefined, true)).toBeNull();
  });
  it("is remembered on the device, and a closed pinned panel keeps its last tab", () => {
    const storage = store();
    expect(readPanelPinned(storage)).toBe(false);
    rememberPanelPinned(true, storage);
    rememberPanel({ tab: "jobs", open: ["jobs"] }, storage);
    rememberPanel({ tab: null, open: ["jobs"] }, storage);
    expect(readPanelTab(1100, storage)).toBe("jobs");
    rememberPanelPinned(false, storage);
    rememberPanel({ tab: null, open: ["jobs"] }, storage);
    expect(readPanelTab(1440, storage)).toBeNull();
  });
});

describe("the chat's Board tab", () => {
  it("is offered to a chat and to the main chat, and is the project's board beside an orchestrator or a member", () => {
    expect(tabsFor("session")).not.toContain("board");
    expect(tabsFor("session", { board: true })).toContain("board");
    expect(tabsFor("main", { board: true })).toContain("board");
    // The project's own board is among the orchestrator's and the member's tools whatever the chat's offer says.
    expect(tabsFor("member", { board: true }).filter((tab) => tab === "board")).toHaveLength(1);
    expect(tabsFor("orchestrator").filter((tab) => tab === "board")).toHaveLength(1);
  });
  it("sits with the agent's tools in the menu, ahead of the helpers", () => {
    const groups = groupTools(tabsFor("session", { board: true, subagents: true }), "session");
    expect(groups.find((g) => g.group === "agent")!.tabs.slice(0, 2)).toEqual(["board", "subagents"]);
  });
  it("writes the task it shows, reads it back from a link, and drops it on any other tab", () => {
    const s = openTask(PANEL_CLOSED, "7f2a1c");
    expect(s.tab).toBe("board");
    expect(panelQuery(s)).toEqual({ panel: "board", path: null, lines: null, sub: null, task: "7f2a1c", tab: null });
    const q = readPanelQuery(new URLSearchParams("panel=board&task=7f2a1c"), routeTabsFor("session"));
    expect(q).toEqual({ tab: "board", path: null, task: "7f2a1c" });
    expect(applyPanelQuery(PANEL_CLOSED, q, base).task).toBe("7f2a1c");
    expect(panelQuery(openTask(s, null)).task).toBeNull();
    expect(panelQuery(openTab(s, "files")).task).toBeNull();
    expect(readPanelQuery(new URLSearchParams("panel=files&task=7f2a1c"))?.task).toBeUndefined();
  });
  it("forgets the task when its tab is closed", () => {
    expect(closeTool(openTask(PANEL_CLOSED, "7f2a1c"), "board").task).toBeNull();
  });
});
