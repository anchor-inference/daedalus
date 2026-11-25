// The Terminals workspace on a desktop: a grid of one, two or four live terminals, each in a pane that
// carries its own header (state, title, folder, owner, its tools) and its own foot (who is typing, and
// the status line). The page header, the list beside the grid and the side panel are the screen's.
//
// The state is `workspacestate.ts`'s and the screen owns it; this file draws it and turns pointer and
// key gestures into its transitions. A pane's terminal is never re-created by a swap, a layout change
// or a splitter: the panes are keyed by terminal and placed by `grid-area`, so moving one changes a
// style and not the DOM, and a program keeps streaming while its pane is dragged.
//
// What the screen hangs on a pane is named in `WorkspaceSlots`: the folder and branch in the header,
// a line over the terminal's foot to write to the program or the agent in it, the foot itself, and a
// panel beside the grid.

import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties, type KeyboardEvent as ReactKeyboardEvent, type PointerEvent as ReactPointerEvent, type ReactNode } from "react";
import type { TerminalView as TerminalRow } from "../api";
import { Icon, type IconName } from "../icons";
import { MenuItem, OverflowMenu } from "../ui/dialogs";
import { Segmented, IconButton } from "../ui/index";
import { t } from "../i18n";
import { navigate, projectPagePath, sessionPath } from "../router";
import { EnvPill } from "../envpill";
import { fontSizeStep, type TerminalState } from "./instance";
import { isMac } from "./keys";
import { ownerPath, ownerWho, running } from "./preview";
import { paneState, withMessage, type PaneState } from "./status";
import { instanceFor } from "./terminals";
import { copyLastOutput, TerminalView } from "./view";
import { COLS_DEBOUNCE_MS } from "./fit";
import { clampFraction, close, focus, maximise, setFractions, setLayout, shownSlots, swap, LAYOUTS, SLOTS, type Layout, type Workspace } from "./workspacestate";
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

/** Where the screen hangs its parts. Each is optional; an absent one draws nothing. */
export type WorkspaceSlots = {
  /** In a pane's header, after its title: the folder and the branch. */
  where?: (pane: PaneContext) => ReactNode;
  /** In a pane's foot: who is typing, and the status line. */
  footer?: (pane: PaneContext) => ReactNode;
  /** Between a pane's terminal and its foot: a line to write to the program or the agent in it. */
  compose?: (pane: PaneContext) => ReactNode;
  /** Beside the grid, on the right: a panel about the active pane. */
  side?: ReactNode;
};

export type TerminalWorkspaceProps = {
  ws: Workspace;
  update: (change: (ws: Workspace) => Workspace) => void;
  rows: TerminalRow[];
  states: Record<string, TerminalState>;
  onState: (id: string, state: TerminalState) => void;
  /** Which terminal is to take the keyboard, and a token that changes with every request. Only the
   *  gestures that choose a pane without touching it (Alt+N, the list) ask; a click inside a pane
   *  already puts the focus where the person clicked, and a request then would take it away from the
   *  field they clicked into (a rename). */
  focus: { id: string | null; token: number };
  onFocusRequest: (id: string) => void;
  /** Shows a terminal in the workspace (the active place, or the first empty one). */
  onOpen: (id: string) => void;
  onRestart: (id: string) => void;
  onRemove: (id: string) => void;
  onEnd: (id: string) => void;
  /** A pane's ✕, Ctrl+Shift+W: end the terminal (asking when anything runs)
   *  and free its place; a finished one's place is only freed. */
  onCloseTerminal: (id: string) => void;
  onRename: (id: string, title: string) => void;
  /** Ask where a new terminal goes, hanging the question from the control pressed. */
  onNew: (anchor: HTMLElement | null) => void;
  toast: (text: string) => void;
  slots?: WorkspaceSlots;
  /** The terminals whose agent another agent's message has just reached: their pane says "message"
   *  for a moment (`MESSAGE_FLASH_MS`). The screen owns the timing. */
  messaged?: ReadonlySet<string>;
};

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

const LAYOUT_WORD: Record<Layout, string> = { 1: "term.layout.one", 2: "term.layout.two", 4: "term.layout.grid" };

/** One, two or four panes, in the header: by the word each makes, "One", "Two", "Grid". */
export function LayoutSwitch({ layout, onChange }: { layout: Layout; onChange: (layout: Layout) => void }) {
  return (
    <Segmented
      className="tp-layouts"
      label={t("term.ws.layout")}
      value={String(layout) as "1" | "2" | "4"}
      onChange={(id) => onChange(Number(id) as Layout)}
      options={LAYOUTS.map((n) => ({ id: String(n) as "1" | "2" | "4", label: t(LAYOUT_WORD[n]) }))}
    />
  );
}

export function TerminalWorkspace(props: TerminalWorkspaceProps) {
  const { ws, update, rows, states, slots, messaged } = props;
  const byId = useMemo(() => new Map(rows.map((r) => [r.id, r])), [rows]);
  const grid = useRef<HTMLDivElement>(null);
  const shown = shownSlots(ws);

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
    // The tracks sit inside the grid's padding, so a fraction is measured from the first pane's edge.
    const box = grid.current.getBoundingClientRect();
    const style = getComputedStyle(grid.current);
    const left = box.left + parseFloat(style.paddingLeft);
    const top = box.top + parseFloat(style.paddingTop);
    const width = box.width - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
    const height = box.height - parseFloat(style.paddingTop) - parseFloat(style.paddingBottom);
    const fraction = resizing === "col" ? (event.clientX - left) / width : (event.clientY - top) / height;
    update((w) => setFractions(w, { [resizing]: fraction }));
  };
  useEffect(() => {
    if (!resizing) return;
    // The whole page shows the resize cursor while a splitter moves, not only the strip under it.
    document.body.classList.add(`tp-resizing-${resizing}`);
    return () => document.body.classList.remove(`tp-resizing-${resizing}`);
  }, [resizing]);
  const splitKeys = (axis: "col" | "row", event: ReactKeyboardEvent<HTMLElement>) => {
    const step = event.key === "ArrowLeft" || event.key === "ArrowUp" ? -0.05 : event.key === "ArrowRight" || event.key === "ArrowDown" ? 0.05 : 0;
    if (!step) return;
    event.preventDefault();
    update((w) => setFractions(w, { [axis]: clampFraction(w[axis] + step) }));
  };

  const dragged = drag ? context(drag.from) : null;

  return (
    <div className={`tp-stage ${slots?.side ? "has-side" : ""}`} data-layout={ws.layout}>
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
              canSplit={ws.layout < 4 && ws.zoom === null}
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
              onDetach={() => update((w) => close(w, slot))}
              onMaximise={() => update((w) => maximise(w, slot))}
              onSplit={() => update((w) => setLayout(w, w.layout === 1 ? 2 : 4))}
              onPick={(id) => { update((w) => focus(w, slot)); props.onOpen(id); }}
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
  canSplit: boolean;
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
  onDetach: () => void;
  onMaximise: () => void;
  onSplit: () => void;
  onPick: (id: string) => void;
  actions: Pick<TerminalWorkspaceProps, "onRestart" | "onRemove" | "onEnd" | "onCloseTerminal" | "onRename" | "onNew" | "onState">;
};

const OWNER_ICON: Record<TerminalRow["owner"]["kind"], IconName> = { session: "chat", staff: "bots", project: "folder", free: "user" };

/** One place of the grid: its header, its terminal (or the chooser of an empty place), and the slots under it. */
export function WorkspacePane({ pane, hidden, grid, maximised, canMaximise, canSplit, gridArea, over, focusToken, rows, ws, slots, onChoose, onGrip, onGripMove, onGripUp, onDetach, onMaximise, onSplit, onPick, actions }: PaneProps) {
  const { id, row, state, status, active, slot } = pane;
  const title = paneTitle(id, row, state);
  const [editing, setEditing] = useState(false);
  const owner = row ? ownerPath(row, (sid) => sessionPath(sid), (pid) => projectPagePath(pid, "team")) : null;
  const live = id ? (row ? running(row) : !state?.exit) : false;
  const where = slots?.where?.(pane);
  const footer = slots?.footer?.(pane);
  const compose = slots?.compose?.(pane);

  const menu: MenuItem[] = id ? [
    ...(state?.commands.active ? [{ label: t("term.marks.copy"), icon: "copy" as const, disabled: !state.commands.ended, onSelect: () => { const i = instanceFor(id); if (i) void copyLastOutput(i); } }] : []),
    { label: t("term.search"), icon: "search", hint: "Ctrl+Shift+F", onSelect: () => instanceFor(id)?.openSearch() },
    { label: t("term.font.smaller"), icon: "compact", onSelect: () => fontSizeStep("font-smaller") },
    { label: t("term.font.bigger"), icon: "expand", onSelect: () => fontSizeStep("font-bigger") },
    { label: t("term.rename"), icon: "pen", hint: t("term.ws.rename.hint"), onSelect: () => setEditing(true) },
    ...(owner ? [{ label: t("term.ws.owner.open"), icon: "external" as const, onSelect: () => navigate(owner) }] : []),
    // Freeing the place and keeping the process is a choice made by name, never the ✕.
    { label: t("term.ws.detach"), icon: "unlink", onSelect: onDetach },
    "-",
    ...(live ? [{ label: t("term.end"), icon: "stop" as const, danger: true, onSelect: () => actions.onEnd(id) }] : [
      { label: t("term.restart"), icon: "reload" as const, onSelect: () => actions.onRestart(id) },
      { label: t("term.remove"), icon: "trash" as const, danger: true, onSelect: () => actions.onRemove(id) },
    ]),
  ] : [];

  const ownerChip = row && row.owner.kind !== undefined ? (
    <>
      <Icon name={OWNER_ICON[row.owner.kind]} size={14} />
      <span className="truncate">{ownerWho(row)}</span>
    </>
  ) : null;

  return (
    <section
      className={`tp-pane ${hidden ? "hidden" : ""} ${active ? "on" : ""} ${row?.env === "host" ? "host" : ""} ${over ? "drop" : ""} ${id ? "" : "empty"}`}
      style={{ gridArea }}
      data-slot={slot}
      data-pane={id ?? ""}
      data-state={id ? status : "empty"}
      aria-label={title}
      onPointerDownCapture={onChoose}
      onFocusCapture={onChoose}
    >
      <header className="tp-pane-head">
        {grid && <span className="tp-grip" onPointerDown={onGrip} onPointerMove={onGripMove} onPointerUp={onGripUp} onPointerCancel={onGripUp} title={t("term.ws.drag")} aria-label={t("term.ws.drag")} role="button" />}
        <StateDot status={status} className={id ? "" : "none"} />
        {editing && id ? (
          <RenameField initial={row?.title ?? title} onDone={(text) => { setEditing(false); if (text !== null && text !== (row?.title ?? "")) actions.onRename(id, text); }} />
        ) : (
          <span className="tp-pane-title" title={id ? `${title} · ${t("term.ws.rename.hint")}` : title} onDoubleClick={() => { if (id) setEditing(true); }}>
            {row?.sandbox && <Icon name="shield" size={12} />}
            <span className="truncate">{title}</span>
          </span>
        )}
        {row?.env === "host" && <EnvPill env="host" tiny />}
        <span className="tp-pane-where">{where}</span>
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
        {ownerChip && (owner ? (
          <a className="tp-pane-owner" href={owner} title={ownerWho(row!)} aria-label={`${t("term.ws.owner.open")}: ${ownerWho(row!)}`} onClick={(e) => { e.preventDefault(); navigate(owner); }}>{ownerChip}</a>
        ) : (
          <span className="tp-pane-owner" title={ownerWho(row!)}>{ownerChip}</span>
        ))}
        <span className="tp-pane-tools">
        {id && canSplit && <IconButton icon="columns" label={t("term.ws.split")} size={32} className="flat tp-pane-split" onClick={onSplit} />}
        {id && canMaximise && <IconButton icon={maximised ? "compact" : "expand"} label={t(maximised ? "term.ws.restore" : "term.ws.maximise")} size={32} pressed={maximised} className="flat tp-pane-max" onClick={onMaximise} />}
        {id && <OverflowMenu className="flat tp-pane-menu" icon="more" label={t("term.ws.pane.menu", { title })} items={menu} />}
        {id && <IconButton icon="close" label={live ? t("term.close") : t("term.ws.close.pane")} size={32} kbd={live ? "Ctrl+Shift+W" : undefined} className="flat tp-pane-close" onClick={() => actions.onCloseTerminal(id)} />}
        </span>
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
function EmptyPane({ rows, ws, onPick, onNew }: { rows: TerminalRow[]; ws: Workspace; onPick: (id: string) => void; onNew: (anchor: HTMLElement | null) => void }) {
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
              {r.env === "host" && <EnvPill env="host" tiny />}
            </button>
          ))}
        </div>
      )}
      <button type="button" className="btn small tp-empty-new" onClick={(e) => onNew(e.currentTarget)} aria-haspopup="menu">
        <Icon name="plus" size={14} />
        <span>{t("term.new")}</span>
      </button>
    </div>
  );
}
