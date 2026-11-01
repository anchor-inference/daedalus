// The Terminals workspace on a desktop: a bar of tabs over a grid of one, two or four live terminals,
// with a compact list of every terminal beside it. Each pane carries its own header (state, title,
// environment, owner, its tools) where the full-screen view had one header for whichever pane was
// touched last.
//
// The state is `workspacestate.ts`'s and the screen owns it; this file draws it and turns pointer and
// key gestures into its transitions. A pane's terminal is never re-created by a swap, a layout change
// or a splitter: the panes are keyed by terminal and placed by `grid-area`, so moving one changes a
// style and not the DOM, and a program keeps streaming while its pane is dragged.
//
// What later work hangs here is named in `WorkspaceSlots`: a line under each pane's terminal, a
// compose line above it, and a panel beside the grid with its toggle in the bar.

import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties, type KeyboardEvent as ReactKeyboardEvent, type PointerEvent as ReactPointerEvent, type ReactNode } from "react";
import type { Project, TerminalLoad, TerminalView as TerminalRow } from "../api";
import { MenuItem, OverflowMenu } from "../ui/dialogs";
import { t } from "../i18n";
import { Icon } from "../icons";
import { loadFigures } from "../machineload";
import { navigate, projectPagePath, sessionPath } from "../router";
import { EnvPill } from "../envpill";
import { fontSizeStep, type TerminalState } from "./instance";
import { isMac } from "./keys";
import { groupTerminals, headerCounts, ownerLine, ownerPath, running } from "./preview";
import { useTerminalCommands } from "./rowmenu";
import { paneState, withMessage, type PaneState } from "./status";
import { instanceFor } from "./terminals";
import { copyLastOutput, TerminalView } from "./view";
import { COLS_DEBOUNCE_MS } from "./fit";
import { activeId, clampFraction, close, focus, maximise, setFractions, setLayout, shownSlots, swap, LAYOUTS, SLOTS, type Layout, type Workspace } from "./workspacestate";
import "./workspace.css";

/** One pane as the slots below see it. `id` is null for an empty place. */
export type PaneContext = {
  slot: number;
  id: string | null;
  row: TerminalRow | null;
  state: TerminalState | undefined;
  status: PaneState;
  active: boolean;
};

/** Where later parts of the workspace mount. Each is optional; an absent one draws nothing. */
export type WorkspaceSlots = {
  /** Under a pane's terminal, the pane's last line: a status line of model, context, cost, branch. */
  footer?: (pane: PaneContext) => ReactNode;
  /** Between a pane's terminal and its footer: a line to write to the program or the agent in it. */
  compose?: (pane: PaneContext) => ReactNode;
  /** Beside the grid, on the right: a panel about the active pane. */
  side?: ReactNode;
  /** The bar's button for that panel; the button is drawn only when this is given. */
  panel?: { open: boolean; onToggle: () => void; count?: number };
};

export type TerminalWorkspaceProps = {
  ws: Workspace;
  update: (change: (ws: Workspace) => Workspace) => void;
  rows: TerminalRow[];
  projects: Project[];
  load: TerminalLoad | null;
  states: Record<string, TerminalState>;
  onState: (id: string, state: TerminalState) => void;
  /** Which terminal is to take the keyboard, and a token that changes with every request. Only the
   *  gestures that choose a pane without touching it (a tab, Alt+N, the list) ask; a click inside a
   *  pane already puts the focus where the person clicked, and a request then would take it away
   *  from the field they clicked into (a rename). */
  focus: { id: string | null; token: number };
  onFocusRequest: (id: string) => void;
  /** Shows a terminal in the workspace (the active place, or the first empty one). */
  onOpen: (id: string) => void;
  onRestart: (id: string) => void;
  onRemove: (id: string) => void;
  onEnd: (id: string) => void;
  onRename: (id: string, title: string) => void;
  onNew: () => void;
  onOverview: () => void;
  toast: (text: string) => void;
  slots?: WorkspaceSlots;
  /** The terminals whose agent another agent's message has just reached: their tab and pane say
   *  "message" for a moment (`MESSAGE_FLASH_MS`). The screen owns the timing. */
  messaged?: ReadonlySet<string>;
};

const RAIL_KEY = "daedalus.term.rail";

function readRail(): boolean {
  try {
    return localStorage.getItem(RAIL_KEY) !== "0";
  } catch {
    return true;
  }
}

/** A pane's place on the grid. The grid's tracks are the panes and, between them, the splitters. */
function area(ws: Workspace, slot: number): string {
  if (ws.zoom !== null || ws.layout === 1) return "1 / 1";
  const column = slot % 2 === 0 ? 1 : 3;
  const line = slot < 2 ? 1 : 3;
  return `${line} / ${column}`;
}

function template(ws: Workspace): CSSProperties {
  if (ws.zoom !== null || ws.layout === 1) return { gridTemplateColumns: "minmax(0, 1fr)", gridTemplateRows: "minmax(0, 1fr)" };
  const columns = `minmax(0, ${ws.col}fr) var(--tp-split) minmax(0, ${1 - ws.col}fr)`;
  const rows = ws.layout === 4 ? `minmax(0, ${ws.row}fr) var(--tp-split) minmax(0, ${1 - ws.row}fr)` : "minmax(0, 1fr)";
  return { gridTemplateColumns: columns, gridTemplateRows: rows };
}

/** "⌥1" on a Mac, "Alt+1" elsewhere: the key that focuses a place. */
export function placeKey(slot: number): string {
  return isMac() ? `⌥${slot + 1}` : `Alt+${slot + 1}`;
}

export function StateDot({ status, className = "" }: { status: PaneState; className?: string }) {
  return <span className={`tp-dot ${className}`} data-state={status} aria-hidden="true" />;
}

export function paneTitle(id: string | null, row: TerminalRow | null | undefined, state: TerminalState | undefined): string {
  if (!id) return t("term.ws.empty");
  // The host's title first: it is what a rename changed, and the program's own title reaches it too.
  return row?.title || state?.title || t("term.untitled");
}

/** The Overview / Workspace switch, in the Overview's header and in the Workspace's bar. */
export function ViewSwitch({ view, onChange }: { view: "overview" | "workspace"; onChange: (view: "overview" | "workspace") => void }) {
  return (
    <div className="tp-views segmented inline" role="radiogroup" aria-label={t("term.ws.view")}>
      {(["overview", "workspace"] as const).map((v) => (
        <button key={v} type="button" role="radio" aria-checked={view === v} className={view === v ? "on" : ""} data-view={v} onClick={() => onChange(v)}>
          {t(`term.ws.view.${v}`)}
        </button>
      ))}
    </div>
  );
}

/** One, two or four panes, each drawn as the shape it makes. */
function LayoutIcon({ layout }: { layout: Layout }) {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.3" aria-hidden="true">
      <rect x="1.5" y="2.5" width="13" height="11" rx="2" />
      {layout >= 2 && <line x1="8" y1="2.5" x2="8" y2="13.5" />}
      {layout === 4 && <line x1="1.5" y1="8" x2="14.5" y2="8" />}
    </svg>
  );
}

function LayoutSwitch({ layout, onChange }: { layout: Layout; onChange: (layout: Layout) => void }) {
  return (
    <div className="tp-layouts" role="radiogroup" aria-label={t("term.ws.layout")}>
      {LAYOUTS.map((n) => (
        <button key={n} type="button" role="radio" aria-checked={layout === n} className={`tp-layout ${layout === n ? "on" : ""}`} data-layout={n} onClick={() => onChange(n)} aria-label={t("term.ws.layout.n", { n })} title={t("term.ws.layout.n", { n })}>
          <LayoutIcon layout={n} />
        </button>
      ))}
    </div>
  );
}

export function TerminalWorkspace(props: TerminalWorkspaceProps) {
  const { ws, update, rows, states, slots, onOpen, messaged } = props;
  const byId = useMemo(() => new Map(rows.map((r) => [r.id, r])), [rows]);
  const grid = useRef<HTMLDivElement>(null);
  const [rail, setRail] = useState(readRail);
  const shown = shownSlots(ws);
  const tabs = ws.zoom !== null ? Array.from({ length: ws.layout }, (_, i) => i) : shown;

  const context = useCallback((slot: number): PaneContext => {
    const id = ws.panes[slot] ?? null;
    const row = id ? byId.get(id) ?? null : null;
    const state = id ? states[id] : undefined;
    return { slot, id, row, state, status: id ? withMessage(paneState(row, state), !!messaged?.has(id)) : "idle", active: ws.active === slot };
  }, [ws, byId, states, messaged]);

  const { onFocusRequest } = props;
  const choose = useCallback((slot: number, takeKeyboard = false) => {
    const id = ws.panes[slot];
    if (id) instanceFor(id)?.interact();
    update((w) => focus(w, slot));
    if (id && takeKeyboard) onFocusRequest(id);
  }, [ws.panes, update, onFocusRequest]);

  // Alt+1…4 focuses a place. Alt rather than Ctrl or ⌘, which belong to the browser's own tabs; taken
  // in the capture phase, before the terminal under the focus sees it, so a shell does not also get
  // ESC 1. That gives up Meta-1…4 inside a workspace pane (readline's digit argument) and nothing else.
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (!event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
      const match = /^Digit([1-4])$/.exec(event.code);
      if (!match) return;
      const slot = Number(match[1]) - 1;
      if (slot >= ws.layout) return;
      event.preventDefault();
      event.stopPropagation();
      choose(slot, true);
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [ws.layout, choose]);

  // ── dragging a pane onto another ──────────────────────────────────────────────────────────
  const [drag, setDrag] = useState<{ from: number; x: number; y: number; over: number | null } | null>(null);
  const slotAt = (x: number, y: number): number | null => {
    for (const el of grid.current?.querySelectorAll<HTMLElement>("[data-slot]") ?? []) {
      if (el.classList.contains("hidden")) continue;
      const box = el.getBoundingClientRect();
      if (x >= box.left && x <= box.right && y >= box.top && y <= box.bottom) return Number(el.dataset.slot);
    }
    return null;
  };
  const startDrag = (slot: number, event: ReactPointerEvent<HTMLElement>) => {
    if (event.button !== 0 || ws.layout === 1 || ws.zoom !== null) return;
    event.preventDefault();
    event.currentTarget.setPointerCapture(event.pointerId);
    setDrag({ from: slot, x: event.clientX, y: event.clientY, over: slot });
  };
  const moveDrag = (event: ReactPointerEvent<HTMLElement>) => {
    if (!drag) return;
    setDrag({ ...drag, x: event.clientX, y: event.clientY, over: slotAt(event.clientX, event.clientY) });
  };
  const endDrag = (event: ReactPointerEvent<HTMLElement>) => {
    if (!drag) return;
    // A drop on itself or outside the grid changes nothing.
    const over = slotAt(event.clientX, event.clientY);
    if (over !== null && over !== drag.from) update((w) => swap(w, drag.from, over));
    setDrag(null);
  };
  useEffect(() => {
    if (!drag) return;
    const cancel = (event: KeyboardEvent) => { if (event.key === "Escape") setDrag(null); };
    window.addEventListener("keydown", cancel);
    return () => window.removeEventListener("keydown", cancel);
  }, [drag]);

  // ── the splitters ─────────────────────────────────────────────────────────────────────────
  const [resizing, setResizing] = useState<"col" | "row" | null>(null);
  const startSplit = (axis: "col" | "row", event: ReactPointerEvent<HTMLElement>) => {
    if (event.button !== 0) return;
    event.preventDefault();
    event.currentTarget.setPointerCapture(event.pointerId);
    setResizing(axis);
  };
  const moveSplit = (event: ReactPointerEvent<HTMLElement>) => {
    if (!resizing || !grid.current) return;
    const box = grid.current.getBoundingClientRect();
    const fraction = resizing === "col" ? (event.clientX - box.left) / box.width : (event.clientY - box.top) / box.height;
    update((w) => setFractions(w, { [resizing]: fraction }));
  };
  useEffect(() => {
    if (!resizing) return;
    // The whole page shows the resize cursor while a splitter moves, not only the 5 px under it.
    document.body.classList.add(`tp-resizing-${resizing}`);
    return () => document.body.classList.remove(`tp-resizing-${resizing}`);
  }, [resizing]);
  const splitKeys = (axis: "col" | "row", event: ReactKeyboardEvent<HTMLElement>) => {
    const step = event.key === "ArrowLeft" || event.key === "ArrowUp" ? -0.05 : event.key === "ArrowRight" || event.key === "ArrowDown" ? 0.05 : 0;
    if (!step) return;
    event.preventDefault();
    update((w) => setFractions(w, { [axis]: clampFraction(w[axis] + step) }));
  };

  const waiting = rows.filter((r) => paneState(r, states[r.id]) === "waiting");
  const dragged = drag ? context(drag.from) : null;

  return (
    <div className={`tp tp-ws ${rail ? "" : "no-rail"} ${slots?.side ? "has-side" : ""}`} data-layout={ws.layout}>
      <div className="tp-top">
        <button type="button" className="iconbtn small flat tp-rail-toggle" onClick={() => { const next = !rail; setRail(next); try { localStorage.setItem(RAIL_KEY, next ? "1" : "0"); } catch { /* this page only */ } }} aria-label={t(rail ? "term.ws.list.hide" : "term.ws.list.show")} title={t(rail ? "term.ws.list.hide" : "term.ws.list.show")} aria-pressed={rail}>
          <Icon name="sidebar" size={16} />
        </button>
        <ViewSwitch view="workspace" onChange={(v) => { if (v === "overview") props.onOverview(); }} />
        <div className="tp-tabs" role="tablist" aria-label={t("term.ws.panes")}>
          {tabs.map((slot) => {
            const pane = context(slot);
            const title = paneTitle(pane.id, pane.row, pane.state);
            // A narrow tab shows its state as the dot alone; the tooltip keeps the word.
            const tip = pane.id && pane.status !== "idle" ? t("term.ws.tab.tip", { title, state: t(`term.ws.state.${pane.status}`) }) : title;
            return (
              <button key={slot} type="button" role="tab" aria-selected={pane.active} className={`tp-tab ${pane.active ? "on" : ""} ${pane.id ? "" : "empty"}`} data-slot={slot} data-state={pane.id ? pane.status : "empty"} onClick={() => choose(slot, true)} title={tip}>
                <StateDot status={pane.status} className={pane.id ? "" : "none"} />
                <span className="tp-tab-title truncate">{title}</span>
                {pane.id && pane.status !== "idle" && <span className="tp-tab-state" data-state={pane.status}>{t(`term.ws.state.${pane.status}`)}</span>}
                <kbd className="tp-tab-key">{placeKey(slot)}</kbd>
              </button>
            );
          })}
        </div>
        {waiting.length > 0 && (
          <button type="button" className="tp-waiting" onClick={() => onOpen(waiting[0].id)} title={waiting.map((r) => r.title).join(", ")}>
            <StateDot status="waiting" />
            <span>{t("term.ws.waiting", { n: waiting.length })}</span>
          </button>
        )}
        <LayoutSwitch layout={ws.layout} onChange={(layout) => update((w) => setLayout(w, layout))} />
        {slots?.panel && (
          <button type="button" className={`iconbtn small flat tp-panel-toggle ${slots.panel.open ? "on" : ""}`} onClick={slots.panel.onToggle} aria-pressed={slots.panel.open} aria-label={t("term.ws.panel")} title={t("term.ws.panel")}>
            <Icon name="panel" size={16} />
            {!!slots.panel.count && <span className="tp-count num">{slots.panel.count}</span>}
          </button>
        )}
        <button type="button" className="btn small tp-new" onClick={props.onNew}>
          <Icon name="plus" size={14} />
          <span>{t("term.new")}</span>
        </button>
      </div>
      <div className="tp-body">
        {rail && <TerminalList rows={rows} states={states} ws={ws} projects={props.projects} load={props.load} onOpen={onOpen} toast={props.toast} />}
        <div ref={grid} className={`tp-grid ${drag ? "dragging" : ""}`} style={template(ws)} data-layout={ws.zoom !== null ? 1 : ws.layout}>
          {/* Every place that holds a terminal stays mounted, hidden when its layout does not show it, so
              4 → 1 → 4 keeps every screen. They are drawn in a fixed order and placed by grid-area,
              so a swap moves no element and remounts no terminal. */}
          {Array.from({ length: SLOTS }, (_, slot) => slot)
            .filter((slot) => ws.panes[slot] || shown.includes(slot))
            .map((slot) => ({ slot, key: ws.panes[slot] ?? `empty-${slot}` }))
            .sort((a, b) => (a.key < b.key ? -1 : a.key > b.key ? 1 : 0))
            .map(({ slot, key }) => (
              <WorkspacePane
                key={key}
                pane={context(slot)}
                hidden={!shown.includes(slot)}
                grid={ws.layout > 1 && ws.zoom === null}
                maximised={ws.zoom === slot}
                canMaximise={ws.layout > 1}
                gridArea={area(ws, slot)}
                over={drag !== null && drag.over === slot && drag.from !== slot}
                focusToken={ws.panes[slot] && props.focus.id === ws.panes[slot] ? props.focus.token : undefined}
                rows={rows}
                ws={ws}
                slots={slots}
                onChoose={() => choose(slot)}
                onGrip={(event) => startDrag(slot, event)}
                onGripMove={moveDrag}
                onGripUp={endDrag}
                onClose={() => update((w) => close(w, slot))}
                onMaximise={() => update((w) => maximise(w, slot))}
                onPick={(id) => { update((w) => focus(w, slot)); onOpen(id); }}
                actions={props}
              />
            ))}
          {ws.zoom === null && ws.layout > 1 && (
            <div
              className={`tp-split col ${resizing === "col" ? "on" : ""}`}
              role="separator"
              aria-orientation="vertical"
              aria-label={t("term.ws.split.col")}
              aria-valuenow={Math.round(ws.col * 100)}
              aria-valuemin={20}
              aria-valuemax={80}
              tabIndex={0}
              style={{ gridArea: "1 / 2 / -1 / 3" }}
              onPointerDown={(e) => startSplit("col", e)}
              onPointerMove={moveSplit}
              onPointerUp={() => setResizing(null)}
              onPointerCancel={() => setResizing(null)}
              onDoubleClick={() => update((w) => setFractions(w, { col: 0.5 }))}
              onKeyDown={(e) => splitKeys("col", e)}
            />
          )}
          {ws.zoom === null && ws.layout === 4 && (
            <div
              className={`tp-split row ${resizing === "row" ? "on" : ""}`}
              role="separator"
              aria-orientation="horizontal"
              aria-label={t("term.ws.split.row")}
              aria-valuenow={Math.round(ws.row * 100)}
              aria-valuemin={20}
              aria-valuemax={80}
              tabIndex={0}
              style={{ gridArea: "2 / 1 / 3 / -1" }}
              onPointerDown={(e) => startSplit("row", e)}
              onPointerMove={moveSplit}
              onPointerUp={() => setResizing(null)}
              onPointerCancel={() => setResizing(null)}
              onDoubleClick={() => update((w) => setFractions(w, { row: 0.5 }))}
              onKeyDown={(e) => splitKeys("row", e)}
            />
          )}
        </div>
        {slots?.side && <aside className="tp-side">{slots.side}</aside>}
      </div>
      {drag && dragged && (
        <div className="tp-ghost" style={{ left: drag.x + 12, top: drag.y + 8 }} aria-hidden="true">
          <StateDot status={dragged.status} />
          <span className="truncate">{paneTitle(dragged.id, dragged.row, dragged.state)}</span>
        </div>
      )}
    </div>
  );
}

type PaneProps = {
  pane: PaneContext;
  hidden: boolean;
  grid: boolean;
  maximised: boolean;
  canMaximise: boolean;
  gridArea: string;
  over: boolean;
  focusToken: number | undefined;
  rows: TerminalRow[];
  ws: Workspace;
  slots?: WorkspaceSlots;
  onChoose: () => void;
  onGrip: (event: ReactPointerEvent<HTMLElement>) => void;
  onGripMove: (event: ReactPointerEvent<HTMLElement>) => void;
  onGripUp: (event: ReactPointerEvent<HTMLElement>) => void;
  onClose: () => void;
  onMaximise: () => void;
  onPick: (id: string) => void;
  actions: Pick<TerminalWorkspaceProps, "onRestart" | "onRemove" | "onEnd" | "onRename" | "onNew" | "onState">;
};

/** One place of the grid: its header, its terminal (or the chooser of an empty place), and the slots under it. */
export function WorkspacePane({ pane, hidden, grid, maximised, canMaximise, gridArea, over, focusToken, rows, ws, slots, onChoose, onGrip, onGripMove, onGripUp, onClose, onMaximise, onPick, actions }: PaneProps) {
  const { id, row, state, status, active, slot } = pane;
  const title = paneTitle(id, row, state);
  const [editing, setEditing] = useState(false);
  const owner = row ? ownerPath(row, (sid) => sessionPath(sid), (pid) => projectPagePath(pid, "team")) : null;
  const live = id ? (row ? running(row) : !state?.exit) : false;
  const footer = slots?.footer?.(pane);
  const compose = slots?.compose?.(pane);

  const menu: MenuItem[] = id ? [
    ...(state?.commands.active ? [{ label: t("term.marks.copy"), icon: "copy" as const, disabled: !state.commands.ended, onSelect: () => { const i = instanceFor(id); if (i) void copyLastOutput(i); } }] : []),
    { label: t("term.search"), icon: "search", hint: "Ctrl+Shift+F", onSelect: () => instanceFor(id)?.openSearch() },
    { label: t("term.font.smaller"), icon: "compact", onSelect: () => fontSizeStep("font-smaller") },
    { label: t("term.font.bigger"), icon: "expand", onSelect: () => fontSizeStep("font-bigger") },
    { label: t("term.rename"), icon: "pen", hint: t("term.ws.rename.hint"), onSelect: () => setEditing(true) },
    ...(owner ? [{ label: t("term.ws.owner.open"), icon: "external" as const, onSelect: () => navigate(owner) }] : []),
    "-",
    ...(live ? [{ label: t("term.end"), icon: "stop" as const, danger: true, onSelect: () => actions.onEnd(id) }] : [
      { label: t("term.restart"), icon: "reload" as const, onSelect: () => actions.onRestart(id) },
      { label: t("term.remove"), icon: "trash" as const, danger: true, onSelect: () => actions.onRemove(id) },
    ]),
  ] : [];

  return (
    <section
      className={`tp-pane ${hidden ? "hidden" : ""} ${active && grid ? "on" : ""} ${row?.env === "host" ? "host" : ""} ${over ? "drop" : ""} ${id ? "" : "empty"}`}
      style={{ gridArea }}
      data-slot={slot}
      data-pane={id ?? ""}
      data-state={id ? status : "empty"}
      aria-label={title}
      onPointerDownCapture={onChoose}
      onFocusCapture={onChoose}
    >
      <header className="tp-pane-head">
        {grid && (
          <span className="tp-grip" onPointerDown={onGrip} onPointerMove={onGripMove} onPointerUp={onGripUp} onPointerCancel={onGripUp} title={t("term.ws.drag")} aria-label={t("term.ws.drag")} role="button">
            <svg width="8" height="14" viewBox="0 0 8 14" fill="currentColor" aria-hidden="true"><circle cx="2" cy="3" r="1.1" /><circle cx="6" cy="3" r="1.1" /><circle cx="2" cy="7" r="1.1" /><circle cx="6" cy="7" r="1.1" /><circle cx="2" cy="11" r="1.1" /><circle cx="6" cy="11" r="1.1" /></svg>
          </span>
        )}
        <StateDot status={status} className={id ? "" : "none"} />
        {editing && id ? (
          <RenameField initial={row?.title ?? title} onDone={(text) => { setEditing(false); if (text !== null && text !== (row?.title ?? "")) actions.onRename(id, text); }} />
        ) : (
          <span className="tp-pane-title truncate" title={id ? `${title} · ${t("term.ws.rename.hint")}` : title} onDoubleClick={() => { if (id) setEditing(true); }}>
            {row?.sandbox && <Icon name="shield" size={12} />}
            {title}
          </span>
        )}
        {row && <EnvPill env={row.env} tiny />}
        {row && owner && (
          <a className="tp-pane-owner truncate" href={owner} onClick={(e) => { e.preventDefault(); navigate(owner); }}>{ownerLine(row, null)}</a>
        )}
        {row && !owner && row.owner.kind !== "free" && <span className="tp-pane-owner truncate">{ownerLine(row, null)}</span>}
        <div className="grow" />
        {status === "waiting" && (
          row?.activity?.action ? (
            <button type="button" className="tp-attn" onClick={() => navigate(row.activity!.action!.path)} title={row.activity.label || t("term.ws.state.waiting")}>
              <StateDot status="waiting" />
              <span>{row.activity.action.kind === "answer" ? t("term.card.answer") : row.activity.action.label}</span>
            </button>
          ) : (
            <span className="tp-attn"><StateDot status="waiting" /><span>{t("term.ws.state.waiting")}</span></span>
          )
        )}
        {id && <OverflowMenu small className="flat tp-pane-menu" icon="vdots" label={t("term.ws.pane.menu", { title })} items={menu} />}
        {canMaximise && (
          <button type="button" className="iconbtn small flat tp-pane-max" onClick={onMaximise} aria-pressed={maximised} aria-label={t(maximised ? "term.ws.restore" : "term.ws.maximise")} title={t(maximised ? "term.ws.restore" : "term.ws.maximise")}>
            <Icon name={maximised ? "compact" : "expand"} size={14} />
          </button>
        )}
        {id && (
          <button type="button" className="iconbtn small flat tp-pane-close" onClick={onClose} aria-label={t("term.ws.close")} title={t("term.ws.close")}>
            <Icon name="close" size={14} />
          </button>
        )}
      </header>
      <div className="tp-pane-body">
        {id ? (
          <TerminalView
            id={id}
            visible={!hidden}
            env={row?.env}
            focusToken={focusToken}
            onState={actions.onState}
            onRestart={() => actions.onRestart(id)}
            onRemove={() => actions.onRemove(id)}
            // A splitter changes rows as well as columns on every frame; rows wait as long as columns
            // do, so a drag ends in one RESIZE per pane rather than one per row it crossed.
            rowsDelay={COLS_DEBOUNCE_MS}
          />
        ) : (
          <EmptyPane rows={rows} ws={ws} onPick={onPick} onNew={actions.onNew} />
        )}
      </div>
      {compose ? <div className="tp-pane-compose">{compose}</div> : null}
      <footer className="tp-pane-foot" data-slot-footer={slot}>{footer}</footer>
    </section>
  );
}

function RenameField({ initial, onDone }: { initial: string; onDone: (text: string | null) => void }) {
  const [text, setText] = useState(initial);
  const done = useRef(false);
  const finish = (value: string | null) => {
    if (done.current) return;
    done.current = true;
    onDone(value);
  };
  return (
    <input
      className="tp-rename"
      autoFocus
      value={text}
      maxLength={200}
      aria-label={t("term.rename")}
      placeholder={t("term.rename.hint")}
      onChange={(e) => setText(e.target.value)}
      onFocus={(e) => e.currentTarget.select()}
      onKeyDown={(e) => {
        // The terminal's keys must not see these: Enter would also reach the shell under the header.
        e.stopPropagation();
        if (e.key === "Enter") finish(text.trim());
        if (e.key === "Escape") finish(null);
      }}
      onBlur={() => finish(text.trim())}
    />
  );
}

/** An empty place: the running terminals no shown pane holds, and a new one. */
function EmptyPane({ rows, ws, onPick, onNew }: { rows: TerminalRow[]; ws: Workspace; onPick: (id: string) => void; onNew: () => void }) {
  const shownIds = new Set(shownSlots(ws).map((s) => ws.panes[s]).filter(Boolean));
  const free = rows.filter((r) => running(r) && !shownIds.has(r.id)).slice(0, 8);
  return (
    <div className="tp-empty">
      <div className="tp-empty-title">{t("term.ws.empty.title")}</div>
      {free.length > 0 && (
        <div className="tp-empty-list">
          {free.map((r) => (
            <button key={r.id} type="button" className="tp-empty-row" data-terminal={r.id} onClick={() => onPick(r.id)}>
              <StateDot status={paneState(r)} />
              <span className="truncate">{r.title || t("term.untitled")}</span>
              <EnvPill env={r.env} tiny />
            </button>
          ))}
        </div>
      )}
      <button type="button" className="btn small tp-empty-new" onClick={onNew}>
        <Icon name="plus" size={14} />
        <span>{t("term.new")}</span>
      </button>
    </div>
  );
}

/** The compact list beside the grid: every terminal, by project, with a search over title, owner and folder. */
function TerminalList({ rows, states, ws, projects, load, onOpen, toast }: { rows: TerminalRow[]; states: Record<string, TerminalState>; ws: Workspace; projects: Project[]; load: TerminalLoad | null; onOpen: (id: string) => void; toast: (text: string) => void }) {
  const [query, setQuery] = useState("");
  const names = useMemo(() => new Map(projects.map((p) => [p.id, p.name])), [projects]);
  const needle = query.trim().toLowerCase();
  const found = needle ? rows.filter((r) => [r.title, r.cwd, r.owner.label ?? "", ownerLine(r, r.project_id ? names.get(r.project_id) ?? null : null)].some((s) => s.toLowerCase().includes(needle))) : rows;
  const groups = groupTerminals(found, projects);
  const current = activeId(ws);
  return (
    <nav className="tp-list" aria-label={t("term.ws.list")}>
      <div className="tp-list-head">
        <Icon name="search" size={14} />
        <input className="tp-list-search" value={query} onChange={(e) => setQuery(e.target.value)} placeholder={t("term.ws.search")} aria-label={t("term.ws.search")} />
      </div>
      <div className="tp-list-body">
        {found.length === 0 && <div className="tp-list-none">{rows.length ? t("term.screen.none") : t("term.screen.empty")}</div>}
        {groups.map((group) => (
          <section key={group.key || "none"} className="tp-list-group">
            <div className="tp-label"><span className="truncate">{group.name ?? t("term.screen.noproject")}</span><span className="num">{group.rows.length}</span></div>
            {group.rows.map((r) => {
              const at = ws.panes.indexOf(r.id);
              return <ListRow key={r.id} row={r} status={paneState(r, states[r.id])} place={at >= 0 && at < ws.layout ? at : null} current={current === r.id} projectName={r.project_id ? names.get(r.project_id) ?? null : null} onOpen={() => onOpen(r.id)} toast={toast} />;
            })}
          </section>
        ))}
      </div>
      {load && <LoadLine load={load} rows={rows} />}
    </nav>
  );
}

function ListRow({ row, status, place, current, projectName, onOpen, toast }: { row: TerminalRow; status: PaneState; place: number | null; current: boolean; projectName: string | null; onOpen: () => void; toast: (text: string) => void }) {
  const { items, layer } = useTerminalCommands(row, toast);
  return (
    <div className={`tp-row ${current ? "on" : ""} ${running(row) ? "" : "finished"}`} data-terminal={row.id} data-state={status}>
      <button type="button" className="tp-row-open" onClick={onOpen} aria-label={t("term.open", { title: row.title || t("term.untitled") })}>
        <StateDot status={status} />
        <span className="tp-row-text">
          <span className="tp-row-title truncate">{row.title || t("term.untitled")}</span>
          <span className="tp-row-owner truncate">{ownerLine(row, projectName)}</span>
        </span>
        {place !== null ? <kbd className="tp-row-key">{placeKey(place)}</kbd> : row.env === "host" ? <EnvPill env="host" tiny /> : null}
      </button>
      <OverflowMenu small className="flat quiet tp-row-menu" contextSelector=".tp-row" label={t("term.row.menu", { title: row.title || t("term.untitled") })} items={items} />
      {layer}
    </div>
  );
}

/** The machine's load as one line under the list: how many run, and memory as a bar coloured by what
 *  the cap would cost. The whole picture, with its key, is the Overview's header. */
function LoadLine({ load, rows }: { load: TerminalLoad; rows: TerminalRow[] }) {
  const f = loadFigures(load);
  const counts = headerCounts(rows);
  const percent = Math.round(f.memPercentNow);
  return (
    <div className="tp-list-foot" data-level={f.known ? f.level : "unknown"}>
      <span className="tp-load-text num">{t("term.screen.counts", { open: counts.open, host: counts.host, staff: counts.staff })}</span>
      {f.known && (
        <span className="tp-load" role="img" aria-label={t("load.percent", { percent })} title={t("load.percent", { percent })}>
          <span className="tp-load-fill" style={{ width: `${Math.min(100, percent)}%` }} />
        </span>
      )}
    </div>
  );
}
