// The Inbox on a phone: what needs the operator first, with its answers on the row; the change
// proposals waiting for a decision; then everything else by day or by project. A row opens its
// detail as a sheet, a swipe marks it read or deletes it, a long press offers the same, and Select
// (in the ⋮) answers or clears several at once. The desktop keeps Inbox.tsx; both read the same
// lists and take the same actions.
//
// A request seen here is out of its context: the operator is not looking at the run it belongs to.
// So nothing on a row or in the bulk sheet is drawn as the white primary, whatever style the host
// gave the button, and "Allow all" only ever takes the requests the host marked quick — an elevated
// one (a host command among them) is answered one by one, from its own sheet.

import { useMemo, useState, type ReactNode } from "react";
import { api, type Notification, type NotificationPage, type Proposal } from "../api";
import { absTime, relTime } from "../format";
import { plural, t } from "../i18n";
import { Icon } from "../icons";
import { navigate, pathFor } from "../router";
import { hold, invalidate, prime, release, useOffline, useQuery } from "../store";
import { SUMMARY_KEY } from "../events";
import { useProjects } from "../projects";
import { act, afterChange, byDay, byProject, categoryLabel, entryPath, listKey, markAllSeen, markSeen, noticeIcon, noticeLine, projectNames, resolutionLabel, toneClass, useNotifications, type ActResult } from "../notifications";
import { PushNudge } from "../pushui";
import { errorText } from "../ui";
import { deleteWithUndo, toast as statusLine, type MenuItem } from "../ui/dialogs";
import { ActionSheet, Banner, BottomSheet, Chip, ChipBar, EmptyState, IconButton, ListRow, RowSkeleton, SectionHeader, TopBar } from "../ui/phone";

type Filter = "all" | "unseen" | "problems" | "projects";
type Open = { kind: "entry"; id: number } | { kind: "proposal"; id: string } | { kind: "menu" } | { kind: "bulk" } | null;
/** A request answered from this page, kept on it (dimmed) until the page is left: the answer, and
 *  whether someone else had answered first. */
type Answered = { entry: Notification; resolution: string; conflict: boolean };

const problem = (e: Notification) => e.tone === "warning" || e.tone === "error";
const allowable = (e: Notification) => !e.resolved && e.actions.some((a) => a.id === "allow" && a.quick);

/** The host's button styles on a phone row: the white primary is dropped (the row is out of context),
 *  a refusal keeps its red, Open stays the quiet word it is. */
function answerClass(style: string, id: string, sheet = false): string {
  if (style === "ghost" || id === "open") return "ph-btn sm ghost";
  if (style === "danger" || (sheet && id === "deny")) return "ph-btn sm danger";
  return "ph-btn sm";
}

/** The first line of a request's text: the command, the question. */
function firstLine(body: string): string {
  return body.split("\n").find((line) => line.trim()) ?? "";
}

export function PhoneInbox({ toast, onOpen }: { toast: (text: string) => void; onOpen: (id: string) => void }) {
  const offline = useOffline();
  const [filter, setFilter] = useState<Filter>("all");
  const view = filter === "projects" ? "all" : filter;
  const key = listKey(view, null, 200);
  const list = useNotifications(view, null, 200);
  const all = useNotifications("all", null, 200);
  const needsList = useNotifications("needs_you", null, 50);
  const proposals = useQuery<Proposal[]>("/api/proposals", { pollMs: 60000, staleMs: 30000 });
  const projects = useProjects();
  const names = projectNames(projects.data);
  const [open, setOpen] = useState<Open>(null);
  const [selected, setSelected] = useState<Set<number> | null>(null);
  const [answered, setAnswered] = useState<Map<number, Answered>>(new Map());
  const data = list.data;
  const unseen = data?.summary.unseen ?? all.data?.summary.unseen ?? 0;
  const problems = (all.data?.entries ?? []).filter(problem).length;
  const keep = (e: Notification) => (view === "unseen" ? !e.seen : view === "problems" ? problem(e) : true);
  const needs = (needsList.data?.entries ?? []).filter(keep);
  const kept = [...answered.values()].filter((a) => !needs.some((e) => e.id === a.entry.id));
  const entries = (data?.entries ?? []).filter((e) => !e.needs_you && !answered.has(e.id));
  const pending = filter === "problems" ? [] : (proposals.data ?? []).filter((p) => p.status === "pending");
  const groups = filter === "projects" ? byProject(entries, names) : byDay(entries);
  const everything = [...needs, ...entries];
  const byId = useMemo(() => new Map([...(needsList.data?.entries ?? []), ...(data?.entries ?? []), ...[...answered.values()].map((a) => a.entry)].map((e) => [e.id, e])), [needsList.data, data, answered]);

  /** Show a change before the server confirms it; the badge follows from the same summary. */
  function patch(fn: (l: Notification[]) => Notification[]) {
    for (const k of new Set([key, listKey("all", null, 200), listKey("needs_you", null, 50)])) {
      const page = k === key ? data : k === listKey("all", null, 200) ? all.data : needsList.data;
      if (page) prime<NotificationPage>(k, { ...page, entries: fn(page.entries) });
    }
  }

  function setSeen(ids: number[], seen: boolean) {
    if (!ids.length) return;
    patch((l) => l.map((e) => (ids.includes(e.id) ? { ...e, seen } : e)));
    // Unread again is the reader's own mark: the host keeps no such state, so it lives in this list
    // until the next read of it, as the desktop's does.
    if (seen) void markSeen(ids);
  }

  function remove(ids: number[]) {
    if (!ids.length) return;
    const before = [data, all.data, needsList.data] as const;
    hold(key);
    patch((l) => l.filter((e) => !ids.includes(e.id)));
    setOpen(null);
    deleteWithUndo(
      plural("inbox.deleted", ids.length),
      async () => {
        try {
          for (const id of ids) await api.delete(`/api/notifications/${id}`);
        } finally {
          release(key);
          list.refresh();
          invalidate(SUMMARY_KEY);
          afterChange();
        }
      },
      () => {
        release(key);
        if (before[0]) prime(key, before[0]);
        if (before[1]) prime(listKey("all", null, 200), before[1]);
        if (before[2]) prime(listKey("needs_you", null, 50), before[2]);
      },
      (e) => toast(errorText(e)),
    );
  }

  const answeredHere = (entry: Notification, result: ActResult) => {
    if (result.resolution) setAnswered((m) => new Map(m).set(entry.id, { entry, resolution: result.resolution!, conflict: result.conflict }));
  };

  const openEntry = (entry: Notification) => {
    if (!entry.seen) setSeen([entry.id], true);
    if (entry.session_id && !entry.link) onOpen(entry.session_id);
    else navigate(entryPath(entry));
  };

  const toggle = (id: number) => setSelected((s) => {
    const next = new Set(s ?? []);
    if (next.has(id)) next.delete(id); else next.add(id);
    return next;
  });

  const row = (entry: Notification, opts: { answered?: Answered } = {}) => {
    const picking = selected !== null;
    const done = opts.answered;
    const needsYou = entry.needs_you && !done && !entry.resolved;
    const tone = toneClass(entry);
    const items: MenuItem[] = [
      ...((entry.link || entry.session_id) ? [{ label: t("notice.open"), icon: "forward" as const, onSelect: () => openEntry(entry) }] : []),
      { label: t("ph.inbox.details"), icon: "inbox" as const, onSelect: () => setOpen({ kind: "entry", id: entry.id }) },
      entry.seen ? { label: t("inbox.markunread"), icon: "inbox" as const, onSelect: () => setSeen([entry.id], false) } : { label: t("ph.inbox.markread"), icon: "check" as const, onSelect: () => setSeen([entry.id], true) },
      { label: t("ph.inbox.select"), icon: "check" as const, onSelect: () => setSelected(new Set([entry.id])) },
      ...(!needsYou ? [{ label: t("common.delete"), icon: "trash" as const, danger: true, onSelect: () => remove([entry.id]) }] : []),
    ];
    const detail = needsYou && entry.body ? firstLine(entry.body) : "";
    return (
      <ListRow
        key={entry.id}
        className={`ph-irow ${done ? "dim" : ""}`}
        data={{ notice: String(entry.id) }}
        unread={!entry.seen && !done}
        title={<>{entry.title}{entry.count > 1 && <span className="ph-irow-n"> ×{entry.count}</span>}</>}
        label={entry.title}
        lead={picking
          ? <span className={`ph-check ${selected!.has(entry.id) ? "on" : ""}`} role="checkbox" aria-checked={selected!.has(entry.id)} aria-label={entry.title}>{selected!.has(entry.id) && <Icon name="check" size={16} />}</span>
          : <span className={`ph-kico ${done ? "" : tone}`} aria-label={categoryLabel(entry.category)}><Icon name={noticeIcon(entry)} size={18} /></span>}
        trail={<span className="ph-irow-tm" title={absTime(entry.updated_at)}>{relTime(entry.updated_at)}{!entry.seen && !done && <span className="ph-udot" aria-hidden />}</span>}
        meta={done ? undefined : <span className="ph-ell">{noticeLine(entry, names)}</span>}
        body={(detail || needsYou || done) && !picking ? (
          <>
            {detail && <span className={`ph-irow-d ${entry.category === "permission" ? "" : "text"}`}>{detail}</span>}
            {done && <span className="ph-irow-m">{done.conflict ? t("ph.inbox.elsewhere", { outcome: resolutionLabel(done.resolution) }) : resolutionLabel(done.resolution)}</span>}
            {needsYou && <Answers entry={entry} offline={offline} onDone={(r) => answeredHere(entry, r)} onOpen={() => openEntry(entry)} />}
          </>
        ) : undefined}
        // A request opens its detail, where it is answered; anything else with a place in the app
        // opens that place, as a notification does — its detail is one item of the long press.
        onOpen={() => (picking ? toggle(entry.id) : needsYou || done || !(entry.link || entry.session_id) ? setOpen({ kind: "entry", id: entry.id }) : openEntry(entry))}
        actions={picking || done ? undefined : items}
        more={false}
        preview={{ title: entry.title, meta: noticeLine(entry, names) }}
        swipe={picking || needsYou || done ? undefined : [
          ...(!entry.seen ? [{ label: t("ph.inbox.markread"), icon: "check" as const, tone: "info" as const, onSelect: () => setSeen([entry.id], true) }] : []),
          { label: t("common.delete"), icon: "trash" as const, tone: "danger" as const, onSelect: () => remove([entry.id]) },
        ]}
        swipeStart={picking || needsYou || done || !entry.seen ? undefined : { label: t("ph.inbox.unread"), icon: "inbox", tone: "accent", onSelect: () => setSeen([entry.id], false) }}
      />
    );
  };

  const nothing = data && entries.length === 0 && pending.length === 0 && needs.length === 0 && kept.length === 0;
  let body: ReactNode;
  if (list.error && !data) {
    body = <EmptyState icon="alert" tone="bad" title={t("inbox.error")} body={list.error} action={<button type="button" className="ph-btn primary" onClick={() => list.refresh()}>{t("common.retry")}</button>} />;
  } else if (!data) {
    body = <RowSkeleton rows={7} lead="square" />;
  } else if (nothing && filter === "all") {
    body = <EmptyState icon="inbox" title={t("inbox.empty")} body={t("ph.inbox.empty.sub")} />;
  } else if (nothing) {
    body = <EmptyState icon="check" tone="ok" title={t(filter === "unseen" ? "inbox.empty.unread" : filter === "problems" ? "inbox.empty.problems" : "inbox.empty")}
      body={t(filter === "unseen" ? "ph.inbox.empty.unread" : "ph.inbox.empty.problems")}
      action={<button type="button" className="ph-btn" onClick={() => setFilter("all")}>{t("ph.inbox.showall")}</button>} />;
  } else {
    body = (
      <>
        <PushNudge />
        {(needs.length > 0 || kept.length > 0) && (
          <section className="ph-needs" aria-label={t("centre.needs")}>
            <SectionHeader tone="warn" count={needs.length || undefined}>{t("centre.needs")}</SectionHeader>
            <div className="ph-list">
              {needs.map((e) => row(e))}
              {kept.map((a) => row(a.entry, { answered: a }))}
            </div>
          </section>
        )}
        {pending.length > 0 && selected === null && (
          <section>
            <SectionHeader count={pending.length}>{t("inbox.waiting")}</SectionHeader>
            <div className="ph-list">
              {pending.map((p) => (
                <ListRow key={p.id} className="ph-irow" data={{ proposal: p.id }} title={p.title} unread
                  lead={<span className="ph-kico"><Icon name="changes" size={18} /></span>}
                  trail={<span className="ph-irow-tm" title={absTime(p.created_at)}>{relTime(p.created_at)}<span className="ph-udot" aria-hidden /></span>}
                  meta={<span className="ph-ell">{[t("ph.inbox.proposal"), p.pr_number ? `PR #${p.pr_number}` : ""].filter(Boolean).join(" · ")}</span>}
                  body={<span className="ph-irow-d quiet">{p.branch}</span>}
                  onOpen={() => setOpen({ kind: "proposal", id: p.id })} />
              ))}
            </div>
          </section>
        )}
        {groups.map((group) => (
          <section key={group.key || "-"}>
            <SectionHeader count={group.key !== "today" && group.key !== "earlier" ? group.entries.length : undefined}>{group.label}</SectionHeader>
            <div className="ph-list">{group.entries.map((e) => row(e))}</div>
          </section>
        ))}
      </>
    );
  }

  const shown = selected ?? new Set<number>();
  const chosen = [...shown].map((id) => byId.get(id)).filter((e): e is Notification => !!e);
  const toAllow = chosen.filter(allowable);
  const current = open?.kind === "entry" ? byId.get(open.id) : undefined;
  const proposal = open?.kind === "proposal" ? (proposals.data ?? []).find((p) => p.id === open.id) : undefined;

  return (
    <div className={`ph-page ph-inbox ${selected ? "selecting" : ""}`}>
      {selected ? (
        <TopBar back={() => setSelected(null)} backIcon="close" backLabel={t("ph.inbox.done")} title={plural("ph.inbox.selected", shown.size)}
          actions={<button type="button" className="ph-link" onClick={() => setSelected(new Set(everything.map((e) => e.id)))}>{t("ph.inbox.selectall")}</button>} />
      ) : (
        <TopBar center title={t("nav.inbox")} sub={unseen > 0 ? plural("inbox.unread", unseen) : undefined}
          actions={<IconButton icon="vdots" label={t("ph.inbox.menu")} popup="menu" onClick={() => setOpen({ kind: "menu" })} />} />
      )}
      {offline && <Banner strip tone="bad" icon="offline">{t("app.offline")}</Banner>}
      {!selected && (data || list.error) && !(nothing && filter === "all") && (
        <ChipBar label={t("ph.inbox.filters")}>
          <Chip on={filter === "all"} onClick={() => setFilter("all")}>{t("common.all")}</Chip>
          <Chip on={filter === "unseen"} count={unseen} onClick={() => setFilter("unseen")}>{t("inbox.filter.unread")}</Chip>
          <Chip on={filter === "problems"} count={problems || undefined} onClick={() => setFilter("problems")}>{t("inbox.filter.problems")}</Chip>
          <Chip on={filter === "projects"} onClick={() => setFilter("projects")}>{t("centre.projects")}</Chip>
        </ChipBar>
      )}
      <div className={`ph-page-body ${data && !nothing ? "list" : ""}`}>{body}</div>
      {selected && (
        <div className="ph-selbar" role="toolbar" aria-label={plural("ph.inbox.selected", shown.size)}>
          <button type="button" disabled={!chosen.some((e) => !e.seen)} onClick={() => { setSeen(chosen.map((e) => e.id), true); setSelected(null); }}><Icon name="check" size={22} />{t("ph.inbox.markread")}</button>
          <button type="button" disabled={!chosen.some((e) => !e.needs_you)} onClick={() => { remove(chosen.filter((e) => !e.needs_you).map((e) => e.id)); setSelected(null); }}><Icon name="trash" size={22} />{t("common.delete")}</button>
          {toAllow.length > 0 && <button type="button" className="warn" disabled={offline} onClick={() => setOpen({ kind: "bulk" })}><Icon name="key" size={22} />{t("ph.inbox.allowN", { n: toAllow.length })}</button>}
        </div>
      )}
      {open?.kind === "menu" && (
        <ActionSheet onClose={() => setOpen(null)} items={[
          { label: t("inbox.markall"), icon: "check", disabled: unseen === 0, onSelect: () => void markAllSeen() },
          { label: t("ph.inbox.select"), icon: "check", onSelect: () => setSelected(new Set()) },
          { label: t("ph.inbox.settings"), icon: "settings", onSelect: () => navigate(pathFor("settings", "notifications")) },
        ]} />
      )}
      {current && <EntrySheet entry={current} names={names} offline={offline} onClose={() => setOpen(null)} onOpen={() => { setOpen(null); openEntry(current); }}
        onUnread={() => { setSeen([current.id], false); setOpen(null); }} onDelete={() => remove([current.id])}
        onDone={(r) => { answeredHere(current, r); setOpen(null); }} answered={answered.get(current.id)} />}
      {proposal && <ProposalSheet p={proposal} toast={toast} onClose={() => setOpen(null)} onDone={() => { setOpen(null); proposals.refresh(); invalidate("/api/proposals"); }} />}
      {open?.kind === "bulk" && <AllowSheet entries={toAllow} names={names} toast={toast} onClose={() => setOpen(null)}
        onDone={(results) => { results.forEach(([entry, r]) => answeredHere(entry, r)); setOpen(null); setSelected(null); }} />}
    </div>
  );
}

/**
 * A request's answers on its row or in its sheet. Offline, a press only marks the choice: it stays
 * marked until the connection is back and the operator presses it again, because an answer sent by
 * a reconnect alone could land long after the moment it was meant for.
 */
function Answers({ entry, offline, sheet = false, onDone, onOpen }: { entry: Notification; offline: boolean; sheet?: boolean; onDone: (result: ActResult) => void; onOpen: () => void }) {
  const [busy, setBusy] = useState(false);
  const [chosen, setChosen] = useState<string | null>(null);
  async function take(id: string) {
    if (offline) { setChosen(id); return; }
    setBusy(true);
    try {
      const result = await act(entry.id, id);
      afterChange();
      onDone(result);
    } catch (e) {
      setChosen(null);
      statusLine(t("notice.failed", { error: errorText(e) }));
    } finally {
      setBusy(false);
    }
  }
  const answers = entry.actions.filter((a) => a.id !== "open");
  const opener = entry.actions.find((a) => a.id === "open");
  return (
    <>
      <span className={`ph-answers ${sheet ? "grid" : ""}`}>
        {answers.map((a) => (
          <button key={a.id} type="button" data-action={a.id} className={`${answerClass(a.style, a.id, sheet)} ${chosen === a.id ? "chosen" : ""}`} disabled={busy} aria-pressed={chosen === a.id || undefined} onClick={() => void take(a.id)}>
            {chosen === a.id && <Icon name="check" size={16} />}{a.label || a.id}
          </button>
        ))}
        {!sheet && opener && <button type="button" data-action="open" className="ph-btn sm ghost" onClick={onOpen}>{opener.label || t("notice.open")}</button>}
      </span>
      {offline && chosen && <span className="ph-irow-m wrap" role="status">{t("ph.inbox.offline.kept")}</span>}
    </>
  );
}

function EntrySheet({ entry, names, offline, answered, onClose, onOpen, onUnread, onDelete, onDone }: { entry: Notification; names: Map<string, string>; offline: boolean; answered?: Answered; onClose: () => void; onOpen: () => void; onUnread: () => void; onDelete: () => void; onDone: (r: ActResult) => void }) {
  const needsYou = entry.needs_you && !entry.resolved && !answered;
  const resolution = answered?.resolution ?? entry.resolved;
  const project = entry.project_id ? names.get(entry.project_id) : undefined;
  const opens = !!(entry.link || entry.session_id);
  const command = entry.category === "permission";
  return (
    <BottomSheet onClose={onClose} className="ph-entry" label={entry.title}
      title={<span className="ph-sheet-kt"><span className={`ph-kico ${toneClass(entry)}`}><Icon name={noticeIcon(entry)} size={18} /></span><span className="ph-sheet-kt-w"><span className="ph-sheet-kt-t">{entry.title}</span><span className="ph-sheet-kt-m">{categoryLabel(entry.category)} · {relTime(entry.updated_at)}</span></span></span>}
      footer={needsYou ? (
        <div className="ph-foot-stack">
          <Answers entry={entry} offline={offline} sheet onDone={onDone} onOpen={onOpen} />
          {opens && <button type="button" className="ph-btn ghost" onClick={onOpen}>{t(entry.session_id ? "inbox.open.session" : "notice.open")}</button>}
        </div>
      ) : (
        <div className="ph-foot-row">
          {opens && <button type="button" className="ph-btn" onClick={onOpen}>{t(entry.session_id ? "inbox.open.session" : "notice.open")}</button>}
          {entry.seen && <button type="button" className="ph-btn ghost" onClick={onUnread}>{t("inbox.markunread")}</button>}
          <span className="grow" />
          <button type="button" className="ph-btn ghost danger" onClick={onDelete}>{t("common.delete")}</button>
        </div>
      )}>
      <div className="ph-sheet-pad">
        {entry.body && (command ? <pre className="ph-code">{entry.body}</pre> : <div className="ph-prose">{entry.body}</div>)}
        <div className="ph-kv"><span>{t("ph.inbox.project")}</span><span>{project ?? t("notice.anywhere")}</span></div>
        <div className="ph-kv"><span>{t("ph.inbox.from")}</span><span>{noticeLine(entry, names)}</span></div>
        <div className="ph-kv"><span>{t("ph.inbox.when")}</span><span>{absTime(entry.updated_at)}{entry.count > 1 ? ` · ×${entry.count}` : ""}</span></div>
        {resolution && <div className="ph-kv"><span>{t("ph.inbox.outcome")}</span><span>{answered?.conflict ? t("ph.inbox.elsewhere", { outcome: resolutionLabel(resolution) }) : resolutionLabel(resolution)}</span></div>}
      </div>
    </BottomSheet>
  );
}

/** A change waiting for a decision: read it, approve it, or reject it with the reason the agent reads. */
function ProposalSheet({ p, toast, onClose, onDone }: { p: Proposal; toast: (text: string) => void; onClose: () => void; onDone: () => void }) {
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  async function decide(decision: "approve" | "reject") {
    setBusy(true);
    try {
      const r = await api.post<{ result: string }>(`/api/proposals/${p.id}/decide`, { decision, reason });
      toast(r.result);
      onDone();
    } catch (e) {
      toast(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <BottomSheet full onClose={onClose} className="ph-proposal" label={p.title}
      title={<span className="ph-sheet-kt-w"><span className="ph-sheet-kt-t">{t("ph.inbox.proposal")}</span><span className="ph-sheet-kt-m">{[p.pr_number ? `PR #${p.pr_number}` : "", relTime(p.created_at)].filter(Boolean).join(" · ")}</span></span>}
      footer={
        <div className="ph-foot-row">
          <button type="button" className="ph-btn ghost" onClick={() => navigate(pathFor("changes", p.id))}>{t("inbox.diff")}</button>
          <span className="grow" />
          <button type="button" className="ph-btn danger" disabled={busy} onClick={() => void decide("reject")}>{t("inbox.reject.do")}</button>
          <button type="button" className="ph-btn primary" disabled={busy} onClick={() => void decide("approve")}>{t("inbox.approve")}</button>
        </div>
      }>
      <div className="ph-sheet-pad">
        <div className="ph-proposal-t">{p.title}</div>
        <div className="ph-tags"><span className="ph-tag mono">{p.branch}</span>{p.repo && <span className="ph-tag">{p.repo}</span>}</div>
        {p.summary && <div className="ph-prose quiet">{p.summary}</div>}
        <label className="ph-label" htmlFor={`reason-${p.id}`}>{t("ph.inbox.reason")}</label>
        <textarea id={`reason-${p.id}`} className="ph-field area" rows={3} value={reason} onChange={(e) => setReason(e.target.value)} placeholder={t("inbox.reason.placeholder")} />
      </div>
    </BottomSheet>
  );
}

/** Several quick requests allowed at once, each once, in its own project. */
function AllowSheet({ entries, names, toast, onClose, onDone }: { entries: Notification[]; names: Map<string, string>; toast: (text: string) => void; onClose: () => void; onDone: (results: [Notification, ActResult][]) => void }) {
  const [busy, setBusy] = useState(false);
  async function allow() {
    setBusy(true);
    const results: [Notification, ActResult][] = [];
    let failed = 0;
    for (const entry of entries) {
      try {
        results.push([entry, await act(entry.id, "allow")]);
      } catch {
        failed += 1;
      }
    }
    afterChange();
    const elsewhere = results.filter(([, r]) => r.conflict).length;
    toast([plural("ph.inbox.allowed", results.length - elsewhere), elsewhere ? plural("ph.inbox.allowed.elsewhere", elsewhere) : "", failed ? plural("ph.inbox.allowed.failed", failed) : ""].filter(Boolean).join(" · "));
    setBusy(false);
    onDone(results);
  }
  return (
    <BottomSheet onClose={onClose} className="ph-allow" label={plural("ph.inbox.allow.title", entries.length)}
      title={<span className="ph-sheet-kt-w"><span className="ph-sheet-kt-t">{plural("ph.inbox.allow.title", entries.length)}</span><span className="ph-sheet-kt-m">{t("ph.inbox.allow.sub")}</span></span>}
      footer={<div className="ph-foot-row even"><button type="button" className="ph-btn" onClick={onClose}>{t("common.cancel")}</button><button type="button" className="ph-btn warn" disabled={busy} onClick={() => void allow()}>{t("ph.inbox.allow.do", { n: entries.length })}</button></div>}>
      <div className="ph-sheet-pad">
        <pre className="ph-code">{entries.map((e) => `${firstLine(e.body) || e.title}\n  ${e.project_id ? names.get(e.project_id) ?? "" : t("notice.anywhere")}`).join("\n")}</pre>
        <Banner tone="info" icon="alert">{t("ph.inbox.allow.note")}</Banner>
      </div>
    </BottomSheet>
  );
}
