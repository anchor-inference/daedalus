import { useState } from "react";
import { api, Notification, NotificationPage, NotificationSummary, Proposal } from "../api";
import { Skeleton } from "../ui/index";
import { OverflowMenu, Sheet, deleteWithUndo } from "../ui/dialogs";
import { absTime, relTime } from "../format";
import { Icon } from "../icons";
import { navigate, pathFor } from "../router";
import { PageHeader, screenTitle, useMedia } from "../ui/index";
import { hold, invalidate, prime, release, useQuery } from "../store";
import { SUMMARY_KEY } from "../events";
import { useProjects } from "../projects";
import { ActionButtons, NeedsYou, NotificationList, NotificationRow, byDay, byProject, entryPath, listKey, projectNames, useNotifications } from "../notifications";
import { PushNudge } from "../pushui";
import { errorText } from "../ui";
import { plural, t } from "../i18n";

type Filter = "all" | "unseen" | "problems" | "projects";

/**
 * The notification centre, and on a phone the only one: what needs the operator first, with its
 * buttons; the change proposals waiting for a decision; then everything else, by day or by project.
 */
export function InboxScreen({ toast, onOpen }: { toast: (t: string) => void; onOpen: (id: string) => void }) {
  const split = useMedia("(min-width: 1280px)");
  const [filter, setFilter] = useState<Filter>("all");
  const view = filter === "projects" ? "all" : filter;
  const key = listKey(view, null, 200);
  const { data, error, loading, refresh } = useNotifications(view, null, 200);
  const proposals = useQuery<Proposal[]>("/api/proposals", { pollMs: 60000, staleMs: 30000 });
  const projects = useProjects();
  const names = projectNames(projects.data);
  const [open, setOpen] = useState<number | null>(null);
  const unseen = data?.summary.unseen ?? 0;
  const needs = data?.summary.needs_you ?? 0;
  // What needs the operator is drawn above, with its buttons; the list below is the rest.
  const entries = (data?.entries ?? []).filter((e) => !e.needs_you);
  const pending = (proposals.data ?? []).filter((p) => p.status === "pending");
  const groups = filter === "projects" ? byProject(entries, names) : byDay(entries);

  /** Show a change before the server has confirmed it; the badge follows from the same summary. */
  function patch(fn: (list: Notification[]) => Notification[], summary?: NotificationSummary) {
    if (!data) return;
    const next = summary ?? data.summary;
    prime<NotificationPage>(key, { ...data, entries: fn(data.entries), summary: next });
    prime(SUMMARY_KEY, next);
  }

  async function markAll() {
    try {
      const r = await api.post<{ marked: number; summary: NotificationSummary }>("/api/notifications/seen", { all: true });
      patch((l) => l.map((e) => ({ ...e, seen: true })), r.summary);
      refresh();
    } catch (e) {
      toast(errorText(e));
    }
  }

  async function markSeen(entry: Notification) {
    if (entry.seen) return;
    patch((l) => l.map((e) => (e.id === entry.id ? { ...e, seen: true } : e)));
    try {
      const r = await api.post<{ marked: number; summary: NotificationSummary }>("/api/notifications/seen", { ids: [entry.id] });
      prime(SUMMARY_KEY, r.summary);
    } catch (e) {
      toast(errorText(e));
    }
  }

  function toggle(entry: Notification) {
    const next = open === entry.id ? null : entry.id;
    setOpen(next);
    if (next !== null) void markSeen(entry);
  }

  function remove(entry: Notification) {
    const before = data;
    hold(key);
    patch((l) => l.filter((e) => e.id !== entry.id));
    setOpen(null);
    deleteWithUndo(
      plural("inbox.deleted", 1),
      async () => {
        try {
          await api.delete(`/api/notifications/${entry.id}`);
        } finally {
          release(key);
          refresh();
          invalidate(SUMMARY_KEY);
        }
      },
      () => {
        release(key);
        if (before) prime(key, before);
      },
      (e) => toast(errorText(e)),
    );
  }

  const detail = (entry: Notification) => (
        <div className="inbox-body" onClick={(e) => e.stopPropagation()}>
          {entry.body && <pre className="inbox-text">{entry.body}</pre>}
          <ActionButtons entry={entry} />
          <div className="btnrow">
            {(entry.link || entry.session_id) && (
              <button className="btn small" onClick={() => (entry.session_id && !entry.link ? onOpen(entry.session_id) : navigate(entryPath(entry)))}>
                <Icon name={entry.session_id ? "bots" : "forward"} size={14} /> {t(entry.session_id ? "inbox.open.session" : "notice.open")}
              </button>
            )}
            <span className="grow" />
            <OverflowMenu
              small
              label={t("inbox.actions")}
              items={[
                { label: t("inbox.markunread"), icon: "inbox", onSelect: () => patch((l) => l.map((e) => (e.id === entry.id ? { ...e, seen: false } : e))) },
                "-",
                { label: t("common.delete"), icon: "trash", danger: true, onSelect: () => remove(entry) },
              ]}
            />
          </div>
        </div>
  );
  const selected = entries.find((entry) => entry.id === open);

  const row = (entry: Notification) => (
    <NotificationRow key={entry.id} entry={entry} names={names} card expanded={open === entry.id} onActivate={() => toggle(entry)}>
      {open === entry.id && !split && detail(entry)}
    </NotificationRow>
  );

  return (
    <>
      <PageHeader
        title={screenTitle("inbox")}
        subtitle={unseen > 0 ? plural("inbox.unread", unseen) : undefined}
        actions={<button className="iconbtn" onClick={markAll} disabled={unseen === 0} title={t("inbox.markall")} aria-label={t("inbox.markall")}><Icon name="check" /></button>}
      >
        <div className="chips">
          <button className="chip select" aria-pressed={filter === "all"} onClick={() => setFilter("all")}>{t("common.all")}</button>
          <button className="chip select" aria-pressed={filter === "unseen"} onClick={() => setFilter("unseen")}>{t("inbox.filter.unread")}{unseen > 0 ? ` · ${unseen}` : ""}</button>
          <button className="chip select" aria-pressed={filter === "problems"} onClick={() => setFilter("problems")}>{t("inbox.filter.problems")}</button>
          <button className="chip select" aria-pressed={filter === "projects"} onClick={() => setFilter("projects")}>{t("centre.projects")}</button>
        </div>
      </PageHeader>
      <div className={`inbox-layout ${split ? "split" : ""}`}>
      <div className="screen narrow inbox-list">
        <PushNudge />
        <NeedsYou names={names} card />
        {pending.length > 0 && filter !== "problems" && (
          <section>
            <div className="section-title">{t("inbox.waiting")} <span className="n">{pending.length}</span></div>
            {pending.map((p) => (
              <ProposalCard key={p.id} p={p} toast={toast} onDone={() => { proposals.refresh(); invalidate("/api/proposals"); }} />
            ))}
          </section>
        )}
        {loading && !error && <Skeleton rows={6} />}
        {error && !data && <div className="empty"><b>{t("inbox.error")}</b><div>{error}</div><button className="btn primary" onClick={refresh}>{t("common.retry")}</button></div>}
        {data && entries.length === 0 && pending.length === 0 && needs === 0 && (
          <div className="empty">
            <b>{t(filter === "unseen" ? "inbox.empty.unread" : filter === "problems" ? "inbox.empty.problems" : "inbox.empty")}</b>
          </div>
        )}
        <NotificationList groups={groups} row={row} />
      </div>
      {split && <aside className="inbox-detail" aria-label={t("nav.inbox")}>
        {selected ? <><div className="inbox-detail-head"><h2>{selected.title}</h2><button className="iconbtn" aria-label={t("common.close")} onClick={() => setOpen(null)}><Icon name="close" /></button></div>{detail(selected)}</> : <div className="empty calm">{t("inbox.select")}</div>}
      </aside>}
      </div>
    </>
  );
}

/** A change waiting for a decision: approve here, reject with a reason, or read the diff on Changes. */
export function ProposalCard({ p, toast, onDone }: { p: Proposal; toast: (t: string) => void; onDone: () => void }) {
  const [rejecting, setRejecting] = useState(false);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  async function decide(decision: "approve" | "reject") {
    setBusy(true);
    try {
      const r = await api.post<{ result: string }>(`/api/proposals/${p.id}/decide`, { decision, reason });
      toast(r.result);
      setRejecting(false);
      onDone();
    } catch (e) {
      toast(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="erow action">
      <span className="kind warning"><Icon name="changes" size={16} /></span>
      <div className="erow-main">
        <div className="erow-head">
          <span className="erow-title clamp-2">{p.title}</span>
          <span className="erow-time num" title={absTime(p.created_at)}>{relTime(p.created_at)}</span>
        </div>
        <div className="erow-meta">
          <span className="mono">{p.branch}</span>
          {p.pr_number && <span className="sep">·</span>}
          {p.pr_number && <span className="nowrap">PR #{p.pr_number}</span>}
        </div>
        {p.summary && <div className="sub clamp-3" style={{ marginTop: 4 }}>{p.summary}</div>}
        <div className="btnrow">
          <button className="btn small primary" disabled={busy} onClick={() => decide("approve")}>{t("inbox.approve")}</button>
          <button className="btn small" disabled={busy} onClick={() => setRejecting(true)}>{t("inbox.reject")}</button>
          <button className="btn small ghost" onClick={() => navigate(pathFor("changes", p.id))}>{t("inbox.diff")}</button>
        </div>
      </div>
      {rejecting && (
        <Sheet title={t("inbox.reject.title")} onClose={() => setRejecting(false)} size="narrow">
          <div className="sub">{p.title}</div>
          <label className="field">{t("inbox.reason")}</label>
          <textarea className="field" rows={3} autoFocus value={reason} onChange={(e) => setReason(e.target.value)} placeholder={t("inbox.reason.placeholder")} />
          <div className="sheet-foot">
            <button className="btn ghost" onClick={() => setRejecting(false)}>{t("common.cancel")}</button>
            <button className="btn danger solid" disabled={busy} onClick={() => decide("reject")}>{t("inbox.reject.do")}</button>
          </div>
        </Sheet>
      )}
    </div>
  );
}
