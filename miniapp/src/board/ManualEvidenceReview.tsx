import { useEffect, useState } from "react";
import { api, ApiError } from "../api";
import { t } from "../i18n";
import { useOffline, useQuery } from "../store";
import { errorText } from "../ui";
import type { ProjectTask } from "./board";
import type { ResultContract } from "./EvidenceReview";
import type { ResultReceipt } from "./ResultFlow";

type Criterion = { id: string; text: string; file_id?: string | null };
type Evidence = { evidence_id: string; criterion_id: string; observation: string;
  verification: "operator_attested" | "verified" | "failed" | "stale";
  manifest_digest_before: string | null; manifest_digest_after: string | null };
type Pending = { path: string; body: Record<string, unknown> };
type Draft = { criterion: string; artifact: string; observation: string; reason: string };

function stored<T>(key: string): T | null {
  try { return JSON.parse(sessionStorage.getItem(key) ?? "null") as T | null; }
  catch { return null; }
}

function keep(key: string, value: unknown | null) {
  try { if (value === null) sessionStorage.removeItem(key); else sessionStorage.setItem(key, JSON.stringify(value)); }
  catch { /* the mounted view retains the exact draft and request */ }
}

function covered(criterion: Criterion, evidence: Evidence[], artifacts: ResultReceipt["artifacts"]): boolean {
  return evidence.some((item) => item.criterion_id === criterion.id && (criterion.file_id
    ? item.verification === "verified" && !!item.manifest_digest_before && item.manifest_digest_before === item.manifest_digest_after &&
      artifacts.some((artifact) => artifact.file_id === criterion.file_id && artifact.digest === item.manifest_digest_before)
    : item.verification === "operator_attested"));
}

export function ManualEvidenceReview({ task, result, contract, blockingComments, toast, onChanged }: {
  task: ProjectTask; result: ResultReceipt; contract: ResultContract; blockingComments: number;
  toast: (message: string) => void; onChanged: () => void;
}) {
  const base = `/api/board/${encodeURIComponent(task.id)}/results/${encodeURIComponent(result.result_id)}`;
  const draftKey = `daedalus.manual.evidence.draft.${result.result_id}`;
  const pendingKey = `daedalus.manual.evidence.pending.${result.result_id}`;
  const [firstDraft] = useState(() => stored<Draft>(draftKey));
  const [criterion, setCriterion] = useState(firstDraft?.criterion ?? "");
  const [artifact, setArtifact] = useState(firstDraft?.artifact ?? "");
  const [observation, setObservation] = useState(firstDraft?.observation ?? "");
  const [reason, setReason] = useState(firstDraft?.reason ?? "");
  const [pending, setPending] = useState<Pending | null>(() => stored<Pending>(pendingKey));
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const offline = useOffline();
  const evidence = useQuery<Evidence[]>(`${base}/evidence`, { staleMs: 0 });
  const criteria: Criterion[] = [...(contract.checklist ?? []), ...(contract.requirements ?? [])];
  if (!criteria.length) criteria.push({ id: "completion", text: t("manual.completion") });
  const selected = criteria.find((item) => item.id === criterion) ?? criteria[0];
  const matching = (result.artifacts ?? []).filter((item) => item.file_id === selected.file_id);
  const selectedArtifact = matching.find((item) => item.id === artifact) ?? matching[0];
  const current = result.current_result_id === result.result_id && result.contract_revision === contract.contract_revision &&
    result.origin_kind === "operator_manual" && task.status === "review";
  const ready = !!evidence.data && criteria.every((item) => covered(item, evidence.data!, result.artifacts ?? []));
  const canVerdict = current && !offline && !busy && !pending && !evidence.error && ready && !blockingComments &&
    result.self_review_waiver_required === false && !!reason.trim();

  useEffect(() => { keep(draftKey, { criterion, artifact, observation, reason }); }, [draftKey, criterion, artifact, observation, reason]);

  function remember(value: Pending | null) { setPending(value); keep(pendingKey, value); }

  async function send(command: Pending) {
    if (offline || busy) return;
    setBusy(true);
    setMessage("");
    try {
      await api.post(command.path, command.body);
      remember(null);
      if (command.path.endsWith("/verdicts")) toast(t("result.verdictRecorded"));
      else { setObservation(""); toast(t("manual.attested")); }
      await evidence.refresh();
      onChanged();
    } catch (failure) {
      setMessage(errorText(failure));
      if (failure instanceof ApiError && failure.status === 409 &&
          !failure.message.includes("command identity was reused with a different request")) {
        remember(null);
        onChanged();
      }
    } finally { setBusy(false); }
  }

  function attest() {
    if (!current || !selected || !observation.trim() || observation.trim().length > 1000 || !contract.entity_revision || pending || busy || offline || evidence.error) return;
    if (selected.file_id && !selectedArtifact) return;
    const path = `${base}/${selected.file_id ? "evidence" : "attest"}`;
    const body = { criterion_id: selected.id, observation: observation.trim(),
      ...(selected.file_id ? { manifest_id: selectedArtifact!.id } : {}),
      expected_entity_revision: contract.entity_revision, client_operation_id: crypto.randomUUID() };
    const command = { path, body };
    remember(command);
    void send(command);
  }

  function verdict() {
    if (!canVerdict || !evidence.data) return;
    const body = { verification: "verified", accepted: true, head: null, base: null, environment_digest: null,
      evidence_ids: evidence.data.filter((item) => criteria.some((criterion) => criterion.id === item.criterion_id &&
        covered(criterion, [item], result.artifacts ?? []))).map((item) => item.evidence_id),
      reason: reason.trim(), expected_entity_revision: contract.entity_revision, client_operation_id: crypto.randomUUID() };
    const command = { path: `${base}/verdicts`, body };
    remember(command);
    void send(command);
  }

  return <details className="result-details"><summary>{t("manual.review")}</summary>
    <p className="sub">{t("manual.reviewHelp")}</p>
    {evidence.error && <div className="result-warning" role="status">{t("manual.evidenceUnavailable")} <button type="button" className="linkbtn" onClick={() => void evidence.refresh()}>{t("common.retry")}</button></div>}
    {!evidence.data && !evidence.error && <p className="sub">{t("common.loading")}</p>}
    <ul>{criteria.map((item) => <li key={item.id}>{item.text} · {t(covered(item, evidence.data ?? [], result.artifacts ?? []) ? "manual.covered" : item.file_id ? "manual.fileNeeded" : "manual.statementNeeded")}</li>)}</ul>
    <label className="field">{t("result.criterion")}<select className="field" value={selected.id} disabled={!!pending || busy} onChange={(event) => { setCriterion(event.target.value); setArtifact(""); }}>
      {criteria.map((item) => <option key={item.id} value={item.id}>{item.text}</option>)}
    </select></label>
    {selected.file_id && <><p className="result-warning">{t("manual.fileProof")}</p>
      {matching.length > 0 && <label className="field">{t("manual.attachedFile")}<select className="field" value={selectedArtifact?.id ?? ""} disabled={!!pending || busy} onChange={(event) => setArtifact(event.target.value)}>
        {matching.map((item) => <option key={item.id} value={item.id}>{item.artifact_key}</option>)}
      </select></label>}
      {!matching.length && <p className="result-warning" role="status">{t("manual.missingFile")}</p>}
    </>}
    <label className="field">{t("result.observation")}<textarea className="field" rows={3} maxLength={1000} value={observation} disabled={!!pending || busy} onChange={(event) => setObservation(event.target.value)} /></label>
    <button type="button" className="btn small" disabled={!current || offline || busy || !!pending || !observation.trim() || !!evidence.error || (!!selected.file_id && !selectedArtifact)} onClick={attest}>{t(selected.file_id ? "manual.recordFileProof" : "manual.recordStatement")}</button>
    <label className="field">{t("result.verdictReason")}<textarea className="field" rows={2} value={reason} disabled={!!pending || busy} onChange={(event) => setReason(event.target.value)} /></label>
    <button type="button" className="btn small primary" disabled={!canVerdict} onClick={verdict}>{t("result.recordVerdict")}</button>
    {!ready && <p className="result-warning" role="status">{t("manual.evidenceMissing")}</p>}
    {result.self_review_waiver_required && <p className="result-warning" role="status">{t("result.selfReviewBlocked")}</p>}
    {pending && <p className="result-warning" role="status">{t("manual.pending")} <button type="button" className="linkbtn" disabled={offline || busy} onClick={() => void send(pending)}>{t("common.retry")}</button></p>}
    {message && <p className="result-warning" role="alert">{message}</p>}
  </details>;
}
