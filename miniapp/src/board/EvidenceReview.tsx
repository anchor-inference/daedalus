// A reviewer records what they actually inspected against one immutable result. Each criterion needs
// bound evidence before a positive verdict; a manual observation is labelled as such, not as a test.
// A passing Verify run the worker recorded on the exact reviewed commit can stand in for a criterion
// with one click, so the operator does not retype what a test already proved.

import { useRef, useState } from "react";
import { api, ApiError } from "../api";
import { Icon } from "../icons";
import { t } from "../i18n";
import { useOffline, useQuery } from "../store";
import { errorText } from "../ui";
import type { ProjectTask, Review, ReviewReceipt } from "./board";
import type { ResultReceipt } from "./ResultFlow";

export type ResultContract = { task_id: string; contract_revision: number; entity_revision: number;
  checklist?: { id: string; text: string }[]; requirements?: { id: string; text: string; file_id?: string | null }[] };
type Evidence = { evidence_id: string; criterion_id: string; observation: string; verification: "verified" | "failed" | "stale"; manifest_digest_before: string | null; manifest_digest_after: string | null; observed_at: string };

const CHECK_TREE = "tree:";

/** Evidence from a check names the commit it ran on; once the branch moves on it no longer covers the merge. */
export function evidenceCurrent(row: Pick<Evidence, "manifest_digest_before">, head: string | null): boolean {
  if (!row.manifest_digest_before?.startsWith(CHECK_TREE)) return true;
  return !!head && row.manifest_digest_before === CHECK_TREE + head;
}

export function evidenceCoverage(checklist: { id: string }[], evidence: Evidence[], head: string | null = null): boolean {
  const usable = evidence.filter((row) => row.verification === "verified" && evidenceCurrent(row, head));
  const valid = new Set(usable.filter((row) => !!row.manifest_digest_before && row.manifest_digest_before === row.manifest_digest_after).map((row) => row.criterion_id));
  return usable.length > 0 && checklist.every((item) => valid.has(item.id));
}

/** The worker's passing checks that ran on exactly the reviewed commit with a clean tree; only these can count. */
export function usableChecks(receipts: ReviewReceipt[], head: string | null | undefined): ReviewReceipt[] {
  if (!head) return [];
  return receipts.filter((receipt) => receipt.id != null && receipt.passed && receipt.exit_code === 0 && receipt.tree === head);
}

/** The criterion a check is offered for first: the first one still without verified evidence. */
export function firstUncovered(checklist: { id: string }[], evidence: Evidence[], head: string | null): string {
  const covered = new Set(evidence.filter((row) => row.verification === "verified" && evidenceCurrent(row, head)).map((row) => row.criterion_id));
  return (checklist.find((item) => !covered.has(item.id)) ?? checklist[0])?.id ?? "result";
}

export function EvidenceReview({ task, result, contract, review, blockingComments, originalAvailable, toast, onChanged }: { task: ProjectTask; result: ResultReceipt; contract: ResultContract; review: Review | null; blockingComments: number; originalAvailable: boolean; toast: (message: string) => void; onChanged: () => void }) {
  const offline = useOffline();
  const base = `/api/board/${encodeURIComponent(task.id)}/results/${encodeURIComponent(result.result_id)}`;
  const evidence = useQuery<Evidence[]>(`${base}/evidence`, { staleMs: 2000 });
  const [criterion, setCriterion] = useState("");
  const [artifact, setArtifact] = useState("");
  const [observation, setObservation] = useState("");
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [choosing, setChoosing] = useState<number | null>(null);
  const [checkCriterion, setCheckCriterion] = useState("");
  const evidenceOperation = useRef<{ fingerprint: string; id: string } | null>(null);
  const checkOperation = useRef<{ fingerprint: string; id: string } | null>(null);
  const verdictOperation = useRef<{ fingerprint: string; id: string } | null>(null);
  const checks = contract.checklist ?? [];
  const selectedCriterion = criterion || checks[0]?.id || "result";
  const selectedArtifact = artifact || result.artifacts?.[0]?.id || "";
  const current = result.current_result_id === result.result_id && result.contract_revision === contract.contract_revision;
  const branchCurrent = !task.branch || !result.verdict_id || (!!review?.head_sha && !!review.base_sha && review.head_sha === result.verdict_head && review.base_sha === result.verdict_base);
  const head = task.branch ? review?.head_sha ?? null : null;
  const evidenceReady = !!evidence.data && evidenceCoverage(checks, evidence.data, head);
  const usable = usableChecks(review?.receipts ?? [], head);
  const otherCommit = (review?.receipts ?? []).some((receipt) => receipt.passed && receipt.id != null && !usable.includes(receipt));
  const canVerdict = !offline && !busy && current && originalAvailable && result.self_review_waiver_required !== true && !!evidence.data && evidenceReady && blockingComments === 0 && !!reason.trim() && (!task.branch || !!review?.head_sha && !!review.base_sha);

  async function attest() {
    if (offline || busy || !current || !selectedArtifact || !observation.trim()) return;
    const body = { criterion_id: selectedCriterion, manifest_id: selectedArtifact, observation: observation.trim(), expected_entity_revision: contract.entity_revision };
    const fingerprint = JSON.stringify(body);
    if (evidenceOperation.current?.fingerprint !== fingerprint) evidenceOperation.current = { fingerprint, id: crypto.randomUUID() };
    setBusy(true);
    try {
      await api.post(`${base}/evidence`, { ...body, client_operation_id: evidenceOperation.current.id });
      evidenceOperation.current = null;
      setObservation("");
      toast(t("result.evidenceRecorded"));
      await evidence.refresh();
      onChanged();
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) evidenceOperation.current = null;
      toast(errorText(error));
      onChanged();
    } finally { setBusy(false); }
  }

  async function attachCheck(receipt: ReviewReceipt, criterionId: string) {
    if (offline || busy || !current || receipt.id == null) return;
    const body = { verification_id: receipt.id, criterion_id: criterionId, expected_entity_revision: contract.entity_revision };
    const fingerprint = JSON.stringify(body);
    if (checkOperation.current?.fingerprint !== fingerprint) checkOperation.current = { fingerprint, id: crypto.randomUUID() };
    setBusy(true);
    try {
      await api.post(`${base}/evidence/check`, { ...body, client_operation_id: checkOperation.current.id });
      checkOperation.current = null;
      setChoosing(null);
      toast(t("result.checks.recorded"));
      await evidence.refresh();
      onChanged();
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) checkOperation.current = null;
      toast(errorText(error));
      onChanged();
    } finally { setBusy(false); }
  }

  function offerCheck(receipt: ReviewReceipt) {
    // With one criterion (or none) there is nothing to choose, so the click attaches at once.
    if (checks.length <= 1) { void attachCheck(receipt, checks[0]?.id ?? "result"); return; }
    setCheckCriterion(firstUncovered(checks, evidence.data ?? [], head));
    setChoosing(choosing === receipt.id ? null : receipt.id ?? null);
  }

  async function verdict() {
    if (!canVerdict || !evidence.data) return;
    const body = { verification: "verified", accepted: true, head: task.branch ? review?.head_sha : null, base: task.branch ? review?.base_sha : null, environment_digest: null, evidence_ids: evidence.data.filter((row) => row.verification === "verified" && evidenceCurrent(row, head)).map((row) => row.evidence_id), reason: reason.trim(), expected_entity_revision: contract.entity_revision };
    const fingerprint = JSON.stringify(body);
    if (verdictOperation.current?.fingerprint !== fingerprint) verdictOperation.current = { fingerprint, id: crypto.randomUUID() };
    setBusy(true);
    try {
      await api.post(`${base}/verdicts`, { ...body, client_operation_id: verdictOperation.current.id });
      verdictOperation.current = null;
      toast(t("result.verdictRecorded"));
      onChanged();
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) verdictOperation.current = null;
      toast(errorText(error));
      onChanged();
    } finally { setBusy(false); }
  }

  return <details className="result-details" open>
    <summary>{t("result.reviewEvidence")}</summary>
    <p className="sub">{t("result.manualEvidence")}</p>
    {evidence.error && <div className="result-warning" role="status">{t("result.block.unconfirmed")} <button type="button" className="linkbtn" onClick={() => evidence.refresh()}>{t("common.retry")}</button></div>}
    <ul>{(evidence.data ?? []).map((row) => <li key={row.evidence_id}>{checks.find((check) => check.id === row.criterion_id)?.text ?? row.criterion_id} · {row.observation} · {t(`result.verification.${row.verification}`)}</li>)}</ul>
    {checks.length > 0 && <ul>{checks.map((check) => <li key={check.id}>{check.text} · {(evidence.data ?? []).some((row) => row.criterion_id === check.id && row.verification === "verified" && evidenceCurrent(row, head)) ? t("result.covered") : t("result.uncovered")}</li>)}</ul>}
    {(usable.length > 0 || otherCommit) && <div className="result-checks" aria-label={t("result.checks.title")}>
      <p className="sub">{t("result.checks.title")}</p>
      <ul>{usable.map((receipt) => <li key={receipt.id}>
        <Icon name="check" size={12} /> {receipt.criterion || t("result.checks.unnamed")}{" "}
        <button type="button" className="btn small" disabled={offline || busy || !current} onClick={() => offerCheck(receipt)}>{t("result.checks.use")}</button>
        {choosing === receipt.id && <div className="result-comment-form">
          <select className="field" aria-label={t("result.checks.for")} value={checkCriterion} onChange={(event) => setCheckCriterion(event.target.value)}>{checks.map((check) => <option key={check.id} value={check.id}>{check.text}</option>)}</select>
          <button type="button" className="btn small primary" disabled={offline || busy || !current || !checkCriterion} onClick={() => void attachCheck(receipt, checkCriterion)}>{t("result.checks.attach")}</button>
        </div>}
        <details className="result-check-details"><summary className="sub">{t("result.checks.details")}</summary>
          <div className="mono sub">{receipt.command}</div>
          <div className="mono sub">{t("result.checks.commit")}: {receipt.tree}</div>
        </details>
      </li>)}</ul>
      {otherCommit && <p className="sub">{t("result.checks.otherCommit")}</p>}
    </div>}
    <div className="result-comment-form">
      {checks.length > 0 && <select className="field" aria-label={t("result.criterion")} value={selectedCriterion} onChange={(event) => setCriterion(event.target.value)}>{checks.map((check) => <option key={check.id} value={check.id}>{check.text}</option>)}</select>}
      {(result.artifacts ?? []).length > 0 && <select className="field" aria-label={t("result.commentArtifact")} value={selectedArtifact} onChange={(event) => setArtifact(event.target.value)}>{result.artifacts.map((item) => <option key={item.id} value={item.id}>{item.artifact_key}</option>)}</select>}
      <textarea className="field" rows={3} value={observation} onChange={(event) => setObservation(event.target.value)} aria-label={t("result.observation")} placeholder={t("result.observationHint")} />
      <button type="button" className="btn small" disabled={offline || busy || !current || !selectedArtifact || !observation.trim()} onClick={() => void attest()}>{t("result.recordEvidence")}</button>
    </div>
    <div className="result-comment-form">
      <label htmlFor={`verdict-${task.id}`}>{t("result.verdictReason")}</label>
      <textarea id={`verdict-${task.id}`} className="field" rows={2} value={reason} onChange={(event) => setReason(event.target.value)} />
      {result.self_review_waiver_required && <div className="result-warning" role="status">{t("result.selfReviewBlocked")}</div>}
      <button type="button" className="btn small primary" disabled={!canVerdict} onClick={() => void verdict()}>{t("result.recordVerdict")}</button>
      {!originalAvailable && <div className="result-warning" role="status">{t("result.originalRequiredForReview")}</div>}
      {!evidenceReady && <div className="result-warning" role="status">{t("result.evidenceMissing")}</div>}
      {blockingComments > 0 && <div className="result-warning" role="status">{t("result.block.comments")}</div>}
      {!branchCurrent && <div className="result-warning" role="status">{t("result.block.changedBranch")}</div>}
    </div>
  </details>;
}
