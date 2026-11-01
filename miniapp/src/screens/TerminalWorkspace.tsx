// The Terminals page's Workspace: the terminals the operator set up, live, side by side. It owns the
// workspace state (`terminal/workspacestate.ts`), keeps it in storage and in the address, and does
// what a pane asks (restart, remove, end, rename) against the host.
//
// `/app/terminals/<id>` is how the rest of the app links a terminal, and it lands here: the terminal
// is shown in the workspace (focused if a pane already holds it). The address then follows the active
// pane, written with `replace` so that moving between panes does not fill the history.
//
// A desktop draws the grid (terminal/workspace.tsx). A phone, or a touch tablet with no mouse, shows
// one pane at a time with the phone's keys row and compose line, and a switcher between the panes.

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, Project, TerminalEnv, TerminalList, TerminalLoad, TerminalView as TerminalRow } from "../api";
import { t } from "../i18n";
import { back, navigate, pathFor } from "../router";
import { useMedia } from "../ui/index";
import { invalidate, useQuery } from "../store";
import { errorText } from "../ui";
import { endTerminal } from "../terminal/actions";
import type { TerminalState } from "../terminal/instance";
import { paneState } from "../terminal/status";
import { instanceFor, setTerminalEnvs, terminals } from "../terminal/terminals";
import { PhoneTerminal } from "../terminal/mobile";
import { paneTitle, StateDot, TerminalWorkspace } from "../terminal/workspace";
import { TerminalSidePanel } from "../terminal/sidepanel";
import { readSide, saveSide, type SidePrefs } from "../terminal/sidestate";
import { activeId, close, focus, fromAddress, loadWorkspace, place, replace, saveView, saveWorkspace, toAddress, type Workspace } from "../terminal/workspacestate";
import { StaffPhoneTerminal } from "../project/phone";
import { NewTerminalSheet } from "./Terminals";

const LIST_POLL_MS = 5000;
const LOAD_POLL_MS = 10000;

export function TerminalWorkspaceScreen({ detail, query, toast, project, projects }: { detail: string | null; query: URLSearchParams; toast: (text: string) => void; project: string; projects: Project[] }) {
  // A touch screen with no mouse takes the phone's layout however wide it is: the desktop's has no
  // keys row and no compose line, and a tablet held without a keyboard had no Esc, no arrows and no
  // way to answer the program's own dialog.
  const roomy = useMedia("(min-width: 1024px)");
  const touchOnly = useMedia("(hover: none) and (pointer: coarse)");
  const wide = roomy && !touchOnly;
  const list = useQuery<TerminalList>("/api/terminals", { pollMs: LIST_POLL_MS, staleMs: 1000 });
  const load = useQuery<TerminalLoad>(wide ? "/api/terminals/load" : null, { pollMs: LOAD_POLL_MS, staleMs: 3000 });
  const rows = useMemo(() => list.data?.terminals ?? [], [list.data]);
  const byId = useMemo(() => new Map(rows.map((r) => [r.id, r])), [rows]);
  const envs: TerminalEnv[] = list.data?.envs ?? [];
  useEffect(() => {
    if (list.data?.envs) setTerminalEnvs(list.data.envs);
  }, [list.data]);

  const address = useMemo(() => ({ detail, query }), [detail, query]);
  // A phone shows one pane at a time, so every one of the four places is a pane it can switch to: a
  // terminal opened there lands in a free place rather than replacing the one shown before it.
  const [ws, setWs] = useState<Workspace>(() => {
    const stored = loadWorkspace();
    return fromAddress(wide ? stored : { ...stored, layout: 4, zoom: null }, address);
  });
  const update = useCallback((change: (ws: Workspace) => Workspace) => setWs((current) => change(current)), []);

  // The terminal asked to take the keyboard: the one the address opened, then whatever a tab, Alt+N
  // or the list chooses (see TerminalWorkspaceProps.focus).
  const current = activeId(ws);
  const [focusAt, setFocusAt] = useState<{ id: string | null; token: number }>(() => ({ id: current, token: 1 }));
  const requestFocus = useCallback((id: string) => setFocusAt((f) => ({ id, token: f.token + 1 })), []);

  // The address changed under the page (the palette opened another terminal, Back or Forward): apply
  // it. The page's own writes come back here too and change nothing, because the address they wrote
  // describes the state they came from; the comparison is what stops the two effects from looping.
  const key = `${detail ?? ""}?${query.toString()}`;
  const applied = useRef(key);
  const latest = useRef(ws);
  latest.current = ws;
  useEffect(() => {
    if (applied.current === key) return;
    applied.current = key;
    const next = fromAddress(latest.current, address);
    if (JSON.stringify(next) === JSON.stringify(latest.current)) return;
    setWs(next);
    // A terminal opened from elsewhere is the one the operator means to type into.
    if (detail) requestFocus(detail);
  }, [key, address, detail, requestFocus]);

  useEffect(() => {
    saveWorkspace(ws);
    const target = toAddress(ws);
    navigate(pathFor("terminals", target.detail, target.query), { replace: true });
  }, [ws]);

  const [states, setStates] = useState<Record<string, TerminalState>>({});
  const onState = useCallback((tid: string, next: TerminalState) => setStates((all) => (all[tid] === next ? all : { ...all, [tid]: next })), []);

  // A terminal removed somewhere else leaves its place: the host says it is gone, and the listing no
  // longer has it. Either alone is not enough — a terminal created a moment ago may not be listed yet.
  useEffect(() => {
    for (const [slot, id] of ws.panes.entries()) {
      const connection = id ? states[id]?.connection : null;
      if (id && list.data && !byId.has(id) && connection?.kind === "unavailable" && connection.reason === "gone") update((w) => close(w, slot));
    }
  }, [ws.panes, states, byId, list.data, update]);


  // The side panel's open state, width and tab, remembered on this device. A desktop draws it; the
  // phone shows one pane at a time and has nothing beside it.
  const [side, setSide] = useState<SidePrefs>(() => readSide());
  useEffect(() => saveSide(side), [side]);

  const [creating, setCreating] = useState(false);
  const lens = projects.find((p) => p.id === project) ?? null;

  const open = useCallback((id: string) => {
    instanceFor(id)?.interact();
    update((w) => place(w, id));
    requestFocus(id);
  }, [update, requestFocus]);

  const restart = async (tid: string) => {
    let fresh: TerminalRow;
    try {
      fresh = await api.restartTerminal(tid);
    } catch (error) {
      toast(errorText(error));
      return;
    }
    invalidate("/api/terminals");
    update((w) => replace(w, tid, fresh.id));
    // The finished instance has no view once the pane has moved on; nothing else needs it.
    setTimeout(() => terminals.remove(tid), 0);
  };

  const remove = async (tid: string) => {
    try {
      await api.removeTerminal(tid);
    } catch (error) {
      toast(errorText(error));
      return;
    }
    invalidate("/api/terminals");
    update((w) => (w.panes.includes(tid) ? close(w, w.panes.indexOf(tid)) : w));
    setTimeout(() => terminals.remove(tid), 0);
  };

  const end = async (tid: string) => {
    await endTerminal(tid, states[tid]?.title || byId.get(tid)?.title, toast);
    invalidate("/api/terminals");
  };

  const rename = async (tid: string, title: string) => {
    try {
      await api.renameTerminal(tid, title);
    } catch (error) {
      toast(errorText(error));
      return;
    }
    invalidate("/api/terminals");
  };

  const overview = () => {
    saveView("overview");
    navigate(pathFor("terminals", null, { view: "overview" }));
  };

  const sheet = creating ? <NewTerminalSheet envs={envs} lens={lens} projects={projects} toast={toast} onClose={() => setCreating(false)} /> : null;

  if (!wide) {
    const filled = ws.panes.map((id, slot) => ({ id, slot })).filter((p): p is { id: string; slot: number } => !!p.id);
    const shownId = current ?? filled[0]?.id ?? null;
    if (!shownId) {
      // Nothing to show: the phone's list of terminals is one Back away, as it is from any terminal.
      return <div className="screen"><div className="empty"><b>{t("term.ws.empty.title")}</b><button className="btn primary" onClick={overview}>{t("term.ws.view.overview")}</button></div></div>;
    }
    const row = byId.get(shownId) ?? null;
    const switcher = filled.length > 1 ? (
      <div className="tp-switch" role="tablist" aria-label={t("term.ws.panes")}>
        {filled.map(({ id, slot }) => (
          <button key={id} type="button" role="tab" aria-selected={id === shownId} className={`tp-switch-tab ${id === shownId ? "on" : ""}`} data-slot={slot} data-pane={id} onClick={() => { instanceFor(id)?.interact(); update((w) => focus(w, slot)); requestFocus(id); }}>
            <StateDot status={paneState(byId.get(id), states[id])} />
            <span className="truncate">{paneTitle(id, byId.get(id), states[id])}</span>
          </button>
        ))}
      </div>
    ) : undefined;
    const slot = ws.panes.indexOf(shownId);
    const phone = {
      id: shownId,
      row,
      onBack: () => back(pathFor("terminals")),
      onEnd: () => void end(shownId),
      onRestart: () => void restart(shownId),
      onRemove: () => void remove(shownId),
      focusToken: focusAt.token,
      onState,
      switcher,
      menu: filled.length > 1 ? [{ label: t("term.grid.close"), icon: "close" as const, onSelect: () => update((w) => close(w, slot)) }] : [],
    };
    // A staff member's terminal: what is written goes to the member as a message, and its open
    // request is answered above the keys.
    return row?.owner.kind === "staff" && row.owner.id ? <StaffPhoneTerminal key={shownId} staffId={row.owner.id} projectId={row.project_id} {...phone} /> : <PhoneTerminal key={shownId} {...phone} />;
  }

  return (
    <>
      <TerminalWorkspace
        ws={ws}
        update={update}
        rows={rows}
        projects={projects}
        load={load.data ?? null}
        states={states}
        onState={onState}
        focus={focusAt}
        onFocusRequest={requestFocus}
        onOpen={open}
        onRestart={(id) => void restart(id)}
        onRemove={(id) => void remove(id)}
        onEnd={(id) => void end(id)}
        onRename={(id, title) => void rename(id, title)}
        onNew={() => setCreating(true)}
        onOverview={overview}
        toast={toast}
        slots={{
          panel: { open: side.open, onToggle: () => setSide((p) => ({ ...p, open: !p.open })) },
          side: side.open ? (
            <TerminalSidePanel ws={ws} update={update} rows={rows} states={states} prefs={side} onPrefs={setSide} onOpen={open} onFocusRequest={requestFocus} toast={toast} />
          ) : undefined,
        }}
      />
      {sheet}
    </>
  );
}
