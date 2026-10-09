// "Continue a session from another program": the explorer of the sessions other programs left on the
// machine, and the import that brings one into Daedalus.
//
// It is the folder browser's frame — crumbs, back and up, places on the left — with sessions where
// the files would be, because the operator remembers a session by the folder it ran in: the programs
// themselves index their sessions by working folder, so the left column lists every folder that has
// any without walking the disk. The preview on the right says, before anything is written, how the
// session will come over and whether it joins a project or starts a chat of its own; nothing is a
// surprise after Import.
//
// Every entry point (the New project menu, the start screen's card, the palette, a phone's projects
// sheet) calls `openImport()`, so none of them needs anything passed down to it.

import { useEffect, useRef, useState, type KeyboardEvent } from "react";
import { plural, t } from "../i18n";
import { Icon } from "../icons";
import { navigate, sessionPath } from "../router";
import { useMedia } from "../shell";
import { Sheet } from "../ui/dialogs";
import { useImportFlow, type ImportFlow, type OpenRequest } from "./flow";
import { canImport, FailurePanel, HarnessMark, importLabel, isAgain, ListHead, PreviewPane, SessionRow } from "./parts";
import { ImportPhone } from "./ImportPhone";
import { baseName, crumbs, groupByFolder, harnessMeta, harnessOrder, importedId, parentOf, shortPath, splitTrivial } from "./model";
import { forgetFresh } from "./fresh";
import "./imports.css";

const OPEN_EVENT = "daedalus:import-session";

/** Open the import explorer from anywhere, on a program, a folder or a session when one is known. */
export function openImport(request: OpenRequest = {}): void {
  window.dispatchEvent(new CustomEvent<OpenRequest>(OPEN_EVENT, { detail: request }));
}

/** Mounted once by the app; listens for `openImport` and draws the desktop's dialog or the phone's screens. */
export function ImportHost({ toast }: { toast: (text: string) => void }) {
  const [open, setOpen] = useState<OpenRequest | null>(null);
  const wide = useMedia("(min-width: 1024px)");
  useEffect(() => {
    const listen = (event: Event) => { const asked = (event as CustomEvent<OpenRequest>).detail; setOpen(asked ?? {}); };
    window.addEventListener(OPEN_EVENT, listen);
    return () => window.removeEventListener(OPEN_EVENT, listen);
  }, []);
  if (!open) return null;
  const close = () => setOpen(null);
  const done = (sessionId: string, into: string) => {
    close();
    forgetFresh();
    toast(into ? t("imp.done.project", { name: into }) : t("imp.done"));
    navigate(sessionPath(sessionId));
  };
  const openChat = (id: string) => { close(); navigate(sessionPath(id)); };
  return wide
    ? <ImportDialog request={open} onClose={close} onDone={done} onOpenChat={openChat} />
    : <ImportPhone request={open} onClose={close} onDone={done} onOpenChat={openChat} />;
}

function ImportDialog({ request, onClose, onDone, onOpenChat }: { request: OpenRequest; onClose: () => void; onDone: (id: string, into: string) => void; onOpenChat: (id: string) => void }) {
  const flow = useImportFlow(request, onDone);
  const search = useRef<HTMLInputElement>(null);
  const home = flow.scan?.home || flow.places?.home || flow.list?.home || undefined;
  // The host's crumbs for the folder it listed, which know a Windows path's rules; worked out here before it answers.
  const trail = flow.scan?.crumbs?.length && flow.scan.path === flow.path ? flow.scan.crumbs : crumbs(flow.path, home);
  const meta = harnessMeta(flow.harness, flow.list?.harnesses.find((h) => h.id === flow.harness)?.name);
  const running = flow.job?.state === "running";

  // The first real session of a folder is chosen when nothing is: the preview is the point of the window.
  useEffect(() => {
    const here = flow.scan?.here ?? [];
    if (flow.selected && here.some((s) => s.id === flow.selected!.id)) return;
    if (flow.selected && flow.asked) return;
    const first = splitTrivial(here).main[0] ?? here[0] ?? null;
    if (first) flow.select(first);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [flow.scan]);

  const go = () => {
    if (!canImport(flow)) return;
    void flow.start(isAgain(flow));
  };
  const onKey = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) { event.preventDefault(); go(); }
    if (event.key === "/" && !(event.target instanceof HTMLInputElement)) { event.preventDefault(); search.current?.focus(); }
    if (event.key === "Backspace" && !(event.target instanceof HTMLInputElement) && flow.path) {
      const up = parentOf(flow.path);
      if (up) { event.preventDefault(); flow.go(up); }
    }
  };

  return (
    <Sheet title={t("imp.title")} onClose={onClose} size="wide" className="imp-sheet">
      <div className="imp" onKeyDown={onKey}>
        <p className="sub imp-intro">{t("imp.intro")}</p>
        <HarnessStrip flow={flow} />
        <div className="fb imp-fb">
          <div className="fb-bar">
            <span className="imp-hostlbl"><Icon name="lock" size={12} />{t("fb.host")}</span>
            <button type="button" className="iconbtn small" onClick={flow.back} disabled={!flow.canBack} title={t("fb.back")} aria-label={t("fb.back")}><Icon name="back" size={15} /></button>
            <button type="button" className="iconbtn small" onClick={() => { const up = parentOf(flow.path); if (up) flow.go(up); }} disabled={!flow.path || !parentOf(flow.path)} title={t("fb.up")} aria-label={t("fb.up")}><Icon name="up" size={15} /></button>
            <div className="fb-crumbs mono">
              {trail.map((crumb, index, all) => (
                <span key={crumb.path} className="fb-crumb-wrap">
                  {index > 0 && <span className="fb-sep">/</span>}
                  <button type="button" className={`fb-crumb ${index === all.length - 1 ? "last" : ""}`} onClick={() => flow.go(crumb.path)}>{crumb.name}</button>
                </span>
              ))}
            </div>
            <label className={`imp-search ${flow.query ? "filled" : ""}`}>
              <Icon name="search" size={13} />
              <input ref={search} type="search" value={flow.query} onChange={(e) => flow.setQuery(e.target.value)} placeholder={t("imp.search", { name: meta.name })} aria-label={t("imp.search", { name: meta.name })}
                onKeyDown={(e) => { if (e.key === "Escape" && flow.query) { e.stopPropagation(); flow.setQuery(""); } }} />
              {flow.query
                ? <button type="button" className="imp-clear" onClick={() => flow.setQuery("")} aria-label={t("ph.search.clear")} title={t("ph.search.clear")}><Icon name="close" size={12} /></button>
                : <kbd aria-hidden>/</kbd>}
            </label>
          </div>
          <div className={`fb-main imp-main ${flow.failure ? "down" : ""}`}>
            <Places flow={flow} home={home} />
            {flow.failure ? <FailurePanel failure={flow.failure} onRetry={flow.retry} /> : (
              <>
                <SessionList flow={flow} home={home} onOpenChat={onOpenChat} />
                <PreviewPane flow={flow} home={home} onOpenChat={onOpenChat} />
              </>
            )}
          </div>
        </div>
      </div>
      <div className="sheet-foot imp-foot">
        <span className="imp-hint"><Icon name="shield" size={13} />{t("imp.hint")}</span>
        <button type="button" className="btn ghost" onClick={onClose}>{t(running ? "imp.hide" : "common.cancel")}</button>
        <button type="button" className="btn primary imp-go" onClick={go} disabled={!canImport(flow)}>
          {importLabel(flow)}<kbd aria-hidden>Ctrl ↵</kbd>
        </button>
      </div>
    </Sheet>
  );
}

/** One tile per program, with how many sessions the machine holds; one not installed stays, dimmed,
 *  so the operator learns it is supported rather than missing. */
function HarnessStrip({ flow }: { flow: ImportFlow }) {
  const list = harnessOrder(flow.list?.harnesses ?? []);
  if (!list.length) return null;
  return (
    <div className="imp-hstrip" role="tablist" aria-label={t("imp.harnesses")}>
      {list.map((h) => (
        <button key={h.id} type="button" role="tab" aria-selected={h.id === flow.harness} data-harness={h.id} disabled={!h.found}
          className={`imp-htile ${h.id === flow.harness ? "on" : ""} ${h.found && h.sessions ? "" : "off"}`} onClick={() => flow.setHarness(h.id)}>
          <HarnessMark id={h.id} name={h.name} />
          <b>{harnessMeta(h.id, h.name).name}</b>
          <span className="imp-n num">{h.found ? h.sessions.toLocaleString() : t("imp.notfound")}</span>
        </button>
      ))}
    </div>
  );
}

/** "Where sessions are": every folder the program has sessions in, freshest first; then the roots. */
function Places({ flow, home }: { flow: ImportFlow; home?: string }) {
  const folders = flow.places?.folders ?? [];
  const roots = flow.places?.roots ?? flow.scan?.roots ?? (home ? [{ name: "~", path: home, kind: "home" }] : []);
  return (
    <nav className="fb-places imp-places" aria-label={t("imp.places")}>
      {folders.length > 0 && <div className="fb-places-h">{t("imp.places.with")}</div>}
      {folders.map((f) => (
        <button key={f.path} type="button" className={`fb-place ${f.path === flow.path && !flow.asked ? "on" : ""}`} onClick={() => flow.go(f.path)} title={shortPath(f.path, home)} data-place={f.path}>
          <Icon name="clock" size={14} /><span className="truncate">{f.name || baseName(f.path)}</span><span className="imp-n num">{f.sessions}</span>
        </button>
      ))}
      {roots.length > 0 && <div className="fb-places-h">{t("fb.roots")}</div>}
      {roots.map((r) => (
        <button key={r.path} type="button" className={`fb-place ${r.path === flow.path ? "on" : ""}`} onClick={() => flow.go(r.path)} title={r.path}>
          <Icon name={r.kind === "home" ? "user" : "folder"} size={14} /><span className="truncate">{r.kind === "home" ? t("fb.root.home") : r.name}</span>
        </button>
      ))}
    </nav>
  );
}

/** The middle column: the folders inside, then the sessions of this folder; or a search's matches by folder. */
function SessionList({ flow, home, onOpenChat }: { flow: ImportFlow; home?: string; onOpenChat: (id: string) => void }) {
  const [trivialOpen, setTrivialOpen] = useState(false);
  const scan = flow.scan;
  const name = harnessMeta(flow.harness).name;
  const row = (s: Parameters<typeof SessionRow>[0]["s"]) => (
    <SessionRow key={`${s.harness}:${s.id}`} s={s} selected={flow.selected?.id === s.id} query={flow.asked} onSelect={() => flow.select(s)}
      onOpen={() => { const chat = importedId(s); if (chat) onOpenChat(chat); else flow.select(s); }} />
  );
  if (!scan) return <div className="fb-list imp-list" aria-busy="true"><div className="imp-skel" /><div className="imp-skel" /><div className="imp-skel short" /></div>;
  if (flow.asked) {
    const groups = groupByFolder(scan.here);
    return (
      <div className="fb-list imp-list" role="listbox" aria-label={t("imp.found.label")} aria-busy={flow.scanning}>
        <ListHead end={<button type="button" className={`imp-deep ${flow.deep ? "on" : ""}`} aria-pressed={flow.deep} onClick={() => flow.setDeep(!flow.deep)}>{t("imp.deep")}</button>}>
          {scan.here.length ? t("imp.found", { folders: plural("imp.folders", groups.length), sessions: plural("imp.sessions", scan.here.length) }) : t("imp.found.none")}
        </ListHead>
        {groups.map((group) => (
          <div key={group.cwd} className="imp-group" data-folder={group.cwd}>
            <button type="button" className="imp-path" onClick={() => flow.go(group.cwd)} title={group.cwd}><Icon name="folder" size={13} /><span className="mono truncate">{shortPath(group.cwd, home)}</span></button>
            {group.sessions.map(row)}
          </div>
        ))}
        {scan.truncated && <div className="fb-none sub">{t("imp.truncated")}</div>}
      </div>
    );
  }
  const { main, trivial } = splitTrivial(scan.here);
  return (
    <div className="fb-list imp-list" role="listbox" aria-label={t("imp.list.label")} aria-busy={flow.scanning}>
      {scan.children.length > 0 && <ListHead>{t("imp.inside")}</ListHead>}
      {scan.children.map((child) => (
        <button key={child.path} type="button" className={`fb-row imp-frow ${child.sessions ? "" : "dim"}`} data-folder={child.path} onClick={() => flow.go(child.path)} title={child.path}>
          <Icon name="folder" size={15} />
          <span className="fb-name">{child.name || baseName(child.path)}</span>
          {child.project && <span className="fb-badge acc">{t("fb.project.badge", { name: child.project.name })}</span>}
          {child.sessions ? <span className="imp-cnt num"><Icon name="bots" size={11} />{child.sessions}</span> : <span className="imp-cnt zero">{t("imp.nosessions")}</span>}
        </button>
      ))}
      <ListHead end={main.length > 1 ? <span className="imp-sort">{t("imp.sort.new")}</span> : undefined}>
        {t("imp.here", { name, n: scan.here.length })}
      </ListHead>
      {main.map(row)}
      {!scan.here.length && <div className="fb-none sub">{t("imp.here.none")}</div>}
      {trivial.length > 0 && (
        <>
          <button type="button" className="fb-group" aria-expanded={trivialOpen} onClick={() => setTrivialOpen((o) => !o)}>
            <span className={`chev ${trivialOpen ? "down" : ""}`}>›</span><b>{t("imp.trivial")}</b><span>· {trivial.length}</span>
          </button>
          {trivialOpen && trivial.map(row)}
        </>
      )}
      {scan.truncated && <div className="fb-none sub">{t("imp.truncated")}</div>}
    </div>
  );
}
