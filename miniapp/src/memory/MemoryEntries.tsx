// The Memory screen's entries: the list of sections grouped as you, projects, topics and recent work,
// and the section a row opens, beside the list in a pane (or as a page when the window is too narrow
// for both): its summary, its facts with where each was learned, and a field that changes it in the
// operator's words, the change shown as a diff before anything is applied. The record grid stays one
// segment away as "All facts".

import { useEffect, useMemo, useState } from "react";
import { api, type MemoryBullet, type MemoryEntry, type MemoryEntryDetail, type MemoryEntryListing, type MemoryOp, type MemoryProposal, type MemorySource } from "../api";
import { dayLabel } from "../format";
import { Icon } from "../icons";
import { plural, t } from "../i18n";
import { navigate, pathFor, sessionPath, useRoute } from "../router";
import { invalidate, useQuery } from "../store";
import { errorText } from "../ui";
import { Page, PageHeader } from "../shell";
import { Skeleton, go } from "../ui/index";
import { Button, IconButton, OverflowMenu, Sheet, confirmDialog, toast, type MenuItem } from "../ui/primitives";
import { MemoryComposer, MemoryHead, MemoryPane, useSplit } from "./frame";
import { chosenOps, diffCounts, diffRows, filterEntries, groupEntries, sourceLabel, splitArchived, statusPill, updatedLabel } from "./model";
import { ReviewBanner, ReviewSheet } from "./review";
import { type Undoable, undoToast } from "./undo";

export const ENTRIES_KEY = "/api/memory/entries";
const SETTINGS_PATH = "/app/settings/memory";

function refreshMemory(): void {
  invalidate("/api/memory");
}

/** One quiet row above the list: whether memory learns from chats, paused, over today's budget, or
 *  when it last changed by itself. It opens Settings, where those are set. */
export function StatusRow({ listing }: { listing: MemoryEntryListing | undefined }) {
  const pill = statusPill(listing?.status);
  if (!pill) return null;
  return (
    <a className={`memory-pill ${pill.tone}`} href={SETTINGS_PATH} onClick={(e) => go(e, SETTINGS_PATH)} data-status={pill.tone} title={t("memory.status.settings")}>
      <span className="memory-pill-dot" aria-hidden />
      <span className="memory-pill-text">{pill.text}</span>
      <Icon name="forward" size={14} />
    </a>
  );
}

function EntryRow({ entry, open }: { entry: MemoryEntry; open: boolean }) {
  const href = pathFor("memory", entry.id);
  const count = entry.count ?? 0;
  const waiting = entry.suggestions ?? 0;
  return (
    <a className={`memory-entry-row ${open ? "on" : ""}`} href={href} onClick={(e) => go(e, href)} data-entry={entry.id} aria-current={open || undefined}>
      <span className={`memory-entry-tile ${entry.section}`} aria-hidden><Icon name={entry.section === "you" ? "user" : entry.section === "area" ? "folder" : "bulb"} size={16} /></span>
      <span className="memory-entry-main">
        <span className="memory-entry-title">
          {entry.title}
          {entry.sensitive.length > 0 && <span className="memory-sensitive" title={t("memory.entry.sensitive", { subjects: entry.sensitive.map((s) => t(`settings.memory.subject.${s}`)).join(", ") })}><Icon name="lock" size={12} /></span>}
        </span>
        <span className="memory-entry-summary">{entry.summary || (entry.project_name && entry.section === "area" ? t("memory.entry.project", { name: entry.project_name }) : "")}</span>
      </span>
      {waiting > 0 && <span className="memory-badge" data-suggestions={waiting} title={plural("memory.review.badge", waiting)} aria-label={plural("memory.review.badge", waiting)}>{waiting}</span>}
      <span className="memory-entry-stats">
        <span className="memory-entry-count">{plural("memory.entry.facts", count)}</span>
        <span className="memory-entry-when">{updatedLabel(entry.updated_at)}</span>
      </span>
    </a>
  );
}

type EntriesProps = {
  query: string;
  onQuery: (query: string) => void;
  onHistory: () => void;
  /** The header's commands; All facts adds Select, so the screen builds the list. */
  menu: MenuItem[];
  onImport: () => void;
};

/** The list of entries with the field under it that files a note, the entry open beside it, and the
 *  empty state when memory holds nothing. */
export function MemoryEntries({ query, onQuery, onHistory, menu, onImport }: EntriesProps) {
  const route = useRoute();
  const split = useSplit();
  const entryId = route.detail ?? null;
  const { data, error, loading, refresh } = useQuery<MemoryEntryListing>(ENTRIES_KEY, { staleMs: 10000 });
  const [reviewing, setReviewing] = useState(false);
  const [busy, setBusy] = useState(false);
  const entries = useMemo(() => filterEntries(data?.entries ?? [], query), [data, query]);
  const groups = useMemo(() => groupEntries(entries), [entries]);
  const empty = !!data && data.records === 0 && data.entries.every((e) => (e.count ?? 0) === 0);

  async function quickAdd(text: string): Promise<boolean> {
    setBusy(true);
    try {
      const res = await api.post<{ decision: string; entry: MemoryEntry | null } & Undoable>("/api/memory/quick", { text });
      const where = res.entry?.title ?? "";
      const said = res.decision === "created" ? t("memory.quick.saved", { entry: where }) : res.decision === "merged" ? t("memory.quick.merged", { entry: where }) : t("memory.quick.held", { entry: where });
      undoToast(said, res.history_id, () => { refreshMemory(); refresh(); });
      refreshMemory();
      refresh();
      return true;
    } catch (e) {
      toast(errorText(e));
      return false;
    } finally {
      setBusy(false);
    }
  }

  // A window too narrow for the pane gives the entry the whole page, with the way back in its header.
  if (entryId && !split) return <EntryView entryId={entryId} as="page" onHistory={onHistory} />;

  const subtitle = data ? `${plural("memory.entries.n", data.entries.length)} · ${plural("memory.facts.n", data.records)}` : undefined;
  return (
    <>
      <MemoryHead mode="entries" subtitle={subtitle} query={query} onQuery={onQuery} onHistory={onHistory} menu={menu} />
      <div className="memory-split" data-view="entries">
        <section className="memory-list">
          <div className="memory-scroll">
            <Page width="list" className="memory memory-entries">
              {loading && !data && <Skeleton rows={6} />}
              {error && !data && <div className="empty"><b>{t("memory.error")}</b><div>{error}</div><button className="btn primary" onClick={refresh}>{t("common.retry")}</button></div>}
              {!empty && (data?.status || (data?.suggestions ?? 0) > 0) && (
                <div className="memory-notes">
                  <StatusRow listing={data} />
                  <ReviewBanner count={data?.suggestions ?? 0} onOpen={() => setReviewing(true)} />
                </div>
              )}
              {empty && (
                <div className="empty memory-empty" data-empty>
                  <Icon name="brain" size={20} />
                  <b>{t("memory.empty.entries")}</b>
                  <div>{t("memory.empty.entries.sub")}</div>
                  <div className="memory-empty-actions">
                    <Button icon="import" onClick={onImport}>{t("memory.import.start")}</Button>
                    <Button variant="quiet" icon="settings" onClick={() => navigate(SETTINGS_PATH)}>{t("memory.settings")}</Button>
                  </div>
                </div>
              )}
              {data && !empty && groups.length === 0 && <div className="empty"><b>{t("memory.nomatch")}</b></div>}
              {!empty && groups.map((group) => (
                <section key={group.id} className="memory-section" data-group={group.id}>
                  <div className="memory-section-head">{t(`memory.group.${group.id}`)}</div>
                  <div className="memory-entry-list">{group.entries.map((entry) => <EntryRow key={entry.id} entry={entry} open={entry.id === entryId} />)}</div>
                </section>
              ))}
            </Page>
          </div>
          <MemoryComposer placeholder={t("memory.quick.placeholder")} label={t("memory.quick.label")} busy={busy} onSend={quickAdd} />
        </section>
        {entryId && <EntryView key={entryId} entryId={entryId} as="pane" onHistory={onHistory} />}
      </div>
      {reviewing && <ReviewSheet onClose={() => setReviewing(false)} onChanged={refresh} />}
    </>
  );
}

/** A source chip: the chat a bullet was learned in, opened at the place it was said when that is known. */
function SourceChip({ source }: { source: MemorySource }) {
  const label = sourceLabel(source);
  const when = dayLabel(source.at);
  const href = source.session_id && source.session_title ? `${sessionPath(source.session_id)}${source.from_seq ? `#m${source.from_seq}` : ""}` : null;
  const body = <><Icon name={source.session_id ? "compose" : source.origin === "import" ? "import" : "user"} size={12} /> <span className="memory-source-t">{label}</span>{source.at && <span className="faint"> · {when}</span>}</>;
  return href ? <a className="memory-source" href={href} onClick={(e) => go(e, href)} data-source={source.session_id}>{body}</a> : <span className="memory-source" data-origin={source.origin}>{body}</span>;
}


function BulletRow({ bullet, onChanged }: { bullet: MemoryBullet; onChanged: () => void }) {
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState(bullet.text);
  const [busy, setBusy] = useState(false);
  useEffect(() => setText(bullet.text), [bullet.text]);
  async function act(run: () => Promise<Undoable>, said: string) {
    setBusy(true);
    try {
      const res = await run();
      undoToast(said, res.history_id, onChanged);
      onChanged();
    } catch (e) {
      toast(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  const save = () => act(async () => {
    const res = await api.patch<Undoable>(`/api/memory/${encodeURIComponent(bullet.id)}`, { text: text.trim(), version: bullet.version });
    setEditing(false);
    return res;
  }, t("memory.updated"));
  const mark = (patch: { locked?: boolean; archived?: boolean }, said: string) => act(() => api.post<Undoable>(`/api/memory/${encodeURIComponent(bullet.id)}/mark`, patch), said);
  // One fact forgotten offers Undo rather than a question: the history brings it back either way, and a
  // question on every fact of a long section would be the louder of the two.
  const forget = () => act(() => api.delete<Undoable>(`/api/memory/${encodeURIComponent(bullet.id)}`), plural("memory.forgotten", 1));
  const operator = bullet.sources.some((s) => s.origin === "operator");
  return (
    <li className={`memory-bullet ${bullet.archived ? "archived" : ""}`} data-bullet={bullet.id} data-locked={bullet.locked || undefined}>
      {editing ? (
        <div className="memory-bullet-edit">
          <textarea className="field" rows={2} autoFocus value={text} onChange={(e) => setText(e.target.value)} aria-label={t("memory.bullet.edit")} onKeyDown={(e) => { if (e.key === "Escape") { setEditing(false); setText(bullet.text); } if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) void save(); }} />
          <div className="memory-bullet-edit-foot">
            <Button variant="quiet" size="sm" onClick={() => { setEditing(false); setText(bullet.text); }}>{t("common.cancel")}</Button>
            <Button variant="primary" size="sm" data-action="save-bullet" disabled={busy || !text.trim() || text.trim() === bullet.text} onClick={() => void save()}>{t("common.save")}</Button>
          </div>
        </div>
      ) : (
        <div className="memory-bullet-text" onDoubleClick={() => setEditing(true)}>{bullet.text}</div>
      )}
      {!editing && (
        <div className="memory-bullet-meta">
          {bullet.sources.slice(0, 2).map((source, i) => <SourceChip key={`${source.origin}:${source.session_id}:${i}`} source={source} />)}
          {bullet.sources.length > 2 && <span className="faint">+{bullet.sources.length - 2}</span>}
          {operator && <span className="memory-mark">{t("memory.bullet.yours")}</span>}
          {bullet.locked && <span className="memory-mark locked" title={t("memory.bullet.locked.hint")}><Icon name="lock" size={12} /> {t("memory.bullet.locked")}</span>}
          <span className="grow" />
          <span className="memory-bullet-actions">
            <IconButton size={28} icon="pen" label={t("memory.bullet.edit")} onClick={() => setEditing(true)} />
            <IconButton size={28} icon="lock" label={t(bullet.locked ? "memory.bullet.unlock" : "memory.bullet.lock")} pressed={!!bullet.locked} disabled={busy} onClick={() => void mark({ locked: !bullet.locked }, t(bullet.locked ? "memory.bullet.unlocked" : "memory.bullet.lockedtoast"))} />
            <IconButton size={28} icon="archive" label={t(bullet.archived ? "memory.bullet.unarchive" : "memory.bullet.archive")} disabled={busy} onClick={() => void mark({ archived: !bullet.archived }, t(bullet.archived ? "memory.bullet.unarchived" : "memory.bullet.archived"))} />
            <IconButton size={28} icon="trash" label={t("memory.forget")} className="danger" disabled={busy} onClick={() => void forget()} />
          </span>
        </div>
      )}
    </li>
  );
}

/** One entry, in the pane beside the list or as a page in a window too narrow for both: its title and
 *  summary edited in place, its facts with their sources, the older ones folded, and the field that
 *  changes it in the operator's words. */
function EntryView({ entryId, as, onHistory }: { entryId: string; as: "pane" | "page"; onHistory: () => void }) {
  const key = `${ENTRIES_KEY}/${encodeURIComponent(entryId)}`;
  const { data, error, loading, refresh } = useQuery<MemoryEntryDetail>(key);
  const [renaming, setRenaming] = useState(false);
  const [title, setTitle] = useState("");
  const [summary, setSummary] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [added, setAdded] = useState("");
  const [busy, setBusy] = useState(false);
  const [proposal, setProposal] = useState<{ instruction: string; proposal: MemoryProposal } | null>(null);
  const [reviewing, setReviewing] = useState(false);
  const listing = useQuery<MemoryEntryListing>(ENTRIES_KEY, { staleMs: 10000 });
  const waiting = listing.data?.entries.find((e) => e.id === entryId)?.suggestions ?? 0;
  const entry = data?.entry;
  const { live, older } = useMemo(() => splitArchived(data?.records ?? []), [data]);
  const changed = () => { refresh(); refreshMemory(); listing.refresh(); };
  const close = () => navigate(pathFor("memory"));

  async function patchEntry(patch: { title?: string; summary?: string }) {
    if (!entry) return;
    try {
      await api.patch(`${ENTRIES_KEY}/${encodeURIComponent(entry.id)}`, { version: entry.version, ...patch });
      changed();
    } catch (e) {
      toast(errorText(e));
      refresh();
    }
  }

  async function addBullet() {
    if (!entry || !added.trim()) return;
    setBusy(true);
    try {
      const res = await api.post<Undoable>(`${ENTRIES_KEY}/${encodeURIComponent(entry.id)}/bullets`, { text: added.trim() });
      setAdded("");
      setAdding(false);
      undoToast(t("memory.bullet.added"), res.history_id, changed);
      changed();
    } catch (e) {
      toast(errorText(e));
    } finally {
      setBusy(false);
    }
  }

  async function instruct(instruction: string): Promise<boolean> {
    if (!entry) return false;
    setBusy(true);
    try {
      const res = await api.post<MemoryProposal>(`${ENTRIES_KEY}/${encodeURIComponent(entry.id)}/instruct`, { instruction, version: entry.version });
      setProposal({ instruction, proposal: res });
      return true;
    } catch (e) {
      toast(errorText(e));
      return false;
    } finally {
      setBusy(false);
    }
  }

  async function remove() {
    if (!entry) return;
    const you = entry.section === "you";
    if (!(await confirmDialog({ title: t(you ? "memory.entry.clear.title" : "memory.entry.delete.title", { title: entry.title }), body: plural("memory.entry.delete.body", data?.records.length ?? 0), action: t(you ? "memory.entry.clear" : "memory.entry.delete"), danger: true }))) return;
    try {
      const res = await api.delete<Undoable>(`${ENTRIES_KEY}/${encodeURIComponent(entry.id)}?version=${entry.version}`);
      undoToast(t(you ? "memory.entry.cleared" : "memory.entry.deleted", { title: entry.title }), res.history_id, refreshMemory);
      refreshMemory();
      if (!you) navigate(pathFor("memory"), { replace: true });
      else refresh();
    } catch (e) {
      toast(errorText(e));
    }
  }

  const menu: MenuItem[] = entry ? [
    { label: t("memory.entry.rename"), icon: "pen", onSelect: () => { setTitle(entry.title); setRenaming(true); } },
    { label: t("memory.bullet.add"), icon: "plus", onSelect: () => setAdding(true) },
    "-",
    { label: t(entry.section === "you" ? "memory.entry.clear" : "memory.entry.delete"), icon: "trash", danger: true, onSelect: () => void remove() },
  ] : [];

  const heading = entry ? (renaming ? (
    <input className="field memory-title-edit" autoFocus value={title} aria-label={t("memory.entry.rename")} onChange={(e) => setTitle(e.target.value)} onBlur={() => { setRenaming(false); if (title.trim() && title.trim() !== entry.title) void patchEntry({ title: title.trim() }); }} onKeyDown={(e) => { if (e.key === "Enter") (e.target as HTMLInputElement).blur(); if (e.key === "Escape") { setTitle(entry.title); setRenaming(false); } }} />
  ) : <span className="memory-entry-heading" data-entry-title>{entry.title}</span>) : t("nav.memory");

  const body = (
    <div className="memory-entry" data-view="entry" data-entry={entryId}>
      {loading && !data && <Skeleton rows={5} />}
      {error && !data && <div className="empty"><b>{t("memory.entry.gone")}</b><div>{error}</div><a className="btn" href={pathFor("memory")} onClick={(e) => go(e, pathFor("memory"))}>{t("memory.entry.back")}</a></div>}
      {entry && (
        <>
          {as === "pane" && <h2 className="memory-pane-title">{heading}</h2>}
          {waiting > 0 && <div className="memory-notes"><ReviewBanner count={waiting} onOpen={() => setReviewing(true)} /></div>}
          {entry.section === "area" && entry.project_id && entry.project_name && (
            <div className="memory-entry-project"><Icon name="folder" size={14} /> {t("memory.entry.project", { name: entry.project_name })}</div>
          )}
          <div className="memory-block">
            <div className="memory-block-head">{t("memory.entry.summary")}</div>
            {summary !== null ? (
              <div className="memory-summary-edit">
                <textarea className="field" rows={2} autoFocus maxLength={150} value={summary} aria-label={t("memory.entry.summary")} onChange={(e) => setSummary(e.target.value)} />
                <div className="memory-bullet-edit-foot">
                  <span className="faint">{summary.length}/150</span>
                  <span className="grow" />
                  <Button variant="quiet" size="sm" onClick={() => setSummary(null)}>{t("common.cancel")}</Button>
                  <Button variant="primary" size="sm" data-action="save-summary" onClick={() => { void patchEntry({ summary }); setSummary(null); }}>{t("common.save")}</Button>
                </div>
              </div>
            ) : (
              <button className="memory-summary" data-action="edit-summary" onClick={() => setSummary(entry.summary)}>
                {entry.summary || <span className="faint">{t("memory.entry.summary.none")}</span>}
                <Icon name="pen" size={12} />
              </button>
            )}
          </div>
          <div className="memory-block">
            <div className="memory-block-head">
              <span>{t("memory.entry.details")}</span>
              <span className="faint">{live.length}</span>
              <span className="grow" />
              <Button variant="quiet" size="sm" icon="plus" data-action="add-bullet" onClick={() => setAdding(true)}>{t("memory.bullet.add")}</Button>
            </div>
            {adding && (
              <div className="memory-bullet-edit memory-bullet-new">
                <textarea className="field" rows={2} autoFocus value={added} placeholder={t("memory.bullet.add.placeholder")} aria-label={t("memory.bullet.add")} onChange={(e) => setAdded(e.target.value)} onKeyDown={(e) => { if (e.key === "Escape") { setAdding(false); setAdded(""); } if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); void addBullet(); } }} />
                <div className="memory-bullet-edit-foot">
                  <Button variant="quiet" size="sm" onClick={() => { setAdding(false); setAdded(""); }}>{t("common.cancel")}</Button>
                  <Button variant="primary" size="sm" data-action="save-new-bullet" disabled={busy || !added.trim()} onClick={() => void addBullet()}>{t("memory.bullet.add.save")}</Button>
                </div>
              </div>
            )}
            {live.length === 0 && !adding && <div className="sub memory-none">{t("memory.entry.empty")}</div>}
            <ul className="memory-bullets">{live.map((bullet) => <BulletRow key={bullet.id} bullet={bullet} onChanged={changed} />)}</ul>
            {older.length > 0 && (
              <details className="memory-older" data-older>
                <summary>{t("memory.entry.older", { n: older.length })}</summary>
                <ul className="memory-bullets">{older.map((bullet) => <BulletRow key={bullet.id} bullet={bullet} onChanged={changed} />)}</ul>
              </details>
            )}
          </div>
        </>
      )}
    </div>
  );
  const field = entry && <MemoryComposer inPane={as === "pane"} placeholder={t("memory.instruct.placeholder")} label={t("memory.instruct.label")} busy={busy} onSend={instruct} />;
  const sheets = (
    <>
      {reviewing && <ReviewSheet entryId={entryId} onClose={() => setReviewing(false)} onChanged={changed} />}
      {entry && proposal && data && <DiffSheet entry={entry} records={data.records} instruction={proposal.instruction} proposal={proposal.proposal} onClose={() => setProposal(null)} onApplied={() => { setProposal(null); changed(); }} />}
    </>
  );

  if (as === "pane") {
    return (
      <>
        <MemoryPane kind={t("memory.pane.entry")} meta={entry ? updatedLabel(entry.updated_at) : undefined} menu={menu} onClose={close} label={entry?.title ?? t("nav.memory")} foot={field}>{body}</MemoryPane>
        {sheets}
      </>
    );
  }
  return (
    <>
      <PageHeader
        back={pathFor("memory")}
        title={heading}
        subtitle={entry ? updatedLabel(entry.updated_at) : undefined}
        actions={entry && (
          <>
            <IconButton icon="clock" label={t("memory.history")} onClick={onHistory} />
            <OverflowMenu label={t("memory.more")} className="memory-entry-more" items={menu} />
          </>
        )}
      />
      <div className="memory-split">
        <section className="memory-list">
          <div className="memory-scroll"><Page width="list" className="memory">{body}</Page></div>
          {field}
        </section>
      </div>
      {sheets}
    </>
  );
}

/** What the model proposes for the entry, before anything changes: added bullets with an accent rule,
 *  removed ones struck through, changed ones with their old text; each change can be unticked. */
function DiffSheet({ entry, records, instruction, proposal, onClose, onApplied }: { entry: MemoryEntry; records: MemoryBullet[]; instruction: string; proposal: MemoryProposal; onClose: () => void; onApplied: () => void }) {
  const [skipped, setSkipped] = useState<Set<number>>(() => new Set(Object.keys(proposal.flags).map(Number)));
  const [busy, setBusy] = useState(false);
  const rows = useMemo(() => diffRows(records, proposal.ops, skipped), [records, proposal.ops, skipped]);
  const counts = diffCounts(rows);
  const ops: MemoryOp[] = chosenOps(proposal.ops, skipped);
  const nothing = ops.length === 0 && proposal.title === null && proposal.summary === null;
  const toggle = (i: number) => setSkipped((s) => { const n = new Set(s); if (n.has(i)) n.delete(i); else n.add(i); return n; });
  async function apply() {
    setBusy(true);
    try {
      const res = await api.post<Undoable>(`${ENTRIES_KEY}/${encodeURIComponent(entry.id)}/instruct`, { instruction, version: proposal.version, apply: true, ops, title: proposal.title, summary: proposal.summary });
      undoToast(t("memory.instruct.applied"), res.history_id, onApplied);
      onApplied();
    } catch (e) {
      toast(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Sheet title={t("memory.instruct.title")} onClose={onClose} className="memory-diff-sheet">
      <div className="memory-instruction"><Icon name="wand" size={14} /> <span>{instruction}</span></div>
      {proposal.note && <div className="memory-note">{proposal.note}</div>}
      {proposal.title !== null && <div className="memory-diff-field" data-diff="title"><span className="faint">{t("memory.instruct.newtitle")}</span> <s>{entry.title}</s> → <b>{proposal.title}</b></div>}
      {proposal.summary !== null && <div className="memory-diff-field" data-diff="summary"><span className="faint">{t("memory.instruct.newsummary")}</span> {proposal.summary}</div>}
      <div className="sub" data-diff-counts>{t("memory.instruct.counts", counts)}{proposal.dropped ? ` · ${plural("memory.instruct.dropped", proposal.dropped)}` : ""}</div>
      <ul className="memory-diff">
        {rows.map((row, i) => (
          <li key={`${row.kind}:${row.id ?? i}`} className={`memory-diff-row ${row.kind}`} data-diff-row={row.kind}>
            {row.op !== undefined ? <input type="checkbox" checked aria-label={t("memory.instruct.keep")} onChange={() => toggle(row.op!)} /> : <span className="memory-diff-gap" aria-hidden />}
            <span className="memory-diff-text">
              {row.kind === "changed" && <s className="memory-diff-before">{row.before}</s>}
              <span>{row.text}</span>
              {row.op !== undefined && proposal.flags[String(row.op)] && <span className="memory-flag bad">{t("memory.import.injection")}</span>}
            </span>
          </li>
        ))}
        {proposal.ops.map((op, i) => skipped.has(i) ? (
          <li key={`skipped-${i}`} className="memory-diff-row skipped" data-diff-row="skipped">
            <input type="checkbox" checked={false} aria-label={t("memory.instruct.keep")} onChange={() => toggle(i)} />
            <span className="memory-diff-text faint">{t(`memory.instruct.op.${op.op}`)}: {op.text ?? records.find((r) => r.id === op.id)?.text ?? ""}</span>
          </li>
        ) : null)}
      </ul>
      <div className="sheet-foot">
        <span className="grow" />
        <button className="btn ghost" onClick={onClose}>{t("memory.instruct.discard")}</button>
        <button className="btn primary" data-action="apply" disabled={busy || nothing} onClick={() => void apply()}>{t("memory.instruct.apply")}</button>
      </div>
    </Sheet>
  );
}
