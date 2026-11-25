// The right panel's state, with nothing of the browser in it: which tab is open, what the Preview
// tab has walked through, whether the panel covers the chat. The route carries the part a link
// should reproduce (`?panel=preview&path=reports/menu.md`), the browser remembers the width and the
// last open tab, and the component in panel.tsx is the only thing that turns either into pixels.

export type PanelTab = "overview" | "details" | "files" | "preview" | "jobs" | "notes" | "subagents" | "board" | "brief" | "wakeups" | "folders" | "questions" | "browser" | "sources" | "agents" | "changes" | "context" | "usage" | "memory" | "canvas" | "attachments";

/** Every tool a panel can hold, for a route or a stored state to be read against. */
export const ALL_TABS: readonly PanelTab[] = ["overview", "details", "files", "preview", "jobs", "notes", "subagents", "board", "brief", "wakeups", "folders", "questions", "browser", "sources", "agents", "changes", "context", "usage", "memory", "canvas", "attachments"];

/** A session's own tools: what the agent works on and with, what it was given and kept, and what it
 *  is and cost. The Overview is the panel's front page and is always there; the rest are opened as
 *  tabs from the "+" menu or from an Overview row, and closed again. */
export const PANEL_TABS: PanelTab[] = ["overview", "files", "preview", "jobs", "context", "memory", "notes", "canvas", "attachments", "details", "usage"];

/** A project's tools, beside a session in the project's focus mode. */
export const PROJECT_TABS: PanelTab[] = ["board", "brief", "wakeups", "folders"];

/** What a panel is beside: an ordinary session, a project's orchestrator, a session inside a
 *  project's focus mode (a staff member, or anyone else working in the project), or the main chat. */
export type PanelContext = "session" | "orchestrator" | "member" | "main";

export type ToolOffer = { browser?: boolean; preview?: boolean; subagents?: boolean; questions?: boolean; sources?: boolean; agents?: boolean; changes?: boolean; board?: boolean };

/**
 * The tools a panel offers, in the order the "+" menu and the Overview list them. The panel used to
 * draw every one of them as a fixed tab, and six to nine words in a 400 px column stood shoulder to
 * shoulder or fell back to bare icons; now only the Overview and what the operator opened are tabs,
 * and this list is the menu they are opened from.
 *
 * Some tools are offered only when there is something behind them, because an always-empty entry is
 * one more line to read past: Changes for a chat whose folder is a git repository (`changes`), the
 * Browser for a session that has had one, Sources once an answer cited any, Subagents for a session
 * with helpers, Agents for a chat that talks to another agent through a link, Questions while an
 * ordinary session's side questions wait (the orchestrator's and the main chat's always). Preview
 * is a session's always, and the orchestrator's once a file was opened there, so the file on screen
 * has a tab to be on.
 *
 * The orchestrator runs no job, so it has no Terminal; it writes no file, yet it has Files to read
 * the project and the staff's worktrees by. A session inside a project gains the project's tools.
 * The chat's own board (`board`) is offered where a page for it is mounted.
 */
export function tabsFor(context: PanelContext, opts: ToolOffer = {}): PanelTab[] {
  const work: PanelTab[] = [...(opts.changes ? (["changes"] as PanelTab[]) : []), "files", "preview", "jobs", ...(opts.browser ? (["browser"] as PanelTab[]) : [])];
  const agent: PanelTab[] = [...(opts.board && context !== "member" ? (["board"] as PanelTab[]) : []), ...(opts.subagents ? (["subagents"] as PanelTab[]) : []), ...(opts.agents ? (["agents"] as PanelTab[]) : []), "context", "memory", ...(opts.sources ? (["sources"] as PanelTab[]) : [])];
  const materials: PanelTab[] = ["notes", "canvas", "attachments"];
  const info: PanelTab[] = ["details", "usage"];
  if (context === "orchestrator") {
    return ["overview", "questions", ...(opts.changes ? (["changes"] as PanelTab[]) : []), "files", ...(opts.preview ? (["preview"] as PanelTab[]) : []), ...(opts.browser ? (["browser"] as PanelTab[]) : []), "context", ...(opts.sources ? (["sources"] as PanelTab[]) : []), "notes", ...PROJECT_TABS, ...info];
  }
  const questions: PanelTab[] = context === "main" || (context === "session" && opts.questions) ? ["questions"] : [];
  const project = context === "member" ? PROJECT_TABS : [];
  return ["overview", ...questions, ...work, ...agent, ...materials, ...project, ...info];
}

/** Every tool a route may name in this context. A link to `?panel=browser` must open the tab even
 *  before the listing that says the session has a browser has arrived. Preview is always among
 *  them: a file card in the orchestrator's chat opened Preview, wrote `?panel=preview` into the
 *  address, and the route, reading a tab it did not know, closed the panel again in the same
 *  moment — the first click fetched the file and showed nothing, and only a second click, landing
 *  on an address that no longer changed, kept it open. */
export function routeTabsFor(context: PanelContext): PanelTab[] {
  return tabsFor(context, { browser: true, preview: true, subagents: true, questions: true, sources: true, agents: true, changes: true, board: true });
}

/** The tab a panel opens on by itself: the Overview, except beside a project's orchestrator and the
 *  main chat, whose first business is what waits for the operator. */
export function firstTab(context: PanelContext): PanelTab {
  return context === "orchestrator" || context === "main" ? "questions" : "overview";
}

/** The groups the "+" menu lists the tools under. A project's board, beside its orchestrator or a
 *  member, is the project's; anywhere else the board is this chat's own and belongs with the agent. */
export type ToolGroup = "work" | "agent" | "materials" | "project" | "info";
export const TOOL_GROUPS: readonly ToolGroup[] = ["work", "agent", "materials", "project", "info"];

export function toolGroup(tab: PanelTab, context: PanelContext): ToolGroup | null {
  switch (tab) {
    case "changes": case "files": case "preview": case "jobs": case "browser": return "work";
    case "questions": case "subagents": case "agents": case "context": case "memory": case "sources": return "agent";
    case "board": return context === "orchestrator" || context === "member" ? "project" : "agent";
    case "notes": case "canvas": case "attachments": return "materials";
    case "brief": case "wakeups": case "folders": return "project";
    case "details": case "usage": return "info";
    default: return null;
  }
}

/** The offered tools under their groups, empty groups left out, the Overview in none. */
export function groupTools(tabs: readonly PanelTab[], context: PanelContext): { group: ToolGroup; tabs: PanelTab[] }[] {
  return TOOL_GROUPS.map((group) => ({ group, tabs: tabs.filter((tab) => toolGroup(tab, context) === group) })).filter((g) => g.tabs.length > 0);
}

/** One file the Preview tab showed: where the bytes come from, and the lines an answer cited, if any. */
export type PanelEntry = { base: string; path: string; lines?: string };

export type PanelState = {
  /** The tab on screen, or null while the panel is closed. */
  tab: PanelTab | null;
  /** The tools open as tabs after the Overview, in the order they were opened. Closing the panel
   *  keeps them: the next time it opens, the tabs the operator was working with are still there. */
  open: PanelTab[];
  /** What Preview has shown, oldest first; `at` points at the one on screen. */
  stack: PanelEntry[];
  at: number;
  /** Whether the panel covers the chat column. Closing the panel drops it. */
  expanded: boolean;
  /** The subagent whose conversation the Subagents tab shows, or null for the list of them. */
  sub: string | null;
};

export const PANEL_CLOSED: PanelState = { tab: null, open: [], stack: [], at: -1, expanded: false, sub: null };

export function isPanelTab(v: string | null | undefined, tabs: readonly PanelTab[] = ALL_TABS): v is PanelTab {
  return !!v && (tabs as readonly string[]).includes(v);
}

/** `tab` on screen, joined to the open tools when it is not among them yet. The Overview is never in
 *  the list: it is the panel's own first tab, not a tool that was opened. */
export function openTab(s: PanelState, tab: PanelTab): PanelState {
  const open = tab === "overview" || s.open.includes(tab) ? s.open : [...s.open, tab];
  return s.tab === tab && open === s.open ? s : { ...s, tab, open };
}

/** A tool's tab closed. The one on screen gives its place to the tab before it, as a browser does,
 *  or to the Overview when it was the first; Preview takes its history with it. */
export function closeTool(s: PanelState, tab: PanelTab): PanelState {
  const at = s.open.indexOf(tab);
  if (at < 0) return s;
  const open = s.open.filter((other) => other !== tab);
  const next = s.tab === tab ? (open[at - 1] ?? "overview") : s.tab;
  return { ...s, open, tab: next, ...(tab === "preview" ? { stack: [], at: -1 } : {}), ...(tab === "subagents" ? { sub: null } : {}) };
}

export function closePanel(s: PanelState): PanelState {
  return s.tab === null ? s : { ...s, tab: null, expanded: false };
}

/** The toggle the shortcut and the header button share: closed → the last tab (the Overview by default), open → closed. */
export function togglePanel(s: PanelState, last: PanelTab | null = null, first: PanelTab = "overview"): PanelState {
  return s.tab === null ? openTab(s, last ?? first) : closePanel(s);
}

/** A subagent opened beside its leader: the Subagents tab on its conversation; null goes back to the list. */
export function openSubagent(s: PanelState, sub: string | null): PanelState {
  return s.tab === "subagents" && s.sub === sub ? s : { ...openTab(s, "subagents"), sub };
}

export function toggleExpanded(s: PanelState): PanelState {
  return s.tab === null ? s : { ...s, expanded: !s.expanded };
}

/** A file opened into Preview: goes on top of the history, forgetting anything that was "forward". */
export function openFile(s: PanelState, entry: PanelEntry): PanelState {
  const current = s.stack[s.at];
  if (current && sameEntry(current, entry)) return openTab(s, "preview");
  const stack = [...s.stack.slice(0, s.at + 1), entry];
  return { ...openTab(s, "preview"), stack, at: stack.length - 1 };
}

/** A file opened next to the one on screen without taking its place: it joins the open files right after
 *  the current one, and the reader stays where they are. The Files tab's "Open beside" is this: in a wide
 *  panel the open files are tabs, and a reader comparing two files wants the second one a click away
 *  rather than in front of the first. A file already open is left where it is. */
export function openBeside(s: PanelState, entry: PanelEntry): PanelState {
  if (s.stack.some((e) => sameEntry(e, entry))) return s;
  if (s.at < 0) return openFile(s, entry);
  const stack = [...s.stack.slice(0, s.at + 1), entry, ...s.stack.slice(s.at + 1)];
  return { ...s, stack };
}

/** The open files, each once, in the order they were first opened: the tabs over the viewer. The
 *  history keeps every step (Back walks it), but a file read twice is one tab, not two. */
export function openEntries(s: PanelState): PanelEntry[] {
  const seen: PanelEntry[] = [];
  for (const entry of s.stack) if (!seen.some((e) => sameEntry(e, entry))) seen.push(entry);
  return seen;
}

/** Whether `entry` is the file on screen. */
export function isCurrent(s: PanelState, entry: PanelEntry): boolean {
  const current = currentEntry(s);
  return !!current && sameEntry(current, entry);
}

/** A file's tab closed: every step of the history that showed it goes, and the viewer moves to the
 *  step nearest the one it was on — the one before, as a browser does, or the next when it was first. */
export function closeEntry(s: PanelState, entry: PanelEntry): PanelState {
  const before = s.stack.slice(0, Math.max(0, s.at)).filter((e) => !sameEntry(e, entry)).length;
  const stack = s.stack.filter((e) => !sameEntry(e, entry));
  if (stack.length === s.stack.length) return s;
  const onIt = isCurrent(s, entry);
  const at = stack.length === 0 ? -1 : onIt ? Math.max(0, Math.min(stack.length - 1, before - 1 < 0 ? 0 : before - 1)) : Math.min(stack.length - 1, before);
  return { ...s, stack, at };
}

/** Makes an open file the one on screen without growing the history: a click on its tab. */
export function showEntry(s: PanelState, entry: PanelEntry): PanelState {
  if (isCurrent(s, entry)) return s.tab === "preview" || s.tab === "files" ? s : openTab(s, "preview");
  let at = -1;
  for (let i = s.stack.length - 1; i >= 0; i--) if (sameEntry(s.stack[i], entry)) { at = i; break; }
  return at < 0 ? openFile(s, entry) : { ...openTab(s, s.tab === "files" ? "files" : "preview"), at };
}

export function canGoBack(s: PanelState): boolean {
  return s.at > 0;
}

export function canGoForward(s: PanelState): boolean {
  return s.at >= 0 && s.at < s.stack.length - 1;
}

export function goBack(s: PanelState): PanelState {
  return canGoBack(s) ? { ...openTab(s, "preview"), at: s.at - 1 } : s;
}

export function goForward(s: PanelState): PanelState {
  return canGoForward(s) ? { ...openTab(s, "preview"), at: s.at + 1 } : s;
}

export function currentEntry(s: PanelState): PanelEntry | null {
  return s.stack[s.at] ?? null;
}

function sameEntry(a: PanelEntry, b: PanelEntry): boolean {
  return a.base === b.base && a.path === b.path && (a.lines ?? "") === (b.lines ?? "");
}

// ── the route ────────────────────────────────────────────────────────────────────────────────

export type PanelQuery = { tab: PanelTab; path: string | null; lines?: string; sub?: string | null };

/** What a link carries: the open tab, the previewed file, the subagent the Subagents tab shows.
 *  `tab=` is read as the tab too, so a link written as `?panel=files&tab=preview&path=…` opens
 *  Preview on that path. */
export function readPanelQuery(query: URLSearchParams, tabs: readonly PanelTab[] = ALL_TABS): PanelQuery | null {
  const tab = query.get("tab");
  const panel = query.get("panel");
  const which = isPanelTab(tab, tabs) ? tab : isPanelTab(panel, tabs) ? panel : null;
  if (!which) return null;
  const path = query.get("path");
  const lines = query.get("lines") ?? "";
  const sub = which === "subagents" ? query.get("sub") || null : null;
  return { tab: which, path: path || null, ...(/^\d+(?:-\d+)?$/.test(lines) ? { lines } : {}), ...(which === "subagents" ? { sub } : {}) };
}

/** The query parameters the state writes; null for the ones to drop. */
export function panelQuery(s: PanelState): Record<string, string | null> {
  const current = currentEntry(s);
  return { panel: s.tab, path: s.tab === "preview" && current ? current.path : null, lines: s.tab === "preview" ? current?.lines ?? null : null, sub: s.tab === "subagents" ? s.sub : null, tab: null };
}

/** The state a route describes, over what the pane already holds: the tab from the link, the file
 *  on top of the history when the link names one the pane is not already showing. */
export function applyPanelQuery(s: PanelState, q: PanelQuery | null, base: string): PanelState {
  if (!q) return closePanel(s);
  let next = q.tab === "subagents" ? openSubagent(s, q.sub ?? null) : openTab(s, q.tab);
  if (q.path && (q.tab === "preview" || !currentEntry(next))) {
    const current = currentEntry(next);
    if (!current || current.path !== q.path || current.lines !== q.lines) next = openFile(next, { base, path: q.path, ...(q.lines ? { lines: q.lines } : {}) });
    if (q.tab !== "preview") next = openTab(next, q.tab);
  }
  return next;
}

// ── the width ────────────────────────────────────────────────────────────────────────────────

/** The narrowest the panel is drawn: under it the Overview's rows and a diff's lines stop reading. */
export const PANEL_MIN_PX = 400;
/** The widest it may be dragged, as a share of the chat: the conversation keeps a third. */
export const PANEL_MAX_PCT = 65;

/** A dragged width held between the floor and the ceiling. With no room for both (a narrow pane) the
 *  floor wins: a panel too narrow to read is no panel. Null for nonsense, which leaves the default. */
export function clampPanelPx(px: number, areaWidth: number): number | null {
  if (!Number.isFinite(px) || px <= 0) return null;
  const ceiling = areaWidth > 0 ? Math.max(PANEL_MIN_PX, Math.floor((areaWidth * PANEL_MAX_PCT) / 100)) : Number.POSITIVE_INFINITY;
  return Math.round(Math.min(ceiling, Math.max(PANEL_MIN_PX, px)));
}

const WIDTH_KEY = "daedalus.width.panel";

/** The width the operator dragged the panel to, in pixels, or null for the stylesheet's own
 *  (`--panel-w`: 30 % of the window between 400 and 520 px). A share of the chat was kept here once;
 *  read as pixels it is under the floor and means "not dragged". */
export function readPanelPx(storage: Pick<Storage, "getItem"> | null = safeStorage()): number | null {
  try {
    const v = Number(storage?.getItem(WIDTH_KEY));
    return Number.isFinite(v) && v >= PANEL_MIN_PX ? Math.round(v) : null;
  } catch {
    return null;
  }
}

export function rememberPanelPx(px: number, storage: Pick<Storage, "setItem"> | null = safeStorage()): void {
  try {
    storage?.setItem(WIDTH_KEY, String(Math.round(px)));
  } catch {
    /* private mode: the width lasts for the visit */
  }
}

// ── the last open tab ────────────────────────────────────────────────────────────────────────

const TAB_KEY = "daedalus.session.panel";

/** Below this the panel is closed unless the route or the operator opens it. */
export const PANEL_OPEN_MIN = 1280;

/** Below this the panel is a sheet over the chat, never a column; a pinned panel does not open itself there. */
export const PANEL_COLUMN_MIN = 1024;

/** What is kept: the tab on screen ("0" for a closed panel) and, after a bar, the open tools —
 *  `files|files,notes`. A bare tab name is a state with that one tool open. */
export function parseStoredPanel(stored: string | null): { tab: string | null; open: PanelTab[] } {
  if (stored === null) return { tab: null, open: [] };
  const [tab, list = ""] = stored.split("|");
  const open = list.split(",").filter((name): name is PanelTab => isPanelTab(name) && name !== "overview");
  if (isPanelTab(tab) && tab !== "overview" && !open.includes(tab)) open.push(tab);
  return { tab, open: [...new Set(open)] };
}

export function storedPanel(s: Pick<PanelState, "tab" | "open">): string {
  return `${s.tab ?? "0"}${s.open.length ? `|${s.open.join(",")}` : ""}`;
}

/** Which tab a session opens on with nothing in the route: what was open last time, or the first tab
 *  on a window wide enough for both; "0" is the operator having closed it. A pinned panel opens in every
 *  chat, on any window that draws it as a column, whatever the last chat did with it: pinning is the
 *  operator saying the panel belongs beside every conversation, and a panel that closed itself on a
 *  1200 px laptop because the window was under the auto-open width would undo that. */
export function defaultPanelTab(stored: string | null, viewportWidth: number, tabs: readonly PanelTab[] = PANEL_TABS, pinned = false): PanelTab | null {
  const tab = parseStoredPanel(stored).tab;
  if (pinned && viewportWidth >= PANEL_COLUMN_MIN) return isPanelTab(tab, tabs) ? tab : tabs[0];
  if (tab === "0") return null;
  if (isPanelTab(tab, tabs)) return viewportWidth >= PANEL_OPEN_MIN ? tab : null;
  return viewportWidth >= PANEL_OPEN_MIN ? tabs[0] : null;
}

/** Where the last open tab is kept. A project's focus mode keeps its own: the board it left open
 *  beside the orchestrator is not a tab an ordinary session has. */
export function panelTabKey(context: PanelContext): string {
  return context === "session" ? TAB_KEY : context === "main" ? `${TAB_KEY}.main` : `${TAB_KEY}.project`;
}

/** The tools to draw a tab for in the order of the default tools: `tabs` with what a context always
 *  shows first (its first tab) and the Overview leading. */
function defaultTabs(context: PanelContext): PanelTab[] {
  const tabs = tabsFor(context);
  const first = firstTab(context);
  return [first, ...tabs.filter((tab) => tab !== first)];
}

/** The state a session opens with: the tab and the tools as they were left. A tool kept from a chat
 *  that had it is offered or left out by the panel that draws the tabs. */
export function readPanelState(viewportWidth: number, storage: Pick<Storage, "getItem"> | null = safeStorage(), context: PanelContext = "session"): Pick<PanelState, "tab" | "open"> {
  let stored: string | null = null;
  try {
    stored = storage?.getItem(panelTabKey(context)) ?? null;
  } catch {
    /* private mode */
  }
  const kept = parseStoredPanel(stored);
  const firstOpen = context === "orchestrator" || context === "main" ? ["questions" as PanelTab] : [];
  const open = stored === null ? firstOpen : kept.open;
  return { tab: defaultPanelTab(stored, viewportWidth, defaultTabs(context), readPanelPinned(storage)), open };
}

export function readPanelTab(viewportWidth: number, storage: Pick<Storage, "getItem"> | null = safeStorage(), context: PanelContext = "session"): PanelTab | null {
  return readPanelState(viewportWidth, storage, context).tab;
}

/** Remembers the open tab and tools, the tab "0" for a panel the operator closed. A pinned panel keeps
 *  its last tab when it is closed in one chat, so the next chat still opens it on that tab rather than
 *  on the Overview. */
export function rememberPanel(s: Pick<PanelState, "tab" | "open">, storage: (Pick<Storage, "setItem"> & Partial<Pick<Storage, "getItem">>) | null = safeStorage(), context: PanelContext = "session"): void {
  let tab = s.tab;
  if (tab === null && storage?.getItem && readPanelPinned(storage as Pick<Storage, "getItem">)) {
    let kept: string | null = null;
    try {
      kept = storage.getItem(panelTabKey(context));
    } catch {
      /* private mode */
    }
    tab = (parseStoredPanel(kept).tab as PanelTab | null) ?? null;
    if (tab === ("0" as PanelTab)) tab = null;
  }
  try {
    storage?.setItem(panelTabKey(context), storedPanel({ tab, open: s.open }));
  } catch {
    /* private mode */
  }
}

// ── the pin ──────────────────────────────────────────────────────────────────────────────────

const PIN_KEY = "daedalus.panel.pinned";

/** Whether the operator pinned the panel open in every chat. Kept per device: a phone draws the panel
 *  as a sheet and a pin made at a wide monitor means nothing there. */
export function readPanelPinned(storage: Pick<Storage, "getItem"> | null = safeStorage()): boolean {
  try {
    return storage?.getItem(PIN_KEY) === "1";
  } catch {
    return false;
  }
}

export function rememberPanelPinned(pinned: boolean, storage: Pick<Storage, "setItem"> | null = safeStorage()): void {
  try {
    storage?.setItem(PIN_KEY, pinned ? "1" : "0");
  } catch {
    /* private mode: the pin lasts for the visit */
  }
}

function safeStorage(): Storage | null {
  try {
    return typeof localStorage === "undefined" ? null : localStorage;
  } catch {
    return null;
  }
}

/**
 * How much of each tab the strip can draw, widest first: the icon and the word, the word on the open
 * tab only (the others as their icons, named by their tooltip), the icons alone. The strip takes the
 * first that fits, measured, never a fixed count of tabs, so no label is ever cut or pressed against
 * its neighbour or the edge.
 */
export const TAB_FITS = ["full", "active", "icons"] as const;
export type TabFit = (typeof TAB_FITS)[number];

/** The widest fit of `order` that `fits` accepts, or the last when none does: the icons are drawn even
 *  when they overflow (the strip then scrolls), because there is nothing narrower to fall back to. */
export function widestFit(order: readonly TabFit[], fits: (fit: TabFit) => boolean): TabFit {
  return order.find(fits) ?? order[order.length - 1];
}

// ── the keyboard ─────────────────────────────────────────────────────────────────────────────

/** `⌘.` / `Ctrl+.` toggles the panel; with Shift it covers the chat. Anything else is not ours. */
export function panelShortcut(e: Pick<KeyboardEvent, "key" | "code" | "metaKey" | "ctrlKey" | "altKey" | "shiftKey">): "toggle" | "expand" | null {
  if (!(e.metaKey || e.ctrlKey) || e.altKey) return null;
  if (e.key !== "." && e.code !== "Period") return null;
  return e.shiftKey ? "expand" : "toggle";
}

// ── the breadcrumb ───────────────────────────────────────────────────────────────────────────

/** The folders of a path, then the file: `reports/menu-check.md` → `["reports", "menu-check.md"]`. */
export function crumbsOf(path: string): string[] {
  return path.split("/").filter(Boolean);
}
