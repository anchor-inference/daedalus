// The import on a phone: the explorer's path as one screen at a time — the program, a folder with its
// sessions, the session — then the import and the chat. The same requests as the desktop's explorer
// (flow.ts), drawn at the phone's scale in a sheet that owns the screen.

import { useEffect, useState } from "react";
import { plural, t } from "../i18n";
import { Icon } from "../icons";
import { relTime } from "../format";
import { Sheet } from "../ui/dialogs";
import { useImportFlow, type OpenRequest } from "./flow";
import { canImport, FailurePanel, HarnessMark, importLabel, isAgain, PreviewPane, SessionRow } from "./parts";
import { baseName, cardSessions, groupByFolder, harnessMeta, harnessOrder, parentOf, shortPath, splitTrivial, type ForeignSession } from "./model";
import { useFreshSessions } from "./fresh";
import "./imports.css";

type Screen = "harness" | "folder" | "session";

export function ImportPhone({ request, onClose, onDone, onOpenChat }: { request: OpenRequest; onClose: () => void; onDone: (id: string, into: string) => void; onOpenChat: (id: string) => void }) {
  const flow = useImportFlow(request, onDone);
  const [screen, setScreen] = useState<Screen>(request.session ? "session" : request.harness ? "folder" : "harness");
  const home = flow.scan?.home || flow.places?.home || flow.list?.home || undefined;
  const meta = harnessMeta(flow.harness, flow.list?.harnesses.find((h) => h.id === flow.harness)?.name);
  const running = flow.job?.state === "running";
  const fresh = useFreshSessions(screen === "harness");

  // A session asked for by an entry point is chosen once its folder's listing names it.
  useEffect(() => {
    if (screen === "session" && !flow.selected && request.session && flow.scan) {
      const found = flow.scan.here.find((s) => s.id === request.session);
      if (found) flow.select(found);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [flow.scan]);

  const back = () => {
    if (screen === "session") { setScreen("folder"); return; }
    if (screen === "folder") { setScreen("harness"); return; }
    onClose();
  };
  const openSession = (s: ForeignSession) => { flow.select(s); setScreen("session"); };
  const openFresh = (s: ForeignSession) => {
    flow.setHarness(s.harness);
    flow.go(s.cwd);
    flow.select(s);
    setScreen("session");
  };
  const title = screen === "harness" ? t("imp.phone.title") : screen === "folder" ? meta.name : t("imp.phone.session");

  return (
    <Sheet size="full" onClose={onClose} className="ph-sheet imp-phone" ariaLabel={t("imp.title")}
      head={<div className="imp-ph-head">
        {screen !== "harness" && <button type="button" className="iconbtn" onClick={back} aria-label={t("shell.back")} title={t("shell.back")}><Icon name="back" size={18} /></button>}
        <h3>{title}</h3>
      </div>}>
      {screen === "harness" && (
        <div className="imp-ph-body" data-screen="harness">
          <p className="imp-ph-sub">{t("imp.phone.sub")}</p>
          {flow.failure ? <FailurePanel failure={flow.failure} onRetry={flow.retry} /> : (
            <>
              <div className="imp-ph-grp" role="list">
                {harnessOrder(flow.list?.harnesses ?? []).map((h) => (
                  <button key={h.id} type="button" role="listitem" data-harness={h.id} className={`imp-ph-row ${h.found && h.sessions ? "" : "off"}`} disabled={!h.found}
                    onClick={() => { flow.setHarness(h.id); setScreen("folder"); }}>
                    <HarnessMark id={h.id} name={h.name} size="lg" />
                    <span className="imp-ph-tx">
                      <b>{harnessMeta(h.id, h.name).name}</b>
                      <span>{!h.found ? t("imp.phone.notfound") : h.folders ? t("imp.phone.count", { sessions: plural("imp.sessions", h.sessions), folders: plural("imp.infolders", h.folders) }) : plural("imp.sessions", h.sessions)}</span>
                    </span>
                    {h.found && <span className="imp-ph-chev"><Icon name="forward" size={16} /></span>}
                  </button>
                ))}
                {!flow.list && <div className="imp-skel" />}
              </div>
              {fresh.sessions.length > 0 && (
                <>
                  <div className="imp-ph-grp-h">{t("imp.phone.fresh")}</div>
                  <div className="imp-ph-grp" role="list">
                    {cardSessions(fresh.sessions).map((s) => (
                      <button key={`${s.harness}:${s.id}`} type="button" role="listitem" className="imp-ph-row" data-fresh={s.id} onClick={() => openFresh(s)}>
                        <HarnessMark id={s.harness} size="md" />
                        <span className="imp-ph-tx"><b>{s.title}</b><span>{shortPath(s.cwd, home)} · {relTime(s.updated_at)}</span></span>
                        <span className="imp-ph-chev"><Icon name="forward" size={16} /></span>
                      </button>
                    ))}
                  </div>
                </>
              )}
            </>
          )}
        </div>
      )}
      {screen === "folder" && (
        <div className="imp-ph-body" data-screen="folder">
          <div className="imp-ph-crumbs">
            <span className="imp-hostlbl"><Icon name="lock" size={10} />{t("fb.host.tag")}</span>
            <span className="mono truncate grow">{shortPath(flow.path, home)}</span>
            <button type="button" className="iconbtn" disabled={!flow.path || !parentOf(flow.path)} onClick={() => { const up = parentOf(flow.path); if (up) flow.go(up); }} aria-label={t("fb.up")} title={t("fb.up")}><Icon name="up" size={16} /></button>
          </div>
          <label className="imp-ph-search">
            <Icon name="search" size={16} />
            <input type="search" value={flow.query} onChange={(e) => flow.setQuery(e.target.value)} placeholder={t("imp.search", { name: meta.name })} aria-label={t("imp.search", { name: meta.name })} />
          </label>
          {!flow.asked && !!flow.places?.folders.length && (
            <div className="imp-ph-chips" role="group" aria-label={t("imp.places.with")}>
              {flow.places.folders.map((f) => (
                <button key={f.path} type="button" className={f.path === flow.path ? "on" : ""} aria-pressed={f.path === flow.path} onClick={() => flow.go(f.path)} data-place={f.path}>
                  {f.name || baseName(f.path)} <b className="num">{f.sessions}</b>
                </button>
              ))}
            </div>
          )}
          {flow.failure ? <FailurePanel failure={flow.failure} onRetry={flow.retry} /> : !flow.scan ? <div className="imp-skel" /> : flow.asked ? (
            groupByFolder(flow.scan.here).map((group) => (
              <div key={group.cwd}>
                <div className="imp-ph-grp-h mono">{shortPath(group.cwd, home)}</div>
                <div className="imp-ph-grp imp-ph-sessions" role="listbox">{group.sessions.map((s) => <SessionRow key={s.id} s={s} selected={false} query={flow.asked} onSelect={() => openSession(s)} />)}</div>
              </div>
            ))
          ) : (
            <>
              {flow.scan.children.filter((c) => c.sessions > 0).length > 0 && (
                <div className="imp-ph-grp" role="list">
                  {flow.scan.children.filter((c) => c.sessions > 0).map((c) => (
                    <button key={c.path} type="button" role="listitem" className="imp-ph-row one" data-folder={c.path} onClick={() => flow.go(c.path)}>
                      <Icon name="folder" size={18} />
                      <span className="imp-ph-tx"><b>{c.name || baseName(c.path)}</b></span>
                      <span className="imp-ph-n num">{c.sessions}</span>
                      <span className="imp-ph-chev"><Icon name="forward" size={16} /></span>
                    </button>
                  ))}
                </div>
              )}
              <div className="imp-ph-grp-h">{t("imp.phone.here", { n: flow.scan.here.length })}</div>
              <div className="imp-ph-grp imp-ph-sessions" role="listbox" aria-label={t("imp.list.label")}>
                {splitTrivial(flow.scan.here).main.map((s) => <SessionRow key={s.id} s={s} selected={flow.selected?.id === s.id} onSelect={() => openSession(s)} />)}
                {!flow.scan.here.length && <div className="fb-none sub">{t("imp.here.none")}</div>}
              </div>
            </>
          )}
        </div>
      )}
      {screen === "session" && (
        <div className="imp-ph-body" data-screen="session">
          <PreviewPane flow={flow} phone home={home} onOpenChat={onOpenChat} />
        </div>
      )}
      {screen === "session" && (
        <div className="sheet-foot ph-sheet-foot imp-ph-foot">
          <button type="button" className="btn ghost" onClick={running ? onClose : back}>{t(running ? "imp.hide" : "shell.back")}</button>
          <button type="button" className="btn primary imp-go" disabled={!canImport(flow)}
            onClick={() => void flow.start(isAgain(flow))}>{importLabel(flow)}</button>
        </div>
      )}
    </Sheet>
  );
}
