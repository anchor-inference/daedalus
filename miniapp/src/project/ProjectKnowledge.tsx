import { useEffect, useState } from "react";
import { api, ApiError } from "../api";
import { t } from "../i18n";
import { useOffline, useQuery } from "../store";
import { errorText } from "../ui";

type Fact = { fact_id: string; version: number; claim: string; kind: string; status: string;
  source_kind: string; source_id: string; source_revision: string; source_digest: string;
  source_status?: "current" | "stale" | "missing"; actor: string; reason: string; created_at: string };
type Inspection = { project_id: string; entity_revision: number; collection_revision: number; facts: Fact[]; next_before: string | null };
type Source = { source_kind: "file" | "manifest"; source_id: string; label: string };
type Pending = { path: string; body: Record<string, unknown> };
type Draft = { claim: string; sourceId: string; reason: string; chosen: string; version: number | null };

function draft(key: string): Draft {
  try {
    const value = JSON.parse(sessionStorage.getItem(key) ?? "null");
    if (value && ["claim", "sourceId", "reason", "chosen"].every((field) => typeof value[field] === "string") &&
        value.claim.length <= 600 && value.reason.length <= 1000 && (value.version === null || Number.isInteger(value.version))) return value;
  } catch { /* a fresh draft remains available when browser storage is unavailable */ }
  return { claim: "", sourceId: "", reason: "", chosen: "", version: null };
}

function remembered(key: string, base: string): Pending | null {
  try {
    const value = JSON.parse(sessionStorage.getItem(key) ?? "null");
    const reviewPath = new RegExp(`^${base.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}/[^/]+/review$`);
    if (value?.path !== `${base}/candidates` && !(typeof value?.path === "string" && reviewPath.test(value.path))) return null;
    return value && typeof value.body?.client_operation_id === "string" ? value as Pending : null;
  } catch { return null; }
}

export function ProjectKnowledge({ projectId, toast }: { projectId: string; toast: (message: string) => void }) {
  const [open, setOpen] = useState(false);
  return <details className="sheet-section" onToggle={(event) => setOpen(event.currentTarget.open)}>
    <summary>{t("knowledge.title")}</summary>
    {open && <KnowledgeContent key={projectId} projectId={projectId} toast={toast} />}
  </details>;
}

function KnowledgeContent({ projectId, toast }: { projectId: string; toast: (message: string) => void }) {
  const base = `/api/projects/${encodeURIComponent(projectId)}/knowledge`;
  const pendingKey = `daedalus.knowledge.pending.${projectId}`;
  const draftKey = `daedalus.knowledge.draft.${projectId}`;
  const [openedDraft] = useState(() => draft(draftKey));
  const [before, setBefore] = useState<string | null>(null);
  const [chosen, setChosen] = useState(openedDraft.chosen);
  const [chosenVersion, setChosenVersion] = useState(openedDraft.version);
  const [createOpen, setCreateOpen] = useState(false);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [historyBefore, setHistoryBefore] = useState<number | null>(null);
  const [claim, setClaim] = useState(openedDraft.claim);
  const [sourceId, setSourceId] = useState(openedDraft.sourceId);
  const [reason, setReason] = useState(openedDraft.reason);
  const [busy, setBusy] = useState(false);
  const [pending, setPending] = useState<Pending | null>(() => remembered(pendingKey, base));
  const offline = useOffline();
  const inspection = useQuery<Inspection>(`${base}?limit=25${before ? `&before=${encodeURIComponent(before)}` : ""}`, { staleMs: 0 });
  const sources = useQuery<Source[]>(createOpen ? `${base}/sources` : null, { staleMs: 0 });
  const current = !inspection.error && inspection.data?.project_id === projectId ? inspection.data : null;
  const selected = current?.facts.find((fact) => fact.fact_id === chosen);
  const history = useQuery<Fact[]>(historyOpen && selected ? `${base}/${encodeURIComponent(selected.fact_id)}/history?limit=25${historyBefore ? `&before_version=${historyBefore}` : ""}` : null, { staleMs: 0 });
  const source = !sources.error ? sources.data?.find((item) => `${item.source_kind}:${item.source_id}` === sourceId) : null;
  const writable = !offline && !busy && !pending && !!current;
  const changed = !!selected && chosenVersion !== selected.version;
  const canReview = writable && !changed;

  useEffect(() => {
    try { sessionStorage.setItem(draftKey, JSON.stringify({ claim, sourceId, reason, chosen, version: chosenVersion })); }
    catch { /* the mounted view retains unsent text */ }
  }, [draftKey, claim, sourceId, reason, chosen, chosenVersion]);

  function remember(value: Pending | null) {
    setPending(value);
    try {
      if (value) sessionStorage.setItem(pendingKey, JSON.stringify(value));
      else sessionStorage.removeItem(pendingKey);
    } catch { /* the mounted view still preserves the exact request */ }
  }

  async function submit(intent: Pending) {
    if (busy || offline) return;
    setBusy(true);
    try {
      const response = await api.post<{ fact_id?: string; version?: number }>(intent.path, intent.body);
      remember(null);
      if (response.fact_id && Number.isInteger(response.version)) { setChosen(response.fact_id); setChosenVersion(response.version!); }
      setReason("");
      if (intent.path.endsWith("/candidates")) { setClaim(""); setSourceId(""); }
      toast(t("knowledge.recorded"));
      await inspection.refresh();
      if (historyOpen) await history.refresh();
    } catch (error) {
      if (error instanceof ApiError && [400, 403, 404, 409, 422].includes(error.status)) {
        remember(null);
        void inspection.refresh();
      }
      toast(errorText(error));
    } finally { setBusy(false); }
  }

  function create() {
    if (!writable || !source || !current || claim.trim().length < 12 || claim.trim().length > 600) return;
    const intent = { path: `${base}/candidates`, body: { claim: claim.trim(), kind: "fact", source_kind: source.source_kind,
      source_id: source.source_id, expected_collection_revision: current.collection_revision, client_operation_id: crypto.randomUUID() } };
    remember(intent);
    void submit(intent);
  }

  function review(verdict: "review" | "promote" | "invalidate" | "forget" | "rollback") {
    if (!canReview || !selected || !current || !reason.trim()) return;
    const intent = { path: `${base}/${encodeURIComponent(selected.fact_id)}/review`, body: { verdict, reason: reason.trim(),
      expected_version: selected.version, expected_entity_revision: current.entity_revision, client_operation_id: crypto.randomUUID() } };
    remember(intent);
    void submit(intent);
  }

  return <div className="project-extension-list">
    <p className="sub">{t("knowledge.intro")}</p>
    {offline && <p className="result-warning" role="status">{t("knowledge.offline")}</p>}
    {inspection.error && <div className="result-warning" role="status">{t("knowledge.readFailed")} <button type="button" className="linkbtn" onClick={() => void inspection.refresh()}>{t("common.retry")}</button></div>}
    {!current && !inspection.error && <p className="sub">{t("common.loading")}</p>}
    {pending && <div className="result-warning" role="status">{t("knowledge.pending")} <button type="button" className="linkbtn" disabled={offline || busy} onClick={() => void submit(pending)}>{t("knowledge.retry")}</button></div>}
    {current && <>
      {!current.facts.length && <p className="sub">{t("knowledge.empty")}</p>}
      <ul className="plain-list">{current.facts.map((fact) => <li key={fact.fact_id}>
        <button type="button" className="linkbtn" aria-pressed={chosen === fact.fact_id} onClick={() => { setChosen(fact.fact_id); setChosenVersion(fact.version); setReason(""); setHistoryOpen(false); setHistoryBefore(null); }}>{fact.claim}</button>
        <div className="sub">{t(`knowledge.status.${fact.status}`)} · {t("knowledge.version", { n: fact.version })}</div>
      </li>)}</ul>
      <div className="project-extension-head">
        {before && <button type="button" className="linkbtn" onClick={() => { setBefore(null); setChosen(""); }}>{t("knowledge.newest")}</button>}
        {current.next_before && <button type="button" className="linkbtn" onClick={() => { setBefore(current.next_before); setChosen(""); }}>{t("knowledge.older")}</button>}
      </div>
      {selected && <article className="project-extension">
        <b>{selected.claim}</b>
        {changed && <p className="result-warning" role="status">{t("knowledge.changed")}</p>}
        <p className={selected.source_status === "current" ? "sub" : "result-warning"}>{t(`knowledge.source.${selected.source_status ?? "missing"}`)}</p>
        <details><summary>{t("knowledge.provenance")}</summary>
          <p className="mono">{selected.source_kind}:{selected.source_id}@{selected.source_revision}</p>
          <p className="mono">{selected.source_digest}</p>
          <p>{selected.reason}</p>
        </details>
        <label className="field">{t("knowledge.reason")}<textarea className="field" value={reason} maxLength={1000} disabled={busy || !!pending} onChange={(event) => setReason(event.target.value)} /></label>
        <div className="project-extension-head">
          {selected.status === "candidate" && <button type="button" className="btn small" disabled={!canReview || !reason.trim() || selected.source_status !== "current"} onClick={() => review("review")}>{t("knowledge.review")}</button>}
          {selected.status === "reviewed" && <button type="button" className="btn small" disabled={!canReview || !reason.trim() || selected.source_status !== "current"} onClick={() => review("promote")}>{t("knowledge.promote")}</button>}
          {selected.status === "promoted" && <button type="button" className="btn small" disabled={!canReview || !reason.trim()} onClick={() => review("invalidate")}>{t("knowledge.invalidate")}</button>}
          {selected.status !== "forgotten" && <button type="button" className="linkbtn" disabled={!canReview || !reason.trim()} onClick={() => review("forget")}>{t("knowledge.forget")}</button>}
          {selected.version > 1 && <details><summary>{t("knowledge.recovery")}</summary><p className="sub">{t("knowledge.rollbackHelp")}</p><button type="button" className="linkbtn" disabled={!canReview || !reason.trim()} onClick={() => review("rollback")}>{t("knowledge.rollback")}</button></details>}
        </div>
        <details open={historyOpen} onToggle={(event) => setHistoryOpen(event.currentTarget.open)}><summary>{t("knowledge.history")}</summary>
          {history.error && <div className="result-warning">{t("knowledge.readFailed")} <button type="button" className="linkbtn" onClick={() => void history.refresh()}>{t("common.retry")}</button></div>}
          {!history.error && history.data?.map((fact) => <p key={fact.version}>{t("knowledge.version", { n: fact.version })} · {t(`knowledge.status.${fact.status}`)} · {fact.reason}</p>)}
          {historyBefore && <button type="button" className="linkbtn" onClick={() => setHistoryBefore(null)}>{t("knowledge.newestDecisions")}</button>}
          {!history.error && history.data?.length === 25 && history.data[0].version > 1 && <button type="button" className="linkbtn" onClick={() => setHistoryBefore(history.data![0].version)}>{t("knowledge.olderDecisions")}</button>}
        </details>
      </article>}
      <details onToggle={(event) => setCreateOpen(event.currentTarget.open)}><summary>{t("knowledge.add")}</summary>
        <label className="field">{t("knowledge.claim")}<textarea className="field" value={claim} maxLength={600} disabled={busy || !!pending} onChange={(event) => setClaim(event.target.value)} /></label>
        <label className="field">{t("knowledge.sourceLabel")}<select className="field" value={sourceId} disabled={!!sources.error || busy || !!pending} onChange={(event) => setSourceId(event.target.value)}>
          <option value="">{t("knowledge.chooseSource")}</option>
          {sources.data?.map((item) => <option key={`${item.source_kind}:${item.source_id}`} value={`${item.source_kind}:${item.source_id}`}>{item.label}</option>)}
        </select></label>
        {sources.error && <div className="result-warning">{t("knowledge.readFailed")} <button type="button" className="linkbtn" onClick={() => void sources.refresh()}>{t("common.retry")}</button></div>}
        {!sources.error && sources.data?.length === 0 && <p className="sub">{t("knowledge.noSources")}</p>}
        <button type="button" className="btn small" disabled={!writable || !source || claim.trim().length < 12} onClick={create}>{t("knowledge.create")}</button>
      </details>
    </>}
  </div>;
}
