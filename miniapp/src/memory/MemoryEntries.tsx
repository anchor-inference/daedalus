// The Memory screen as claude.ai draws it: the entries grouped (you, projects, topics, recent work),
// each opening onto its bullets with where every one was learned; a field at the bottom that files a
// note (on the list) or changes the entry in the operator's words (on an entry), the change shown as
// a diff before anything is applied. The record grid stays one segment away as "All facts".

import { useEffect, useMemo, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { api, type MemoryBullet, type MemoryEntry, type MemoryEntryDetail, type MemoryEntryListing, type MemoryOp, type MemoryProposal, type MemorySource } from "../api";
import { dayLabel } from "../format";
import { Icon } from "../icons";
import { plural, t } from "../i18n";
import { navigate, pathFor, sessionPath } from "../router";
import { invalidate, useQuery } from "../store";
import { confirmAsync, errorText } from "../ui";
import { OverflowMenu, Sheet, toast } from "../ui/dialogs";
import { PageHeader, Skeleton, go } from "../ui/index";
import { chosenOps, diffCounts, diffRows, filterEntries, groupEntries, sourceLabel, splitArchived, statusPill, updatedLabel } from "./model";
import { ImportSheet, downloadExport } from "./transfer";
import { type Undoable, undoToast } from "./undo";

export const ENTRIES_KEY = "/api/memory/entries";
const SETTINGS_PATH = "/app/settings/memory";

function refreshMemory(): void {
  invalidate("/api/memory");
}

/** The pill under the title: on, paused, not learning, today's budget used, or when memory last changed. */
export function StatusPill({ listing }: { listing: MemoryEntryListing | undefined }) {
  const pill = statusPill(listing?.status);
  if (!pill) return null;
  return (
    <a className={`memory-pill ${pill.tone}`} href={SETTINGS_PATH} onClick={(e) => go(e, SETTINGS_PATH)} data-status={pill.tone} title={t("memory.status.settings")}>
      <span className="memory-pill-dot" aria-hidden />
      <span>{pill.text}</span>
    </a>
  );
}

/** The field pinned under the list or the entry: one line that grows, sent with Enter. */
function MemoryComposer({ placeholder, label, busy, onSend }: { placeholder: string; label: string; busy: boolean; onSend: (text: string) => Promise<boolean> }) {
  const [text, setText] = useState("");
  const area = useRef<HTMLTextAreaElement>(null);
  useEffect(() => {
    const el = area.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 160)}px`;
  }, [text]);
  async function send() {
    const value = text.trim();
    if (!value || busy) return;
    if (await onSend(value)) setText("");
  }
  const key = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      void send();
    }
  };
  return (
    <div className="memory-composer" data-busy={busy || undefined}>
      <div className="memory-composer-pill">
        <textarea ref={area} rows={1} value={text} placeholder={placeholder} aria-label={label} onChange={(e) => setText(e.target.value)} onKeyDown={key} disabled={busy} />
        <button type="button" className="iconbtn primary memory-send" onClick={() => void send()} disabled={busy || !text.trim()} aria-label={label} title={label}>
          <Icon name={busy ? "dot" : "send"} />
        </button>
      </div>
    </div>
  );
}

function EntryRow({ entry, index }: { entry: MemoryEntry; index: number }) {
  const href = pathFor("memory", entry.id);
  const count = entry.count ?? 0;
  return (
    <a className="erow memory-entry-row" href={href} onClick={(e) => go(e, href)} data-entry={entry.id} style={{ ["--stagger" as string]: `${Math.min(index, 12) * 30}ms` }}>
      <span className={`memory-entry-tile ${entry.section}`} aria-hidden><Icon name={entry.section === "you" ? "user" : entry.section === "area" ? "folder" : "bulb"} size={16} /></span>
      <span className="memory-entry-main">
        <span className="memory-entry-title">
          {entry.title}
          {entry.sensitive.length > 0 && <span className="memory-sensitive" title={t("memory.entry.sensitive", { subjects: entry.sensitive.map((s) => t(`settings.memory.subject.${s}`)).join(", ") })}><Icon name="lock" size={12} /></span>}
        </span>
        <span className="memory-entry-summary">{entry.summary || (entry.project_name && entry.section === "area" ? t("memory.entry.project", { name: entry.project_name }) : plural("memory.entry.facts", count))}</span>
      </span>
      <span className="memory-entry-when">{updatedLabel(entry.updated_at)}</span>
    </a>
  );
}

/** The list of entries, the quick add under it, and the empty state when memory holds nothing. */
export function MemoryHome({ switcher, onHistory }: { switcher: ReactNode; onHistory: () => void }) {
  const { data, error, loading, refresh } = useQuery<MemoryEntryListing>(ENTRIES_KEY, { staleMs: 10000 });
  const [query, setQuery] = useState("");
  const [searching, setSearching] = useState(false);
  const [importing, setImporting] = useState(false);
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

  let index = 0;
  return (
    <>
      <PageHeader
        title={t("nav.memory")}
        subtitle={data ? `${plural("memory.entries.n", data.entries.length)} · ${plural("memory.facts.n", data.records)}` : undefined}
        actions={
          <>
            <button className={`iconbtn ${searching ? "on" : ""}`} onClick={() => { setSearching((v) => !v); if (searching) setQuery(""); }} title={t("common.search")} aria-label={t("common.search")} aria-pressed={searching}><Icon name="search" /></button>
            <button className="iconbtn" data-action="history" onClick={onHistory} title={t("memory.history")} aria-label={t("memory.history")}><Icon name="clock" /></button>
            <OverflowMenu
              label={t("memory.more")}
              className="memory-more"
              items={[
                { label: t("memory.import"), icon: "import", onSelect: () => setImporting(true) },
                { label: t("memory.export"), icon: "download", onSelect: () => void exportMemory() },
                "-",
                { label: t("memory.settings"), icon: "settings", onSelect: () => navigate(SETTINGS_PATH) },
              ]}
            />
          </>
        }
      >
        {searching && <input className="field search" autoFocus placeholder={t("memory.entries.search")} value={query} onChange={(e) => setQuery(e.target.value)} onKeyDown={(e) => { if (e.key === "Escape") { setQuery(""); setSearching(false); } }} aria-label={t("memory.entries.search")} />}
        <div className="memory-modebar">
          {switcher}
          <StatusPill listing={data} />
        </div>
      </PageHeader>
      <div className="screen narrow memory memory-entries" data-view="entries">
        {loading && !data && <Skeleton rows={6} />}
        {error && !data && <div className="empty"><b>{t("memory.error")}</b><div>{error}</div><button className="btn primary" onClick={refresh}>{t("common.retry")}</button></div>}
        {empty && (
          <div className="card memory-empty" data-empty>
            <div className="memory-empty-title">{t("memory.empty.entries")}</div>
            <div className="sub">{t("memory.empty.entries.sub")}</div>
            <div className="memory-empty-actions">
              <button className="btn" onClick={() => setImporting(true)}><Icon name="import" size={14} /> {t("memory.import.start")}</button>
              <a className="btn ghost" href={SETTINGS_PATH} onClick={(e) => go(e, SETTINGS_PATH)}><Icon name="settings" size={14} /> {t("memory.settings")}</a>
            </div>
          </div>
        )}
        {data && !empty && groups.length === 0 && <div className="empty"><b>{t("memory.nomatch")}</b></div>}
        {!empty && groups.map((group) => (
          <section key={group.id} className="memory-section" data-group={group.id}>
            <div className="memory-section-head">{t(`memory.group.${group.id}`)}</div>
            <div className="memory-entry-list">{group.entries.map((entry) => <EntryRow key={entry.id} entry={entry} index={index++} />)}</div>
          </section>
        ))}
        <div className="memory-composer-space" aria-hidden />
      </div>
      <MemoryComposer placeholder={t("memory.quick.placeholder")} label={t("memory.quick.label")} busy={busy} onSend={quickAdd} />
      {importing && <ImportSheet onClose={() => setImporting(false)} onDone={() => { setImporting(false); refreshMemory(); refresh(); }} />}
    </>
  );
}

async function exportMemory(): Promise<void> {
  try {
    await downloadExport();
  } catch (e) {
    toast(errorText(e));
  }
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
  const forget = () => act(() => api.delete<Undoable>(`/api/memory/${encodeURIComponent(bullet.id)}`), plural("memory.forgotten", 1));
  const operator = bullet.sources.some((s) => s.origin === "operator");
  return (
    <li className={`memory-bullet ${bullet.archived ? "archived" : ""}`} data-bullet={bullet.id} data-locked={bullet.locked || undefined}>
      {editing ? (
        <div className="memory-bullet-edit">
          <textarea className="field" rows={2} autoFocus value={text} onChange={(e) => setText(e.target.value)} aria-label={t("memory.bullet.edit")} onKeyDown={(e) => { if (e.key === "Escape") { setEditing(false); setText(bullet.text); } if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) void save(); }} />
          <div className="memory-bullet-edit-foot">
            <button className="btn small ghost" onClick={() => { setEditing(false); setText(bullet.text); }}>{t("common.cancel")}</button>
            <button className="btn small primary" data-action="save-bullet" disabled={busy || !text.trim() || text.trim() === bullet.text} onClick={() => void save()}>{t("common.save")}</button>
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
            <button className="iconbtn small" data-action="edit" onClick={() => setEditing(true)} title={t("memory.bullet.edit")} aria-label={t("memory.bullet.edit")}><Icon name="pen" size={14} /></button>
            <button className={`iconbtn small ${bullet.locked ? "on" : ""}`} data-action="lock" aria-pressed={!!bullet.locked} disabled={busy} onClick={() => void mark({ locked: !bullet.locked }, t(bullet.locked ? "memory.bullet.unlocked" : "memory.bullet.lockedtoast"))} title={t(bullet.locked ? "memory.bullet.unlock" : "memory.bullet.lock")} aria-label={t(bullet.locked ? "memory.bullet.unlock" : "memory.bullet.lock")}><Icon name="lock" size={14} /></button>
            <button className="iconbtn small" data-action="archive" disabled={busy} onClick={() => void mark({ archived: !bullet.archived }, t(bullet.archived ? "memory.bullet.unarchived" : "memory.bullet.archived"))} title={t(bullet.archived ? "memory.bullet.unarchive" : "memory.bullet.archive")} aria-label={t(bullet.archived ? "memory.bullet.unarchive" : "memory.bullet.archive")}><Icon name="archive" size={14} /></button>
            <button className="iconbtn small danger" data-action="forget" disabled={busy} onClick={() => void forget()} title={t("memory.forget")} aria-label={t("memory.forget")}><Icon name="trash" size={14} /></button>
          </span>
        </div>
      )}
    </li>
  );
}

/** One entry: its title and summary edited in place, its bullets with their sources, the older ones
 *  folded, and the field that changes it in the operator's words. */
export function MemoryEntryView({ entryId, onHistory }: { entryId: string; onHistory: () => void }) {
  const key = `${ENTRIES_KEY}/${encodeURIComponent(entryId)}`;
  const { data, error, loading, refresh } = useQuery<MemoryEntryDetail>(key);
  const [renaming, setRenaming] = useState(false);
  const [title, setTitle] = useState("");
  const [summary, setSummary] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [added, setAdded] = useState("");
  const [busy, setBusy] = useState(false);
  const [proposal, setProposal] = useState<{ instruction: string; proposal: MemoryProposal } | null>(null);
  const entry = data?.entry;
  const { live, older } = useMemo(() => splitArchived(data?.records ?? []), [data]);
  const changed = () => { refresh(); refreshMemory(); };

  async function patchEntry(patch: { title?: string; summary?: string }) {
    if (!entry) return;
    try {
      await api.patch(`${ENTRIES_KEY}/${encodeURIComponent(entry.id)}`, { version: entry.version, ...patch });
      toast(t("memory.entry.saved"));
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
    if (!(await confirmAsync(t(you ? "memory.entry.clear.title" : "memory.entry.delete.title", { title: entry.title }), { body: plural("memory.entry.delete.body", data?.records.length ?? 0), action: t(you ? "memory.entry.clear" : "memory.entry.delete") }))) return;
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

  return (
    <>
      <PageHeader
        back={pathFor("memory")}
        title={entry ? (renaming ? (
          <input className="field memory-title-edit" autoFocus value={title} aria-label={t("memory.entry.rename")} onChange={(e) => setTitle(e.target.value)} onBlur={() => { setRenaming(false); if (title.trim() && title.trim() !== entry.title) void patchEntry({ title: title.trim() }); }} onKeyDown={(e) => { if (e.key === "Enter") (e.target as HTMLInputElement).blur(); if (e.key === "Escape") { setTitle(entry.title); setRenaming(false); } }} />
        ) : <span className="memory-entry-heading" data-entry-title>{entry.title}</span>) : t("nav.memory")}
        subtitle={entry ? updatedLabel(entry.updated_at) : undefined}
        actions={entry && (
          <>
            <button className="iconbtn" data-action="history" onClick={onHistory} title={t("memory.history")} aria-label={t("memory.history")}><Icon name="clock" /></button>
            <OverflowMenu
              label={t("memory.more")}
              className="memory-entry-more"
              items={[
                { label: t("memory.entry.rename"), icon: "pen", onSelect: () => { setTitle(entry.title); setRenaming(true); } },
                { label: t("memory.bullet.add"), icon: "plus", onSelect: () => setAdding(true) },
                "-",
                { label: t(entry.section === "you" ? "memory.entry.clear" : "memory.entry.delete"), icon: "trash", danger: true, onSelect: () => void remove() },
              ]}
            />
          </>
        )}
      />
      <div className="screen narrow memory memory-entry" data-view="entry" data-entry={entryId}>
        {loading && !data && <Skeleton rows={5} />}
        {error && !data && <div className="empty"><b>{t("memory.entry.gone")}</b><div>{error}</div><a className="btn" href={pathFor("memory")} onClick={(e) => go(e, pathFor("memory"))}>{t("memory.entry.back")}</a></div>}
        {entry && (
          <>
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
                    <button className="btn small ghost" onClick={() => setSummary(null)}>{t("common.cancel")}</button>
                    <button className="btn small primary" data-action="save-summary" onClick={() => { void patchEntry({ summary }); setSummary(null); }}>{t("common.save")}</button>
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
                <button className="btn small ghost" data-action="add-bullet" onClick={() => setAdding(true)}><Icon name="plus" size={14} /> {t("memory.bullet.add")}</button>
              </div>
              {adding && (
                <div className="memory-bullet-edit memory-bullet-new">
                  <textarea className="field" rows={2} autoFocus value={added} placeholder={t("memory.bullet.add.placeholder")} aria-label={t("memory.bullet.add")} onChange={(e) => setAdded(e.target.value)} onKeyDown={(e) => { if (e.key === "Escape") { setAdding(false); setAdded(""); } if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); void addBullet(); } }} />
                  <div className="memory-bullet-edit-foot">
                    <button className="btn small ghost" onClick={() => { setAdding(false); setAdded(""); }}>{t("common.cancel")}</button>
                    <button className="btn small primary" data-action="save-new-bullet" disabled={busy || !added.trim()} onClick={() => void addBullet()}>{t("memory.bullet.add.save")}</button>
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
        <div className="memory-composer-space" aria-hidden />
      </div>
      {entry && <MemoryComposer placeholder={t("memory.instruct.placeholder")} label={t("memory.instruct.label")} busy={busy} onSend={instruct} />}
      {entry && proposal && data && <DiffSheet entry={entry} records={data.records} instruction={proposal.instruction} proposal={proposal.proposal} onClose={() => setProposal(null)} onApplied={() => { setProposal(null); changed(); }} />}
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
