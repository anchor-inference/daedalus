import { useMemo, useState } from "react";
import { api, MemoryListing, MemoryRecord } from "../api";
import { ChipRow, Skeleton } from "../components";
import { Sheet, deleteWithUndo } from "../dialogs";
import { absTime, relTime } from "../format";
import { Icon } from "../icons";
import { PageHeader, screenTitle } from "../shell";
import { hold, prime, release, useQuery } from "../store";
import { confirmAsync, errorText } from "../ui";
import { plural, t } from "../i18n";

const KINDS = ["fact", "decision", "preference", "reflection", "skill", "note"];

/** The kinds the app has a word for; a kind the agent invents is shown as it wrote it. */
const kindWord = (kind: string) => (KINDS.includes(kind) ? t(`memory.kind.${kind}`) : kind);

/** What the agent remembered — global and per session — to read, correct, add to and prune. */
export function MemoryScreen({ toast, onOpen }: { toast: (t: string) => void; onOpen: (id: string) => void }) {
  const key = "/api/memory";
  const { data, error, loading, refresh } = useQuery<MemoryListing>(key, { staleMs: 10000 });
  const [bucket, setBucket] = useState<string>("all");
  const [kind, setKind] = useState<string>("all");
  const [query, setQuery] = useState("");
  const [searching, setSearching] = useState(false);
  const [selecting, setSelecting] = useState(false);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [editing, setEditing] = useState<MemoryRecord | null>(null);
  const [adding, setAdding] = useState(false);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());

  const q = query.trim().toLowerCase();
  const all = data?.records ?? [];
  const kinds = useMemo(() => {
    const counts = new Map<string, number>();
    for (const r of all) counts.set(r.kind, (counts.get(r.kind) ?? 0) + 1);
    return [...counts.entries()].sort((a, b) => b[1] - a[1]);
  }, [all]);
  const records = useMemo(
    () => all.filter((r) => (bucket === "all" || `${r.scope}:${r.scope_key}` === bucket) && (kind === "all" || r.kind === kind) && (!q || r.text.toLowerCase().includes(q))),
    [all, bucket, kind, q],
  );
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

  const sessions = Object.entries(data?.sessions ?? {});
  return (
    <>
      <PageHeader
        title={screenTitle("memory")}
        subtitle={data ? (records.length === all.length ? plural("memory.count", all.length) : t("memory.count.of", { shown: records.length, total: all.length })) : undefined}
        actions={
          <>
            <button className={`iconbtn ${searching ? "on" : ""}`} onClick={() => { setSearching((v) => !v); if (searching) setQuery(""); }} title={t("common.search")} aria-label={t("common.search")} aria-pressed={searching}><Icon name="search" /></button>
            <button className={`iconbtn ${selecting ? "on" : ""}`} onClick={() => { setSelecting((v) => !v); setSelected(new Set()); }} title={t(selecting ? "memory.select.done" : "memory.select")} aria-label={t(selecting ? "memory.select.done" : "memory.select")} aria-pressed={selecting}><Icon name="check" /></button>
            <button className="iconbtn primary" onClick={() => setAdding(true)} title={t("memory.add")} aria-label={t("memory.add")}><Icon name="plus" /></button>
          </>
        }
      >
        {searching && <input className="field search" autoFocus placeholder={t("memory.search")} value={query} onChange={(e) => setQuery(e.target.value)} onKeyDown={(e) => { if (e.key === "Escape") { setQuery(""); setSearching(false); } }} aria-label={t("memory.search.label")} />}
        <ChipRow label={t("memory.kind")}>
          <button className="chip select" aria-pressed={kind === "all"} onClick={() => setKind("all")}>{t("common.all")} · {all.length}</button>
          {kinds.map(([k, n]) => (
            <button key={k} className="chip select" aria-pressed={kind === k} onClick={() => setKind(k)}>{kindWord(k)} · {n}</button>
          ))}
        </ChipRow>
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
        {error && !data && <div className="empty"><b>{t("memory.error")}</b><div>{error}</div><button className="btn" onClick={refresh}>{t("common.retry")}</button></div>}
        {data && records.length === 0 && (
          <div className="empty">
            <b>{t(all.length === 0 ? "memory.empty" : "memory.nomatch")}</b>
            {all.length === 0 && <div>{t("memory.empty.sub")}</div>}
          </div>
        )}
        <div className="memory-grid">
          {records.map((r) => {
            const long = r.text.length > 320 || r.text.split("\n").length > 5;
            const open = expanded.has(r.id);
            return (
              <div key={r.id} className={`erow memory-card ${selecting ? "selecting" : ""} ${selected.has(r.id) ? "selected" : ""}`} role={selecting ? "checkbox" : "button"} aria-checked={selecting ? selected.has(r.id) : undefined} tabIndex={0} onClick={() => (selecting ? toggle(r.id) : setEditing(r))} onKeyDown={(e) => { if (e.target !== e.currentTarget) return; if (e.key === "Enter" || e.key === " ") { e.preventDefault(); selecting ? toggle(r.id) : setEditing(r); } }}>
                {selecting && <input type="checkbox" className="memory-check" checked={selected.has(r.id)} onChange={() => toggle(r.id)} onClick={(e) => e.stopPropagation()} aria-label={t("memory.select.one")} />}
                <div className="erow-main">
                  <div className="erow-meta">
                    <span className={`chip kind-${r.kind}`}>{kindWord(r.kind)}</span>
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
                </div>
              </div>
            );
          })}
        </div>
      </div>
      {selecting && (
        <div className="bulkbar">
          <button className="btn ghost small" onClick={() => setSelected(new Set(records.every((r) => selected.has(r.id)) ? [] : records.map((r) => r.id)))}>
            {t(records.every((r) => selected.has(r.id)) ? "common.none" : "memory.allshown")}
          </button>
          <span className="sub">{t("memory.selected", { n: selected.size })}</span>
          <span className="grow" />
          <button className="btn small danger" disabled={selected.size === 0} onClick={forgetMany}><Icon name="trash" size={14} /> {t("memory.forget")} {selected.size || ""}</button>
        </div>
      )}
      {editing && <MemorySheet r={editing} sessions={sessions} onClose={() => setEditing(null)} onSaved={() => { setEditing(null); refresh(); }} onForget={() => forget([editing.id])} toast={toast} />}
      {adding && <MemorySheet sessions={sessions} onClose={() => setAdding(false)} onSaved={() => { setAdding(false); refresh(); }} toast={toast} />}
    </>
  );
}

function MemorySheet({ r, sessions, onClose, onSaved, onForget, toast }: { r?: MemoryRecord; sessions: [string, string][]; onClose: () => void; onSaved: () => void; onForget?: () => void; toast: (t: string) => void }) {
  const [text, setText] = useState(r?.text ?? "");
  const [kind, setKind] = useState(r?.kind ?? "fact");
  const [scope, setScope] = useState(r ? (r.scope === "global" ? "global" : `session:${r.scope_key}`) : "global");
  const [busy, setBusy] = useState(false);
  async function save() {
    setBusy(true);
    try {
      if (r) {
        await api.patch(`/api/memory/${encodeURIComponent(r.id)}`, { text: text.trim(), kind });
        toast(t("memory.updated"));
      } else {
        const res = await api.post<{ decision: string; record: MemoryRecord }>("/api/memory", { scope: scope === "global" ? "global" : "session", scope_key: scope === "global" ? "" : scope.slice(8), kind, text: text.trim() });
        toast(res.decision.toLowerCase() === "create" ? t("memory.remembered") : t("memory.similar", { decision: res.decision.toLowerCase() }));
      }
      onSaved();
    } catch (e) {
      toast(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Sheet title={t(r ? "memory.sheet.edit" : "memory.sheet.add")} onClose={onClose}>
      <label className="field">{t("memory.text")}</label>
      <textarea className="field" rows={6} autoFocus value={text} onChange={(e) => setText(e.target.value)} placeholder={t("memory.text.placeholder")} />
      <div className="grid2">
        <div>
          <label className="field">{t("memory.kind")}</label>
          <select className="field" value={kind} onChange={(e) => setKind(e.target.value)}>
            {[...new Set([...KINDS, kind])].map((k) => <option key={k} value={k}>{kindWord(k)}</option>)}
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
