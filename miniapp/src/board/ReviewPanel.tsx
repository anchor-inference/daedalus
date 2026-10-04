// The review of a staff branch on its task: what merging it would bring and what stands in its way, the
// diff, and the two answers — Merge, or send it back with a note. It sits in the task sheet, so it is
// the same on the board page, in focus mode's board tab and on a phone. Merge is disabled with its
// reason rather than hidden: the operator should see that the folder is dirty, not wonder where the
// button went.

import { useRef, useState } from "react";
import { api, ApiError } from "../api";
import { Skeleton } from "../ui/components";
import { Sheet } from "../ui/dialogs";
import { relTime } from "../format";
import { Icon } from "../icons";
import { plural, t } from "../i18n";
import { DiffView } from "../previewparts";
import { invalidate, useOffline, useQuery } from "../store";
import { BLOCKER_CODES, Review, ReviewBlocker, mergeBlock } from "./board";

export const reviewKey = (taskId: string) => `/api/board/${encodeURIComponent(taskId)}/review`;

const COMMITS_SHOWN = 5;

/** A blocker in the reader's language; the host's own sentence when the app has no words for its code. */
export function blockerText(blocker: ReviewBlocker, review: Pick<Review, "current" | "base" | "conflicts" | "ci_checks">): string {
  if (blocker.code === "ci") return t(review.ci_checks.length ? "pboard.review.block.ciPending" : "pboard.review.block.ciMissing");
  if (!(BLOCKER_CODES as readonly string[]).includes(blocker.code)) return blocker.text;
  return t(`pboard.review.block.${blocker.code}`, { current: review.current, base: review.base, files: (review.conflicts ?? []).slice(0, 3).join(", ") });
}

type RequirementIntent = { client_operation_id: string; expected_entity_revision: number; provider: "github"; repository_id: string; check_names: string[] };

/** A policy change returns the submitted result for another review, so the old verdict cannot approve new checks. */
function CiRequirements({ taskId, checks, onChanged, toast }: { taskId: string; checks: Review["ci_checks"]; onChanged: () => void; toast: (text: string) => void }) {
  const contractKey = `/api/board/${encodeURIComponent(taskId)}/contract`;
  const { data: contract, error: contractError, refresh } = useQuery<{ entity_revision: number }>(contractKey, { staleMs: 0 });
  const offline = useOffline();
  const [repository, setRepository] = useState(checks[0]?.repository_id ?? "");
  const [names, setNames] = useState(checks.map((check) => check.check_name).join("\n"));
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState("");
  const pending = useRef<RequirementIntent | null>(null);
  const parsedNames = names.split(/\r?\n/).map((name) => name.trim()).filter(Boolean);
  const valid = /^[1-9]\d*$/.test(repository) && repository.length <= 30 && parsedNames.length > 0 && parsedNames.length <= 32 && parsedNames.every((name) => name.length <= 200);

  async function save() {
    if (!valid || busy || offline) return;
    if (!pending.current) {
      if (!Number.isInteger(contract?.entity_revision)) { setProblem(t("pboard.review.ci.noRevision")); return; }
      pending.current = { client_operation_id: crypto.randomUUID(), expected_entity_revision: contract!.entity_revision,
        provider: "github", repository_id: repository, check_names: parsedNames };
    }
    setBusy(true);
    setProblem("");
    try {
      const receipt = await api.post<{ receipt_id: string; entity_revision: number }>(`/api/board/${encodeURIComponent(taskId)}/ci/requirements`, pending.current);
      pending.current = null;
      toast(t("pboard.review.ci.saved", { receipt: receipt.receipt_id }));
      invalidate(contractKey);
      invalidate(reviewKey(taskId));
      onChanged();
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) {
        pending.current = null;
        refresh();
        setProblem(t("pboard.review.ci.changed"));
      } else setProblem(t("pboard.review.ci.unknown"));
    } finally { setBusy(false); }
  }

  return <details className="review-ci-setup">
    <summary>{t(checks.length ? "pboard.review.ci.change" : "pboard.review.ci.configure")}</summary>
    <div className="review-ci-fields">
      <p className="sub">{t("pboard.review.ci.setupHint")}</p>
      <span className="sub">{t("pboard.review.ci.provider")}: GitHub</span>
      <label>{t("pboard.review.ci.repository")}<input className="field" inputMode="numeric" value={repository} onChange={(event) => { setRepository(event.target.value); pending.current = null; }} placeholder={t("pboard.review.ci.repositoryPlaceholder")} /></label>
      <label>{t("pboard.review.ci.names")}<textarea className="field" rows={3} value={names} onChange={(event) => { setNames(event.target.value); pending.current = null; }} placeholder={t("pboard.review.ci.namesPlaceholder")} /></label>
      <p className="sub attn">{t("pboard.review.ci.returnHint")}</p>
      {contractError && <p className="sub bad">{t("pboard.review.ci.loadError")} <button className="linkbtn" onClick={refresh}>{t("common.retry")}</button></p>}
      {problem && <p className="sub bad" role="alert">{problem}</p>}
      <button className="btn small" disabled={busy || offline || !valid || !Number.isInteger(contract?.entity_revision)} onClick={() => void save()}>{busy ? t("pboard.review.ci.saving") : pending.current ? t("pboard.review.ci.retry") : t("pboard.review.ci.save")}</button>
    </div>
  </details>;
}

export function ReviewPanel({ taskId, onChanged, toast }: { taskId: string; onChanged: () => void; toast: (text: string) => void }) {
  const { data, error, loading, refresh } = useQuery<Review>(reviewKey(taskId), { staleMs: 2000 });
  const [diff, setDiff] = useState(false);
  const [showAll, setShowAll] = useState(false);


  if (loading && !data) return <section className="review-panel" aria-label={t("pboard.review")}><Skeleton rows={2} /></section>;
  if (error && !data) {
    return (
      <section className="review-panel" aria-label={t("pboard.review")}>
        <div className="sub bad">{t("pboard.review.error", { detail: error })}</div>
        <button className="btn small" onClick={refresh}>{t("common.retry")}</button>
      </section>
    );
  }
  if (!data) return null;
  const block = mergeBlock(data);
  // The reason Merge waits is said once, under the button it disables; the list above keeps only the
  // others. Both used to print it, word for word, two lines apart.
  const others = data.blockers.filter((b) => b.code !== block?.code);
  const commits = showAll ? data.commits : data.commits.slice(0, COMMITS_SHOWN);
  const hidden = data.commits.length - commits.length;
  return (
    <section className="review-panel" aria-label={t("pboard.review")}>
      <div className="review-head">
        <Icon name="fork" size={14} />
        <code className="pcard-branch truncate" title={data.branch}>{data.branch}</code>
        <span className="sub nowrap">→ {data.current || data.base}</span>
      </div>
      <div className="review-stat">
        <span className="tk-add num">+{data.added}</span> <span className="tk-del num">−{data.removed}</span>
        <span className="sub"> · {plural("pboard.review.files", data.files.length)} · {plural("pboard.review.commits", data.commits.length)}{data.more_commits ? "+" : ""}</span>
        {data.merge_state === "conflict" && <span className="chip tiny bad">{t("pboard.review.state.conflict")}</span>}
      </div>
      {data.commits.length > 0 && (
        <ul className="review-commits">
          {commits.map((c) => (
            <li key={c.sha}>
              <code className="faint">{c.sha.slice(0, 7)}</code> <span className="truncate">{c.subject}</span> <span className="faint nowrap">{relTime(c.at)}</span>
            </li>
          ))}
          {hidden > 0 && <li><button className="linkbtn" onClick={() => setShowAll(true)}>{t("pboard.review.commits.more", { n: hidden })}</button></li>}
        </ul>
      )}
      {data.files.length > 0 && (
        <ul className="review-files">
          {data.files.slice(0, 12).map((f) => (
            <li key={f.path}>
              <span className="truncate" title={f.path}>{f.path}</span>
              <span className="num nowrap">{f.added === null ? t("pboard.review.binary") : <><span className="tk-add">+{f.added}</span> <span className="tk-del">−{f.removed}</span></>}</span>
            </li>
          ))}
          {data.files.length > 12 && <li className="faint">{t("pboard.review.files.more", { n: data.files.length - 12 })}</li>}
        </ul>
      )}
      {data.receipts.length > 0 && (
        <ul className="review-receipts" aria-label={t("pboard.review.receipts")}>
          {data.receipts.slice(0, 5).map((r, i) => (
            <li key={i} className={r.passed ? "ok" : "bad"} title={r.command}>
              <Icon name={r.passed ? "check" : "close"} size={12} /> <span className="truncate">{r.criterion || r.command}</span>
            </li>
          ))}
        </ul>
      )}
      <div className={`sub ${data.ci_status === "passed" ? "ok" : "attn"}`}>
        {t(data.ci_status === "passed" ? "pboard.review.ci.passed" : "pboard.review.ci.blocked", { head: data.head_sha?.slice(0, 10) || "?" })}
      </div>
      {data.ci_checks.length > 0 && (
        <ul className="review-receipts" aria-label={t("pboard.review.ci.checks")}>
          {data.ci_checks.map((check) => (
            <li key={`${check.provider}:${check.repository_id}:${check.check_name}`} className={check.state === "passed" ? "ok" : "bad"}>
              <Icon name={check.state === "passed" ? "check" : "close"} size={12} />
              <span className="truncate">{check.check_name}: {t(`pboard.review.ci.state.${check.state}`)}</span>
            </li>
          ))}
        </ul>
      )}
      {data.ci_status === "blocked" && data.status === "review" && !data.merged && <CiRequirements taskId={taskId} checks={data.ci_checks} onChanged={onChanged} toast={toast} />}
      {others.length > 0 && (
        <ul className="review-blockers">
          {others.map((b) => <li key={b.code} className={b.code === "conflicts" ? "bad" : "attn"} title={b.text}>{blockerText(b, data)}</li>)}
        </ul>
      )}
      <div className="btnrow review-actions">
        <button className="btn small" disabled={!data.patch} onClick={() => setDiff(true)}><Icon name="changes" size={14} /> {t("pboard.review.diff")}</button>
        <span className="grow" />
      </div>
      <div className={`sub review-why ${block?.code === "conflicts" ? "bad" : "attn"}`}>{block ? t("pboard.review.why", { reason: blockerText(block, data) }) : t("result.block.unverified")}</div>
      {diff && (
        <Sheet title={data.branch} onClose={() => setDiff(false)} size="full" className="review-diff">
          {!data.patch_complete && <div className="sub attn">{t("pboard.review.diff.cut")}</div>}
          <DiffView text={data.patch} />
        </Sheet>
      )}
    </section>
  );
}
