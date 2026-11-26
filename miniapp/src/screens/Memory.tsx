import { useEffect, useMemo, useState } from "react";
import { api, MEMORY_CATEGORIES, MemoryHistoryEntry, MemoryListing, MemoryRecord, MemorySuggestions } from "../api";
import { Page } from "../shell";
import { MemoryEntries } from "../memory/MemoryEntries";
import { MemoryComposer, MemoryHead, MemoryPane, useSplit, type Mode } from "../memory/frame";
import { byTopic, categoryWord, parseTopics } from "../memory/model";
import { learnedWhy } from "../memory/review";
import { downloadExport, ImportSheet } from "../memory/transfer";
import { undoToast, type Undoable } from "../memory/undo";
import { navigate, pathFor, useRoute } from "../router";
import { Segmented, Skeleton } from "../ui/index";
import { Button, Field, IconButton, Select, Sheet, confirmDialog, deleteWithUndo, toast as showToast, type MenuItem } from "../ui/primitives";
import { absTime, relTime } from "../format";
import { Icon } from "../icons";
import { hold, invalidate, prime, release, useQuery } from "../store";
import { errorText } from "../ui";
import { plural, t } from "../i18n";

type View = "list" | "topics" | "categories";
const VIEWS: View[] = ["list", "topics", "categories"];
const VIEW_KEY = "daedalus.memory.view";
const SETTINGS_PATH = "/app/settings/memory";

function storedView(): View {
  try {
    const v = localStorage.getItem(VIEW_KEY);
    return VIEWS.includes(v as View) ? (v as View) : "list";
  } catch {
    return "list";
  }
}

/** The Memory screen: the entries (claude.ai's list) or every fact (the record list), each with the
 *  thing it opens beside it, and one entry at /app/memory/<id>. ``?view=facts`` opens the facts, as
 *  does the tidying's notification link (``?suggestions=1``), whose suggestions live there;
 *  ``?record=<id>`` (the chat's "Saved to memory" chip) opens the entry that record is filed in. */
export function MemoryScreen({ toast }: { toast: (t: string) => void }) {
  const route = useRoute();
  const [history, setHistory] = useState(false);
  const [importing, setImporting] = useState(false);
  const [query, setQuery] = useState("");
  const record = route.query.get("record");
  const mode: Mode = route.query.get("view") === "facts" || route.query.get("suggestions") === "1" ? "facts" : "entries";
  if (record) return <RecordRedirect id={record} />;
  const menu: MenuItem[] = [
    { label: t("memory.import"), icon: "import", onSelect: () => setImporting(true) },
    { label: t("memory.export"), icon: "download", onSelect: () => void exportMemory() },
    "-",
    { label: t("memory.settings"), icon: "settings", onSelect: () => navigate(SETTINGS_PATH) },
  ];
  const shared = { query, onQuery: setQuery, onHistory: () => setHistory(true), menu };
  return (
    <>
      {mode === "facts" ? <MemoryFacts {...shared} toast={toast} /> : <MemoryEntries {...shared} onImport={() => setImporting(true)} />}
      {history && <HistorySheet onClose={() => setHistory(false)} onChanged={() => invalidate("/api/memory")} toast={toast} />}
      {importing && <ImportSheet onClose={() => setImporting(false)} onDone={() => { setImporting(false); invalidate("/api/memory"); }} />}
    </>
  );
}

async function exportMemory(): Promise<void> {
  try {
    await downloadExport();
  } catch (e) {
    showToast(errorText(e));
  }
}

/** The entry a record is filed in, looked up and opened in place of this address. */
function RecordRedirect({ id }: { id: string }) {
  const { data, error } = useQuery<MemoryListing>("/api/memory", { staleMs: 0 });
  useEffect(() => {
    if (!data) return;
    const entry = data.records.find((r) => r.id === id)?.entry;
    navigate(entry ? pathFor("memory", entry) : pathFor("memory"), { replace: true });
  }, [data, id]);
  return error ? <div className="empty">{error}</div> : <Skeleton rows={4} />;
}

type Opened = { kind: "fact"; id: string } | { kind: "new" };
type FactsProps = { query: string; onQuery: (query: string) => void; onHistory: () => void; menu: MenuItem[]; toast: (t: string) => void };

/** What the agent remembered — by category and topic — to read, correct, merge, add to and prune. */
function MemoryFacts({ query, onQuery, onHistory, menu, toast }: FactsProps) {
  const key = "/api/memory";
  const split = useSplit();
  const { data, error, loading, refresh } = useQuery<MemoryListing>(key, { staleMs: 10000 });
  const [bucket, setBucket] = useState<string>("all");
  const [category, setCategory] = useState<string>("all");
  const [topic, setTopic] = useState<string | null>(null);
  const [view, setViewState] = useState<View>(storedView);
  const [selecting, setSelecting] = useState(false);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [opened, setOpened] = useState<Opened | null>(null);
  const [merging, setMerging] = useState<MemoryRecord[] | null>(null);
  const [renaming, setRenaming] = useState<string | null>(null);
  const [tidying, setTidying] = useState(() => new URLSearchParams(window.location.search).get("suggestions") === "1");
  const [busy, setBusy] = useState(false);

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
  // What the scope, the search and the topic leave, before the category: the category list counts
  // this, so each choice promises what it shows.
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
  const bucketLabel = (scope: string, k: string) => (scope === "global" ? t("memory.scope.global") : scope === "project" ? data?.projects[k] ?? t("memory.scope.deleted", { id: k }) : `${scope}: ${k}`);
  const projects = Object.entries(data?.projects ?? {});
  const chosen = all.filter((r) => selected.has(r.id));
  const current = opened?.kind === "fact" ? all.find((r) => r.id === opened.id) : undefined;

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
    setOpened(null);
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
    if (!(await confirmDialog({ title: t("memory.forget.many.title", { n: ids.length }), body: t("memory.forget.body"), action: t("memory.forget"), danger: true }))) return;
    forget(ids);
    setSelecting(false);
  }

  async function quickAdd(text: string): Promise<boolean> {
    setBusy(true);
    try {
      const res = await api.post<{ decision: string; entry: { title: string } | null } & Undoable>("/api/memory/quick", { text });
      const where = res.entry?.title ?? "";
      const said = res.decision === "created" ? t("memory.quick.saved", { entry: where }) : res.decision === "merged" ? t("memory.quick.merged", { entry: where }) : t("memory.quick.held", { entry: where });
      undoToast(said, res.history_id, () => { invalidate("/api/memory"); refresh(); });
      invalidate("/api/memory");
      refresh();
      return true;
    } catch (e) {
      toast(errorText(e));
      return false;
    } finally {
      setBusy(false);
    }
  }

  const factsMenu: MenuItem[] = [
    { label: t(selecting ? "memory.select.done" : "memory.select"), icon: "check", onSelect: () => { setSelecting((v) => !v); setSelected(new Set()); } },
    "-",
    ...menu,
  ];

  function row(r: MemoryRecord, group: string) {
    const on = opened?.kind === "fact" && opened.id === r.id;
    const where = r.scope === "global" ? null : r.scope === "project" ? data?.projects[r.scope_key] ?? t("memory.scope.deleted.short") : `${r.scope}: ${r.scope_key}`;
    const open = () => (selecting ? toggle(r.id) : setOpened({ kind: "fact", id: r.id }));
    return (
      <div key={`${group}:${r.id}`} data-memory={r.id} className={`memory-card ${selecting ? "selecting" : ""} ${selected.has(r.id) ? "selected" : ""} ${on ? "on" : ""}`} role={selecting ? "checkbox" : "button"} aria-checked={selecting ? selected.has(r.id) : undefined} aria-current={on || undefined} tabIndex={0} onClick={open}
        onKeyDown={(e) => { if (e.target !== e.currentTarget) return; if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); } }}>
        {selecting
          ? <input type="checkbox" className="memory-check" checked={selected.has(r.id)} onChange={() => toggle(r.id)} onClick={(e) => e.stopPropagation()} aria-label={t("memory.select.one")} />
          : <Icon name="brain" size={16} />}
        <span className="memory-card-main">
          <span className="memory-card-text">{r.text}</span>
          <span className="memory-card-meta">
            <span>{categoryWord(r.category)}</span>
            {r.topics.slice(0, 2).map((x) => (
              <button key={x} type="button" className={`topic-tag ${topic === x ? "on" : ""}`} onClick={(e) => { e.stopPropagation(); setTopic(topic === x ? null : x); }} aria-pressed={topic === x}>{x}</button>
            ))}
            {where && <span>{where}</span>}
          </span>
        </span>
        {r.created_at && <span className="memory-card-when" title={absTime(r.created_at)}>{relTime(r.created_at)}</span>}
      </div>
    );
  }

  function groupHead(name: string, size: number) {
    if (view === "list") return null;
    const label = view === "topics" ? (name ? `#${name}` : t("memory.topic.none")) : categoryWord(name);
    return (
      <div className="memory-group-head">
        {view === "topics" && name ? <button className="linkbtn" onClick={() => setTopic(topic === name ? null : name)}>{label}</button> : <span>{label}</span>}
        <span className="faint">{size}</span>
        <span className="grow" />
        {view === "topics" && name && <IconButton size={28} icon="pen" label={t("memory.topic.rename")} onClick={() => setRenaming(name)} />}
      </div>
    );
  }

  const editor = opened && (opened.kind === "new" || current) && (
    <FactEditor key={opened.kind === "new" ? "new" : `${current!.id}:${current!.version}`} r={current} projects={projects} as={split ? "pane" : "sheet"}
      onClose={() => setOpened(null)} onSaved={() => { if (opened.kind === "new") setOpened(null); refresh(); }} onForget={current ? () => forget([current.id]) : undefined} toast={toast} />
  );
  const allSelected = records.length > 0 && records.every((r) => selected.has(r.id));
  return (
    <>
      <MemoryHead mode="facts" subtitle={data ? (records.length === all.length ? plural("memory.count", all.length) : t("memory.count.of", { shown: records.length, total: all.length })) : undefined}
        query={query} onQuery={onQuery} onHistory={onHistory} menu={factsMenu} onAdd={() => setOpened({ kind: "new" })} />
      <div className="memory-split" data-view="facts">
        <section className="memory-list">
          <div className="memory-scroll">
            <Page width="list" className="memory memory-facts">
              {all.length > 0 && (
                <div className="memory-bar">
                  <Segmented label={t("memory.view")} size="sm" className="memory-view" value={view} onChange={setView} options={VIEWS.map((v) => ({ id: v, label: t(`memory.view.${v}`) }))} />
                  <span className="grow" />
                  {topic && <button type="button" className="chip select memory-filter" aria-pressed onClick={() => setTopic(null)} aria-label={t("memory.topic.clear")}>#{topic} <Icon name="close" size={12} /></button>}
                  <Select size="sm" className="memory-category" label={t("memory.category")} value={category}
                    options={[{ id: "all", label: `${t("common.all")} · ${pool.length}` }, ...categories.map((c) => ({ id: c, label: `${categoryWord(c)} · ${counts.get(c) ?? 0}` }))]} onChange={setCategory} />
                  {(data?.buckets.length ?? 0) > 1 && (
                    <Select size="sm" className="memory-scope" label={t("memory.scope")} value={bucket}
                      options={[{ id: "all", label: t("memory.scope.every") }, ...(data?.buckets ?? []).map((b) => ({ id: `${b.scope}:${b.scope_key}`, label: `${bucketLabel(b.scope, b.scope_key)} · ${b.count}` }))]} onChange={setBucket} />
                  )}
                </div>
              )}
              {loading && !data && !error && <Skeleton rows={5} />}
              {error && !data && <div className="empty"><b>{t("memory.error")}</b><div>{error}</div><button className="btn primary" onClick={refresh}>{t("common.retry")}</button></div>}
              {(data?.suggestions ?? 0) > 0 && (
                <div className="memory-notes">
                  <button className="memory-suggest-banner" onClick={() => setTidying(true)}>
                    <Icon name="bulb" size={16} />
                    <span className="memory-banner-text">{plural("memory.suggest.banner", data!.suggestions)}</span>
                    <span className="memory-banner-act">{t("memory.review.open")}<Icon name="forward" size={14} /></span>
                  </button>
                </div>
              )}
              {data && records.length === 0 && (
                <div className="empty memory-empty">
                  <Icon name="brain" size={20} />
                  <b>{t(all.length === 0 ? "memory.empty" : "memory.nomatch")}</b>
                  {all.length === 0 && <div>{t("memory.empty.sub")}</div>}
                </div>
              )}
              {groups.map(([name, rs]) => (
                <section key={name || "-"} className={`memory-group ${view}`} data-group={view === "list" ? undefined : name}>
                  {groupHead(name, rs.length)}
                  <div className="memory-grid">{rs.map((r) => row(r, name))}</div>
                </section>
              ))}
            </Page>
          </div>
          {selecting ? (
            <div className="memory-bulkbar">
              <Button variant="quiet" size="sm" onClick={() => setSelected(new Set(allSelected ? [] : records.map((r) => r.id)))}>{t(allSelected ? "common.none" : "memory.allshown")}</Button>
              <span className="sub">{t("memory.selected", { n: selected.size })}</span>
              <span className="grow" />
              <Button size="sm" data-action="merge" disabled={selected.size < 2} onClick={() => setMerging(chosen)}>{t("memory.merge")}</Button>
              <Button variant="danger" size="sm" icon="trash" disabled={selected.size === 0} onClick={forgetMany}>{t("memory.forget")} {selected.size || ""}</Button>
            </div>
          ) : (
            <MemoryComposer placeholder={t("memory.quick.placeholder")} label={t("memory.quick.label")} busy={busy} onSend={quickAdd} />
          )}
        </section>
        {split && editor}
      </div>
      {!split && editor}
      {merging && <MergeSheet records={merging} onClose={() => setMerging(null)} onDone={() => { setMerging(null); setSelecting(false); setSelected(new Set()); refresh(); }} toast={toast} />}
      {renaming && <RenameSheet topic={renaming} onClose={() => setRenaming(null)} onDone={(to) => { setRenaming(null); if (topic === renaming) setTopic(to || null); refresh(); }} toast={toast} />}
      {tidying && <SuggestionsSheet projects={data?.projects ?? {}} onClose={() => setTidying(false)} onChanged={refresh} toast={toast} />}
    </>
  );
}

/** A fact to read and correct, or a new one to write: its text, category, scope and topics. It is the
 *  pane beside the list, or a sheet when the window has no room for the pane. */
function FactEditor({ r, projects, as, onClose, onSaved, onForget, toast }: { r?: MemoryRecord; projects: [string, string][]; as: "pane" | "sheet"; onClose: () => void; onSaved: () => void; onForget?: () => void; toast: (t: string) => void }) {
  const [text, setText] = useState(r?.text ?? "");
  // A new note may leave its category and topics to the host, which infers them as Remember does.
  const [category, setCategory] = useState(r?.category ?? "auto");
  const [topics, setTopics] = useState((r?.topics ?? []).join(", "));
  const [scope, setScope] = useState(r ? (r.scope === "global" ? "global" : `project:${r.scope_key}`) : "global");
  const [busy, setBusy] = useState(false);
  const chosen = category === "auto" ? "" : category;
  const unchanged = !!r && text.trim() === r.text && chosen === r.category && parseTopics(topics).join(",") === r.topics.join(",");
  async function save() {
    setBusy(true);
    try {
      if (r) {
        await api.patch(`/api/memory/${encodeURIComponent(r.id)}`, { text: text.trim(), category: chosen, topics: parseTopics(topics) });
        toast(t("memory.updated"));
      } else {
        const typed = parseTopics(topics);
        const res = await api.post<{ decision: string; record: MemoryRecord }>("/api/memory", { scope: scope === "global" ? "global" : "project", scope_key: scope === "global" ? "" : scope.slice("project:".length), category: chosen, topics: typed.length ? typed : null, text: text.trim() });
        // Not stored as written: the operator's words went into what was already there.
        if (res.decision.toLowerCase() !== "created") toast(t("memory.similar", { decision: res.decision.toLowerCase() }));
      }
      onSaved();
    } catch (e) {
      toast(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  const options = [...(r ? [] : ["auto"]), ...MEMORY_CATEGORIES, ...(r && !(MEMORY_CATEGORIES as readonly string[]).includes(r.category) ? [r.category] : [])];
  const body = (
    <div className="memory-fact-form" data-view="fact">
      <Field multiline rows={6} autoFocus={!r} label={t("memory.text")} value={text} onChange={(e) => setText(e.target.value)} placeholder={t("memory.text.placeholder")} />
      <div className="memory-fact-pair">
        <div className="field-box">
          <label className="field-label" htmlFor="memory-category">{t("memory.category")}</label>
          <Select id="memory-category" size="md" label={t("memory.category")} value={category} onChange={setCategory} options={options.map((c) => ({ id: c, label: c === "auto" ? t("memory.category.auto") : categoryWord(c) }))} />
        </div>
        <div className="field-box">
          <label className="field-label" htmlFor="memory-scope">{t("memory.scope")}</label>
          <Select id="memory-scope" size="md" label={t("memory.scope")} value={scope} disabled={!!r} onChange={setScope} options={[{ id: "global", label: t("memory.scope.globaloption") }, ...projects.map(([id, name]) => ({ id: `project:${id}`, label: name }))]} />
        </div>
      </div>
      {r && <div className="sub memory-fact-fixed">{t("memory.scope.fixed")}</div>}
      <Field label={t("memory.topics")} value={topics} onChange={(e) => setTopics(e.target.value)} placeholder={t("memory.topics.placeholder")} />
      {r && (
        <div className="sub memory-fact-history">
          {r.created_at ? t("memory.created", { when: absTime(r.created_at) }) : ""}{r.version > 1 ? t("memory.version", { n: r.version }) : ""}{r.last_accessed_at ? t("memory.recalled", { t: relTime(r.last_accessed_at) }) : ""}
        </div>
      )}
    </div>
  );
  const actions = (
    <>
      {r && onForget && <Button variant="danger" icon="trash" onClick={onForget}>{t("memory.forget")}</Button>}
      <span className="grow" />
      <Button variant="quiet" onClick={onClose}>{t("common.cancel")}</Button>
      <Button variant="primary" disabled={busy || !text.trim() || unchanged} onClick={() => void save()}>{t(r ? "common.save" : "memory.sheet.add")}</Button>
    </>
  );
  if (as === "sheet") return <Sheet title={t(r ? "memory.sheet.edit" : "memory.sheet.add")} onClose={onClose} footer={actions}>{body}</Sheet>;
  return (
    <MemoryPane kind={t(r ? "memory.pane.fact" : "memory.sheet.add")} meta={r?.updated_at ? relTime(r.updated_at) : undefined} onClose={onClose} label={t(r ? "memory.sheet.edit" : "memory.sheet.add")} foot={<div className="memory-pane-foot">{actions}</div>}>
      {body}
    </MemoryPane>
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
      onDone();
    } catch (e) {
      toast(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Sheet title={t("memory.merge.title", { n: records.length })} onClose={onClose} className="memory-merge"
      footer={<><span className="grow" /><Button variant="quiet" onClick={onClose}>{t("common.cancel")}</Button><Button variant="primary" data-action="merge-confirm" disabled={busy || !text.trim()} onClick={merge}>{t("memory.merge")}</Button></>}>
      <div className="sub">{t("memory.merge.sub")}</div>
      <div className="memory-fact-form">
        <Field multiline rows={7} autoFocus label={t("memory.text")} value={text} onChange={(e) => setText(e.target.value)} />
        <div className="memory-fact-pair">
          <div className="field-box">
            <label className="field-label" htmlFor="merge-category">{t("memory.category")}</label>
            <Select id="merge-category" size="md" label={t("memory.category")} value={category} onChange={setCategory} options={MEMORY_CATEGORIES.map((c) => ({ id: c, label: categoryWord(c) }))} />
          </div>
          <Field id="merge-topics" label={t("memory.topics")} value={topics} onChange={(e) => setTopics(e.target.value)} />
        </div>
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
      await api.post("/api/memory/topics/rename", { topic, to: target });
      onDone(parseTopics(target)[0] ?? "");
    } catch (e) {
      toast(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Sheet title={t("memory.topic.rename.title", { topic })} onClose={onClose} size="sm"
      footer={<><Button variant="danger" disabled={busy} onClick={() => void rename("")}>{t("memory.topic.remove")}</Button><span className="grow" /><Button variant="quiet" onClick={onClose}>{t("common.cancel")}</Button><Button variant="primary" disabled={busy || !parseTopics(to).length || parseTopics(to)[0] === topic} onClick={() => void rename(to)}>{t("common.save")}</Button></>}>
      <div className="sub">{t("memory.topic.rename.sub")}</div>
      <Field id="topic-rename" label={t("memory.topic.rename.to")} autoFocus value={to} onChange={(e) => setTo(e.target.value)} onKeyDown={(e) => { if (e.key === "Enter" && parseTopics(to).length) void rename(to); }} />
    </Sheet>
  );
}

function SuggestionsSheet({ projects, onClose, onChanged, toast }: { projects: Record<string, string>; onClose: () => void; onChanged: () => void; toast: (t: string) => void }) {
  const { data, error, loading, refresh } = useQuery<MemorySuggestions>("/api/memory/suggestions");
  const [busy, setBusy] = useState(false);
  const where = (r: MemoryRecord) => (r.scope === "global" ? t("memory.global") : r.scope === "project" ? projects[r.scope_key] ?? t("memory.scope.deleted.short") : `${r.scope}: ${r.scope_key}`);
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
      await api.post("/api/memory/consolidate", {});
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
    <Sheet title={t("memory.suggest.title")} onClose={onClose} size="xl" className="memory-suggestions"
      footer={<><Button variant="quiet" disabled={busy} onClick={() => void tidy()}>{t("memory.tidy")}</Button><span className="grow" /><Button onClick={onClose}>{t("common.close")}</Button></>}>
      <div className="sub">{t("memory.suggest.sub")}</div>
      {loading && !data && <Skeleton rows={3} />}
      {error && !data && <div className="empty">{error}</div>}
      {data && list.length === 0 && <div className="empty"><b>{t("memory.suggest.empty")}</b></div>}
      {list.map((s) => {
        const learned = s.kind === "revision" || s.kind === "addition";
        // The background pass's proposals name what they do: a change or removal of a bullet, or a held-back new one.
        const kind = s.kind === "revision" ? `revision.${s.action === "retire" ? "retire" : "update"}` : s.kind;
        const apply = s.kind === "revision" ? (s.action === "retire" ? "retire" : "revision") : s.kind;
        return (
          <div key={s.id} className="card memory-suggestion" data-suggestion={s.id} data-kind={s.kind}>
            <div className="section-title">{t(`memory.suggest.${kind}`)}</div>
            {s.memory_ids.map((id) => {
              const r = data?.records[id];
              return r ? (
                <div key={id} className="memory-suggestion-record">
                  <div className="memory-card-meta"><span>{categoryWord(r.category)}</span><span>{where(r)}</span>{r.updated_at && <span>{relTime(r.updated_at)}</span>}</div>
                  <div className="memory-text">{r.text}</div>
                </div>
              ) : null;
            })}
            {!learned && <div className="sub"><b>{t("memory.suggest.keep")}</b> {s.text}</div>}
            {learned && s.action !== "retire" && <div className="memory-text memory-suggestion-proposed"><b>{t(s.kind === "addition" ? "memory.suggest.new" : "memory.suggest.proposed")}</b> {s.text}</div>}
            {learned && <div className="sub">{s.kind === "addition" && s.entry_title ? t("memory.suggest.into", { entry: s.entry_title }) + " · " : ""}{learnedWhy(s)}</div>}
            <div className="row-actions">
              <Button variant="quiet" size="sm" disabled={busy} onClick={() => void act(`/api/memory/suggestions/${s.id}/dismiss`)}>{t(learned ? "memory.suggest.dismiss.learned" : "memory.suggest.dismiss")}</Button>
              <Button variant="primary" size="sm" data-action="apply" disabled={busy} onClick={() => void act(`/api/memory/suggestions/${s.id}/apply`)}>{t(`memory.suggest.apply.${apply}`)}</Button>
            </div>
          </div>
        );
      })}
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
    <Sheet title={t("memory.history.title")} onClose={onClose} size="xl" className="memory-history" footer={<><span className="grow" /><Button onClick={onClose}>{t("common.close")}</Button></>}>
      {loading && !data && <Skeleton rows={3} />}
      {error && !data && <div className="empty">{error}</div>}
      {data && entries.length === 0 && <div className="empty"><b>{t("memory.history.empty")}</b></div>}
      {entries.map((e) => (
        <div key={e.id} className={`card memory-history-entry ${e.undone_at ? "undone" : ""}`} data-history={e.id}>
          <div className="memory-card-meta">
            <b>{t(`memory.history.action.${e.action}`)}</b>
            <span>· {t(e.actor === "auto" ? "memory.history.auto" : "memory.history.you")}</span>
            <span title={absTime(e.at)}>· {relTime(e.at)}</span>
            <span className="grow" />
            {e.undone_at ? <span>{t("memory.history.undone")}</span> : <Button size="sm" icon="undo" data-action="undo" disabled={busy === e.id} onClick={() => void undo(e.id)}>{t("memory.history.undo")}</Button>}
          </div>
          {e.after.slice(0, 1).map((r) => <div key={r.id} className="memory-text">{r.text}</div>)}
          {e.after.length === 0 && e.before.length === 0 && e.summary && <div className="memory-text faint">{e.summary}</div>}
          {e.before.length > 0 && (
            <details className="memory-history-before">
              <summary>{t("memory.history.before")}</summary>
              {e.before.map((r) => <div key={r.id} className="memory-text faint">{categoryWord(r.kind)} · {r.text}</div>)}
            </details>
          )}
        </div>
      ))}
    </Sheet>
  );
}
