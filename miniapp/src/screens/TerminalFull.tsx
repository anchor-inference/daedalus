// A terminal over the whole screen, at its own address (`/app/terminals/<id>`), and up to three more
// beside it (`?with=<id>,<id>,<id>`) in a grid. Every pane is on screen, so every pane may size its
// terminal; leaving the view unmounts them, and a terminal nobody shows never sends a size.
//
// The header acts on the pane that was touched last: its title, its environment, its owner as a link,
// copying its last command's output, its search and its End. The font size is the device's, shared with the dock.

import { useCallback, useEffect, useMemo, useState } from "react";
import { api, TerminalList, TerminalView as TerminalRow } from "../api";
import { MenuItem, OverflowMenu } from "../ui/dialogs";
import { t } from "../i18n";
import { Icon } from "../icons";
import { back, navigate, pathFor, projectPagePath, sessionPath } from "../router";
import { useMedia } from "../ui/index";
import { invalidate, useQuery } from "../store";
import { errorText } from "../ui";
import { endTerminal } from "../terminal/actions";
import { EnvPill } from "../envpill";
import type { TerminalState } from "../terminal/instance";
import { fontSizeStep } from "../terminal/instance";
import { gridIds, ownerLine, ownerPath } from "../terminal/preview";
import { instanceFor, setTerminalEnvs, terminals } from "../terminal/terminals";
import { CopyOutputButton, TerminalView } from "../terminal/view";
import { PhoneTerminal } from "../terminal/mobile";
import { StaffPhoneTerminal } from "../project/phone";

const LIST_POLL_MS = 5000;

/** The address of a grid: the first terminal in the path, the others beside it. */
export function gridPath(ids: string[]): string {
  return pathFor("terminals", ids[0], { with: ids.slice(1).join(",") || undefined });
}

export function TerminalFullScreen({ id, beside, toast }: { id: string; beside: string | null; toast: (text: string) => void }) {
  const ids = useMemo(() => gridIds(id, beside), [id, beside]);
  // A touch screen with no mouse takes the phone's layout however wide it is: the desktop's has no
  // keys row and no compose line, and a tablet held without a keyboard had no Esc, no arrows and no
  // way to answer the program's own dialog.
  const roomy = useMedia("(min-width: 1024px)");
  const touchOnly = useMedia("(hover: none) and (pointer: coarse)");
  const wide = roomy && !touchOnly;
  const list = useQuery<TerminalList>("/api/terminals", { pollMs: LIST_POLL_MS, staleMs: 1000 });
  const rows = useMemo(() => new Map((list.data?.terminals ?? []).map((r) => [r.id, r])), [list.data]);
  useEffect(() => {
    if (list.data?.envs) setTerminalEnvs(list.data.envs);
  }, [list.data]);

  const [active, setActive] = useState(ids[0]);
  const current = ids.includes(active) ? active : ids[0];
  const [focusToken, setFocusToken] = useState(1);
  const [states, setStates] = useState<Record<string, TerminalState>>({});
  const onState = useCallback((tid: string, next: TerminalState) => setStates((all) => (all[tid] === next ? all : { ...all, [tid]: next })), []);

  const choose = (tid: string) => {
    if (tid === current) return;
    instanceFor(tid)?.interact();
    setActive(tid);
    setFocusToken((n) => n + 1);
  };

  const leave = () => back(pathFor("terminals"));
  const replaceGrid = (next: string[]) => {
    if (!next.length) navigate(pathFor("terminals"), { replace: true });
    else navigate(gridPath(next), { replace: true });
  };

  const row = rows.get(current) ?? null;
  const title = (tid: string) => states[tid]?.title || rows.get(tid)?.title || t("term.untitled");
  const owner = row ? ownerPath(row, (sid) => sessionPath(sid), (pid) => projectPagePath(pid, "team")) : null;

  const addable = (list.data?.terminals ?? []).filter((r) => r.status === "running" && !ids.includes(r.id));
  const splitItems: MenuItem[] = addable.length
    ? addable.map((r) => ({ label: r.title || t("term.untitled"), icon: r.env === "host" ? "lock" : "terminal", warn: r.env === "host", onSelect: () => { setActive(r.id); setFocusToken((n) => n + 1); replaceGrid([...ids, r.id]); } }))
    : [{ label: t("term.grid.none"), disabled: true, onSelect: () => undefined }];

  const restart = async (tid: string) => {
    let fresh: TerminalRow;
    try {
      fresh = await api.restartTerminal(tid);
    } catch (error) {
      toast(errorText(error));
      return;
    }
    invalidate("/api/terminals");
    setActive(fresh.id);
    replaceGrid(ids.map((x) => (x === tid ? fresh.id : x)));
    // The finished instance has no view once the grid has moved on; nothing else needs it.
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
    replaceGrid(ids.filter((x) => x !== tid));
    setTimeout(() => terminals.remove(tid), 0);
  };

  const end = async () => {
    await endTerminal(current, states[current]?.title, toast);
    invalidate("/api/terminals");
  };

  const exited = row ? row.status !== "running" : !!states[current]?.exit;

  // A phone (or a touch tablet) shows one pane at a time, with the keys row and the compose line; the others of a grid
  // are one menu item away, and the first Back leaves the grid as a whole.
  if (!wide) {
    const others: MenuItem[] = ids.filter((tid) => tid !== current).map((tid) => ({ label: t("term.phone.show", { title: title(tid) }), icon: rows.get(tid)?.env === "host" ? "lock" : "terminal", onSelect: () => choose(tid) }));
    const phone = {
      id: current,
      row,
      onBack: leave,
      onEnd: () => void end(),
      onRestart: () => void restart(current),
      onRemove: () => void remove(current),
      focusToken,
      onState,
      menu: [...others, ...(ids.length > 1 ? [{ label: t("term.grid.close"), icon: "close" as const, onSelect: () => replaceGrid(ids.filter((x) => x !== current)) }] : [])],
    };
    // A staff member's terminal: what is written goes to the member as a message, and its open
    // request is answered above the keys (the harness plan's M3).
    return row?.owner.kind === "staff" && row.owner.id ? <StaffPhoneTerminal key={current} staffId={row.owner.id} projectId={row.project_id} {...phone} /> : <PhoneTerminal key={current} {...phone} />;
  }

  return (
    <div className="term-page" data-count={ids.length}>
      <div className="term-page-head">
        <button className="iconbtn small flat" onClick={leave} aria-label={t("term.screen.back")} title={t("term.screen.back")}>
          <Icon name="back" size={16} />
        </button>
        <span className="term-page-title truncate">{title(current)}</span>
        {row && <EnvPill env={row.env} />}
        {row && owner && (
          <a className="term-page-owner sub truncate" href={owner} onClick={(e) => { e.preventDefault(); navigate(owner); }}>
            {/* Said as the cards say it, "staff Ira": a bare "Ira" after a title that already ends in
                "Ira" read as the same word printed twice. */}
            {ownerLine(row, null)}
          </a>
        )}
        <div className="grow" />
        <div className="term-page-tools">
          {ids.length < 4 && <OverflowMenu small icon="split" label={t("term.grid.add")} className="flat" items={splitItems} />}
          {ids.length > 1 && (
            <button className="iconbtn small flat" onClick={() => replaceGrid(ids.filter((x) => x !== current))} aria-label={t("term.grid.close")} title={t("term.grid.close")}>
              <Icon name="close" size={16} />
            </button>
          )}
          <CopyOutputButton id={current} state={states[current]} />
          <button className="iconbtn small flat" onClick={() => instanceFor(current)?.openSearch()} aria-label={t("term.search")} title={`${t("term.search")} (Ctrl+Shift+F)`}>
            <Icon name="search" size={16} />
          </button>
          <button className="iconbtn small flat term-font" onClick={() => fontSizeStep("font-smaller")} aria-label={t("term.font.smaller")} title={`${t("term.font.smaller")} (Ctrl+−)`}>A−</button>
          <button className="iconbtn small flat term-font" onClick={() => fontSizeStep("font-bigger")} aria-label={t("term.font.bigger")} title={`${t("term.font.bigger")} (Ctrl+=)`}>A+</button>
          {!exited && (
            <button className="iconbtn small flat danger" onClick={() => void end()} aria-label={t("term.end")} title={t("term.end")}>
              <Icon name="stop" size={16} />
            </button>
          )}
        </div>
      </div>
      <div className="term-grid" data-count={ids.length}>
        {ids.map((tid) => (
          <div key={tid} className={`term-grid-pane ${ids.length > 1 && tid === current ? "on" : ""} ${rows.get(tid)?.env === "host" ? "host" : ""}`} data-pane={tid} onPointerDownCapture={() => choose(tid)} onFocusCapture={() => choose(tid)}>
            {ids.length > 1 && (
              <div className="term-grid-label truncate">
                <span>{title(tid)}</span>
              </div>
            )}
            <TerminalView
              id={tid}
              visible
              env={rows.get(tid)?.env}
              focusToken={tid === current ? focusToken : undefined}
              onState={onState}
              onRestart={() => void restart(tid)}
              onRemove={() => void remove(tid)}
            />
          </div>
        ))}
      </div>
    </div>
  );
}
