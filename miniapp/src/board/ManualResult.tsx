import { useEffect, useState } from "react";
import { api, ApiError } from "../api";
import { t } from "../i18n";
import { useOffline, useQuery } from "../store";
import { errorText } from "../ui";
import type { ProjectTask } from "./board";
import type { ResultContract } from "./EvidenceReview";
import type { ResultReceipt } from "./ResultFlow";

type Attached = { manifest_id: string | null; file_id: string; label: string; digest: string; size_bytes: number };
type Readiness = { eligible: boolean; blockers: string[]; entity_revision: number; contract_revision: number; attached_artifacts: Attached[] };
type Intent = { path: string; body: Record<string, unknown> };
type Draft = { original: string; selected: string[] };

function stored<T>(key: string): T | null {
  try { return JSON.parse(sessionStorage.getItem(key) ?? "null") as T | null; }
  catch { return null; }
}

function keep(key: string, value: unknown | null) {
  try { if (value === null) sessionStorage.removeItem(key); else sessionStorage.setItem(key, JSON.stringify(value)); }
  catch { /* the mounted view retains its exact draft and request */ }
}

function blocker(code: string): string {
  return t(`manual.block.${["status", "branch", "attempt_bound", "execution_owned", "dependencies", "missing_contract"].includes(code) ? code : "other"}`);
}

export function ManualResult({ task, toast, onChanged }: { task: ProjectTask; toast: (message: string) => void; onChanged: () => void }) {
  const [open, setOpen] = useState(false);
  return <section className="task-workflow">
    <button type="button" className="pboard-fold section-title" aria-expanded={open} onClick={() => setOpen(!open)}>{t("manual.title")}</button>
    {open && <ManualContent task={task} toast={toast} onChanged={onChanged} />}
  </section>;
}

function ManualContent({ task, toast, onChanged }: { task: ProjectTask; toast: (message: string) => void; onChanged: () => void }) {
  const base = `/api/board/${encodeURIComponent(task.id)}`;
  const draftKey = `daedalus.manual.result.draft.${task.id}`;
  const pendingKey = `daedalus.manual.result.pending.${task.id}`;
  const [initial] = useState(() => stored<Draft>(draftKey));
  const [original, setOriginal] = useState(initial?.original ?? "");
  const [selected, setSelected] = useState<string[]>(initial?.selected ?? []);
  const [pending, setPending] = useState<Intent | null>(() => stored<Intent>(pendingKey));
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [revising, setRevising] = useState(false);
  const offline = useOffline();
  const readiness = useQuery<Readiness>(`${base}/manual-review`, { staleMs: 0 });
  const contract = useQuery<ResultContract>(`${base}/contract`, { staleMs: 0 });
  const results = useQuery<ResultReceipt[]>(`${base}/results`, { staleMs: 0 });
  const current = (results.data ?? []).find((item) => item.current_result_id === item.result_id);
  const canRevise = current?.origin_kind === "operator_manual" && current.verdict_accepted !== true && task.status === "review";
  const files = readiness.data?.attached_artifacts ?? [];
  const requiredFiles = (contract.data?.requirements ?? []).filter((item) => !!item.file_id);
  const requiredIds = new Set(requiredFiles.map((item) => item.file_id));
  const chosen = files.filter((item) => item.manifest_id && selected.includes(item.manifest_id));
  const missingFile = requiredFiles.some((item) => !chosen.some((file) => file.file_id === item.file_id));
  const available = !offline && !busy && !pending && !readiness.error && !contract.error && !results.error &&
    readiness.data?.eligible === true && readiness.data.contract_revision === contract.data?.contract_revision &&
    readiness.data.entity_revision === contract.data?.entity_revision && (!current || canRevise && revising) && !missingFile;

  useEffect(() => keep(draftKey, { original, selected }), [draftKey, original, selected]);

  function remember(value: Intent | null) { setPending(value); keep(pendingKey, value); }

  async function send(intent: Intent) {
    if (offline || busy) return;
    setBusy(true);
    setMessage("");
    try {
      await api.post(intent.path, intent.body);
      remember(null);
      if (intent.path.endsWith("/artifacts")) {
        toast(t("manual.fileRegistered"));
        await readiness.refresh();
        await contract.refresh();
      } else {
        setOriginal("");
        setRevising(false);
        toast(t("manual.submitted"));
        await results.refresh();
        onChanged();
      }
    } catch (failure) {
      setMessage(errorText(failure));
      if (failure instanceof ApiError && failure.status === 409 &&
          !failure.message.includes("command identity was reused with a different request")) {
        remember(null);
        void readiness.refresh();
        void contract.refresh();
        void results.refresh();
      }
    } finally { setBusy(false); }
  }

  function register(file: Attached) {
    if (offline || busy || pending || !readiness.data || !Number.isInteger(readiness.data.entity_revision) || file.manifest_id) return;
    const intent = { path: `${base}/artifacts`, body: { artifact_kind: "other", artifact_key: `operator-file-${file.file_id}`,
      artifact_revision: 1, digest: file.digest, size_bytes: file.size_bytes, file_id: file.file_id,
      expected_entity_revision: readiness.data.entity_revision, client_operation_id: crypto.randomUUID() } };
    remember(intent);
    void send(intent);
  }

  function submit() {
    if (!available || original.trim().length < 8 || !readiness.data) return;
    const intent = { path: `${base}/results`, body: { client_operation_id: crypto.randomUUID(),
      expected_entity_revision: readiness.data.entity_revision, contract_revision: readiness.data.contract_revision,
      outcome: "complete", original_text: original.trim(), manifest_ids: chosen.map((item) => item.manifest_id!) } };
    remember(intent);
    void send(intent);
  }

  return <div className="task-workflow-body">
    <p className="sub">{t("manual.intro")}</p>
    {readiness.error || contract.error || results.error ? <div className="result-warning" role="status">{t("manual.unavailable")} <button type="button" className="linkbtn" onClick={() => { void readiness.refresh(); void contract.refresh(); void results.refresh(); }}>{t("common.retry")}</button></div> : null}
    {!readiness.data && !readiness.error && <p className="sub">{t("common.loading")}</p>}
    {current && <p className="sub">{t(canRevise ? "manual.reviseHelp" : "manual.currentResult")}</p>}
    {canRevise && <button type="button" className="linkbtn" disabled={busy || !!pending} onClick={() => setRevising((value) => !value)}>{t(revising ? "manual.cancelRevision" : "manual.revise")}</button>}
    {!!readiness.data?.blockers.length && <ul className="result-warning">{readiness.data.blockers.map((code) => <li key={code}>{blocker(code)}</li>)}</ul>}
    <label className="field">{t("manual.report")}<textarea className="field" rows={5} maxLength={100000} value={original} disabled={busy || !!pending} onChange={(event) => setOriginal(event.target.value)} placeholder={t("manual.reportHint")} /></label>
    {!!requiredFiles.length && <div className="project-extension">
      <b>{t("manual.requiredFiles")}</b><p className="sub">{t("manual.requiredFilesHelp")}</p>
      {requiredFiles.map((requirement) => <div key={requirement.id}>
        <p>{requirement.text}</p>
        {files.filter((item) => item.file_id === requirement.file_id).map((file) => <div className="btnrow" key={`${file.file_id}:${file.manifest_id ?? "new"}`}>
          {file.manifest_id ? <label><input type="checkbox" checked={selected.includes(file.manifest_id)} disabled={busy || !!pending} onChange={(event) => setSelected((prior) => event.target.checked ? [...prior, file.manifest_id!] : prior.filter((id) => id !== file.manifest_id))} /> {file.label}</label>
            : <button type="button" className="linkbtn" disabled={offline || busy || !!pending || !readiness.data?.eligible} onClick={() => register(file)}>{t("manual.registerFile", { name: file.label })}</button>}
        </div>)}
        {!files.some((item) => item.file_id === requirement.file_id) && <p className="result-warning">{t("manual.fileNotAttached")}</p>}
      </div>)}
    </div>}
    <button type="button" className="btn small primary" disabled={!available || original.trim().length < 8} onClick={submit}>{t(revising ? "manual.submitRevision" : "manual.submit")}</button>
    {missingFile && <p className="result-warning" role="status">{t("manual.chooseFiles")}</p>}
    {pending && <p className="result-warning" role="status">{t("manual.pending")} <button type="button" className="linkbtn" disabled={offline || busy} onClick={() => void send(pending)}>{t("common.retry")}</button></p>}
    {message && <p className="result-warning" role="alert">{message}</p>}
    {offline && <p className="result-warning" role="status">{t("manual.offline")}</p>}
    {!!requiredIds.size && <p className="sub">{t("manual.fileAuthority")}</p>}
  </div>;
}
