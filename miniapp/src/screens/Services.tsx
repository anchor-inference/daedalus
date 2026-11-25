import { useState } from "react";
import { ServiceView } from "../api";
import { Skeleton, statusWord, useService } from "../ui/components";
import { Sheet } from "../ui/dialogs";
import { Badge, Button, Dot, OverflowMenu, Segmented } from "../ui/primitives";
import { Icon } from "../icons";
import { relTime } from "../format";
import { PageHeader, screenTitle, useMedia } from "../ui/index";
import { navigate, newAgentPath } from "../router";
import { invalidate, useQuery } from "../store";
import { plural, t } from "../i18n";

type Row = ServiceView & { session_id: string; session_title: string };
type Tone = "live" | "attention" | "idle";

const keyOf = (r: Row) => `${r.session_id}/${r.name}`;

/** A service's state as a tone and a word: running works, one whose state is unknown or that died on
 *  its own needs the operator, and a stopped one is simply off. */
function stateOf(s: ServiceView): { tone: Tone; word: string } {
  if (s.status === "running") return { tone: "live", word: t("services.state.running") };
  if (s.status === "unknown" || s.status === "dead") return { tone: "attention", word: statusWord(s.status) };
  return { tone: "idle", word: t("services.state.stopped") };
}

type Hooks = { onChange: () => void; toast: (t: string) => void; onLogs: (row: Row, text: string) => void };

/** One service in the list: its name, what it runs in mono, the chat that started it, its state, and
 *  its commands in a menu. The row opens the service beside the list. */
function ServiceItem({ row, on, onPick, hooks }: { row: Row; on: boolean; onPick: () => void; hooks: Hooks }) {
  const service = useService({ s: row, sessionId: row.session_id, onChange: hooks.onChange, toast: hooks.toast, onLogs: (text) => hooks.onLogs(row, text) });
  const state = stateOf(row);
  return (
    <div className={`pg-item ${on ? "on" : ""}`} data-service={row.name} data-status={row.status}>
      <button type="button" className="pg-item-main" aria-current={on} onClick={onPick}>
        <Dot tone={state.tone} />
        <span className="pg-item-body">
          <span className="pg-item-top">
            <span className="pg-item-name">{row.name}</span>
            {row.port && <span className="pg-item-chip mono">:{row.port}</span>}
            {row.machine === "host" && <Badge>{t("svc.host")}</Badge>}
            <span className={`pg-state pg-item-tag ${state.tone}`}>{state.word}</span>
          </span>
          <span className="pg-item-sub mono" title={row.command}>{row.command}</span>
          <span className="pg-item-sub">{row.session_title} · {row.status === "running" ? t("services.started", { t: relTime(row.started_at) }) : t("services.since.stopped", { t: relTime(row.stopped_at ?? row.started_at) })}</span>
        </span>
      </button>
      <div className="pg-item-trail">
        <OverflowMenu small label={t("svc.actions", { name: row.name })} items={service.items} />
      </div>
      {service.accessSheet}
    </div>
  );
}

/** The open service: what it runs and where, who started it, who can reach it, and what can be done. */
function ServicePane({ row, hooks, onOpenChat }: { row: Row; hooks: Hooks; onOpenChat: (id: string) => void }) {
  const service = useService({ s: row, sessionId: row.session_id, onChange: hooks.onChange, toast: hooks.toast, onLogs: (text) => hooks.onLogs(row, text) });
  const state = stateOf(row);
  const running = row.status === "running";
  const shareWord = t(`svc.access.${service.mode}`);
  return (
    <div className="pg-pane" data-pane="service">
      <div className="pg-pane-head">
        <span className={`pg-state ${state.tone}`}><Dot tone={state.tone} />{state.word}</span>
        <span className="grow" />
        {service.openUrl && running && (
          <a className="btn primary" href={service.openUrl} target="_blank" rel="noreferrer" title={t(service.shared ? "svc.open.shared" : "svc.open.lan")}>
            <Icon name="external" size={16} />{t("common.open")}
          </a>
        )}
        <OverflowMenu label={t("svc.actions", { name: row.name })} items={service.items} />
      </div>
      <h2 className="pg-pane-title">{row.name}</h2>
      <div className="pg-facts">
        <div className="pg-fact"><span className="pg-fact-key">{t("services.pane.command")}</span><span className="pg-fact-value mono">{row.command}</span></div>
        <div className="pg-fact"><span className="pg-fact-key">{t("services.pane.folder")}</span><span className="pg-fact-value mono">{row.cwd}</span></div>
        {running && service.lan && <div className="pg-fact"><span className="pg-fact-key">{t("services.pane.address")}</span><span className="pg-fact-value mono">{service.lan}</span></div>}
        {running && row.port && (
          <div className="pg-fact">
            <span className="pg-fact-key">{t("services.pane.access")}</span>
            <span className="pg-fact-value">{shareWord}</span>
            <Button size="sm" onClick={service.openAccess}>{t("svc.menu.access")}</Button>
          </div>
        )}
        <div className="pg-fact"><span className="pg-fact-key">{t("services.pane.owner")}</span><button type="button" className="pg-fact-value linkbtn" onClick={() => onOpenChat(row.session_id)} title={t("services.open.session")}>{row.session_title}</button></div>
        <div className="pg-fact"><span className="pg-fact-key">{t("services.pane.started")}</span><span className="pg-fact-value">{relTime(row.started_at)}</span></div>
        {row.stopped_at && !running && <div className="pg-fact"><span className="pg-fact-key">{t("services.pane.stopped")}</span><span className="pg-fact-value">{relTime(row.stopped_at)}{row.note ? ` · ${row.note}` : ""}</span></div>}
        {row.pid && running && <div className="pg-fact"><span className="pg-fact-key">{t("services.pane.pid")}</span><span className="pg-fact-value mono">{row.pid}</span></div>}
      </div>
      <div className="pg-pane-actions">
        <Button icon="file" onClick={() => void service.logs()}>{t("svc.menu.log")}</Button>
        {running && <Button icon="stop" onClick={() => void service.stop()}>{t("common.stop")}</Button>}
        <Button variant="danger" icon="trash" onClick={() => void service.remove()}>{t(running ? "svc.menu.remove.running" : "svc.menu.remove")}</Button>
      </div>
      {service.accessSheet}
    </div>
  );
}

/** Every service the agents host: a list of rows, and the open one beside it. */
export function ServicesScreen({ onOpen, toast }: { onOpen: (id: string) => void; toast: (t: string) => void }) {
  const { data: rows, error, loading, refresh } = useQuery<Row[]>("/api/services", { pollMs: 10000, staleMs: 3000 });
  const [log, setLog] = useState<{ title: string; text: string } | null>(null);
  const [showStopped, setShowStopped] = useState(false);
  const [picked, setPicked] = useState<string | null>(null);
  const split = useMedia("(min-width: 1280px)");
  const reload = () => {
    refresh();
    invalidate("/api/services");
  };
  const all = rows ?? [];
  // A host service the host's daemon cannot be asked about is still believed to run: it is listed with
  // the running ones, where its note says why its state is unknown, rather than hidden under "all".
  const running = all.filter((r) => r.status === "running" || r.status === "unknown");
  const stopped = all.filter((r) => r.status !== "running" && r.status !== "unknown");
  const shown = showStopped ? [...running, ...stopped] : running;
  const current = shown.find((r) => keyOf(r) === picked) ?? (split ? shown[0] : undefined);
  const hooks: Hooks = { onChange: reload, toast, onLogs: (row, text) => setLog({ title: `${row.session_title} · ${row.name}`, text }) };
  const pane = current && <ServicePane key={keyOf(current)} row={current} hooks={hooks} onOpenChat={onOpen} />;
  const empty = rows && rows.length === 0;
  return (
    <>
      <PageHeader title={screenTitle("services")} subtitle={rows ? `${t("services.running", { n: running.length })}${stopped.length ? t("services.stopped", { n: stopped.length }) : ""}` : undefined} ruled
        filters={stopped.length > 0 && (
          <Segmented
            label={t("services.filter")}
            value={showStopped ? "all" : "running"}
            options={[{ id: "running", label: t("services.filter.running", { n: running.length }) }, { id: "all", label: t("services.filter.all", { n: all.length }) }]}
            onChange={(id) => setShowStopped(id === "all")}
          />
        )}
      />
      {loading && !error && !rows && <div className="pg-list alone"><Skeleton rows={3} /></div>}
      {error && !rows && <div className="empty"><b>{t("services.error")}</b><div>{error}</div><button className="btn primary" onClick={refresh}>{t("common.retry")}</button></div>}
      {empty && (
        <div className="empty services-empty">
          <Icon name="globe" size={20} />
          <b>{t("services.empty")}</b>
          <div>{t("services.empty.sub")}</div>
          <Button variant="primary" icon="compose" onClick={() => navigate(newAgentPath())}>{t("services.empty.action")}</Button>
        </div>
      )}
      {rows && rows.length > 0 && (
        <div className="pg-split">
          <section className={`pg-list ${split ? "" : "alone"}`} aria-label={t("services.list")}>
            {shown.length === 0 && <div className="empty"><b>{t("services.none.running")}</b><div>{plural("services.none.running.sub", stopped.length)}</div></div>}
            {shown.map((r) => <ServiceItem key={keyOf(r)} row={r} on={current !== undefined && keyOf(r) === keyOf(current)} onPick={() => setPicked(keyOf(r))} hooks={hooks} />)}
          </section>
          {split ? <section className="pg-detail" aria-label={t("services.open")}>{pane}</section> : picked && current && <Sheet title={current.name} size="md" onClose={() => setPicked(null)}>{pane}</Sheet>}
        </div>
      )}
      {log && (
        <Sheet title={log.title} onClose={() => setLog(null)} size="xl">
          <pre className="filetext">{log.text}</pre>
        </Sheet>
      )}
    </>
  );
}
