// The right panel: the session's workspace beside the conversation, at the window's full height. Its
// tabs are the Overview, always first, and the tools the operator opened, each closable; the "+" opens
// any other tool the chat has, grouped and searchable, and the Overview lists the same tools as rows,
// each with what is behind it. A drag handle on its left edge, a menu for pinning and covering the chat,
// and close. On a desktop it is a column beside the conversation; on a phone the same tabs in a
// full-height sheet. The state is a value (panel.ts); this file draws it and wires the pointer and keys.

import { useCallback, useEffect, useId, useLayoutEffect, useMemo, useRef, useState, type ReactNode, type RefObject } from "react";
import { flushSync } from "react-dom";
import { Sheet, toast as statusToast, useLayer } from "./ui/dialogs";
import { OverflowMenu, Popover } from "./ui/primitives";
import { useEdgeFade, useMoreBelow } from "./edgefade";
import { Icon, IconName } from "./icons";
import {
  PANEL_CLOSED,
  TAB_FITS,
  widestFit,
  type TabFit,
  PANEL_TABS,
  PanelContext,
  PanelEntry,
  PanelState,
  PanelTab,
  applyPanelQuery,
  canGoBack,
  canGoForward,
  clampPanelPx,
  closePanel,
  closeEntry,
  closeTool,
  crumbsOf,
  currentEntry,
  firstTab,
  groupTools,
  isCurrent,
  openBeside,
  openEntries,
  readPanelPinned,
  rememberPanelPinned,
  showEntry,
  goBack,
  goForward,
  openFile,
  openSubagent,
  openTask,
  openTab,
  panelQuery,
  readPanelPx,
  readPanelQuery,
  readPanelState,
  rememberPanel,
  rememberPanelPx,
  routeTabsFor,
  toggleExpanded,
  togglePanel,
} from "./panel";
import { navigate, pathFor } from "./router";
import { PreviewSource, Viewer, ViewerInfo } from "./preview";
import { HtmlNavigation } from "./htmlpreview";
import { SPLIT_MIN, TREE_W, TREE_W_MIN, TREE_W_MAX } from "./explorer";
import { PaneHandle, clampWidth, pixelDrag, usePaneWidth, type PaneDrag } from "./layout";
import { t } from "./i18n";
import { SnapshotButton } from "./annotate/Annotate";
import { previewSnapshot } from "./annotate/source";
import { fileIcon } from "./artifact";
import "./panel.css";

export type PanelHostProps = {
  state: PanelState;
  onTab: (tab: PanelTab) => void;
  /** A tool's tab closed with its ×, Delete or a middle click. */
  onCloseTool?: (tab: PanelTab) => void;
  onClose: () => void;
  onExpand: () => void;
  onBack: () => void;
  onForward: () => void;
  /** The root crumb: the project's name, or "workspace". */
  root: string;
  /** Where the current file downloads from, for open-in-new. */
  downloadUrl: (entry: PanelEntry) => string;
  /** The tools offered, in order, the Overview first (panel.ts, `tabsFor`). */
  tabs?: PanelTab[];
  /** Tools drawn as tabs whether or not they were opened, while they want the operator (questions
   *  waiting): a tab that needs an answer is not one the operator has to go and find. */
  attention?: PanelTab[];
  /** Tools drawn as tabs after the opened ones while they hold something, though nobody opened them
   *  and nothing waits on an answer: the chat's own board while it has open tasks. They never take
   *  the active tab and never open a closed panel, which is why a tool that wants an answer is not
   *  one of them. */
  alongside?: PanelTab[];
  /** The Details, Files and Terminal tabs, rendered by the screen that owns their data. */
  details?: (ids: string) => ReactNode;
  files?: ReactNode;
  jobs?: ReactNode;
  /** Every other tool's body: the Overview, the chat's changes, its context and spend, what it kept and
   *  was given, and a project's board, brief, wake-ups and folders in its focus mode. */
  pages?: Partial<Record<PanelTab, ReactNode>>;
  /** A number on a tab: helpers working, jobs running, notes kept; or a short figure of its own,
   *  the board's "3/5". */
  badges?: Partial<Record<PanelTab, number | string>>;
  /** A dot on a tab instead of a number: the agent busy in its browser, or waiting for the operator. */
  marks?: Partial<Record<PanelTab, "busy" | "attn">>;
  /** What the "+" menu says beside a tool that is not open: its count or its share ("3/5", "21%"). */
  figures?: Partial<Record<PanelTab, string>>;
  /** Phones: the tabs in a full sheet instead of a column. */
  sheet?: boolean;
  /** Desktop: what dragging the left edge does. */
  drag?: PaneDrag;
  /** Whether the panel stays open in every chat, and the switch for it. */
  pinned?: boolean;
  onPin?: () => void;
  /** The open files' tabs: show one, close one. */
  onShowEntry?: (entry: PanelEntry) => void;
  onCloseEntry?: (entry: PanelEntry) => void;
  /** Where the panel is, for the tab a tool's close falls back to. */
  context?: PanelContext;
};

/** What the panel keeps for itself: a reload counter for the viewer, the phone-width toggle,
 *  viewer info, HTML navigation, the closing state, and its id. The tab and body prefix uses that
 *  same id — two panels are open at once on a wide screen, and an id that is not this panel's own
 *  would point a reader's screen reader at the other one. */
type Local = { gen: number; reload: () => void; narrow: boolean; toggleNarrow: () => void; info: ViewerInfo | null; setInfo: (info: ViewerInfo) => void; nav: HtmlNavigation | null; setNav: (nav: HtmlNavigation | null) => void; id: string; ids: string; closing: boolean; wide: boolean; setWide: (wide: boolean) => void; tree: boolean; toggleTree: () => void };
type HostProps = PanelHostProps & { local: Local };

export function Panel(props: PanelHostProps) {
  const { state, sheet } = props;
  const [last, setLast] = useState(state);
  useEffect(() => {
    if (state.tab !== null) { setLast(state); return; }
    const delay = window.matchMedia("(prefers-reduced-motion: reduce)").matches ? 0 : 180;
    const timer = window.setTimeout(() => setLast(state), delay);
    return () => window.clearTimeout(timer);
  }, [state]);
  const shown = state.tab === null ? last : state;
  const shownProps = { ...props, state: shown };
  const [gen, setGen] = useState(0);
  const [narrow, setNarrow] = useState(false);
  const [info, setInfo] = useState<ViewerInfo | null>(null);
  const [nav, setNav] = useState<HtmlNavigation | null>(null);
  const [wide, setWide] = useState(false);
  const [tree, setTree] = useState(readTreeShown);
  const id = useId();
  const toggleTree = () => setTree((shown) => { rememberTreeShown(!shown); return !shown; });
  const local: Local = { closing: state.tab === null, info, setInfo, nav, setNav, id, ids: id, gen, reload: () => setGen((g) => g + 1), narrow, toggleNarrow: () => setNarrow((n) => !n), wide, setWide, tree, toggleTree };
  if (shown.tab === null) return null;
  if (sheet) {
    return (
      <Sheet size="full" className={`panel-sheet ${local.closing ? "panel-closing" : ""}`} ariaLabel={t("panel.label")} onClose={props.onClose} head={<Tabs {...shownProps} local={local} inSheet />}>
        <FileTabs {...shownProps} local={local} />
        <Toolbar {...shownProps} local={local} />
        <Body {...shownProps} local={local} />
      </Sheet>
    );
  }
  return <Column {...shownProps} local={local} />;
}

/** The desktop host: a column with the handle, a layer for Escape (expanded → restore, then close). */
function Column(props: HostProps) {
  const { state } = props;
  const box = useRef<HTMLElement>(null);
  const [visible, setVisible] = useState(false);
  useEffect(() => {
    const raf = requestAnimationFrame(() => setVisible(true));
    return () => cancelAnimationFrame(raf);
  }, []);
  // Escape reaches the panel only when nothing above it is open (a sheet, a menu) and the reader is
  // not in a text field elsewhere — the composer's own Escape (clearing a slash command) stays its own.
  useLayer(() => {
    const active = document.activeElement as HTMLElement | null;
    const typing = !!active && (active.tagName === "INPUT" || active.tagName === "TEXTAREA" || active.isContentEditable);
    if (typing && !box.current?.contains(active)) return;
    if (state.expanded) props.onExpand();
    else props.onClose();
  });
  return (
    <aside ref={box} inert={props.local.closing} className={`panel ${props.local.closing ? "panel-closing" : ""} ${state.expanded ? "full" : ""} ${visible ? "shown" : ""} ${props.local.narrow ? "phone-width" : ""}`} aria-label={t("panel.label")}>
      {props.drag && !state.expanded && <PaneHandle side="right" drag={props.drag} />}
      <Tabs {...props} />
      {!splitOf(props) && <FileTabs {...props} />}
      {!splitOf(props) && <Toolbar {...props} />}
      <Body {...props} />
    </aside>
  );
}

export const TAB_ICON: Record<PanelTab, IconName> = {
  overview: "grid", details: "info", files: "folder", preview: "eye", jobs: "activity", notes: "note", subagents: "spawn", board: "board", brief: "pen", wakeups: "clock", folders: "folder",
  questions: "ask", browser: "globe", sources: "link", agents: "bots", changes: "changes", context: "gauge", usage: "coin", memory: "brain", canvas: "pen", attachments: "attach",
};

/** The tabs drawn: the Overview, the tools that want the operator, then the ones opened and the ones
 *  that hold something, each once and only while the chat still offers it (a kept Changes tab in a chat
 *  that is not a repository is not drawn). */
export function shownTabs(state: Pick<PanelState, "open" | "tab">, offered: readonly PanelTab[], attention: readonly PanelTab[] = [], alongside: readonly PanelTab[] = []): PanelTab[] {
  const out: PanelTab[] = ["overview"];
  for (const tab of [...attention, ...state.open, ...alongside, ...(state.tab ? [state.tab] : [])]) {
    if (!out.includes(tab) && offered.includes(tab)) out.push(tab);
  }
  // A tab on screen that the chat does not list (a link naming it before the listing arrived) is
  // still drawn, so the body under it has a tab to belong to.
  if (state.tab && !out.includes(state.tab)) out.push(state.tab);
  return offered.includes("overview") ? out : out.filter((tab) => tab !== "overview");
}

/** Which fit the strip draws: the widest that fits, chosen again whenever the strip's width or what it
 *  holds changes. Every fit is tried on the strip's own class list and measured in one go, and React
 *  hears only the answer, and only when it differs from what is drawn.
 *
 *  It used to step through the fits by state: a new width reset the fit to the widest with flushSync,
 *  and a layout effect stepped one fit narrower per commit until the strip stopped overflowing. That is
 *  two commits or more for every change of width, and dragging the sidebar's or the panel's edge
 *  changes the strip's width on every frame, so the drag committed React twice a move and lagged the
 *  pointer (check_pane_resize.py). The resize observer reports once a frame, after layout and before the
 *  paint, which is both the coalescing and the moment to measure; flushSync is kept there so a changed
 *  fit is drawn in the same frame and the overflowing one never paints. */
function useTabFit(strip: RefObject<HTMLDivElement | null>, content: string): TabFit {
  const [fit, setFit] = useState<TabFit>(TAB_FITS[0]);
  const drawn = useRef(fit);
  drawn.current = fit;
  const measure = useCallback((): TabFit => {
    const el = strip.current;
    // A strip that is not laid out (a hidden sheet) overflows at every fit: keep what is drawn.
    if (!el || !el.clientWidth) return drawn.current;
    const draw = (which: TabFit) => {
      for (const other of TAB_FITS) el.classList.toggle(`fit-${other}`, other === which);
    };
    // The row the strip sits in: the strip may take what the "+" and the panel's own buttons leave.
    const row = el.parentElement;
    const chosen = widestFit(TAB_FITS, (which) => {
      draw(which);
      return el.scrollWidth <= el.clientWidth + 1 && (!row || row.scrollWidth <= row.clientWidth + 1);
    });
    // Hand the class list back as React drew it, so React's own record of it stays true.
    draw(drawn.current);
    return chosen;
  }, [strip]);
  // What the strip holds changed (a tab, a badge, the language): choose again before the paint.
  useLayoutEffect(() => {
    const chosen = measure();
    if (chosen !== drawn.current) setFit(chosen);
  }, [measure, content]);
  useEffect(() => {
    const el = strip.current?.parentElement;
    if (!el || typeof ResizeObserver === "undefined") return;
    let width = Math.round(el.clientWidth);
    const observer = new ResizeObserver(([entry]) => {
      const now = Math.round(entry.contentRect.width);
      if (now === width) return;
      width = now;
      const chosen = measure();
      if (chosen !== drawn.current) flushSync(() => setFit(chosen));
    });
    observer.observe(el);
    return () => observer.disconnect();
  }, [strip, measure]);
  return fit;
}

function Tabs(props: HostProps & { inSheet?: boolean }) {
  const { state, onTab, onCloseTool, onClose, onExpand, badges, marks, inSheet, local, pinned, onPin, tabs = PANEL_TABS, attention, alongside } = props;
  const strip = useRef<HTMLDivElement>(null);
  const shown = shownTabs(state, tabs, attention, alongside);
  // What the strip holds and how long its words are: a new tab, a badge, the open tab, the language.
  const fit = useTabFit(strip, `${shown.join()}|${state.tab}|${JSON.stringify(badges ?? {})}|${shown.map((tab) => t(`panel.tab.${tab}`)).join()}`);
  useEdgeFade(strip, state.tab);
  useEffect(() => {
    strip.current?.querySelector<HTMLElement>(".panel-tab.on")?.scrollIntoView?.({ block: "nearest", inline: "nearest" });
  }, [state.tab]);
  const closable = (tab: PanelTab) => !!onCloseTool && tab !== "overview" && state.open.includes(tab);
  const onKey = (e: React.KeyboardEvent) => {
    const i = shown.indexOf(state.tab!);
    if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
      const next = shown[(i + (e.key === "ArrowRight" ? 1 : shown.length - 1)) % shown.length];
      onTab(next);
      strip.current?.querySelector<HTMLElement>(`[data-tab="${next}"]`)?.focus();
      e.preventDefault();
    } else if ((e.key === "Delete" || e.key === "Backspace") && state.tab && closable(state.tab)) {
      onCloseTool!(state.tab);
      e.preventDefault();
      window.requestAnimationFrame(() => strip.current?.querySelector<HTMLElement>(".panel-tab.on")?.focus());
    }
  };
  const addButton = useRef<HTMLButtonElement>(null);
  const [adding, setAdding] = useState(false);
  const menu = [
    ...(onPin ? [{ label: t(pinned ? "panel.unpin" : "panel.pin"), icon: "pushpin" as IconName, checked: !!pinned, onSelect: onPin }] : []),
    { label: t(state.expanded ? "panel.restore" : "panel.expand"), icon: (state.expanded ? "panel" : "expand") as IconName, kbd: state.expanded ? "Esc" : "Ctrl ⇧ .", onSelect: onExpand },
  ];
  return (
    <div className="panel-tabs">
      <div ref={strip} className={`panel-tablist fit-${fit}`} data-fit={fit} role="tablist" aria-label={t("panel.label")} onKeyDown={onKey}>
        {shown.map((tab) => {
          const n = badges?.[tab] ?? 0;
          const label = t(`panel.tab.${tab}`);
          const on = state.tab === tab;
          // The name rides on the button itself: a narrow panel draws the tab as its icon alone, and
          // with the word hidden the tab was announced, and hovered, as nothing but its count.
          return (
            <span key={tab} className={`panel-tabwrap ${on ? "on" : ""} ${closable(tab) && on ? "closable" : ""}`} role="presentation">
              <button role="tab" id={`${local.ids}-tab-${tab}`} aria-controls={`${local.ids}-body`} data-tab={tab} className={`panel-tab ${on ? "on" : ""}`} aria-selected={on} aria-label={label} title={label} tabIndex={on ? 0 : -1} onClick={() => onTab(tab)}
                onAuxClick={(e) => { if (e.button === 1 && closable(tab)) { e.preventDefault(); onCloseTool!(tab); } }}>
                <Icon name={TAB_ICON[tab]} size={16} />
                <span className="panel-tab-word">{label}</span>
                {n !== 0 && <span className={`count ${tab === "questions" ? "attn" : ""}`}>{n}</span>}
                {marks?.[tab] && <span className={`tab-mark ${marks[tab]}`} aria-hidden="true" />}
              </button>
              {closable(tab) && on && <button type="button" className="panel-tab-close" tabIndex={-1} aria-label={t("panel.tab.close", { name: label })} title={t("panel.tab.close", { name: label })} onClick={() => onCloseTool!(tab)}><Icon name="close" size={12} /></button>}
            </span>
          );
        })}
      </div>
      <button ref={addButton} type="button" className={`iconbtn panel-add ${adding ? "on" : ""}`} aria-label={t("panel.add")} title={t("panel.add.title")} aria-haspopup="menu" aria-expanded={adding} onClick={() => setAdding((open) => !open)}>
        <Icon name="plus" size={16} />
      </button>
      {adding && <ToolMenu {...props} anchor={addButton.current} onClose={() => setAdding(false)} />}
      <span className="grow" />
      {!inSheet && (
        <div className="panel-actions">
          <OverflowMenu items={menu} label={t("panel.menu")} className="panel-menu" />
          <button className="iconbtn panel-close" onClick={onClose} aria-label={t("panel.close")} title={t("panel.close")}>
            <Icon name="close" size={16} />
          </button>
        </div>
      )}
    </div>
  );
}

/** The "+" menu: every tool the chat can open in the panel, under its group, with a search over the
 *  names. An open tool says so; one that is not says what is behind it, as its tab's figure would. */
function ToolMenu({ tabs = PANEL_TABS, state, onTab, figures, badges, context = "session", anchor, onClose }: HostProps & { anchor: HTMLElement | null; onClose: () => void }) {
  const [query, setQuery] = useState("");
  const groups = useMemo(() => {
    const words = query.trim().toLowerCase();
    return groupTools(tabs.filter((tab) => tab !== "overview"), context)
      .map((g) => ({ ...g, tabs: g.tabs.filter((tab) => !words || toolName(tab, context).toLowerCase().includes(words) || t(`panel.tab.${tab}`).toLowerCase().includes(words)) }))
      .filter((g) => g.tabs.length > 0);
  }, [tabs, context, query]);
  const pick = (tab: PanelTab) => { onClose(); onTab(tab); };
  return (
    <Popover anchor={anchor} onClose={onClose} label={t("panel.add.menu")} side="bottom" align="start" width={300} className="panel-tools">
      <label className="panel-tools-search">
        <Icon name="search" size={16} />
        <input type="text" className="model-search" value={query} placeholder={t("panel.add.menu")} aria-label={t("panel.add.menu")} onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter" && groups[0]?.tabs[0]) { e.preventDefault(); pick(groups[0].tabs[0]); } }} />
      </label>
      {groups.length === 0 && <div className="panel-tools-none">{t("panel.add.none")}</div>}
      {groups.map((g) => (
        <div key={g.group} role="group" aria-label={t(`panel.group.${g.group}`)}>
          <div className="panel-tools-group">{t(`panel.group.${g.group}`)}</div>
          {g.tabs.map((tab) => {
            const open = state.open.includes(tab) || state.tab === tab;
            const hint = open ? t("panel.add.open") : figures?.[tab] ?? (badges?.[tab] ? String(badges[tab]) : "");
            return (
              <button key={tab} type="button" role="menuitem" className={`menu-row panel-tools-row ${open ? "open" : ""}`} data-tool={tab} onClick={() => pick(tab)}>
                <Icon name={TAB_ICON[tab]} size={16} />
                <span className="grow">{toolName(tab, context)}</span>
                {hint && <span className="panel-tools-hint">{hint}</span>}
              </button>
            );
          })}
        </div>
      ))}
    </Popover>
  );
}

/** A tool's name in the menu, which may say more than its tab has room for: the chat's own board. */
function toolName(tab: PanelTab, context: PanelContext): string {
  if (tab === "board" && context !== "orchestrator" && context !== "member") return t("panel.tool.board");
  if (tab === "browser") return t("panel.tool.browser");
  return t(`panel.tab.${tab}`);
}

/** The Preview tab's toolbar: history, the breadcrumb, open-in-new, phone width, download. The
 *  other tabs carry their own controls in their bodies and draw no toolbar. */
function Toolbar(props: HostProps) {
  const { state, onBack, onForward, onTab, root, downloadUrl, sheet, local, files } = props;
  const entry = currentEntry(state);
  if (state.tab !== "preview" && !splitOf(props)) return null;
  const html = local.info?.kind === "html" ? local.nav : null;
  const crumbs = entry ? crumbsOf(html?.path ?? entry.path) : [];
  // The tree beside the file is offered only where it could be drawn: a panel wide enough for both.
  const treeable = files !== undefined && local.wide && !sheet;
  return (
    <div className="panel-toolbar">
      {treeable && (
        <button className={`iconbtn small panel-tree-toggle ${local.tree ? "on" : ""}`} aria-pressed={local.tree} onClick={local.toggleTree} aria-label={t(local.tree ? "panel.tree.hide" : "panel.tree.show")} title={t(local.tree ? "panel.tree.hide" : "panel.tree.show")}><Icon name="sidebar" size={16} /></button>
      )}
      <button className="iconbtn small" onClick={html ? html.back ?? undefined : onBack} disabled={html ? !html.back : !canGoBack(state)} aria-label={t("panel.back")} title={t("panel.back")}><Icon name="back" size={16} /></button>
      <button className="iconbtn small" onClick={html ? html.forward ?? undefined : onForward} disabled={html ? !html.forward : !canGoForward(state)} aria-label={t("panel.forward")} title={t("panel.forward")}><Icon name="forward" size={16} /></button>
      <button className="iconbtn small" onClick={html ? html.reload : local.reload} aria-label={t("panel.reload")} title={t("panel.reload")}><Icon name="reload" size={16} /></button>
      <div className="panel-crumbs" aria-label={t("panel.crumbs")}>
        {/* A panel without a Files tab shows its root as a name, not a way to a tab it does not
            offer. */}
        {files !== undefined ? <button className="crumb" onClick={() => onTab("files")} title={t("panel.tab.files")}>{root}</button> : <span className="crumb">{root}</span>}
        {crumbs.map((c, i) => (
          <span key={i} className={i === crumbs.length - 1 ? "crumb last" : "crumb"}>
            <span className="sep">›</span>
            {c}
          </span>
        ))}
        {!entry && <span className="sub">{t("panel.preview.none")}</span>}
      </div>
      {entry && (
        <a className="iconbtn small" href={html ? downloadUrl({ ...entry, path: html.path.split("#")[0] }) : downloadUrl(entry)} target="_blank" rel="noreferrer" aria-label={t("panel.opennew")} title={t("panel.opennew")}><Icon name="external" size={16} /></a>
      )}
      {entry && (local.info?.kind === "html" || local.info?.kind === "image") && !local.info.loading && (
        <SnapshotButton toast={statusToast} capture={() => previewSnapshot({
          kind: local.info?.kind ?? "", url: local.info?.url ?? null, base: entry.base, path: html ? html.path.split("#")[0] : entry.path,
          frame: document.getElementById(`${local.ids}-body`)?.querySelector(".html-frame")?.getBoundingClientRect() ?? null,
        })} />
      )}
      {entry && <a className="iconbtn small" href={downloadUrl(html ? { ...entry, path: html.path.split("#")[0] } : entry)} download={entry.path.split("/").pop()} aria-label={t("common.download")} title={t("common.download")}><Icon name="download" size={16} /></a>}
      {!sheet && (
        <button className={`iconbtn small ${local.narrow ? "on" : ""}`} aria-pressed={local.narrow} onClick={local.toggleNarrow} aria-label={t("panel.phonewidth")} title={t("panel.phonewidth")}><Icon name="phone" size={16} /></button>
      )}
    </div>
  );
}

/** Whether the panel draws the tree and the open files side by side: a panel wide enough for both, on
 *  Preview with the tree shown, or on Files once a file is open — so in a wide panel Files and Preview
 *  are one view and the reader never loses the tree to read a file, nor the file to find the next one. */
function splitOf({ state, files, sheet, local }: HostProps): boolean {
  if (files === undefined || sheet || !local.wide || !local.tree) return false;
  return state.tab === "preview" || (state.tab === "files" && currentEntry(state) !== null);
}

/** The open files as tabs over the viewer, each closable. Shown on Preview and in the wide view; the
 *  history behind Back and Forward still holds every step, the tabs are its files once each. */
function FileTabs(props: HostProps) {
  const { state, onShowEntry, onCloseEntry } = props;
  const entries = openEntries(state);
  const strip = useRef<HTMLDivElement>(null);
  useEdgeFade(strip, state.at);
  useEffect(() => {
    strip.current?.querySelector<HTMLElement>(".file-tab.on")?.scrollIntoView?.({ block: "nearest", inline: "nearest" });
  }, [state.at]);
  if (!onShowEntry || entries.length === 0) return null;
  if (state.tab !== "preview" && !splitOf(props)) return null;
  const onKey = (e: React.KeyboardEvent) => {
    const i = entries.findIndex((entry) => isCurrent(state, entry));
    if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
      const next = entries[(i + (e.key === "ArrowRight" ? 1 : entries.length - 1)) % entries.length];
      onShowEntry(next);
      e.preventDefault();
      window.requestAnimationFrame(() => strip.current?.querySelector<HTMLElement>(".file-tab.on")?.focus());
    } else if ((e.key === "Delete" || e.key === "Backspace") && onCloseEntry && entries[i]) {
      onCloseEntry(entries[i]);
      e.preventDefault();
    }
  };
  return (
    <div ref={strip} className="panel-filetabs" role="tablist" aria-label={t("panel.files.open")} onKeyDown={onKey}>
      {entries.map((entry) => {
        const on = isCurrent(state, entry);
        const name = entry.path.split("/").pop() || entry.path;
        return (
          <div key={`${entry.base}:${entry.path}:${entry.lines ?? ""}`} className={`file-tab ${on ? "on" : ""}`} title={entry.path}>
            <button role="tab" aria-selected={on} tabIndex={on ? 0 : -1} className="file-tab-name" onClick={() => onShowEntry(entry)} onAuxClick={(e) => { if (e.button === 1 && onCloseEntry) { e.preventDefault(); onCloseEntry(entry); } }}>
              <Icon name={fileIcon(name)} size={14} />
              <span className="truncate">{name}{entry.lines ? `:${entry.lines}` : ""}</span>
            </button>
            {onCloseEntry && <button className="file-tab-close" tabIndex={-1} aria-label={t("panel.files.close", { name })} title={t("panel.files.close", { name })} onClick={() => onCloseEntry(entry)}><Icon name="close" size={12} /></button>}
          </div>
        );
      })}
    </div>
  );
}

function Body(props: HostProps) {
  const { state, local } = props;
  const entry = currentEntry(state);
  const box = useRef<HTMLDivElement>(null);
  const { wide, setWide } = local;
  const [width, setWidth] = usePaneWidth("tree", TREE_W, TREE_W_MIN, TREE_W_MAX);
  const files = useRef<HTMLDivElement>(null);
  const treeDrag = pixelDrag(() => files.current, "width", () => files.current?.getBoundingClientRect().width ?? width, (w) => clampWidth(w, TREE_W_MIN, TREE_W_MAX), setWidth);
  useEffect(() => {
    if (!box.current) return;
    const observer = new ResizeObserver(([e]) => setWide(e.contentRect.width >= SPLIT_MIN));
    observer.observe(box.current);
    return () => observer.disconnect();
  }, [setWide]);
  const [visited, setVisited] = useState(state.tab === "files");
  const split = splitOf(props);
  useEffect(() => { if (state.tab === "files" || split) setVisited(true); }, [state.tab, split]);
  useMoreBelow(box, state.tab);
  const progress = local.nav?.busy ? null : local.info?.progress;
  const loading = state.tab === "preview" && (local.info?.loading || local.nav?.busy);
  return (
    <div ref={box} id={`${local.ids}-body`} role="tabpanel" aria-labelledby={`${local.ids}-tab-${state.tab}`} tabIndex={-1} className={`panel-body tab-${split ? "preview" : state.tab} ${split ? "split" : ""}`}>
      {loading && <div className={`preview-progress ${progress == null ? "busy" : ""}`} role="progressbar" aria-label={t("common.loading")} aria-valuemin={0} aria-valuemax={100} aria-valuenow={progress == null ? undefined : Math.round(progress * 100)}><i style={progress == null ? undefined : { width: `${progress * 100}%` }} /></div>}
      {state.tab === "details" && props.details?.(local.ids)}
      {props.files !== undefined && <div ref={files} className="panel-files" hidden={state.tab !== "files" && !split} style={split ? { width } : undefined}>{(visited || state.tab === "files" || split) && props.files}{split && <PaneHandle side="left" drag={treeDrag} />}</div>}
      {split ? (
        // The wide view: the tree on the left, the open files as tabs on the right with the viewer's
        // toolbar under them — a file manager and an editor side by side, the way a wide panel is used.
        <div className="panel-beside">
          <FileTabs {...props} />
          <Toolbar {...props} />
          {entry ? <Viewer key={`${entry.base}:${entry.path}:${entry.lines ?? ""}:${local.gen}`} src={entry as PreviewSource} onInfo={local.setInfo} onNavigation={local.setNav} className="panel-viewer" /> : <div className="empty">{t("panel.preview.empty")}</div>}
        </div>
      ) : state.tab === "preview" && (entry ? <Viewer key={`${entry.base}:${entry.path}:${entry.lines ?? ""}:${local.gen}`} src={entry as PreviewSource} onInfo={local.setInfo} onNavigation={local.setNav} className="panel-viewer" /> : <div className="empty">{t("panel.preview.empty")}</div>)}
      {state.tab === "jobs" && props.jobs}
      {state.tab && props.pages?.[state.tab]}
    </div>
  );
}

// ── the state, held by the screen ────────────────────────────────────────────────────────────

export type PanelControls = {
  state: PanelState;
  open: (tab: PanelTab) => void;
  /** A tool's tab closed; the panel stays open on the tab beside it. */
  closeTool: (tab: PanelTab) => void;
  close: () => void;
  toggle: () => void;
  expand: () => void;
  back: () => void;
  forward: () => void;
  openFile: (entry: PanelEntry) => void;
  /** The Subagents tab on one helper's conversation, or on the list with null. */
  openSubagent: (sub: string | null) => void;
  /** The Board tab on one task, or on its list with null. */
  openTask: (task: string | null) => void;
  /** A file opened beside the one on screen, as a tab of its own. */
  openBeside: (entry: PanelEntry) => void;
  showEntry: (entry: PanelEntry) => void;
  closeEntry: (entry: PanelEntry) => void;
  /** Whether the panel opens in every chat on this device, and the switch for it. */
  pinned: boolean;
  togglePin: () => void;
};

/** A pane's panel. With `route`, the URL carries the open tab and the previewed path, so a link
 *  reproduces the view and the browser's Back closes what was opened; the second pane of a dual
 *  view keeps its panel to itself. Below 1920 px only one pane's panel is open at a time.
 *
 *  `context` decides the tabs (`tabsFor`) and where the last one is remembered; `at` is the address
 *  the pane lives at, for a pane that is not an agent's own page — a project's focus mode writes the
 *  panel into its own route, or opening a tab would leave the project. */
export function usePanel(sessionId: string, base: string, opts: { route: URLSearchParams | null; beside?: string | null; pane?: "left" | "right"; context?: PanelContext; at?: (query: Record<string, string | null>) => string; closed?: boolean; shown?: () => PanelTab[] }): PanelControls {
  const { route, beside, pane, context = "session", at, closed, shown } = opts;
  // What the route may name, the Browser tab included; what a panel opens on by itself is `first`.
  const tabs = routeTabsFor(context);
  const first = firstTab(context);
  const [state, setState] = useState<PanelState>(() => {
    const kept = readPanelState(window.innerWidth, undefined, context);
    const start = { ...PANEL_CLOSED, open: kept.open };
    const fromRoute = route ? readPanelQuery(route, tabs) : null;
    if (fromRoute) return applyPanelQuery(start, fromRoute, base);
    // The second pane keeps its panel closed until asked while the window has room for one only.
    if (pane === "right" && window.innerWidth < DUAL_BOTH_MIN) return start;
    if (closed) return start;
    return kept.tab ? openTab(start, kept.tab) : start;
  });
  const last = useRef<PanelTab | null>(state.tab);
  if (state.tab) last.current = state.tab;
  // The tab the panel reopens on: the last one, unless it is a tab the panel no longer offers — a
  // session's Questions once its side questions are all answered — which would reopen on an empty page.
  const shownRef = useRef(shown);
  shownRef.current = shown;
  const reopen = useCallback((): PanelTab => {
    const tab = last.current;
    return tab && (!shownRef.current || shownRef.current().includes(tab)) ? tab : first;
  }, [first]);
  // What this pane last wrote into the URL, so a route change it caused is not applied back to it.
  const written = useRef<string>(route ? routeKey(route, base, tabs) : "");
  const atRef = useRef(at);
  atRef.current = at;
  const commit = useCallback(
    (next: PanelState, push = false) => {
      setState(next);
      rememberPanel(next, undefined, context);
      if (!route) return;
      const q = panelQuery(next);
      written.current = queryString(q);
      navigate(atRef.current ? atRef.current(q) : pathFor("agents", sessionId, { with: beside ?? undefined, ...q }), { replace: !push });
    },
    [route, sessionId, beside, context],
  );
  // The route moved under the pane (Back, a link, the other pane): follow it.
  useEffect(() => {
    if (!route) return;
    const q = readPanelQuery(route, tabs);
    const incoming = routeKey(route, base, tabs);
    if (incoming === written.current) return;
    written.current = incoming;
    setState((s) => {
      const next = applyPanelQuery(s, q, base);
      rememberPanel(next, undefined, context);
      return next;
    });
    // `tabs` is derived from `context`, which is what the effect depends on.
  }, [route, base, context]);
  // One panel at a time below 1920: opening this pane's closes the other's.
  useEffect(() => {
    if (!pane) return;
    const on = (e: Event) => {
      if ((e as CustomEvent<string>).detail !== pane && window.innerWidth < DUAL_BOTH_MIN) setState((s) => closePanel(s));
    };
    window.addEventListener(PANEL_EVENT, on);
    return () => window.removeEventListener(PANEL_EVENT, on);
  }, [pane]);
  const announce = useCallback(() => {
    if (pane) window.dispatchEvent(new CustomEvent(PANEL_EVENT, { detail: pane }));
  }, [pane]);
  const stateRef = useRef(state);
  stateRef.current = state;
  const [pinned, setPinned] = useState(readPanelPinned);
  const open = useCallback(
    (tab: PanelTab) => {
      const s = stateRef.current;
      if (s.tab === null) announce();
      commit(openTab(s, tab), s.tab === null);
    },
    [commit, announce],
  );
  return {
    state,
    open,
    closeTool: useCallback((tab: PanelTab) => commit(closeTool(stateRef.current, tab)), [commit]),
    close: useCallback(() => commit(closePanel(stateRef.current)), [commit]),
    toggle: useCallback(() => {
      const s = stateRef.current;
      if (s.tab === null) announce();
      commit(togglePanel(s, reopen(), first), s.tab === null);
    }, [commit, announce, first, reopen]),
    expand: useCallback(() => {
      const s = stateRef.current;
      if (s.tab !== null) {
        commit(toggleExpanded(s));
        return;
      }
      announce();
      commit({ ...openTab(s, reopen()), expanded: true }, true);
    }, [commit, announce, first, reopen]),
    back: useCallback(() => commit(goBack(stateRef.current)), [commit]),
    forward: useCallback(() => commit(goForward(stateRef.current)), [commit]),
    openFile: useCallback(
      (entry: PanelEntry) => {
        const s = stateRef.current;
        if (s.tab === null) announce();
        commit(openFile(s, entry), s.tab === null);
      },
      [commit, announce],
    ),
    openBeside: useCallback(
      (entry: PanelEntry) => {
        const s = stateRef.current;
        if (s.tab === null) announce();
        // Nothing open yet: the first file is the one on screen, on Preview as any opened file is.
        commit(s.tab === null || currentEntry(s) === null ? openFile(s, entry) : openBeside(s, entry), s.tab === null);
      },
      [commit, announce],
    ),
    showEntry: useCallback((entry: PanelEntry) => commit(showEntry(stateRef.current, entry)), [commit]),
    closeEntry: useCallback((entry: PanelEntry) => commit(closeEntry(stateRef.current, entry)), [commit]),
    pinned,
    togglePin: useCallback(() => setPinned((was) => {
      rememberPanelPinned(!was);
      // The open tab is written again under the new pin, so a panel pinned while open is the one the
      // next chat opens on.
      if (!was && stateRef.current.tab) rememberPanel(stateRef.current, undefined, context);
      return !was;
    }), [context]),
    openTask: useCallback(
      (task: string | null) => {
        const s = stateRef.current;
        if (s.tab === null) announce();
        // A card opened is a step the browser's Back can undo; leaving it for the list replaces that
        // entry rather than stacking a second list on top of the first.
        commit(openTask(s, task), s.tab === null || (task !== null && s.task !== task));
      },
      [commit, announce],
    ),
    openSubagent: useCallback(
      (sub: string | null) => {
        const s = stateRef.current;
        if (s.tab === null) announce();
        // A step into a helper's conversation is one Back can undo; a step back to the list is not
        // worth an entry of its own.
        commit(openSubagent(s, sub), s.tab === null || (sub !== null && s.sub !== sub));
      },
      [commit, announce],
    ),
  };
}

// Whether the file tree stands beside a previewed file. A convenience of this browser: the reader
// who hid it to read a wide diff does not want it back on the next file.
const TREE_KEY = "daedalus.panel.tree";

function readTreeShown(): boolean {
  try {
    return localStorage.getItem(TREE_KEY) !== "0";
  } catch {
    return true;
  }
}

function rememberTreeShown(shown: boolean): void {
  try {
    localStorage.setItem(TREE_KEY, shown ? "1" : "0");
  } catch {
    /* private mode: it lasts for the visit */
  }
}

const PANEL_EVENT = "daedalus:panel-open";
/** From here two panes can each keep a panel open. */
export const DUAL_BOTH_MIN = 1920;

/** The panel part of a route, normalised the way this pane writes it. */
function routeKey(route: URLSearchParams, base: string, tabs: readonly PanelTab[]): string {
  const q = readPanelQuery(route, tabs);
  return queryString(panelQuery(q ? applyPanelQuery(PANEL_CLOSED, q, base) : PANEL_CLOSED));
}

function queryString(q: Record<string, string | null>): string {
  return Object.entries(q)
    .filter(([, v]) => v)
    .map(([k, v]) => `${k}=${v}`)
    .join("&");
}

/** The panel's width as the operator dragged it, in pixels, or null for the stylesheet's own. The drag
 *  writes `--panel-w` on the chat itself while the pointer moves (see PaneDrag); the state follows
 *  once, when it lets go. `area` is the chat the panel stands in, the header included. */
export function usePanelWidth(area: RefObject<HTMLElement | null>): [number | null, PaneDrag] {
  const [px, setPx] = useState<number | null>(() => readPanelPx());
  const areaWidth = () => area.current?.clientWidth || window.innerWidth;
  const drag: PaneDrag = {
    // What is on the page, not the stored width: the stylesheet holds the panel between its bounds.
    begin: () => area.current?.querySelector<HTMLElement>(":scope > .panel, :scope > .chat-body > .panel")?.getBoundingClientRect().width ?? px ?? 440,
    show: (w) => area.current?.style.setProperty("--panel-w", `${clampPanelPx(w, areaWidth()) ?? w}px`),
    commit: (w) => {
      const next = clampPanelPx(w, areaWidth());
      if (next === null) return;
      rememberPanelPx(next);
      setPx(next);
      // React writes the same value back only when it differs from the last render's; a drag that
      // ends where it began would otherwise leave the unrounded width on the page.
      area.current?.style.setProperty("--panel-w", `${next}px`);
    },
    sign: -1,
  };
  return [px, drag];
}
