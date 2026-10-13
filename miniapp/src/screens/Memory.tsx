import { useMemo, useState } from "react";
import { api, MEMORY_CATEGORIES, MemoryHistoryEntry, MemoryListing, MemoryRecord, MemorySuggestions } from "../api";
import { ChipRow, Segmented, Skeleton } from "../ui/index";
import { Sheet, deleteWithUndo } from "../ui/dialogs";
import { absTime, relTime } from "../format";
import { Icon } from "../icons";
import { PageHeader, screenTitle } from "../ui/index";
import { hold, prime, release, useQuery } from "../store";
import { confirmAsync, errorText } from "../ui";
import { plural, t } from "../i18n";

type View = "list" | "topics" | "categories";
const VIEWS: View[] = ["list", "topics", "categories"];
const VIEW_KEY = "daedalus.memory.view";

/** The categories the app has a word for; anything else (a record the agent filed oddly) shows as written. */
export const categoryWord = (category: string) => ((MEMORY_CATEGORIES as readonly string[]).includes(category) ? t(`memory.cat.${category}`) : category);

/** Topics as the host keeps them, from what was typed: lower case, hyphens for spaces, no `#`. */
export function parseTopics(text: string): string[] {
  const out: string[] = [];
  for (const part of text.split(/[,;\n]/)) {
    const topic = part.trim().toLowerCase().replace(/^#+/, "").replace(/\s+/g, "-");
    if (topic && !out.includes(topic)) out.push(topic);
  }
  return out.slice(0, 5);
}

/** Records grouped under each topic they carry (a record with two topics is under both), the
 *  biggest group first and the records without a topic last. */
export function byTopic(records: MemoryRecord[]): [string, MemoryRecord[]][] {
  const groups = new Map<string, MemoryRecord[]>();
  for (const r of records) for (const topic of r.topics.length ? r.topics : [""]) groups.set(topic, [...(groups.get(topic) ?? []), r]);
  return [...groups.entries()].sort((a, b) => (a[0] === "" ? 1 : b[0] === "" ? -1 : b[1].length - a[1].length || a[0].localeCompare(b[0])));
}

function storedView(): View {
  try {
    const v = localStorage.getItem(VIEW_KEY);
    return VIEWS.includes(v as View) ? (v as View) : "list";
  } catch {
    return "list";
  }
}

/** What the agent remembered — by category and topic — to read, correct, merge, add to and prune. */
export function MemoryScreen({ toast, onOpen }: { toast: (t: string) => void; onOpen: (id: string) => void }) {
  const key = "/api/memory";
  const { data, error, loading, refresh } = useQuery<MemoryListing>(key, { staleMs: 10000 });
  const [bucket, setBucket] = useState<string>("all");
  const [category, setCategory] = useState<string>("all");
  const [topic, setTopic] = useState<string | null>(null);
  const [view, setViewState] = useState<View>(storedView);
  const [query, setQuery] = useState("");
  const [searching, setSearching] = useState(false);
  const [selecting, setSelecting] = useState(false);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [editing, setEditing] = useState<MemoryRecord | null>(null);
  const [adding, setAdding] = useState(false);
  const [merging, setMerging] = useState<MemoryRecord[] | null>(null);
  const [renaming, setRenaming] = useState<string | null>(null);
  const [tidying, setTidying] = useState(() => new URLSearchParams(window.location.search).get("suggestions") === "1");
  const [history, setHistory] = useState(false);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());

  function setView(next: View) {
    setViewState(next);
    try {
      localStorage.setItem(VIEW_KEY, next);
    } catch {
      /* a webview without site data: the view lasts for this visit */
    }
  }

  const q = query.trim().toLowerCase();
  const all = data?.records ?? [];
  // What the scope, the search and the topic leave, before the category: the chips count this, so
  // each promises what a tap on it shows.
  const pool = useMemo(
    () => all.filter((r) => (bucket === "all" || `${r.scope}:${r.scope_key}` === bucket) && (!topic || r.topics.includes(topic)) && (!q || r.text.toLowerCase().includes(q) || r.topics.some((x) => x.includes(q)))),
    [all, bucket, q, topic],
  );
  const counts = useMemo(() => {
    const n = new Map<string, number>();
    for (const r of pool) n.set(r.category, (n.get(r.category) ?? 0) + 1);
    return n;
  }, [pool]);
  const categories = useMemo(() => [...MEMORY_CATEGORIES, ...[...new Set(all.map((r) => r.category))].filter((c) => !(MEMORY_CATEGORIES as readonly string[]).includes(c))], [all]);
  const records = useMemo(() => pool.filter((r) => category === "all" || r.category === category), [pool, category]);
  const groups: [string, MemoryRecord[]][] = useMemo(() => {
    if (view === "topics") return byTopic(records);
    if (view === "categories") return categories.map((c) => [c, records.filter((r) => r.category === c)] as [string, MemoryRecord[]]).filter(([, rs]) => rs.length > 0);
    return [["", records]];
  }, [view, records, categories]);
  const bucketLabel = (scope: string, k: string) => (scope === "global" ? t("memory.scope.global") : scope === "session" ? data?.sessions[k] ?? t("memory.scope.deleted", { id: k }) : `${scope}: ${k}`);

  function toggle(id: string) {
    setSelected((s) => {
      const n = new Set(s);
      if (n.has(id)) n.delete(id);
      else n.add(id);
      return n;
    });
  }

  function forget(ids: string[]) {
    if (!ids.length || !data) return;
    const before = data;
    hold(key);
    prime(key, { ...data, records: data.records.filter((r) => !ids.includes(r.id)) });
    setSelected(new Set());
    setEditing(null);
    deleteWithUndo(
      plural("memory.forgotten", ids.length),
      async () => {
        try {
          await api.post("/api/memory/delete", { ids });
        } finally {
          release(key);
          refresh();
        }
      },
      () => {
        release(key);
        prime(key, before);
      },
      (e) => toast(errorText(e)),
    );
  }

  async function forgetMany() {
    const ids = [...selected];
    if (!(await confirmAsync(t("memory.forget.many.title", { n: ids.length }), { body: t("memory.forget.body"), action: t("memory.forget") }))) return;
    forget(ids);
    setSelecting(false);
  }

  function card(r: MemoryRecord, group: string) {
    const long = r.text.length > 320 || r.text.split("\n").length > 5;
    const open = expanded.has(r.id);
    return (
      <div key={`${group}:${r.id}`} data-memory={r.id} className={`erow memory-card ${selecting ? "selecting" : ""} ${selected.has(r.id) ? "selected" : ""}`} role={selecting ? "checkbox" : "button"} aria-checked={selecting ? selected.has(r.id) : undefined} tabIndex={0} onClick={() => (selecting ? toggle(r.id) : setEditing(r))} onKeyDown={(e) => { if (e.target !== e.currentTarget) return; if (e.key === "Enter" || e.key === " ") { e.preventDefault(); selecting ? toggle(r.id) : setEditing(r); } }}>
        {selecting && <input type="checkbox" className="memory-check" checked={selected.has(r.id)} onChange={() => toggle(r.id)} onClick={(e) => e.stopPropagation()} aria-label={t("memory.select.one")} />}
        <div className="erow-main">
          <div className="erow-meta">
            <span className={`chip cat-${r.category}`}>{categoryWord(r.category)}</span>
            {r.scope === "global" ? <span>{t("memory.global")}</span> : r.scope === "session" ? (data?.sessions[r.scope_key] ? <button className="linkbtn" onClick={(e) => { e.stopPropagation(); onOpen(r.scope_key); }}>{data.sessions[r.scope_key]}</button> : <span>{t("memory.scope.deleted.short")}</span>) : <span>{r.scope}: {r.scope_key}</span>}
            {r.created_at && <span className="sep">·</span>}
            {r.created_at && <span className="faint" title={absTime(r.created_at)}>{relTime(r.created_at)}</span>}
          </div>
          <div className={`memory-text ${long && !open ? "clamp-4" : ""}`}>{r.text}</div>
          {long && (
            <button className="linkbtn more" onClick={(e) => { e.stopPropagation(); setExpanded((s) => { const n = new Set(s); if (n.has(r.id)) n.delete(r.id); else n.add(r.id); return n; }); }}>
              {t(open ? "common.less" : "common.more")}
            </button>
          )}
          {r.topics.length > 0 && (
            <div className="memory-topics">
              {r.topics.map((x) => (
                <button key={x} className={`topic-tag ${topic === x ? "on" : ""}`} onClick={(e) => { e.stopPropagation(); setTopic(topic === x ? null : x); }} aria-pressed={topic === x}>#{x}</button>
              ))}
            </div>
          )}
        </div>
      </div>
    );
  }

  function groupHead(name: string, size: number) {
    if (view === "list") return null;
    const label = view === "topics" ? (name ? `#${name}` : t("memory.topic.none")) : categoryWord(name);
    return (
      <div className="memory-group-head">
        {view === "topics" && name ? <button className="linkbtn" onClick={() => setTopic(topic === name ? null : name)}>{label}</button> : <span className={view === "categories" ? `chip cat-${name}` : ""}>{label}</span>}
        <span className="faint">{size}</span>
        <span className="grow" />
        {view === "topics" && name && <button className="iconbtn small" onClick={() => setRenaming(name)} title={t("memory.topic.rename")} aria-label={t("memory.topic.rename")}><Icon name="pen" size={14} /></button>}
      </div>
    );
  }

  const sessions = Object.entries(data?.sessions ?? {});
  const chosen = all.filter((r) => selected.has(r.id));
  return (
    <>
      <PageHeader
        title={screenTitle("memory")}
        subtitle={data ? (records.length === all.length ? plural("memory.count", all.length) : t("memory.count.of", { shown: records.length, total: all.length })) : undefined}
        actions={
          <>
            <button className={`iconbtn ${searching ? "on" : ""}`} onClick={() => { setSearching((v) => !v); if (searching) setQuery(""); }} title={t("common.search")} aria-label={t("common.search")} aria-pressed={searching}><Icon name="search" /></button>
            <button className="iconbtn" data-action="history" onClick={() => setHistory(true)} title={t("memory.history")} aria-label={t("memory.history")}><Icon name="clock" /></button>
            <button className={`iconbtn ${selecting ? "on" : ""}`} data-action="select" onClick={() => { setSelecting((v) => !v); setSelected(new Set()); }} title={t(selecting ? "memory.select.done" : "memory.select")} aria-label={t(selecting ? "memory.select.done" : "memory.select")} aria-pressed={selecting}><Icon name="check" /></button>
            <button className="iconbtn primary" onClick={() => setAdding(true)} title={t("memory.add")} aria-label={t("memory.add")}><Icon name="plus" /></button>
          </>
        }
      >
        {searching && <input className="field search" autoFocus placeholder={t("memory.search")} value={query} onChange={(e) => setQuery(e.target.value)} onKeyDown={(e) => { if (e.key === "Escape") { setQuery(""); setSearching(false); } }} aria-label={t("memory.search.label")} />}
        <div className="memory-view">
          <Segmented label={t("memory.view")} value={view} onChange={setView} options={VIEWS.map((v) => ({ id: v, label: t(`memory.view.${v}`) }))} />
        </div>
        <ChipRow label={t("memory.category")}>
          <button className="chip select" aria-pressed={category === "all"} onClick={() => setCategory("all")}>{t("common.all")} · {pool.length}</button>
          {categories.map((c) => (
            <button key={c} className="chip select" data-category={c} aria-pressed={category === c} onClick={() => setCategory(c)}>{categoryWord(c)} · {counts.get(c) ?? 0}</button>
          ))}
        </ChipRow>
        {topic && (
          <div className="memory-filter">
            <button className="chip select" aria-pressed onClick={() => setTopic(null)} aria-label={t("memory.topic.clear")}>#{topic} <Icon name="close" size={12} /></button>
          </div>
        )}
        {(data?.buckets.length ?? 0) > 1 && (
          <select className="field scope" value={bucket} onChange={(e) => setBucket(e.target.value)} aria-label={t("memory.scope")}>
            <option value="all">{t("memory.scope.every")}</option>
            {(data?.buckets ?? []).map((b) => (
              <option key={`${b.scope}:${b.scope_key}`} value={`${b.scope}:${b.scope_key}`}>{bucketLabel(b.scope, b.scope_key)} · {b.count}</option>
            ))}
          </select>
        )}
      </PageHeader>
      <div className="screen narrow memory">
        {loading && !error && <Skeleton rows={5} />}
        {error && !data && <div className="empty"><b>{t("memory.error")}</b><div>{error}</div><button className="btn primary" onClick={refresh}>{t("common.retry")}</button></div>}
        {(data?.suggestions ?? 0) > 0 && (
          <button className="memory-suggest-banner" onClick={() => setTidying(true)}>
            <Icon name="bulb" size={16} /> <span>{plural("memory.suggest.banner", data!.suggestions)}</span>
          </button>
        )}
        {data && records.length === 0 && (
          <div className="empty">
            <b>{t(all.length === 0 ? "memory.empty" : "memory.nomatch")}</b>
            {all.length === 0 && <div>{t("memory.empty.sub")}</div>}
          </div>
        )}
        {groups.map(([name, rs]) => (
          <section key={name || "-"} className={`memory-group ${view}`} data-group={view === "list" ? undefined : name}>
            {groupHead(name, rs.length)}
            <div className="memory-grid">{rs.map((r) => card(r, name))}</div>
          </section>
        ))}
      </div>
      {selecting && (
        <div className="bulkbar">
          <button className="btn ghost small" onClick={() => setSelected(new Set(records.every((r) => selected.has(r.id)) ? [] : records.map((r) => r.id)))}>
            {t(records.every((r) => selected.has(r.id)) ? "common.none" : "memory.allshown")}
          </button>
          <span className="sub">{t("memory.selected", { n: selected.size })}</span>
          <span className="grow" />
          <button className="btn small" data-action="merge" disabled={selected.size < 2} onClick={() => setMerging(chosen)}>{t("memory.merge")}</button>
          <button className="btn small danger" disabled={selected.size === 0} onClick={forgetMany}><Icon name="trash" size={14} /> {t("memory.forget")} {selected.size || ""}</button>
        </div>
      )}
      {editing && <MemorySheet r={editing} sessions={sessions} onClose={() => setEditing(null)} onSaved={() => { setEditing(null); refresh(); }} onForget={() => forget([editing.id])} toast={toast} />}
      {adding && <MemorySheet sessions={sessions} onClose={() => setAdding(false)} onSaved={() => { setAdding(false); refresh(); }} toast={toast} />}
      {merging && <MergeSheet records={merging} onClose={() => setMerging(null)} onDone={() => { setMerging(null); setSelecting(false); setSelected(new Set()); refresh(); }} toast={toast} />}
      {renaming && <RenameSheet topic={renaming} onClose={() => setRenaming(null)} onDone={(to) => { setRenaming(null); if (topic === renaming) setTopic(to || null); refresh(); }} toast={toast} />}
      {tidying && <SuggestionsSheet sessions={data?.sessions ?? {}} onClose={() => setTidying(false)} onChanged={refresh} toast={toast} />}
      {history && <HistorySheet onClose={() => setHistory(false)} onChanged={refresh} toast={toast} />}
    </>
  );
}

function MemorySheet({ r, sessions, onClose, onSaved, onForget, toast }: { r?: MemoryRecord; sessions: [string, string][]; onClose: () => void; onSaved: () => void; onForget?: () => void; toast: (t: string) => void }) {
  const [text, setText] = useState(r?.text ?? "");
  // A new note may leave its category and topics to the host, which infers them as Remember does.
  const [category, setCategory] = useState(r?.category ?? "");
  const [topics, setTopics] = useState((r?.topics ?? []).join(", "));
  const [scope, setScope] = useState(r ? (r.scope === "global" ? "global" : `session:${r.scope_key}`) : "global");
  const [busy, setBusy] = useState(false);
  async function save() {
    setBusy(true);
    try {
      if (r) {
        await api.patch(`/api/memory/${encodeURIComponent(r.id)}`, { text: text.trim(), category, topics: parseTopics(topics) });
        toast(t("memory.updated"));
      } else {
        const typed = parseTopics(topics);
        const res = await api.post<{ decision: string; record: MemoryRecord }>("/api/memory", { scope: scope === "global" ? "global" : "session", scope_key: scope === "global" ? "" : scope.slice(8), category, topics: typed.length ? typed : null, text: text.trim() });
        toast(res.decision.toLowerCase() === "created" ? t("memory.remembered") : t("memory.similar", { decision: res.decision.toLowerCase() }));
      }
      onSaved();
    } catch (e) {
      toast(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  const options = [...new Set([...(r ? [] : [""]), ...MEMORY_CATEGORIES, ...(category ? [category] : [])])];
  return (
    <Sheet title={t(r ? "memory.sheet.edit" : "memory.sheet.add")} onClose={onClose}>
      <label className="field">{t("memory.text")}</label>
      <textarea className="field" rows={6} autoFocus value={text} onChange={(e) => setText(e.target.value)} placeholder={t("memory.text.placeholder")} />
      <div className="grid2">
        <div>
          <label className="field" htmlFor="memory-category">{t("memory.category")}</label>
          <select id="memory-category" className="field" value={category} onChange={(e) => setCategory(e.target.value)}>
            {options.map((c) => <option key={c || "auto"} value={c}>{c ? categoryWord(c) : t("memory.category.auto")}</option>)}
          </select>
        </div>
        <div>
          <label className="field">{t("memory.scope")}</label>
          <select className="field" value={scope} disabled={!!r} title={r ? t("memory.scope.fixed") : undefined} onChange={(e) => setScope(e.target.value)}>
            <option value="global">{t("memory.scope.globaloption")}</option>
            {sessions.map(([id, title]) => (
              <option key={id} value={`session:${id}`}>{title}</option>
            ))}
          </select>
        </div>
      </div>
      <label className="field" htmlFor="memory-topics">{t("memory.topics")}</label>
      <input id="memory-topics" className="field" value={topics} onChange={(e) => setTopics(e.target.value)} placeholder={t("memory.topics.placeholder")} />
      {r && (
        <div className="sub" style={{ marginTop: 8 }}>
          {r.created_at ? t("memory.created", { when: absTime(r.created_at) }) : ""}{r.version > 1 ? t("memory.version", { n: r.version }) : ""}{r.last_accessed_at ? t("memory.recalled", { t: relTime(r.last_accessed_at) }) : ""}
        </div>
      )}
      <div className="sheet-foot">
        {r && onForget && <button className="btn danger" onClick={onForget}><Icon name="trash" size={14} /> {t("memory.forget")}</button>}
        <span className="grow" />
        <button className="btn ghost" onClick={onClose}>{t("common.cancel")}</button>
        <button className="btn primary" disabled={busy || !text.trim()} onClick={save}>{t(r ? "common.save" : "memory.sheet.add")}</button>
      </div>
    </Sheet>
  );
}

function MergeSheet({ records, onClose, onDone, toast }: { records: MemoryRecord[]; onClose: () => void; onDone: () => void; toast: (t: string) => void }) {
  const [text, setText] = useState(() => [...new Set(records.map((r) => r.text.trim()))].join("\n"));
  const [category, setCategory] = useState(() => {
    const n = new Map<string, number>();
    for (const r of records) n.set(r.category, (n.get(r.category) ?? 0) + 1);
    return [...n.entries()].sort((a, b) => b[1] - a[1])[0]?.[0] ?? "fact";
  });
  const [topics, setTopics] = useState(() => [...new Set(records.flatMap((r) => r.topics))].join(", "));
  const [busy, setBusy] = useState(false);
  async function merge() {
    setBusy(true);
    try {
      await api.post("/api/memory/merge", { ids: records.map((r) => r.id), text: text.trim(), category, topics: parseTopics(topics) });
      toast(t("memory.merged"));
      onDone();
    } catch (e) {
      toast(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Sheet title={t("memory.merge.title", { n: records.length })} onClose={onClose} className="memory-merge">
      <div className="sub">{t("memory.merge.sub")}</div>
      <label className="field">{t("memory.text")}</label>
      <textarea className="field" rows={7} autoFocus value={text} onChange={(e) => setText(e.target.value)} />
      <div className="grid2">
        <div>
          <label className="field" htmlFor="merge-category">{t("memory.category")}</label>
          <select id="merge-category" className="field" value={category} onChange={(e) => setCategory(e.target.value)}>
            {MEMORY_CATEGORIES.map((c) => <option key={c} value={c}>{categoryWord(c)}</option>)}
          </select>
        </div>
        <div>
          <label className="field" htmlFor="merge-topics">{t("memory.topics")}</label>
          <input id="merge-topics" className="field" value={topics} onChange={(e) => setTopics(e.target.value)} />
        </div>
      </div>
      <div className="sheet-foot">
        <span className="grow" />
        <button className="btn ghost" onClick={onClose}>{t("common.cancel")}</button>
        <button className="btn primary" data-action="merge-confirm" disabled={busy || !text.trim()} onClick={merge}>{t("memory.merge")}</button>
      </div>
    </Sheet>
  );
}

function RenameSheet({ topic, onClose, onDone, toast }: { topic: string; onClose: () => void; onDone: (to: string) => void; toast: (t: string) => void }) {
  const [to, setTo] = useState(topic);
  const [busy, setBusy] = useState(false);
  async function rename(target: string) {
    setBusy(true);
    try {
      const res = await api.post<{ changed: number }>("/api/memory/topics/rename", { topic, to: target });
      toast(plural("memory.topic.renamed", res.changed));
      onDone(parseTopics(target)[0] ?? "");
    } catch (e) {
      toast(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Sheet title={t("memory.topic.rename.title", { topic })} onClose={onClose} size="narrow">
      <div className="sub">{t("memory.topic.rename.sub")}</div>
      <label className="field" htmlFor="topic-rename">{t("memory.topic.rename.to")}</label>
      <input id="topic-rename" className="field" autoFocus value={to} onChange={(e) => setTo(e.target.value)} onKeyDown={(e) => { if (e.key === "Enter" && parseTopics(to).length) void rename(to); }} />
      <div className="sheet-foot">
        <button className="btn danger ghost" disabled={busy} onClick={() => void rename("")}>{t("memory.topic.remove")}</button>
        <span className="grow" />
        <button className="btn ghost" onClick={onClose}>{t("common.cancel")}</button>
        <button className="btn primary" disabled={busy || !parseTopics(to).length || parseTopics(to)[0] === topic} onClick={() => void rename(to)}>{t("common.save")}</button>
      </div>
    </Sheet>
  );
}

function SuggestionsSheet({ sessions, onClose, onChanged, toast }: { sessions: Record<string, string>; onClose: () => void; onChanged: () => void; toast: (t: string) => void }) {
  const { data, error, loading, refresh } = useQuery<MemorySuggestions>("/api/memory/suggestions");
  const [busy, setBusy] = useState(false);
  const where = (r: MemoryRecord) => (r.scope === "global" ? t("memory.global") : r.scope === "session" ? sessions[r.scope_key] ?? t("memory.scope.deleted.short") : `${r.scope}: ${r.scope_key}`);
  async function act(path: string, body?: object) {
    setBusy(true);
    try {
      await api.post(path, body ?? {});
      refresh();
      onChanged();
    } catch (e) {
      toast(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  async function tidy() {
    setBusy(true);
    try {
      const res = await api.post<{ merged: number; suggested: number }>("/api/memory/consolidate", {});
      toast(t("memory.tidied", res));
      refresh();
      onChanged();
    } catch (e) {
      toast(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  const list = data?.suggestions ?? [];
  return (
    <Sheet title={t("memory.suggest.title")} onClose={onClose} size="wide" className="memory-suggestions">
      <div className="sub">{t("memory.suggest.sub")}</div>
      {loading && !data && <Skeleton rows={3} />}
      {error && !data && <div className="empty">{error}</div>}
      {data && list.length === 0 && <div className="empty"><b>{t("memory.suggest.empty")}</b></div>}
      {list.map((s) => (
        <div key={s.id} className="card memory-suggestion" data-suggestion={s.id}>
          <div className="section-title" style={{ marginTop: 0 }}>{t(`memory.suggest.${s.kind}`)}</div>
          {s.memory_ids.map((id) => {
            const r = data?.records[id];
            return r ? (
              <div key={id} className="memory-suggestion-record">
                <div className="erow-meta"><span className={`chip cat-${r.category}`}>{categoryWord(r.category)}</span><span>{where(r)}</span>{r.updated_at && <span className="faint">· {relTime(r.updated_at)}</span>}</div>
                <div className="memory-text">{r.text}</div>
              </div>
            ) : null;
          })}
          <div className="sub"><b>{t("memory.suggest.keep")}</b> {s.text}</div>
          <div className="row-actions">
            <button className="btn small ghost" disabled={busy} onClick={() => void act(`/api/memory/suggestions/${s.id}/dismiss`)}>{t("memory.suggest.dismiss")}</button>
            <button className="btn small primary" data-action="apply" disabled={busy} onClick={() => void act(`/api/memory/suggestions/${s.id}/apply`)}>{t(`memory.suggest.apply.${s.kind}`)}</button>
          </div>
        </div>
      ))}
      <div className="sheet-foot">
        <button className="btn ghost" disabled={busy} onClick={() => void tidy()}>{t("memory.tidy")}</button>
        <span className="grow" />
        <button className="btn" onClick={onClose}>{t("common.close")}</button>
      </div>
    </Sheet>
  );
}

function HistorySheet({ onClose, onChanged, toast }: { onClose: () => void; onChanged: () => void; toast: (t: string) => void }) {
  const { data, error, loading, refresh } = useQuery<{ entries: MemoryHistoryEntry[] }>("/api/memory/history");
  const [busy, setBusy] = useState<number | null>(null);
  async function undo(id: number) {
    setBusy(id);
    try {
      await api.post(`/api/memory/history/${id}/undo`, {});
      toast(t("memory.history.restored"));
      refresh();
      onChanged();
    } catch (e) {
      toast(errorText(e));
    } finally {
      setBusy(null);
    }
  }
  const entries = data?.entries ?? [];
  return (
    <Sheet title={t("memory.history.title")} onClose={onClose} size="wide" className="memory-history">
      {loading && !data && <Skeleton rows={3} />}
      {error && !data && <div className="empty">{error}</div>}
      {data && entries.length === 0 && <div className="empty"><b>{t("memory.history.empty")}</b></div>}
      {entries.map((e) => (
        <div key={e.id} className={`card memory-history-entry ${e.undone_at ? "undone" : ""}`} data-history={e.id}>
          <div className="erow-meta">
            <b>{t(`memory.history.action.${e.action}`)}</b>
            <span className="faint">· {t(e.actor === "auto" ? "memory.history.auto" : "memory.history.you")}</span>
            <span className="faint" title={absTime(e.at)}>· {relTime(e.at)}</span>
            <span className="grow" />
            {e.undone_at ? <span className="faint">{t("memory.history.undone")}</span> : <button className="btn small" data-action="undo" disabled={busy === e.id} onClick={() => void undo(e.id)}><Icon name="undo" size={14} /> {t("memory.history.undo")}</button>}
          </div>
          {e.after.slice(0, 1).map((r) => <div key={r.id} className="memory-text">{r.text}</div>)}
          {e.before.length > 0 && (
            <details className="memory-history-before">
              <summary>{t("memory.history.before")}</summary>
              {e.before.map((r) => <div key={r.id} className="memory-text faint">{categoryWord(r.kind)} · {r.text}</div>)}
            </details>
          )}
        </div>
      ))}
      <div className="sheet-foot">
        <span className="grow" />
        <button className="btn" onClick={onClose}>{t("common.close")}</button>
      </div>
    </Sheet>
  );
}
