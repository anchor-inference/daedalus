import { useMemo, useState } from "react";
import { api, Notification, NotificationPage, NotificationSummary, Proposal } from "../api";
import { Skeleton } from "../ui/index";
import { deleteWithUndo } from "../ui/dialogs";
import { relTime } from "../format";
import { Icon } from "../icons";
import { navigate, pathFor } from "../router";
import { PageHeader, screenTitle, useMedia } from "../ui/index";
import { Button, IconButton, Select, Sheet, Tabs } from "../ui/primitives";
import { hold, invalidate, prime, release, useQuery } from "../store";
import { SUMMARY_KEY } from "../events";
import { useProjects } from "../projects";
import { byDay, categoryLabel, listKey, markAllSeen, markSeen, markUnseen, noticeIcon, projectNames, useSummary, whereLine } from "../notifications";
import { errorText } from "../ui";
import { plural, t } from "../i18n";
import { PhoneInbox } from "./InboxPhone";
import { InboxDetail, type InboxItem } from "./InboxDetail";
import { INBOX_FILTERS, INBOX_LIMIT, firstLine, tintOf, useInboxLists, viewOf, waiting, type InboxFilter } from "./inboxdata";
import { MovesInProgress } from "../project/moves";
import { usePendingSchedules, type ScheduleCardView } from "../schedulecard";

/**
 * The notification centre, and on a phone the only one: a list of what happened, what needs the
 * operator first, and beside it the entry that is open with the buttons that answer it. A request is
 * answered from here; an entry opened is read, except one that waits on the operator.
 */
export function InboxScreen(props: { toast: (t: string) => void; onOpen: (id: string) => void }) {
  // A phone draws the same lists from its own pieces (InboxPhone.tsx): rows, sheets and a selection.
  const phone = !useMedia("(min-width: 1024px)");
  return phone ? <PhoneInbox {...props} /> : <DesktopInbox {...props} />;
}

type Group = { key: string; label: string; waiting?: boolean; items: InboxItem[] };

function entryItem(entry: Notification, names: Map<string, string>): InboxItem {
  return {
    key: `n${entry.id}`, entry, icon: noticeIcon(entry), tint: tintOf(entry), kind: categoryLabel(entry.category), title: entry.title,
    preview: firstLine(entry.body) || categoryLabel(entry.category), where: whereLine(entry, names), at: entry.updated_at, unread: !entry.seen,
  };
}

function proposalItem(p: Proposal): InboxItem {
  return { key: `p${p.id}`, proposal: p, icon: "changes", tint: "attention", kind: t("ph.inbox.proposal"), title: p.title, preview: firstLine(p.summary) || p.branch, where: `${p.repo} · ${p.branch}`, at: p.created_at, unread: true };
}

function scheduleItem(view: ScheduleCardView): InboxItem {
  const p = view.proposal!;
  return { key: `s${p.id}`, schedule: view, icon: "clock", tint: "attention", kind: t("inbox.schedule"), title: p.name, preview: firstLine(p.prompt), where: t("notice.anywhere"), at: p.next_run_at, unread: true };
}

function DesktopInbox({ toast, onOpen }: { toast: (t: string) => void; onOpen: (id: string) => void }) {
  const split = useMedia("(min-width: 1280px)");
  const [filter, setFilter] = useState<InboxFilter>("all");
  const [project, setProject] = useState<string | null>(null);
  const view = viewOf(filter);
  const key = listKey(view, project, INBOX_LIMIT);
  const lists = useInboxLists(project);
  const { data, error, loading, refresh } = lists.by[filter];
  const proposals = useQuery<Proposal[]>("/api/proposals", { pollMs: 60000, staleMs: 30000 });
  const projects = useProjects();
  const names = useMemo(() => projectNames(projects.data), [projects.data]);
  const unseen = useSummary().unseen;
  const [picked, setPicked] = useState<string | null>(null);
  const schedules = usePendingSchedules();

  // A proposal or a schedule carries no project and no failure, so only the unfiltered views and the
  // one for what waits show them.
  const outside = project === null;
  const extras = outside && (filter === "all" || filter === "needs");
  const allPending = outside ? (proposals.data ?? []).filter((p) => p.status === "pending") : [];
  const pending = extras ? allPending : [];
  const proposed = extras ? schedules : [];

  const groups = useMemo<Group[]>(() => {
    const entries = data?.entries ?? [];
    const open = entries.filter(waiting).map((e) => entryItem(e, names));
    const rest = entries.filter((e) => !waiting(e));
    const out: Group[] = [];
    const head = [...open, ...pending.map(proposalItem), ...proposed.map(scheduleItem)];
    if (head.length) out.push({ key: "waiting", label: t("inbox.waiting"), waiting: true, items: head });
    for (const g of byDay(rest)) out.push({ key: g.key, label: g.label, items: g.entries.map((e) => entryItem(e, names)) });
    return out;
  }, [data, names, pending, proposed]);

  const items = groups.flatMap((g) => g.items);
  const current = items.find((i) => i.key === picked) ?? (split ? items[0] : undefined);
  // A tab counts what it would show, whichever tab is open.
  const counts = (f: InboxFilter) => lists.count(f, allPending.length + (outside ? schedules.length : 0));

  /** The entry as the page last heard of it: the list's copy, or the one an answer brought back. */
  function patch(fn: (list: Notification[]) => Notification[], summary?: NotificationSummary) {
    if (!data) return;
    const next = summary ?? data.summary;
    prime<NotificationPage>(key, { ...data, entries: fn(data.entries), summary: next });
    prime(SUMMARY_KEY, next);
  }

  function pick(item: InboxItem) {
    setPicked(item.key);
    // Opened is read, except what still waits for an answer: reading it is not answering it.
    if (item.entry && !item.entry.seen && !waiting(item.entry)) void markSeen([item.entry.id]);
  }

  function remove(entry: Notification) {
    const before = data;
    hold(key);
    patch((l) => l.filter((e) => e.id !== entry.id));
    setPicked(null);
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

  const detail = current && (
    <InboxDetail
      item={current}
      names={names}
      toast={toast}
      onOpenChat={onOpen}
      onToggleRead={(entry) => void (entry.seen ? markUnseen([entry.id]) : markSeen([entry.id]))}
      onDelete={remove}
      onDecided={() => { proposals.refresh(); invalidate("/api/proposals"); }}
    />
  );

  /** Up and Down walk the rows with the keyboard; Enter or Space opens the one that has focus. */
  function moveFocus(e: React.KeyboardEvent<HTMLElement>) {
    if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
    const rows = [...e.currentTarget.querySelectorAll<HTMLElement>(".inbox-row")];
    const at = rows.indexOf(document.activeElement as HTMLElement);
    if (at < 0) return;
    e.preventDefault();
    rows[Math.max(0, Math.min(rows.length - 1, at + (e.key === "ArrowDown" ? 1 : -1)))].focus();
  }

  const empty = data && items.length === 0;
  return (
    <>
      <PageHeader
        title={screenTitle("inbox")}
        ruled
        filters={
          <Tabs
            variant="page"
            className="inbox-tabs"
            label={t("inbox.filters")}
            value={filter}
            onChange={(id) => { setFilter(id as InboxFilter); setPicked(null); }}
            tabs={INBOX_FILTERS.map((f) => ({ id: f.id, label: t(f.label), count: counts(f.id) }))}
          />
        }
        actions={
          <>
            <Select
              size="sm"
              className="inbox-project"
              label={t("inbox.project")}
              value={project ?? "all"}
              options={[{ id: "all", label: t("inbox.project.all") }, ...(projects.data ?? []).map((p) => ({ id: p.id, label: p.name }))]}
              onChange={(id) => { setProject(id === "all" ? null : id); setPicked(null); }}
            />
            <Button variant="quiet" icon="check" disabled={unseen === 0} onClick={() => void markAllSeen()}>{t("inbox.markall")}</Button>
            <IconButton icon="settings" label={t("ph.inbox.settings")} onClick={() => navigate(pathFor("settings", "notifications"))} />
          </>
        }
      />
      {error && !data ? (
        <div className="empty"><b>{t("inbox.error")}</b><div>{error}</div><button className="btn primary" onClick={refresh}>{t("common.retry")}</button></div>
      ) : empty ? (
        <div className="empty inbox-empty">
          <Icon name={filter === "problems" || filter === "done" ? "check" : "inbox"} size={20} />
          <b>{t(filter === "needs" ? "inbox.empty.needs" : filter === "problems" ? "inbox.empty.problems" : filter === "done" ? "inbox.empty.done" : "inbox.empty")}</b>
          {filter !== "all" || project !== null ? <button className="btn" onClick={() => { setFilter("all"); setProject(null); }}>{t("ph.inbox.showall")}</button> : <div className="sub">{t("ph.inbox.empty.sub")}</div>}
        </div>
      ) : (
        <div className="inbox-split">
          <section className="inbox-list" aria-label={t("inbox.list")} onKeyDown={moveFocus}>
            <MovesInProgress projects={projects.data} />
            {loading && !data && <Skeleton rows={6} />}
            {groups.map((group) => (
              <div key={group.key} className="inbox-group" data-group={group.key}>
                <div className={`inbox-group-head ${group.waiting ? "waiting" : ""}`}>{group.label}<span className="n">{group.items.length}</span></div>
                {group.items.map((item) => (
                  <button
                    key={item.key}
                    type="button"
                    className={`inbox-row ${current?.key === item.key ? "on" : ""} ${item.unread ? "unread" : ""} ${item.entry?.level === "quiet" ? "quiet" : ""}`}
                    data-notice={item.entry?.id}
                    data-proposal={item.proposal?.id}
                    data-schedule={item.schedule?.proposal?.id}
                    aria-current={current?.key === item.key}
                    onClick={() => pick(item)}
                  >
                    <span className={`inbox-row-mark ${item.tint}`} aria-hidden="true" />
                    <span className="inbox-row-body">
                      <span className="inbox-row-top">
                        <span className="inbox-row-title">{item.title}</span>
                        {item.entry && item.entry.count > 1 && <span className="inbox-row-count num">×{item.entry.count}</span>}
                        <span className="inbox-row-unread" aria-hidden="true" />
                      </span>
                      <span className="inbox-row-preview">{item.preview}</span>
                      <span className="inbox-row-foot">
                        <span className="inbox-row-where">{item.where}</span>
                        <span className="inbox-row-time num">{relTime(item.at)}</span>
                      </span>
                    </span>
                  </button>
                ))}
              </div>
            ))}
          </section>
          {split ? (
            <section className="inbox-detail" aria-label={t("inbox.open")}>{detail}</section>
          ) : (
            picked && current && <Sheet size="md" ariaLabel={current.title} onClose={() => setPicked(null)}>{detail}</Sheet>
          )}
        </div>
      )}
    </>
  );
}
