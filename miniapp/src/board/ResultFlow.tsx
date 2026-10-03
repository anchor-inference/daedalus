// A task's result is one immutable candidate with its own verification and acceptance. The exact
// result, verdict, contract and current branch must agree before the operator can accept it.

import { useRef, useState } from "react";
import { api, ApiError } from "../api";
import { absTime } from "../format";
import { t } from "../i18n";
import { navigate, projectSessionPath, sessionPath } from "../router";
import { useOffline, useQuery, invalidate } from "../store";
import { errorText } from "../ui";
import { mergeBlock, type ProjectTask, type Review } from "./board";
import { reviewKey } from "./ReviewPanel";
import { EvidenceReview, type ResultContract } from "./EvidenceReview";
import { ResultTransfer } from "./ResultTransfer";
import { ManualEvidenceReview } from "./ManualEvidenceReview";
import { ManualReopen } from "./ManualReopen";

export type ResultReceipt = {
  result_id: string;
  task_id: string;
  contract_revision: number;
  outcome: string;
  origin_kind?: "operator_manual" | "worker";
  original_preview: string;
  original_digest: string;
  original_size_bytes: number;
  artifacts: { id: string; artifact_kind: string; artifact_key: string; artifact_revision: number; digest: string; size_bytes: number; file_id?: string | null }[];
  checks: unknown[];
  limitations: unknown[];
  verification: "verified" | "failed" | "stale" | "unverified";
  verdict_id: string | null;
  verdict_accepted?: boolean | null;
  self_review_waiver_required?: boolean;
  verdict_head?: string | null;
  verdict_base?: string | null;
  current_result_id?: string | null;
  acceptance_state: string;
  accepted: boolean;
  created_at: string;
};

type Contract = ResultContract;
type Comment = { comment_id: string; result_id: string; priority: "blocking" | "important" | "suggestion"; body: string; manifest_id: string | null; path: string | null; head: string | null; line_start: number | null; line_end: number | null; state: string; created_at: string };
type SourceTurn = { session_id: string; turn_seq: number; source_ref: string; source_digest: string; source_current: boolean };

/** A missing source field is a blocker, never an invitation to infer readiness from the card status. */
export function acceptanceBlock(task: ProjectTask, result: ResultReceipt | null, contract: Contract | null, review: Review | null, uncertain: boolean): string | null {
  if (uncertain) return "unconfirmed";
  if (!result) return "noResult";
  if (!contract || !Number.isInteger(contract.entity_revision)) return "noContract";
  if (result.current_result_id !== result.result_id) return "changedResult";
  if (result.contract_revision !== contract.contract_revision) return "changedContract";
  if (task.acceptance_state !== "accepted") return "coordinator";
  if (result.outcome !== "complete") return "partial";
  if (result.verification !== "verified" || result.verdict_accepted !== true || !result.verdict_id) return "unverified";
  if (task.branch) {
    if (!review || review.merge_receipt?.state !== "merged") return "branch";
    if (!result.verdict_head || !result.verdict_base || !review.merge_receipt.merge_sha || review.current_sha !== review.merge_receipt.merge_sha || review.merge_receipt.result_id !== result.result_id || review.merge_receipt.verdict_id !== result.verdict_id || review.merge_receipt.head_sha !== result.verdict_head || review.merge_receipt.base_sha !== result.verdict_base) return "changedBranch";
  } else if (result.verdict_head !== null || result.verdict_base !== null) return "changedBranch";
  return null;
}

export function mergeGuard(task: ProjectTask, result: ResultReceipt | null, contract: Contract | null, review: Review | null, uncertain: boolean): string | null {
  if (uncertain) return "unconfirmed";
  if (!task.branch || !review || !result || !contract || !Number.isInteger(contract.entity_revision)) return "noResult";
  if (result.current_result_id !== result.result_id || result.contract_revision !== contract.contract_revision) return "changedResult";
  if (task.acceptance_state !== "accepted") return "coordinator";
  if (result.outcome !== "complete" || result.verification !== "verified" || result.verdict_accepted !== true || !result.verdict_id) return "unverified";
  if (!result.verdict_head || !result.verdict_base || review.head_sha !== result.verdict_head || review.base_sha !== result.verdict_base) return "changedBranch";
  if (review.merge_receipt?.state === "queued") return "mergeQueued";
  if (review.blockers.length || mergeBlock(review)) return "branch";
  return null;
}

function detail(value: unknown): string {
  if (typeof value === "string") return value;
  if (value && typeof value === "object") {
    const row = value as Record<string, unknown>;
    return [row.criterion, row.text, row.result, row.reason].filter((part): part is string => typeof part === "string" && !!part).join(" · ") || JSON.stringify(value);
  }
  return String(value);
}

export function ResultFlow({ task, onAccepted, toast }: { task: ProjectTask; onAccepted: () => void; toast: (text: string) => void }) {
  const base = `/api/board/${encodeURIComponent(task.id)}`;
  const results = useQuery<ResultReceipt[]>(`${base}/results`, { staleMs: 2000 });
  const contract = useQuery<Contract>(`${base}/contract`, { staleMs: 2000 });
  const review = useQuery<Review>(task.branch ? reviewKey(task.id) : null, { staleMs: 2000, pollMs: task.status === "review" ? 5000 : 30000 });
  const offline = useOffline();
  const [original, setOriginal] = useState<{ resultId: string; text: string } | null>(null);
  const [originalError, setOriginalError] = useState("");
  const [loadingOriginal, setLoadingOriginal] = useState(false);
  const [sourceTurns, setSourceTurns] = useState<{ resultId: string; turns: SourceTurn[] } | null>(null);
  const [sourceError, setSourceError] = useState<{ resultId: string; text: string } | null>(null);
  const [loadingSources, setLoadingSources] = useState(false);
  const [evidenceOpen, setEvidenceOpen] = useState(false);
  const [comparedId, setComparedId] = useState("");
  const [busy, setBusy] = useState(false);
  const [returning, setReturning] = useState(false);
  const [returnNote, setReturnNote] = useState("");
  const [commentText, setCommentText] = useState("");
  const [commentPriority, setCommentPriority] = useState<Comment["priority"]>("suggestion");
  const [commentArtifact, setCommentArtifact] = useState("");
  const [commentPath, setCommentPath] = useState("");
  const [commentLine, setCommentLine] = useState("");
  const [resolving, setResolving] = useState<string | null>(null);
  const [resolution, setResolution] = useState<"resolved" | "waived">("resolved");
  const [resolutionReason, setResolutionReason] = useState("");
  const operation = useRef<string | null>(null);
  const mergeOperation = useRef<string | null>(null);
  const returnOperation = useRef<{ fingerprint: string; id: string } | null>(null);
  const commentOperation = useRef<{ fingerprint: string; id: string } | null>(null);
  const resolutionOperation = useRef<{ fingerprint: string; id: string } | null>(null);
  const list = Array.isArray(results.data) ? results.data : [];
  const currentId = list[0]?.current_result_id;
  const result = list.find((item) => item.result_id === currentId) ?? list[0] ?? null;
  const earlier = list.filter((item) => item.result_id !== result?.result_id);
  const compared = earlier.find((item) => item.result_id === comparedId) ?? earlier[0] ?? null;
  const comments = useQuery<Comment[]>(result ? `${base}/results/${encodeURIComponent(result.result_id)}/comments` : null, { staleMs: 2000 });
  const uncertain = offline || !!results.error || !!contract.error || (task.branch ? !!review.error : false) || !results.data || !contract.data || (!!task.branch && !review.data);
  const annotationUncertain = !!result && (!comments.data || !!comments.error);
  const blockingComments = (comments.data ?? []).filter((comment) => comment.priority === "blocking" && (comment.state === "open" || comment.state === "reopened"));
  const block = acceptanceBlock(task, result, contract.data ?? null, review.data ?? null, uncertain || annotationUncertain) ?? (blockingComments.length ? "comments" : null);
  const mergeReason = mergeGuard(task, result, contract.data ?? null, review.data ?? null, uncertain || annotationUncertain) ?? (blockingComments.length ? "comments" : null);
  const returnBlocked = uncertain || !result || !contract.data || result.current_result_id !== result.result_id || result.contract_revision !== contract.data.contract_revision || !result.verdict_id;

  async function showOriginal() {
    if (!result || loadingOriginal) return;
    setLoadingOriginal(true);
    setOriginalError("");
    try {
      const report = await api.get<{ original_text: string }>(`${base}/results/${encodeURIComponent(result.result_id)}/original`);
      setOriginal({ resultId: result.result_id, text: report.original_text });
    } catch (error) {
      setOriginalError(errorText(error));
    } finally {
      setLoadingOriginal(false);
    }
  }

  async function showSources() {
    if (!result || loadingSources) return;
    setLoadingSources(true);
    setSourceError(null);
    try {
      const turns = await api.get<SourceTurn[]>(`${base}/results/${encodeURIComponent(result.result_id)}/turns`);
      setSourceTurns({ resultId: result.result_id, turns });
    } catch (error) {
      setSourceError({ resultId: result.result_id, text: errorText(error) });
    } finally { setLoadingSources(false); }
  }

  async function accept() {
    if (!result || block || busy || !contract.data) return;
    setBusy(true);
    const id = operation.current ?? crypto.randomUUID();
    operation.current = id;
    try {
      await api.post(`${base}/results/${encodeURIComponent(result.result_id)}/accept`, {
        result_id: result.result_id,
        verdict_id: result.verdict_id,
        contract_revision: result.contract_revision,
        expected_entity_revision: contract.data.entity_revision,
        client_operation_id: id,
      });
      operation.current = null;
      toast(t("result.accepted"));
      invalidate(base);
      invalidate(`/api/projects/${encodeURIComponent(task.project_id ?? "")}/board`);
      onAccepted();
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) operation.current = null;
      toast(errorText(error));
      results.refresh();
      contract.refresh();
      review.refresh();
    } finally {
      setBusy(false);
    }
  }

  async function merge() {
    if (!result || !contract.data || !review.data || mergeReason || busy) return;
    setBusy(true);
    const id = mergeOperation.current ?? crypto.randomUUID();
    mergeOperation.current = id;
    try {
      await api.post(`${base}/results/${encodeURIComponent(result.result_id)}/merge`, {
        verdict_id: result.verdict_id,
        expected_entity_revision: contract.data.entity_revision, client_operation_id: id,
      });
      mergeOperation.current = null;
      toast(t("result.mergeQueued"));
      review.refresh();
      contract.refresh();
      results.refresh();
      onAccepted();
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) mergeOperation.current = null;
      toast(errorText(error));
      review.refresh();
      contract.refresh();
    } finally { setBusy(false); }
  }

  async function returnResult() {
    if (!result || !contract.data || !returnNote.trim() || returnBlocked || busy) return;
    const body = { verdict_id: result.verdict_id, contract_revision: result.contract_revision, reason: returnNote.trim(), expected_entity_revision: contract.data.entity_revision };
    const fingerprint = JSON.stringify(body);
    if (returnOperation.current?.fingerprint !== fingerprint) returnOperation.current = { fingerprint, id: crypto.randomUUID() };
    setBusy(true);
    try {
      await api.post(`${base}/results/${encodeURIComponent(result.result_id)}/return`, { ...body, client_operation_id: returnOperation.current.id });
      returnOperation.current = null;
      setReturning(false);
      setReturnNote("");
      toast(t("result.returned"));
      results.refresh();
      contract.refresh();
      review.refresh();
      onAccepted();
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) returnOperation.current = null;
      toast(errorText(error));
      results.refresh();
      contract.refresh();
    } finally { setBusy(false); }
  }

  async function addComment() {
    if (!result || !contract.data || !commentText.trim() || offline || busy) return;
    const line = Number(commentLine);
    if (commentLine && (!task.branch || !review.data?.head_sha || !commentPath.trim() || !Number.isInteger(line) || line < 1)) return;
    const body = { priority: commentPriority, body: commentText.trim(), manifest_id: commentArtifact || null, verdict_id: result.verdict_id, path: commentLine ? commentPath.trim() : null, head: commentLine ? review.data?.head_sha : null, line_start: commentLine ? line : null, expected_entity_revision: contract.data.entity_revision };
    const fingerprint = JSON.stringify(body);
    if (commentOperation.current?.fingerprint !== fingerprint) commentOperation.current = { fingerprint, id: crypto.randomUUID() };
    setBusy(true);
    try {
      await api.post(`${base}/results/${encodeURIComponent(result.result_id)}/comments`, { ...body, client_operation_id: commentOperation.current.id });
      commentOperation.current = null;
      setCommentText("");
      setCommentPath("");
      setCommentLine("");
      toast(t("result.commentAdded"));
      comments.refresh();
      contract.refresh();
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) commentOperation.current = null;
      toast(errorText(error));
      comments.refresh();
      contract.refresh();
    } finally { setBusy(false); }
  }

  async function resolveComment(commentId: string) {
    if (!result || !contract.data || !resolutionReason.trim() || offline || busy) return;
    const body = { resolution, reason: resolutionReason.trim(), expected_entity_revision: contract.data.entity_revision };
    const fingerprint = `${commentId}:${JSON.stringify(body)}`;
    if (resolutionOperation.current?.fingerprint !== fingerprint) resolutionOperation.current = { fingerprint, id: crypto.randomUUID() };
    setBusy(true);
    try {
      await api.post(`${base}/results/${encodeURIComponent(result.result_id)}/comments/${encodeURIComponent(commentId)}/resolve`, { ...body, client_operation_id: resolutionOperation.current.id });
      resolutionOperation.current = null;
      setResolving(null);
      setResolutionReason("");
      toast(t("result.commentUpdated"));
      comments.refresh();
      contract.refresh();
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) resolutionOperation.current = null;
      toast(errorText(error));
      comments.refresh();
      contract.refresh();
    } finally { setBusy(false); }
  }

  return <section className="result-flow" aria-label={t("result.title")}>
    <h3>{t("result.title")}</h3>
    {!result && !results.error && <p className="sub">{t(results.loading ? "result.loading" : "result.none")}</p>}
    {result && <>
      <div className="result-summary">{result.original_preview?.split("\n")[0] || t("result.noSummary")}</div>
      {result.origin_kind === "operator_manual" && <p className="sub">{t("manual.origin")}</p>}
      <p className="result-state">{t("result.state", { outcome: t(`result.outcome.${result.outcome}`), verification: t(`result.verification.${result.verification}`), acceptance: t(`result.acceptance.${task.acceptance_state || "open"}`) })}</p>
      {(result.limitations ?? []).length > 0 && <div className="result-warning" role="status"><b>{t("result.limitations")}</b><ul>{result.limitations.map((item, index) => <li key={index}>{detail(item)}</li>)}</ul></div>}
      {(result.checks ?? []).length > 0 && <div className="result-checks"><b>{t("result.checks")}</b><ul>{result.checks.map((item, index) => <li key={index}>{detail(item)}</li>)}</ul></div>}
      <details className="result-details" onToggle={(event) => setEvidenceOpen(event.currentTarget.open)}>
        <summary>{t("result.evidence")}</summary>
        <div>{t("result.version", { revision: result.contract_revision })} · {absTime(result.created_at)}</div>
        <ul>{(result.artifacts ?? []).map((artifact) => <li key={artifact.id}>{artifact.artifact_kind}: {artifact.artifact_key} · {artifact.digest}</li>)}</ul>
        {evidenceOpen && task.project_id && (result.artifacts ?? []).length > 0 && <ResultTransfer projectId={task.project_id} artifacts={result.artifacts} toast={toast} />}
        <div className="mono">{result.original_digest}</div>
        <button type="button" className="btn small" disabled={loadingOriginal} onClick={() => void showOriginal()}>{t("result.original")}</button>
        {originalError && <p className="bad" role="alert">{originalError}</p>}
        {original?.resultId === result.result_id && <pre className="result-original">{original.text}</pre>}
        <button type="button" className="btn small" disabled={loadingSources} onClick={() => void showSources()}>{t("result.sourceMessages")}</button>
        {sourceError?.resultId === result.result_id && <p className="bad" role="alert">{sourceError.text}</p>}
        {sourceTurns?.resultId === result.result_id && (sourceTurns.turns.length ? <ul>{sourceTurns.turns.map((turn) => <li key={`${turn.session_id}:${turn.turn_seq}`}>
          <button type="button" className="linkbtn" onClick={() => navigate((task.project_id ? projectSessionPath(task.project_id, turn.session_id) : sessionPath(turn.session_id)) + `#m${turn.turn_seq}`)}>{t("result.openSourceMessage")}</button>
          {!turn.source_current && <span className="chip tiny warn">{t("result.sourceChanged")}</span>}
        </li>)}</ul> : <p className="sub">{t("result.noSourceMessages")}</p>)}
      </details>
      {compared && <details className="result-details">
        <summary>{t("result.previous", { count: earlier.length })}</summary>
        <label htmlFor={`result-compare-${task.id}`}>{t("result.compareWith")}</label>
        <select id={`result-compare-${task.id}`} className="field" value={compared.result_id} onChange={(event) => setComparedId(event.target.value)}>
          {earlier.map((item) => <option key={item.result_id} value={item.result_id}>{absTime(item.created_at)} · {t(`result.outcome.${item.outcome}`)}</option>)}
        </select>
        {compared.contract_revision !== result.contract_revision && <p className="result-warning" role="status">{t("result.compareChangedCriteria")}</p>}
        <div className="result-compare">
          {[result, compared].map((item, index) => <section key={item.result_id}>
            <h4>{t(index === 0 ? "result.compareCurrent" : "result.compareEarlier")}</h4>
            <p>{item.original_preview?.split("\n")[0] || t("result.noSummary")}</p>
            <p className="sub">{t(`result.outcome.${item.outcome}`)} · {t(`result.verification.${item.verification}`)} · {t("result.version", { revision: item.contract_revision })}</p>
            <b>{t("result.checks")}</b><ul>{(item.checks ?? []).map((check, number) => <li key={number}>{detail(check)}</li>)}</ul>
            <b>{t("result.limitations")}</b><ul>{(item.limitations ?? []).map((limit, number) => <li key={number}>{detail(limit)}</li>)}</ul>
          </section>)}
        </div>
      </details>}
      {contract.data && task.status === "review" && (result.origin_kind === "operator_manual"
        ? <ManualEvidenceReview task={task} result={result} contract={contract.data} blockingComments={blockingComments.length} toast={toast} onChanged={() => { results.refresh(); contract.refresh(); onAccepted(); }} />
        : <EvidenceReview task={task} result={result} contract={contract.data} review={review.data ?? null} blockingComments={blockingComments.length} toast={toast} onChanged={() => { results.refresh(); contract.refresh(); review.refresh(); onAccepted(); }} />)}
      {contract.data && task.status === "done" && result.origin_kind === "operator_manual" && <ManualReopen task={task} result={result} contract={contract.data} onChanged={() => { results.refresh(); contract.refresh(); onAccepted(); }} toast={toast} />}
      <details className="result-details">
        <summary>{t("result.annotations", { count: (comments.data ?? []).length })}</summary>
        {comments.error && <div className="result-warning" role="status">{t("result.block.unconfirmed")} <button type="button" className="linkbtn" onClick={() => comments.refresh()}>{t("common.retry")}</button></div>}
        <ul>{(comments.data ?? []).map((comment) => <li key={comment.comment_id} className={comment.priority === "blocking" && (comment.state === "open" || comment.state === "reopened") ? "result-warning" : ""}>
          <b>{t(`result.priority.${comment.priority}`)}</b> · {comment.body} {comment.path && <span className="mono">{comment.path}{comment.line_start ? `:${comment.line_start}` : ""}</span>} {comment.head && comment.head !== (review.data?.merge_receipt?.head_sha ?? review.data?.head_sha) && <span className="chip tiny warn">{t("result.commentLocationStale")}</span>} · {t(`result.commentState.${comment.state}`)}
          {(comment.state === "open" || comment.state === "reopened") && <button type="button" className="linkbtn" disabled={offline} onClick={() => setResolving(resolving === comment.comment_id ? null : comment.comment_id)}>{t("result.resolve")}</button>}
          {resolving === comment.comment_id && <div className="result-comment-form">
            <select className="field" aria-label={t("result.resolution")} value={resolution} onChange={(event) => setResolution(event.target.value as "resolved" | "waived")}>
              <option value="resolved">{t("result.commentState.resolved")}</option>
              <option value="waived">{t("result.commentState.waived")}</option>
            </select>
            <input className="field" aria-label={t("result.resolutionReason")} value={resolutionReason} onChange={(event) => setResolutionReason(event.target.value)} />
            <button type="button" className="btn small" disabled={busy || offline || !resolutionReason.trim()} onClick={() => void resolveComment(comment.comment_id)}>{t("common.save")}</button>
          </div>}
        </li>)}</ul>
        <div className="result-comment-form">
          <label htmlFor={`comment-${task.id}`}>{t("result.comment")}</label>
          <textarea id={`comment-${task.id}`} className="field" rows={3} value={commentText} onChange={(event) => setCommentText(event.target.value)} />
          <select className="field" aria-label={t("result.commentPriority")} value={commentPriority} onChange={(event) => setCommentPriority(event.target.value as Comment["priority"])}>
            {(["suggestion", "important", "blocking"] as const).map((priority) => <option key={priority} value={priority}>{t(`result.priority.${priority}`)}</option>)}
          </select>
          {(result.artifacts ?? []).length > 0 && <select className="field" aria-label={t("result.commentArtifact")} value={commentArtifact} onChange={(event) => setCommentArtifact(event.target.value)}>
            <option value="">{t("result.wholeResult")}</option>
            {result.artifacts.map((artifact) => <option key={artifact.id} value={artifact.id}>{artifact.artifact_key}</option>)}
          </select>}
          {task.branch && <details className="result-comment-location"><summary>{t("result.commentLocation")}</summary>
            <input className="field" aria-label={t("result.commentPath")} value={commentPath} onChange={(event) => setCommentPath(event.target.value)} placeholder={t("result.commentPath")} />
            <input className="field" type="number" min="1" aria-label={t("result.commentLine")} value={commentLine} onChange={(event) => setCommentLine(event.target.value)} placeholder={t("result.commentLine")} />
            <p className="sub">{t("result.commentLocationHint")}</p>
          </details>}
          <button type="button" className="btn small" disabled={busy || offline || !commentText.trim() || !contract.data || (!!commentLine && (!commentPath.trim() || !review.data?.head_sha || !Number.isInteger(Number(commentLine)) || Number(commentLine) < 1))} onClick={() => void addComment()}>{t("result.addComment")}</button>
        </div>
      </details>
    </>}
    <div className="result-action">
      {task.branch && task.merge_state !== "merged" && review.data?.merge_receipt?.state !== "merged" && <>
        <button type="button" className="btn" disabled={busy || mergeReason !== null} onClick={() => void merge()}>{t("result.merge")}</button>
        {mergeReason && <div className="result-warning" role="status">{t(`result.block.${mergeReason}`)}</div>}
      </>}
      {result && task.acceptance_state !== "operator_approved" && <>
        <button type="button" className="btn" disabled={busy || returnBlocked} onClick={() => setReturning((value) => !value)}>{t("result.return")}</button>
        {returning && <div className="result-return">
          <label htmlFor={`return-${task.id}`}>{t("result.returnReason")}</label>
          <textarea id={`return-${task.id}`} className="field" rows={3} value={returnNote} onChange={(event) => setReturnNote(event.target.value)} />
          <button type="button" className="btn warn" disabled={busy || returnBlocked || !returnNote.trim()} onClick={() => void returnResult()}>{t("result.sendBack")}</button>
        </div>}
      </>}
      {task.status === "review" && <><button type="button" className="btn primary" disabled={busy || block !== null} aria-describedby={block ? `result-block-${task.id}` : undefined} onClick={() => void accept()}>{t("result.accept")}</button>
      {block && <div id={`result-block-${task.id}`} className="result-warning" role="status">{t(`result.block.${block}`)}</div>}</>}
    </div>
  </section>;
}
