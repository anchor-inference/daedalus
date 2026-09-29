// The session's terminals: the dock under the conversation (tabs, a split, a height the operator
// drags), the full-screen view a terminal is maximised into, and on a phone the sheet that lists them.
//
// The dock is a set of views onto terminals that live in the page's registry. A tab's ✕ only takes
// the view away — the process runs on and the tab comes back from the menu; ending a process is the
// separate End, which asks first when something is running in it. Every tab keeps its view mounted
// (hidden when not shown), so its connection stays up and an unseen line or a bell can mark the tab;
// a hidden view never sends a size, which is the rule the old web terminal broke.

import { CSSProperties, PointerEvent as ReactPointerEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { api, TerminalCreate, TerminalEnv, TerminalEnvName, TerminalList, TerminalView as TerminalRow } from "../api";
import { confirmDialog, MenuItem, OverflowMenu, Sheet } from "../dialogs";
import { plural, t } from "../i18n";
import { EnvPill } from "../envpill";
import { Icon } from "../icons";
import { useQuery } from "../store";
import { errorText } from "../ui";
import { createTerminalConfirmed, endTerminal } from "./actions";
import { clampHeight, closeTab, DockState, loadDock, loadSandboxChoice, openTab, prune, replaceTab, sandboxOffer, sandboxToggle, saveDock, saveSandboxChoice, setSplit, splitCandidate, toggleDock } from "./dockstate";
import type { TerminalState } from "./instance";
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

/** The environment a new terminal goes to: the project's default when it is up, else the first that is. */
export function pickEnv(envs: TerminalEnv[], preferred?: TerminalEnvName): TerminalEnvName | null {
  const up = envs.filter((e) => e.available);
  if (preferred && up.some((e) => e.env === preferred)) return preferred;
  return (up.find((e) => e.env === "container") ?? up[0])?.env ?? null;
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
  // New terminals in the sandbox: the menu's checkbox, remembered for the device.
  const [boxed, setBoxed] = useState(loadSandboxChoice);
  const toggleSandbox = useCallback(() => {
    setBoxed((on) => {
      saveSandboxChoice(!on);
      return !on;
    });
  }, []);
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

  const create = useCallback(
    async (env?: TerminalEnvName, place: "tab" | "split" = "tab") => {
      const chosen = env ?? pickEnv(terms.envs, preferredEnv);
      if (!chosen) {
        toast(unavailableReason(terms.envs, true) || t("term.unavailable.none"));
        return;
      }
      const body: TerminalCreate = { env: chosen, owner_kind: "session", owner_id: sessionId };
      // Where no environment can sandbox, the checkbox shows as unavailable and does not apply: a
      // stale tick from a visit when it could must not leave the operator with no terminal at all.
      if (boxed && sandboxToggle(terms.envs).ok) {
        // Asked for and not available in this environment is a wall that is missing, not a detail:
        // the terminal is not opened without it, and the operator can untick the box.
        const offer = sandboxOffer(terms.envs.find((e) => e.env === chosen));
        if (!offer.ok) {
          toast(t("term.sandbox.unavailable", { reason: offer.reason || t("term.unavailable.none") }));
          return;
        }
        body.sandbox = true;
      }
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
    [terms, preferredEnv, sessionId, phone, toast, focus, boxed],
  );

  const toggle = useCallback(() => {
    if (full) {
      setFull(null);
      focus();
      return;
    }
    const out = toggleDock(state, terms.running.map((r) => r.id));
    setState(out.state);
    if (out.create) void create();
    else if (out.state.open) focus();
  }, [full, state, terms.running, create, focus]);

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
    } else void create(undefined, "split");
  }, [state, create]);

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
    toggle,
    activate,
    detach,
    show,
    end,
    restart,
    remove,
    split,
    maximise,
    restore,
    boxed,
    toggleSandbox,
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

/** The menu behind ▾: a terminal in either environment, the session's terminals without a tab, End. */
function dockMenu(dock: DockController, active: string | null): MenuItem[] {
  const envs = dock.terms.envs;
  const env = (name: TerminalEnvName) => envs.find((e) => e.env === name);
  const container = env("container");
  const host = env("host");
  const toggle = sandboxToggle(envs);
  // With the sandbox chosen, an environment that cannot give it offers no terminal: the item says why.
  const blocked = (e: TerminalEnv | undefined) => {
    const offer = sandboxOffer(e);
    return dock.boxed && !!e?.available && !offer.ok ? t("term.sandbox.unavailable", { reason: offer.reason }) : "";
  };
  const containerWhy = !container?.available ? t("term.unavailable.container") : blocked(container);
  const hostWhy = !host?.available ? t("term.unavailable.host") : blocked(host);
  const items: MenuItem[] = [
    {
      label: t("term.sandbox.toggle"),
      icon: "shield",
      checked: dock.boxed && toggle.ok,
      disabled: !toggle.ok,
      hint: toggle.ok ? t("term.sandbox.hint") : t("term.sandbox.unavailable", { reason: toggle.reason || t("term.unavailable.none") }),
      onSelect: dock.toggleSandbox,
    },
    "-",
    { label: t("term.new.container"), icon: "terminal", disabled: !!containerWhy, hint: containerWhy || undefined, onSelect: () => void dock.create("container") },
    { label: t("term.new.host"), icon: "lock", warn: true, disabled: !!hostWhy, hint: hostWhy || undefined, onSelect: () => void dock.create("host") },
  ];
  const untabbed = dock.terms.running.filter((r) => !dock.state.tabs.includes(r.id));
  if (untabbed.length) {
    items.push("-");
    for (const row of untabbed) items.push({ label: t("term.open", { title: tabTitle(row, dock.states[row.id]) }), icon: "terminal", onSelect: () => dock.show(row.id) });
  }
  const activeRow = dock.rowOf(active);
  if (active && activeRow?.status === "running") {
    items.push("-");
    if (activeRow.sandbox) items.push({ label: t("term.sandbox.restartOff"), icon: "reload", onSelect: () => void dock.restart(active, false) });
    else {
      const offer = sandboxOffer(env(activeRow.env));
      items.push({ label: t("term.sandbox.restartOn"), icon: "shield", disabled: !offer.ok, hint: offer.ok ? undefined : t("term.sandbox.unavailable", { reason: offer.reason }), onSelect: () => void dock.restart(active, true) });
    }
    items.push({ label: t("term.end"), icon: "stop", danger: true, onSelect: () => void dock.end(active) });
  }
  return items;
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
      title={phone ? label : `${label} (Ctrl+\`)`}
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

  return (
    <div ref={box} className={`term-dock ${state.open ? "open" : "closed"}`} style={{ "--dock-h": `${height}px` } as CSSProperties} aria-label={t("term.dock")} role="region">
      <div className="term-grip" role="separator" aria-orientation="horizontal" aria-label={t("term.dock.resize")} onPointerDown={drag} />
      <div className="term-bar">
        <div className="term-tabs" role="tablist" aria-label={t("term.dock")}>
          {state.tabs.map((id) => {
            const row = dock.rowOf(id);
            const st = dock.states[id];
            const dot = tabDot(row, st);
            const on = id === active || id === state.split;
            return (
              <div key={id} role="tab" tabIndex={0} aria-selected={on} className={`term-tab ${on ? "on" : ""} ${row?.env === "host" ? "host" : ""}`} data-tab={id} onClick={() => dock.activate(id)} onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); dock.activate(id); } }} title={row?.cwd || undefined}>
                <span className={`term-dot ${dot}`} aria-hidden="true" title={dotTitle(st)} data-result={st?.commands.last?.result} />
                <Shield row={row} />
                <span className="term-tab-title truncate">{tabTitle(row, st)}</span>
                {dot === "exited" && exitCode(row, st) && <span className="term-tab-code num">{exitCode(row, st)}</span>}
                <button className="term-tab-x" aria-label={t("term.detach")} title={t("term.detach")} onClick={(e) => { e.stopPropagation(); dock.detach(id); }}>
                  <Icon name="close" size={12} />
                </button>
              </div>
            );
          })}
          <button className="iconbtn small flat" onClick={() => void dock.create()} aria-label={t("term.new")} title={t("term.new")} disabled={!!dock.reason}><Icon name="plus" size={16} /></button>
          <OverflowMenu small icon="chevron" label={t("term.menu")} className="flat" items={dockMenu(dock, active)} />
        </div>
        <div className="term-tools">
          {activeRow && <EnvPill env={activeRow.env} />}
          <CopyOutputButton id={active} state={active ? dock.states[active] : undefined} />
          <button className="iconbtn small flat" onClick={() => active && instanceFor(active)?.openSearch()} aria-label={t("term.search")} title={`${t("term.search")} (Ctrl+Shift+F)`} disabled={!active}><Icon name="search" size={16} /></button>
          <button className={`iconbtn small flat ${state.split ? "on" : ""}`} onClick={dock.split} aria-label={t("term.split")} title={t("term.split")} aria-pressed={!!state.split} disabled={!active}><Icon name="split" size={16} /></button>
          <button className="iconbtn small flat" onClick={() => active && dock.maximise(active)} aria-label={t("term.maximize")} title={t("term.maximize")} disabled={!active}><Icon name="expand" size={16} /></button>
          <button className="iconbtn small flat" onClick={dock.toggle} aria-label={t("term.dock.close")} title={`${t("term.dock.close")} (Ctrl+\`)`}><Icon name="close" size={16} /></button>
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

export function TerminalSheet({ dock }: { dock: DockController }) {
  if (!dock.sheet) return null;
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
        <button className="btn" disabled={!!dock.reason} onClick={() => { close(); void dock.create(); }}><Icon name="plus" size={16} />{t("term.new")}</button>
      </div>
    </Sheet>
  );
}

/** How many terminals a delete would end, for the session's delete dialog. */
export function endsTerminals(n: number): string {
  return n > 0 ? plural("term.delete.ends", n) : "";
}
