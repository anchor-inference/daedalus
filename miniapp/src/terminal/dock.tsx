// The session's terminals: the dock under the conversation (tabs, a split, a height the operator
// drags), the full-screen view a terminal is maximised into, and on a phone the sheet that lists them.
//
// The dock is a set of views onto terminals that live in the page's registry. A tab's ✕ ends the
// terminal, asking first unless the host is sure only a prompt is lost, and takes the tab away; a
// finished one's ✕ only takes the tab. Keeping a process running without its tab is the menu's
// Detach, named for what it does: three crosses that all meant "hide" left forgotten shells running
// for days. A new terminal always asks where (picker.tsx). Every tab keeps its view mounted
// (hidden when not shown), so its connection stays up and an unseen line or a bell can mark the tab;
// a hidden view never sends a size, which is the rule the old web terminal broke.

import { CSSProperties, PointerEvent as ReactPointerEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useContextActions } from "../ui/context-menu";
import { createPortal } from "react-dom";
import { api, Project, TerminalCreate, TerminalEnv, TerminalEnvName, TerminalList, TerminalView as TerminalRow } from "../api";
import { confirmDialog, MenuItem, OverflowMenu, Sheet } from "../ui/dialogs";
import { plural, t } from "../i18n";
import { EnvPill } from "../envpill";
import { Icon } from "../icons";
import { peek, useQuery } from "../store";
import { errorText } from "../ui";
import { createTerminalConfirmed, endTerminal, endTerminals } from "./actions";
import { clampHeight, closeTab, DockState, loadDock, openTab, prune, replaceTab, sandboxOffer, saveDock, setSplit, splitCandidate, toggleDock } from "./dockstate";
import { PickedPlace, TerminalPicker } from "./picker";
import { useRenameSheet } from "./rowmenu";
import type { TerminalState } from "./instance";
import { isMac } from "./keys";
import { instanceFor, setTerminalEnvs, terminals } from "./terminals";
import { PhoneTerminal } from "./mobile";
import { CopyOutputButton, TerminalView } from "./view";

/** How often the session's terminals are listed while its screen is open. */
export const LIST_POLL_MS = 5000;

export type SessionTerminals = {
  loaded: boolean;
  rows: TerminalRow[];
  envs: TerminalEnv[];
  running: TerminalRow[];
  refresh: () => Promise<void>;
};

/** The session's terminals and the environments, listed by the host every few seconds. */
export function useSessionTerminals(sessionId: string): SessionTerminals {
  const query = useQuery<TerminalList>(sessionId ? `/api/terminals?owner_kind=session&owner_id=${encodeURIComponent(sessionId)}` : null, { pollMs: LIST_POLL_MS });
  const data = query.data;
  useEffect(() => {
    if (data?.envs) setTerminalEnvs(data.envs);
  }, [data]);
  return useMemo(() => {
    const rows = data?.terminals ?? [];
    return { loaded: !!data, rows, envs: data?.envs ?? [], running: rows.filter((r) => r.status === "running"), refresh: query.refresh };
  }, [data, query.refresh]);
}

/** Why no terminal can be opened, or "" when one can. */
export function unavailableReason(envs: TerminalEnv[], loaded: boolean): string {
  if (!loaded) return "";
  if (envs.some((e) => e.available)) return "";
  const container = envs.find((e) => e.env === "container");
  if (container && container.reason === "not_installed") return t("term.unavailable.container");
  return t("term.unavailable.none");
}

export type DockController = ReturnType<typeof useTerminalDock>;

/** Everything the dock, the header button, the phone sheet and the full view share for one session. */
export function useTerminalDock(sessionId: string, terms: SessionTerminals, options: { preferredEnv?: TerminalEnvName; phone: boolean; toast: (text: string) => void }) {
  const { preferredEnv, phone, toast } = options;
  const [state, setState] = useState<DockState>(() => loadDock(sessionId));
  const [full, setFull] = useState<string | null>(null);
  const [sheet, setSheet] = useState(false);
  const [focusToken, setFocusToken] = useState(0);
  const [states, setStates] = useState<Record<string, TerminalState>>({});
  // The open "where?" question: whether the new terminal takes a tab or the split's right pane, and
  // the control it hangs from (null: the dock's "+", once the dock is drawn).
  const [picking, setPicking] = useState<{ place: "tab" | "split"; anchor: HTMLElement | null } | null>(null);
  // Terminals made here that the next listing may not have caught up with yet: a poll answered a
  // moment before the create must not close the tab that was just opened.
  const fresh = useRef(new Set<string>());
  // The session screen is reused when the route moves to another session: the dock follows it.
  const [owner, setOwner] = useState(sessionId);
  if (owner !== sessionId) {
    setOwner(sessionId);
    setState(loadDock(sessionId));
    setFull(null);
    setSheet(false);
    setPicking(null);
  }

  useEffect(() => {
    if (sessionId) saveDock(sessionId, state);
  }, [sessionId, state]);

  useEffect(() => {
    if (!terms.loaded) return;
    const listed = terms.rows.map((r) => r.id);
    for (const id of listed) fresh.current.delete(id);
    setState((s) => prune(s, [...listed, ...fresh.current]));
  }, [terms.loaded, terms.rows]);

  const rowOf = useCallback((id: string | null) => (id ? terms.rows.find((r) => r.id === id) ?? null : null), [terms.rows]);
  const focus = useCallback(() => setFocusToken((n) => n + 1), []);
  const onState = useCallback((id: string, next: TerminalState) => setStates((all) => (all[id] === next ? all : { ...all, [id]: next })), []);

  /** Open a terminal where the picker said. The session owns it whatever the folder. */
  const create = useCallback(
    async (picked: PickedPlace, place: "tab" | "split" = "tab") => {
      const body: TerminalCreate = { env: picked.env, owner_kind: "session", owner_id: sessionId };
      if (picked.cwd) body.cwd = picked.cwd;
      if (picked.sandbox) body.sandbox = true;
      let row: TerminalRow | null;
      try {
        // The machine-wide cap is a question for the operator, not a refusal (`actions.ts`).
        row = await createTerminalConfirmed(body);
      } catch (error) {
        toast(errorText(error));
        return;
      }
      if (!row) return;
      fresh.current.add(row.id);
      if (row.sandbox_skipped?.length) toast(skippedText(row.sandbox_skipped));
      setState((s) => (place === "split" && s.active ? setSplit({ ...s, open: true }, row.id) : openTab(s, row.id)));
      if (phone) setFull(row.id);
      focus();
      void terms.refresh();
    },
    [terms, sessionId, phone, toast, focus],
  );

  /** Ask where a new terminal goes: every "+", the split with nothing to show, the new-terminal key. */
  const ask = useCallback(
    (place: "tab" | "split" = "tab", anchor: HTMLElement | null = null) => {
      if (terms.loaded && !terms.envs.some((e) => e.available)) {
        toast(unavailableReason(terms.envs, true) || t("term.unavailable.none"));
        return;
      }
      setState((s) => (s.open || phone ? s : { ...s, open: true }));
      setPicking({ place, anchor });
    },
    [terms, phone, toast],
  );

  const toggle = useCallback(() => {
    if (full) {
      setFull(null);
      focus();
      return;
    }
    const out = toggleDock(state, terms.running.map((r) => r.id));
    setState(out.state);
    if (out.create) ask();
    else if (out.state.open) focus();
  }, [full, state, terms.running, ask, focus]);

  const activate = useCallback(
    (id: string) => {
      instanceFor(id)?.interact();
      setState((s) => (s.split === id ? s : { ...s, active: id }));
      focus();
    },
    [focus],
  );

  const detach = useCallback((id: string) => {
    setState((s) => closeTab(s, id));
    setFull((f) => (f === id ? null : f));
  }, []);

  const show = useCallback(
    (id: string) => {
      instanceFor(id)?.interact();
      setState((s) => openTab(s, id));
      if (phone) setFull(id);
      focus();
    },
    [phone, focus],
  );

  const end = useCallback(
    async (id: string) => {
      await endTerminal(id, states[id]?.title, toast);
      void terms.refresh();
    },
    [states, terms, toast],
  );

  /** A tab's ✕, its middle click, Ctrl+Shift+W: end the terminal and take its tab; a finished one's
   *  tab only goes. Declined, both stay. */
  const close = useCallback(
    async (id: string) => {
      const row = terms.rows.find((r) => r.id === id);
      const finished = !!states[id]?.exit || (!!row && row.status !== "running");
      if (!finished && !(await endTerminal(id, states[id]?.title, toast))) return;
      detach(id);
      void terms.refresh();
    },
    [terms, states, toast, detach],
  );

  /** Every other running tab, after one question that lists them. */
  const endOthers = useCallback(
    async (id: string) => {
      const others = state.tabs.filter((tab) => tab !== id).map((tab) => terms.rows.find((r) => r.id === tab)).filter((r): r is TerminalRow => !!r && r.status === "running");
      const ended = await endTerminals(others, toast);
      if (ended === null) return;
      for (const row of others) detach(row.id);
      void terms.refresh();
    },
    [state.tabs, terms, toast, detach],
  );

  const restart = useCallback(
    async (id: string, sandbox?: boolean) => {
      if (sandbox !== undefined) {
        // Switching the sandbox restarts the program: what runs in it ends, so a busy one asks first.
        let current: TerminalRow;
        try {
          current = await api.terminal(id);
        } catch (error) {
          toast(errorText(error));
          return;
        }
        const title = states[id]?.title || current.title || t("term.untitled");
        if (current.status === "running" && current.live?.busy && !(await confirmDialog({ title: t(sandbox ? "term.sandbox.restartOn" : "term.sandbox.restartOff"), body: t("term.end.confirm", { command: title }), action: t("term.restart"), danger: true }))) return;
      }
      let row: TerminalRow;
      try {
        row = await api.restartTerminal(id, sandbox);
      } catch (error) {
        toast(errorText(error));
        return;
      }
      fresh.current.add(row.id);
      if (row.sandbox_skipped?.length) toast(skippedText(row.sandbox_skipped));
      setState((s) => replaceTab(s, id, row.id));
      setFull((f) => (f === id ? row.id : f));
      // The old instance showed a finished process; once its view has gone, nothing needs it.
      setTimeout(() => terminals.remove(id), 0);
      focus();
      void terms.refresh();
    },
    [terms, toast, focus, states],
  );

  const remove = useCallback(
    async (id: string) => {
      try {
        await api.removeTerminal(id);
      } catch (error) {
        toast(errorText(error));
        return;
      }
      detach(id);
      setTimeout(() => terminals.remove(id), 0);
      void terms.refresh();
    },
    [detach, terms, toast],
  );

  const split = useCallback(() => {
    if (state.split) {
      setState((s) => setSplit(s, null));
      return;
    }
    const other = splitCandidate(state);
    if (other) {
      instanceFor(other)?.interact();
      setState((s) => setSplit(s, other));
    } else ask("split");
  }, [state, ask]);

  const maximise = useCallback(
    (id: string) => {
      instanceFor(id)?.interact();
      setFull(id);
      focus();
    },
    [focus],
  );

  const restore = useCallback(() => {
    const id = full;
    setFull(null);
    if (id) instanceFor(id)?.interact();
    focus();
  }, [full, focus]);

  return {
    sessionId,
    terms,
    state,
    setState,
    full,
    sheet,
    setSheet,
    focusToken,
    states,
    onState,
    rowOf,
    create,
    ask,
    picking,
    setPicking,
    toggle,
    activate,
    detach,
    close,
    endOthers,
    show,
    end,
    restart,
    remove,
    split,
    maximise,
    restore,
    preferredEnv,
    phone,
    toast,
    reason: unavailableReason(terms.envs, terms.loaded),
  };
}

// ── pieces ──────────────────────────────────────────────────────────────────────────────────

/** The shield of a sandboxed terminal. */
function Shield({ row }: { row: TerminalRow | null }) {
  if (!row?.sandbox) return null;
  return (
    <span className="term-shield" title={t("term.sandbox.on")} aria-label={t("term.sandbox.on")} role="img">
      <Icon name="shield" size={12} />
    </span>
  );
}

/** What a sandboxed start left read-only, for a toast. */
function skippedText(skipped: { path: string; reason: string }[]): string {
  return t("term.sandbox.skipped", { paths: skipped.map((s) => `${s.path} (${s.reason})`).join(", ") });
}

function tabTitle(row: TerminalRow | null, state: TerminalState | undefined): string {
  return state?.title || row?.title || t("term.untitled");
}

type Dot = "exited" | "bell" | "unseen" | "cmd-running" | "cmd-ok" | "cmd-failed" | "";

/**
 * What a tab's dot says, most urgent first: the process ended, it rang, its last command failed, it
 * printed unseen, a command runs, the last one succeeded. A failure outranks unseen output because it
 * is the thing to go and look at; success is the quietest thing a dot can say.
 */
function tabDot(row: TerminalRow | null, state: TerminalState | undefined): Dot {
  if (state?.exit || row?.status === "exited" || row?.status === "lost") return "exited";
  if (state?.bell) return "bell";
  const last = state?.commands.last;
  if (last?.result === "failed") return "cmd-failed";
  if (state?.unseen) return "unseen";
  if (last?.result === "running") return "cmd-running";
  if (last?.result === "ok") return "cmd-ok";
  return "";
}

/** The dot's words, for a tooltip and a screen reader: how the last command went. */
function dotTitle(state: TerminalState | undefined): string | undefined {
  const last = state?.commands.last;
  if (!last) return undefined;
  const command = last.command || t("term.marks.command");
  return last.result === "failed" ? t("term.marks.failed", { command, code: last.exitCode ?? "?" }) : t(`term.marks.${last.result}`, { command });
}

/** What a finished tab shows: the signal that ended it, or its exit code. */
function exitCode(row: TerminalRow | null, state: TerminalState | undefined): string {
  const signal = state?.exit?.signal ?? row?.exit_signal;
  if (signal) return signal;
  const code = state?.exit?.code ?? row?.exit_code;
  return code === null || code === undefined ? "" : String(code);
}

/** The menu behind ▾: the session's terminals without a tab, and the active one's own commands —
 *  the sandbox switch that restarts it, Detach, End. Opening a new one is the "+"'s question only. */
function dockMenu(dock: DockController, active: string | null): MenuItem[] {
  const env = (name: TerminalEnvName) => dock.terms.envs.find((e) => e.env === name);
  const items: MenuItem[] = [];
  const untabbed = dock.terms.running.filter((r) => !dock.state.tabs.includes(r.id));
  for (const row of untabbed) items.push({ label: t("term.open", { title: tabTitle(row, dock.states[row.id]) }), icon: "terminal", onSelect: () => dock.show(row.id) });
  const activeRow = dock.rowOf(active);
  if (active && activeRow?.status === "running") {
    if (items.length) items.push("-");
    if (activeRow.sandbox) items.push({ label: t("term.sandbox.restartOff"), icon: "reload", onSelect: () => void dock.restart(active, false) });
    else {
      const offer = sandboxOffer(env(activeRow.env));
      items.push({ label: t("term.sandbox.restartOn"), icon: "shield", disabled: !offer.ok, hint: offer.ok ? undefined : t("term.sandbox.unavailable", { reason: offer.reason }), onSelect: () => void dock.restart(active, true) });
    }
    items.push({ label: t("term.detach"), icon: "unlink", onSelect: () => dock.detach(active) });
    items.push({ label: t("term.end"), icon: "stop", danger: true, onSelect: () => void dock.end(active) });
  }
  return items;
}

/** The dock's key as a tooltip names it: VS Code's, the one people already know. */
function dockKeyHint(): string {
  return isMac() ? "\u2318J" : "Ctrl+J";
}

/** The new-terminal key, VS Code's too. */
function newKeyHint(): string {
  return "Ctrl+Shift+`";
}

/** The header's terminal button: a count, and on a phone the sheet instead of the dock. */
export function TerminalButton({ dock, phone }: { dock: DockController; phone: boolean }) {
  const count = dock.terms.running.length;
  const disabled = !!dock.reason && count === 0;
  const label = disabled ? dock.reason : phone ? t("term.list") : t("term.dock.toggle");
  const unseen = dock.state.tabs.some((id) => dock.states[id]?.unseen || dock.states[id]?.bell);
  return (
    <button
      className={`iconbtn term-button ${!phone && dock.state.open ? "on" : ""}`}
      onClick={() => (phone ? dock.setSheet(true) : dock.toggle())}
      aria-label={label}
      title={phone ? label : `${label} (${dockKeyHint()})`}
      disabled={disabled}
      {...(phone ? {} : { "aria-pressed": dock.state.open })}
      data-count={count}
    >
      <Icon name="terminal" />
      {count > 0 && <span className={`term-count num ${unseen && !dock.state.open ? "unseen" : ""}`}>{count}</span>}
    </button>
  );
}

// ── the dock ────────────────────────────────────────────────────────────────────────────────

export function TerminalDock({ dock, workspace, fileOpener }: { dock: DockController; workspace?: string; fileOpener?: (path: string, line?: number) => void }) {
  const { state } = dock;
  const box = useRef<HTMLDivElement>(null);
  const [column, setColumn] = useState(0);

  // The dock may not outgrow four fifths of the conversation's column, measured as it is now.
  useEffect(() => {
    const parent = box.current?.parentElement;
    if (!parent) return;
    const measure = () => setColumn(parent.clientHeight);
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(parent);
    return () => observer.disconnect();
  }, []);

  const height = column ? clampHeight(state.height, column) : state.height;

  const drag = (e: ReactPointerEvent<HTMLDivElement>) => {
    e.preventDefault();
    const startY = e.clientY;
    const startH = height;
    const target = e.currentTarget;
    target.setPointerCapture(e.pointerId);
    const move = (ev: PointerEvent) => {
      const next = clampHeight(startH + (startY - ev.clientY), box.current?.parentElement?.clientHeight ?? column);
      for (const id of [state.active, state.split]) if (id) instanceFor(id)?.interact();
      dock.setState((s) => (s.height === next ? s : { ...s, height: next }));
    };
    const up = () => {
      target.removeEventListener("pointermove", move);
      target.removeEventListener("pointerup", up);
      target.removeEventListener("pointercancel", up);
    };
    target.addEventListener("pointermove", move);
    target.addEventListener("pointerup", up);
    target.addEventListener("pointercancel", up);
  };

  const active = state.active;
  const activeRow = dock.rowOf(active);
  const shown = (id: string) => state.open && dock.full !== id && (id === active || id === state.split);
  const menu = dockMenu(dock, active);
  const plus = useRef<HTMLButtonElement>(null);
  const picking = dock.picking;

  return (
    <div ref={box} className={`term-dock ${state.open ? "open" : "closed"}`} style={{ "--dock-h": `${height}px` } as CSSProperties} aria-label={t("term.dock")} role="region">
      <div className="term-grip" role="separator" aria-orientation="horizontal" aria-label={t("term.dock.resize")} onPointerDown={drag} />
      <div className="term-bar">
        <div className="term-tabs" role="tablist" aria-label={t("term.dock")}>
          {state.tabs.map((id) => <DockTab key={id} dock={dock} id={id} on={id === active || id === state.split} />)}
          <button ref={plus} className="iconbtn small flat term-plus" onClick={(e) => dock.ask("tab", e.currentTarget)} aria-label={t("term.new")} title={`${t("term.new")} (${newKeyHint()})`} aria-haspopup="menu" aria-expanded={!!dock.picking} disabled={!!dock.reason}><Icon name="plus" size={16} /></button>
          {menu.length > 0 && <OverflowMenu small icon="chevron" label={t("term.menu")} className="flat" items={menu} />}
        </div>
        <div className="term-tools">
          {activeRow && <EnvPill env={activeRow.env} />}
          <CopyOutputButton id={active} state={active ? dock.states[active] : undefined} />
          <button className="iconbtn small flat" onClick={() => active && instanceFor(active)?.openSearch()} aria-label={t("term.search")} title={`${t("term.search")} (Ctrl+Shift+F)`} disabled={!active}><Icon name="search" size={16} /></button>
          <button className={`iconbtn small flat ${state.split ? "on" : ""}`} onClick={dock.split} aria-label={t("term.split")} title={t("term.split")} aria-pressed={!!state.split} disabled={!active}><Icon name="split" size={16} /></button>
          <button className="iconbtn small flat" onClick={() => active && dock.maximise(active)} aria-label={t("term.maximize")} title={t("term.maximize")} disabled={!active}><Icon name="expand" size={16} /></button>
          <button className="iconbtn small flat term-hide" onClick={dock.toggle} aria-label={t("term.dock.close")} title={`${t("term.dock.close")} (${dockKeyHint()})`}><Icon name="chevron" size={16} /></button>
        </div>
      </div>
      <div className={`term-panes ${state.split && state.open ? "split" : ""}`}>
        {!state.tabs.length && <div className="term-empty sub">{dock.reason || t("term.empty")}</div>}
        {state.tabs.map((id) =>
          dock.full === id ? (
            <div key={id} className="term-away sub">{t("term.away")}</div>
          ) : (
            <div key={id} className={`term-slot ${id === active ? "left" : id === state.split ? "right" : "hidden"}`}>
              <TerminalView
                id={id}
                visible={shown(id)}
                env={dock.rowOf(id)?.env}
                workspace={workspace}
                fileOpener={fileOpener}
                focusToken={id === active || id === state.split ? dock.focusToken : undefined}
                onState={dock.onState}
                onRestart={() => void dock.restart(id)}
                onRemove={() => void dock.remove(id)}
              />
            </div>
          ),
        )}
      </div>
      {picking && state.open && <DockPicker dock={dock} anchor={picking.anchor ?? plus.current} workspace={workspace} />}
    </div>
  );
}

/** The "where?" question for a session terminal: the session's own folder in the container, the
 *  operator's home on the host, or a folder they choose. */
function DockPicker({ dock, anchor, workspace, phone = false }: { dock: DockController; anchor: HTMLElement | null; workspace?: string; phone?: boolean }) {
  const place = dock.picking?.place ?? "tab";
  const projects = peek<{ projects?: Project[] }>("/api/projects")?.projects ?? [];
  return (
    <TerminalPicker
      envs={dock.terms.envs}
      folders={{ container: { path: workspace || null }, host: { path: null } }}
      projects={projects}
      suggested={dock.preferredEnv}
      anchor={anchor}
      align="left"
      phone={phone}
      // The quick places start where the host puts a session's terminal (its own folder, or the
      // project's); only a folder chosen by hand is sent.
      onPick={(picked) => dock.create(picked.choice === "folder" ? picked : { ...picked, cwd: undefined }, place)}
      onClose={() => dock.setPicking(null)}
    />
  );
}

/** One tab: its dot, title and exit code; ✕ and a middle click end it; a right click has Rename,
 *  Detach, End and End the others. */
function DockTab({ dock, id, on }: { dock: DockController; id: string; on: boolean }) {
  const row = dock.rowOf(id);
  const st = dock.states[id];
  const dot = tabDot(row, st);
  const title = tabTitle(row, st);
  const finished = dot === "exited";
  const box = useRef<HTMLDivElement>(null);
  const renaming = useRenameSheet(id, row?.title ?? "", dock.toast);
  const others = dock.state.tabs.some((tab) => tab !== id && dock.rowOf(tab)?.status === "running");
  const items = useMemo<MenuItem[]>(() => [
    { label: t("term.rename"), icon: "pen", hint: t("term.rename.hint"), disabled: !row, onSelect: renaming.open },
    { label: t("term.detach"), icon: "unlink", onSelect: () => dock.detach(id) },
    "-",
    finished ? { label: t("term.close.tab"), icon: "close", onSelect: () => dock.detach(id) } : { label: t("term.end"), icon: "stop", danger: true, hint: "Ctrl+Shift+W", onSelect: () => void dock.close(id) },
    { label: t("term.end.others"), icon: "stop", danger: true, disabled: !others, onSelect: () => void dock.endOthers(id) },
  ], [row, renaming.open, dock, id, finished, others]);
  useContextActions(box, items);
  const label = finished ? t("term.close.tab") : t("term.close");
  return (
    <div
      ref={box}
      role="tab"
      tabIndex={0}
      aria-selected={on}
      className={`term-tab ${on ? "on" : ""} ${row?.env === "host" ? "host" : ""}`}
      data-tab={id}
      onClick={() => dock.activate(id)}
      // The middle button closes a tab as in every browser and editor. Its press is held back too, or
      // the browser starts autoscroll under the pointer.
      onMouseDown={(e) => { if (e.button === 1) e.preventDefault(); }}
      onAuxClick={(e) => { if (e.button === 1) { e.preventDefault(); void dock.close(id); } }}
      onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); dock.activate(id); } }}
      title={row?.cwd || undefined}
    >
      <span className={`term-dot ${dot}`} aria-hidden="true" title={dotTitle(st)} data-result={st?.commands.last?.result} />
      <Shield row={row} />
      <span className="term-tab-title truncate">{title}</span>
      {finished && exitCode(row, st) && <span className="term-tab-code num">{exitCode(row, st)}</span>}
      <button className="term-tab-x" aria-label={label} title={finished ? label : `${label} (Ctrl+Shift+W)`} onClick={(e) => { e.stopPropagation(); void dock.close(id); }}>
        <Icon name="close" size={12} />
      </button>
      {renaming.layer}
    </div>
  );
}

// ── full screen ─────────────────────────────────────────────────────────────────────────────

/**
 * One terminal over the whole window: the dock's maximise, and a phone's only way to show one. The
 * same instance moves here (its element is re-parented), so nothing is reattached or redrawn from a
 * snapshot. Escape is left to the program in the terminal; the header's button restores.
 */
export function TerminalFull({ dock, phone, workspace, fileOpener }: { dock: DockController; phone: boolean; workspace?: string; fileOpener?: (path: string, line?: number) => void }) {
  const id = dock.full;
  if (!id) return null;
  const row = dock.rowOf(id);
  const st = dock.states[id];
  // A phone gets the terminal with its keys row, compose line and touch (mobile.tsx).
  if (phone) {
    return (
      <PhoneTerminal
        id={id}
        row={row}
        onBack={dock.restore}
        onEnd={() => void dock.end(id)}
        onRestart={() => void dock.restart(id)}
        onRemove={() => void dock.remove(id)}
        workspace={workspace}
        fileOpener={fileOpener}
        focusToken={dock.focusToken}
        onState={dock.onState}
      />
    );
  }
  return createPortal(
    <div className="term-full" role="dialog" aria-label={tabTitle(row, st)} data-full={id}>
      <div className="term-full-head">
        <button className="iconbtn small flat" onClick={dock.restore} aria-label={phone ? t("shell.back") : t("term.restore")} title={phone ? t("shell.back") : t("term.restore")}>
          <Icon name={phone ? "back" : "columns"} size={16} />
        </button>
        <Shield row={row} />
        <span className="term-full-title truncate">{tabTitle(row, st)}</span>
        {row && <EnvPill env={row.env} />}
        <div className="grow" />
        <CopyOutputButton id={id} state={st} />
        <button className="iconbtn small flat" onClick={() => instanceFor(id)?.openSearch()} aria-label={t("term.search")} title={t("term.search")}><Icon name="search" size={16} /></button>
        {row?.status === "running" && (
          <button className="iconbtn small flat danger" onClick={() => void dock.end(id)} aria-label={t("term.end")} title={t("term.end")}><Icon name="stop" size={16} /></button>
        )}
      </div>
      <TerminalView
        id={id}
        visible
        env={row?.env}
        workspace={workspace}
        fileOpener={fileOpener}
        focusToken={dock.focusToken}
        onState={dock.onState}
        onRestart={() => void dock.restart(id)}
        onRemove={() => void dock.remove(id)}
      />
    </div>,
    document.body,
  );
}

// ── the phone's list ────────────────────────────────────────────────────────────────────────

export function TerminalSheet({ dock, workspace }: { dock: DockController; workspace?: string }) {
  // A phone has no dock to hang the question from: it comes up as a sheet of its own.
  if (!dock.sheet) return dock.phone && dock.picking ? <DockPicker dock={dock} anchor={null} workspace={workspace} phone /> : null;
  const rows = dock.terms.rows;
  const close = () => dock.setSheet(false);
  return (
    <Sheet title={t("term.list")} onClose={close} className="term-sheet">
      {rows.length === 0 && <div className="sub term-sheet-empty">{dock.reason || t("term.empty")}</div>}
      {rows.map((row) => (
        <button key={row.id} className="term-sheet-row" onClick={() => { close(); dock.show(row.id); }}>
          <span className={`term-dot ${tabDot(row, dock.states[row.id])}`} aria-hidden="true" />
          <Shield row={row} />
          <span className="term-sheet-title truncate">{tabTitle(row, dock.states[row.id])}</span>
          <EnvPill env={row.env} />
          <span className="sub num">{row.status === "running" ? t("term.running") : t("term.exited", { code: row.exit_code ?? "?" })}</span>
        </button>
      ))}
      <div className="term-sheet-actions">
        <button className="btn" disabled={!!dock.reason} onClick={() => { close(); dock.ask(); }}><Icon name="plus" size={16} />{t("term.new")}</button>
      </div>
    </Sheet>
  );
}

/** How many terminals a delete would end, for the session's delete dialog. */
export function endsTerminals(n: number): string {
  return n > 0 ? plural("term.delete.ends", n) : "";
}
