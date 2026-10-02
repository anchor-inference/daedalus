import { useState } from "react";
import { ServiceView } from "../api";
import { ServiceRow, Skeleton } from "../ui/components";
import { Sheet } from "../ui/dialogs";
import { relTime } from "../format";
import { PageHeader, screenTitle } from "../ui/index";
import { invalidate, useQuery } from "../store";
import { plural, t } from "../i18n";

type Row = ServiceView & { session_id: string; session_title: string };

/** Every service the agents host, grouped by the session that runs it. */
export function ServicesScreen({ onOpen, toast }: { onOpen: (id: string) => void; toast: (t: string) => void }) {
  const { data: rows, error, loading, refresh } = useQuery<Row[]>("/api/services", { pollMs: 10000, staleMs: 3000 });
  const [log, setLog] = useState<{ title: string; text: string } | null>(null);
  const [showStopped, setShowStopped] = useState(false);
  const reload = () => {
    refresh();
    invalidate("/api/services");
  };
  const all = rows ?? [];
  const running = all.filter((r) => r.status === "running");
  const stopped = all.filter((r) => r.status !== "running");
  const shown = showStopped ? all : running;
  const groups = new Map<string, Row[]>();
  for (const r of shown) groups.set(r.session_id, [...(groups.get(r.session_id) ?? []), r]);
  return (
    <>
      <PageHeader title={screenTitle("services")} subtitle={rows ? `${t("services.running", { n: running.length })}${stopped.length ? t("services.stopped", { n: stopped.length }) : ""}` : undefined}>
        {stopped.length > 0 && (
          <div className="chips">
            <button className="chip select" aria-pressed={!showStopped} onClick={() => setShowStopped(false)}>{t("services.filter.running", { n: running.length })}</button>
            <button className="chip select" aria-pressed={showStopped} onClick={() => setShowStopped(true)}>{t("services.filter.all", { n: all.length })}</button>
          </div>
        )}
      </PageHeader>
      <div className="screen narrow">
        {loading && !error && <Skeleton rows={3} />}
        {error && !rows && <div className="empty"><b>{t("services.error")}</b><div>{error}</div><button className="btn primary" onClick={refresh}>{t("common.retry")}</button></div>}
        {rows && rows.length === 0 && (
          <div className="empty">
            <b>{t("services.empty")}</b>
            <div>{t("services.empty.sub")}</div>
          </div>
        )}
        {rows && rows.length > 0 && shown.length === 0 && <div className="empty"><b>{t("services.none.running")}</b><div>{plural("services.none.running.sub", stopped.length)}</div></div>}
        {[...groups.entries()].map(([sid, items]) => (
          <section key={sid} className="service-group">
            <div className="section-title">
              <button className="linkbtn" onClick={() => onOpen(sid)} title={t("services.open.session")}>{items[0].session_title}</button>
              <span className="n">{t("services.started", { t: relTime(items[0].started_at) })}</span>
            </div>
            {items.map((s) => (
              <ServiceRow key={s.name} s={s} sessionId={sid} onChange={reload} toast={toast} onLogs={(text) => setLog({ title: `${items[0].session_title} · ${s.name}`, text })} card />
            ))}
          </section>
        ))}
      </div>
      {log && (
        <Sheet title={log.title} onClose={() => setLog(null)} size="wide">
          <pre className="filetext">{log.text}</pre>
        </Sheet>
      )}
    </>
  );
}
