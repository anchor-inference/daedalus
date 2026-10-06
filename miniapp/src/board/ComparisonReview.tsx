import { useRef, useState } from "react";
import { api } from "../api";
import { t } from "../i18n";
import { useOffline, useQuery } from "../store";
import { confirmAsync, errorText } from "../ui";
import type { ProjectTask } from "./board";
import { evidenceCoverage } from "./EvidenceReview";
import type { ResultReceipt } from "./ResultFlow";

type Blocker = { code: string; text: string };
type Artifact = { manifest_id: string; artifact_key: string; file_id: string | null; digest: string };
type SlotReview = { group_id: string; slot: number; attempt_id: string; result_id: string | null;
  verdict_id: string | null; head_sha: string | null; base_sha: string | null; verification: string;
  verdict_accepted: boolean; source_current: boolean; can_choose: boolean; blockers: Blocker[];
  self_review_waiver_required: boolean; artifacts: Artifact[]; physical_exit_verified: boolean;
  observed_cost_microusd: number | null; patch: string; patch_complete: boolean };
type Evidence = { evidence_id: string; criterion_id: string; observation: string; verification: "verified" | "failed" | "stale";
  manifest_digest_before: string | null; manifest_digest_after: string | null; observed_at: string };
type Comment = { comment_id: string; priority: "blocking" | "important" | "suggestion"; body: string; state: string;
  path: string | null; line_start: number | null; head: string | null };
type Contract = { contract_revision: number; entity_revision: number; checklist?: { id: string; text: string }[] };
type PendingChoice = { result_id: string; verdict_id: string; expected_entity_revision: number; client_operation_id: string };
type PendingStop = { client_operation_id: string; expected_entity_revision: number; reason: string };
const BLOCKER_CODES = new Set(["status", "branch", "merged", "dirty", "moved", "unknown", "conflicts",
  "result_missing", "result_incomplete", "verdict_missing", "verdict_stale", "comments", "exit_unknown", "cost_unknown"]);

function blockerText(code: string): string {
  return t(`pair.review.code.${BLOCKER_CODES.has(code) ? code : "other"}`);
}

function choiceKey(taskId: string, groupId: string, slot: number): string {
  return `daedalus.pair.choice.${taskId}.${groupId}.${slot}`;
}

function readChoice(key: string): PendingChoice | null {
  try {
    const value = JSON.parse(sessionStorage.getItem(key) ?? "null");
    return value && typeof value.client_operation_id === "string" && typeof value.result_id === "string" ? value as PendingChoice : null;
  } catch { return null; }
}

function saveChoice(key: string, value: PendingChoice | null): void {
  try { if (value) sessionStorage.setItem(key, JSON.stringify(value)); else sessionStorage.removeItem(key); }
  catch { /* the mounted intent remains available for retry */ }
}

export function ComparisonReview({ task, groupId, slot, candidate, contract, groupReady, attemptId, groupOpen, onChanged, toast }: {
  task: ProjectTask; groupId: string; slot: number; candidate: ResultReceipt | null; contract: Contract | null;
  groupReady: boolean; attemptId: string | null; groupOpen: boolean; onChanged: () => void; toast: (message: string) => void;
}) {
  const offline = useOffline();
  const key = choiceKey(task.id, groupId, slot);
  const [pendingChoice, setPendingChoice] = useState<PendingChoice | null>(() => readChoice(key));
  const stopKey = `daedalus.pair.stop.${task.id}.${groupId}.${slot}`;
  const [pendingStop, setPendingStop] = useState<PendingStop | null>(() => {
    try { return JSON.parse(sessionStorage.getItem(stopKey) ?? "null") as PendingStop | null; }
    catch { return null; }
  });
  const [stopEffectId, setStopEffectId] = useState<string | null>(() => {
    try { return sessionStorage.getItem(`${stopKey}.effect`); }
    catch { return null; }
  });
  const [busy, setBusy] = useState(false);
  const [warning, setWarning] = useState("");
  const [original, setOriginal] = useState<string | null>(null);
  const [criterion, setCriterion] = useState("");
  const [artifact, setArtifact] = useState("");
  const [observation, setObservation] = useState("");
  const [reason, setReason] = useState("");
  const [commentText, setCommentText] = useState("");
  const [commentPriority, setCommentPriority] = useState<Comment["priority"]>("suggestion");
  const commentOperation = useRef<{ fingerprint: string; id: string } | null>(null);
  const [resolving, setResolving] = useState("");
  const [resolutionReason, setResolutionReason] = useState("");
  const resolutionOperation = useRef<{ fingerprint: string; id: string } | null>(null);
  const evidenceOperation = useRef<{ fingerprint: string; id: string } | null>(null);
  const verdictOperation = useRef<{ fingerprint: string; id: string } | null>(null);
  const base = `/api/board/${encodeURIComponent(task.id)}`;
  const review = useQuery<SlotReview>(attemptId ? `${base}/comparisons/${encodeURIComponent(groupId)}/slots/${slot}/review` : null, { pollMs: 5000, staleMs: 0 });
  const stopEffect = useQuery<{ state: string }>(stopEffectId ? `/api/control/effects/${encodeURIComponent(stopEffectId)}` : null, { pollMs: 5000, staleMs: 0 });
  const evidence = useQuery<Evidence[]>(candidate ? `${base}/results/${encodeURIComponent(candidate.result_id)}/evidence` : null, { staleMs: 0 });
  const comments = useQuery<Comment[]>(candidate ? `${base}/results/${encodeURIComponent(candidate.result_id)}/comments` : null, { staleMs: 0 });
  const current = review.data?.group_id === groupId && review.data.slot === slot && review.data.result_id === candidate?.result_id &&
    review.data.attempt_id === (candidate as ResultReceipt & { attempt_id?: string })?.attempt_id &&
    candidate?.contract_revision === contract?.contract_revision;
  const checklist = contract?.checklist ?? [];
  const manifest = artifact || review.data?.artifacts.find((item) => item.file_id)?.manifest_id || "";
  const criterionId = criterion || checklist[0]?.id || "result";
  const covered = evidenceCoverage(checklist, evidence.data ?? []);
  const blockingComments = (comments.data ?? []).filter((item) => item.priority === "blocking" && ["open", "reopened"].includes(item.state));
  const canAttest = !offline && !busy && !!current && !!contract && !!manifest && !!observation.trim() && !review.error;
  const canVerdict = !offline && !busy && !!current && !!review.data?.source_current && !!candidate &&
    candidate.outcome === "complete" && !review.data.self_review_waiver_required && covered && !!reason.trim() &&
    !review.data.blockers.some((item) => item.code === "comments") && !!contract?.entity_revision && !evidence.error && !!comments.data && !comments.error && blockingComments.length === 0;
  const canChoose = !offline && !busy && !!current && !!review.data?.can_choose && !!candidate &&
    candidate.outcome === "complete" && !!review.data.verdict_id && review.data.verdict_accepted &&
    review.data.verification === "verified" && groupReady && !!contract?.entity_revision && !!comments.data && !comments.error && blockingComments.length === 0;

  async function loadOriginal() {
    if (!candidate || busy) return;
    setBusy(true);
    try {
      const response = await api.get<{ original_text: string }>(`${base}/results/${encodeURIComponent(candidate.result_id)}/original`);
      setOriginal(response.original_text);
    } catch (error) { setWarning(errorText(error)); }
    finally { setBusy(false); }
  }

  async function attest() {
    if (!canAttest || !candidate || !contract) return;
    const body = { criterion_id: criterionId, manifest_id: manifest, observation: observation.trim(),
      expected_entity_revision: contract.entity_revision };
    const fingerprint = JSON.stringify(body);
    if (evidenceOperation.current?.fingerprint !== fingerprint) evidenceOperation.current = { fingerprint, id: crypto.randomUUID() };
    setBusy(true);
    try {
      await api.post(`${base}/results/${encodeURIComponent(candidate.result_id)}/evidence`, { ...body, client_operation_id: evidenceOperation.current.id });
      evidenceOperation.current = null;
      setObservation("");
      await evidence.refresh();
      toast(t("pair.evidenceSaved"));
      onChanged();
    } catch (error) { setWarning(errorText(error)); }
    finally { setBusy(false); }
  }

  async function verdict() {
    if (!canVerdict || !candidate || !contract || !evidence.data) return;
    const body = { result_id: candidate.result_id, verification: "verified", accepted: true,
      evidence_ids: evidence.data.filter((item) => item.verification === "verified" &&
        item.manifest_digest_before && item.manifest_digest_before === item.manifest_digest_after).map((item) => item.evidence_id),
      reason: reason.trim(), expected_entity_revision: contract.entity_revision };
    const fingerprint = JSON.stringify(body);
    if (verdictOperation.current?.fingerprint !== fingerprint) verdictOperation.current = { fingerprint, id: crypto.randomUUID() };
    setBusy(true);
    try {
      await api.post(`${base}/comparisons/${encodeURIComponent(groupId)}/slots/${slot}/verdicts`,
        { ...body, client_operation_id: verdictOperation.current.id });
      verdictOperation.current = null;
      setReason("");
      review.refresh();
      toast(t("pair.verdictSaved"));
      onChanged();
    } catch (error) { setWarning(errorText(error)); }
    finally { setBusy(false); }
  }

  async function resolveComment(commentId: string) {
    if (!candidate || !contract || !resolutionReason.trim() || busy || offline) return;
    const body = { resolution: "resolved", reason: resolutionReason.trim(), expected_entity_revision: contract.entity_revision };
    const fingerprint = `${commentId}:${JSON.stringify(body)}`;
    if (resolutionOperation.current?.fingerprint !== fingerprint) resolutionOperation.current = { fingerprint, id: crypto.randomUUID() };
    setBusy(true);
    try {
      await api.post(`${base}/results/${encodeURIComponent(candidate.result_id)}/comments/${encodeURIComponent(commentId)}/resolve`,
        { ...body, client_operation_id: resolutionOperation.current.id });
      resolutionOperation.current = null;
      setResolving("");
      setResolutionReason("");
      comments.refresh();
      review.refresh();
      toast(t("result.commentUpdated"));
      onChanged();
    } catch (error) { setWarning(errorText(error)); }
    finally { setBusy(false); }
  }

  async function addComment() {
    if (!candidate || !contract || !commentText.trim() || busy || offline) return;
    const body = { priority: commentPriority, body: commentText.trim(), manifest_id: null,
      verdict_id: review.data?.verdict_id ?? null, path: null, head: null, line_start: null,
      expected_entity_revision: contract.entity_revision };
    const fingerprint = JSON.stringify(body);
    if (commentOperation.current?.fingerprint !== fingerprint) commentOperation.current = { fingerprint, id: crypto.randomUUID() };
    setBusy(true);
    try {
      await api.post(`${base}/results/${encodeURIComponent(candidate.result_id)}/comments`,
        { ...body, client_operation_id: commentOperation.current.id });
      commentOperation.current = null;
      setCommentText("");
      comments.refresh();
      review.refresh();
      toast(t("result.commentAdded"));
      onChanged();
    } catch (error) { setWarning(errorText(error)); }
    finally { setBusy(false); }
  }

  async function choose(intent?: PendingChoice) {
    if (busy || offline || (!intent && (!candidate || !review.data || !contract))) return;
    if (!intent) {
      if (!candidate || !review.data || !contract) return;
      if (!canChoose || !(await confirmAsync(t("pair.choose.title"), {
        body: t("pair.choose.body"), action: t("pair.choose") }))) return;
      intent = { client_operation_id: crypto.randomUUID(), expected_entity_revision: contract.entity_revision,
        result_id: candidate.result_id, verdict_id: review.data.verdict_id! };
      setPendingChoice(intent);
      saveChoice(key, intent);
    }
    setBusy(true);
    try {
      const receipt = await api.post<{ selection_receipt_id: string; selected_result_id: string }>(
        `${base}/comparisons/${encodeURIComponent(groupId)}/choose`, intent);
      if (!receipt.selection_receipt_id || receipt.selected_result_id !== intent.result_id) throw new Error(t("pair.badReceipt"));
      setPendingChoice(null);
      saveChoice(key, null);
      toast(t("pair.chosen"));
      onChanged();
    } catch (error) { setWarning(errorText(error)); }
    finally { setBusy(false); }
  }

  async function stop(intent?: PendingStop) {
    if (offline || busy || (!intent && (!groupOpen || !contract?.entity_revision || review.data?.physical_exit_verified))) return;
    if (!intent) {
      if (!(await confirmAsync(t("pair.stop.title"), { body: t("pair.stop.body"), action: t("pair.stop"), danger: true }))) return;
      intent = { client_operation_id: crypto.randomUUID(), expected_entity_revision: contract!.entity_revision, reason: t("pair.stop.reason") };
      setPendingStop(intent);
      try { sessionStorage.setItem(stopKey, JSON.stringify(intent)); } catch { /* keep mounted command */ }
    }
    setBusy(true);
    try {
      const receipt = await api.post<{ state: string; effect_id?: string }>(`${base}/comparisons/${encodeURIComponent(groupId)}/slots/${slot}/stop`, intent);
      if (!["cancelled_pending", "queued", "already_exited"].includes(receipt.state)) throw new Error(t("pair.badReceipt"));
      setPendingStop(null);
      try {
        sessionStorage.removeItem(stopKey);
        if (receipt.effect_id) sessionStorage.setItem(`${stopKey}.effect`, receipt.effect_id);
      } catch { /* no site data */ }
      setStopEffectId(receipt.effect_id ?? null);
      toast(t(receipt.state === "already_exited" ? "pair.stopEnded" : receipt.state === "cancelled_pending" ? "pair.stopCancelled" : "pair.stopQueued"));
      review.refresh();
      onChanged();
    } catch (error) { setWarning(errorText(error)); review.refresh(); }
    finally { setBusy(false); }
  }

  async function discardStop() {
    if (!pendingStop || !(await confirmAsync(t("pair.abandon.title"), { body: t("pair.stop.abandon.body"), action: t("pair.abandon") }))) return;
    setPendingStop(null);
    try { sessionStorage.removeItem(stopKey); } catch { /* no site data */ }
  }

  async function discardChoice() {
    if (!pendingChoice || !(await confirmAsync(t("pair.abandon.title"), { body: t("pair.choose.abandon.body"), action: t("pair.abandon") }))) return;
    setPendingChoice(null);
    saveChoice(key, null);
  }

  return <div className="project-extension-list">
    {pendingChoice && <div className="result-warning" role="status">{t("pair.chooseUnknown")} <button className="linkbtn" type="button" disabled={busy || offline} onClick={() => void choose(pendingChoice)}>{t("common.retry")}</button> <button className="linkbtn" type="button" disabled={busy} onClick={() => void discardChoice()}>{t("pair.abandon")}</button></div>}
    {(groupOpen && !review.data?.physical_exit_verified || pendingStop) && <div className="btnrow">
      <button type="button" className="btn small warn" disabled={offline || busy || (!pendingStop && !contract?.entity_revision)} onClick={() => void stop(pendingStop ?? undefined)}>{pendingStop ? t("pair.stopRetry") : t("pair.stop")}</button>
      <span className="sub">{t("pair.stopScope")}</span>
      {pendingStop && <button className="linkbtn" type="button" disabled={busy} onClick={() => void discardStop()}>{t("pair.abandon")}</button>}
    </div>}
    {stopEffectId && <div className="result-warning" role="status">{t(stopEffect.error || !stopEffect.data ? "pair.stopUnknown" : stopEffect.data.state === "completed" ? "pair.stopEnded" : stopEffect.data.state === "pending" || stopEffect.data.state === "claimed" ? "pair.stopQueued" : "pair.stopUnknown")}</div>}
    {review.error && <div className="result-warning" role="status">{t("pair.reviewUnavailable")} <button className="linkbtn" type="button" onClick={() => review.refresh()}>{t("common.retry")}</button></div>}
    {review.data && <>
      <div className="sub">{review.data.source_current ? t("pair.sourceCurrent") : t("pair.sourceUnknown")}</div>
      {review.data.blockers.length > 0 && <ul className="plain-list">{review.data.blockers.map((item) => <li className="result-warning" key={item.code}>{blockerText(item.code)}</li>)}</ul>}
      {candidate && <>
        <button className="linkbtn" type="button" disabled={busy} onClick={() => void loadOriginal()}>{t("result.original")}</button>
        {original !== null && <pre className="result-original">{original}</pre>}
        {review.data.patch && <details><summary>{t("pair.diff")}</summary><pre className="result-original">{review.data.patch}</pre>{!review.data.patch_complete && <div className="result-warning">{t("pair.diffPartial")}</div>}</details>}
        <details><summary>{t("result.annotations", { count: comments.data?.length ?? 0 })}</summary>
          {comments.error && <div className="result-warning">{t("pair.reviewUnavailable")}</div>}
          <ul>{(comments.data ?? []).map((item) => <li key={item.comment_id} className={item.priority === "blocking" && ["open", "reopened"].includes(item.state) ? "result-warning" : ""}>
            <b>{t(`result.priority.${item.priority}`)}</b> · {item.body} {item.path && <span className="mono">{item.path}{item.line_start ? `:${item.line_start}` : ""}</span>}
            {item.head && item.head !== review.data?.head_sha && <span className="chip tiny warn">{t("result.commentLocationStale")}</span>} · {t(`result.commentState.${item.state}`)}
            {["open", "reopened"].includes(item.state) && <button type="button" className="linkbtn" disabled={offline || busy} onClick={() => setResolving(item.comment_id)}>{t("result.resolve")}</button>}
            {resolving === item.comment_id && <div className="result-comment-form">
              <input className="field" aria-label={t("result.resolutionReason")} value={resolutionReason} onChange={(event) => setResolutionReason(event.target.value)} />
              <button type="button" className="btn small" disabled={!resolutionReason.trim() || offline || busy} onClick={() => void resolveComment(item.comment_id)}>{t("common.save")}</button>
            </div>}
          </li>)}</ul>
          <textarea className="field" rows={2} aria-label={t("result.comment")} value={commentText} onChange={(event) => setCommentText(event.target.value)} />
          <select className="field" aria-label={t("result.commentPriority")} value={commentPriority} onChange={(event) => setCommentPriority(event.target.value as Comment["priority"])}>
            {(["suggestion", "important", "blocking"] as const).map((priority) => <option key={priority} value={priority}>{t(`result.priority.${priority}`)}</option>)}
          </select>
          <button type="button" className="btn small" disabled={offline || busy || !current || !commentText.trim()} onClick={() => void addComment()}>{t("result.addComment")}</button>
        </details>
        <details><summary>{t("result.reviewEvidence")}</summary>
          <p className="sub">{t("result.manualEvidence")}</p>
          {(evidence.data ?? []).map((item) => <div className="sub" key={item.evidence_id}>{checklist.find((check) => check.id === item.criterion_id)?.text ?? item.criterion_id} · {item.observation}</div>)}
          {checklist.map((item) => <div className="sub" key={item.id}>{item.text} · {evidence.data?.some((row) => row.criterion_id === item.id && row.verification === "verified") ? t("result.covered") : t("result.uncovered")}</div>)}
          {evidence.error && <div className="result-warning">{t("pair.reviewUnavailable")}</div>}
          {checklist.length > 0 && <select className="field" aria-label={t("result.criterion")} value={criterionId} onChange={(event) => setCriterion(event.target.value)}>{checklist.map((item) => <option key={item.id} value={item.id}>{item.text}</option>)}</select>}
          <select className="field" aria-label={t("result.commentArtifact")} value={manifest} onChange={(event) => setArtifact(event.target.value)}>
            {!manifest && <option value="">{t("pair.chooseArtifact")}</option>}
            {review.data.artifacts.filter((item) => item.file_id).map((item) => <option key={item.manifest_id} value={item.manifest_id}>{item.artifact_key}</option>)}
          </select>
          <textarea className="field" rows={2} aria-label={t("result.observation")} value={observation} onChange={(event) => setObservation(event.target.value)} />
          <button className="btn small" type="button" disabled={!canAttest} onClick={() => void attest()}>{t("result.recordEvidence")}</button>
          <textarea className="field" rows={2} aria-label={t("result.verdictReason")} value={reason} onChange={(event) => setReason(event.target.value)} />
          {review.data.self_review_waiver_required && <div className="result-warning">{t("result.selfReviewBlocked")}</div>}
          {!covered && <div className="result-warning">{t("result.evidenceMissing")}</div>}
          <button className="btn small" type="button" disabled={!canVerdict} onClick={() => void verdict()}>{t("result.recordVerdict")}</button>
        </details>
      </>}
      <button className="btn small primary" type="button" disabled={!canChoose || !!pendingChoice} onClick={() => void choose()}>{t("pair.choose")}</button>
    </>}
    {warning && <div className="result-warning" role="status">{warning}</div>}
  </div>;
}
