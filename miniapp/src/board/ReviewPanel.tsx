// The review of a staff branch on its task: what merging it would bring and what stands in its way, the
// diff, and the two answers — Merge, or send it back with a note. It sits in the task sheet, so it is
// the same on the board page, in focus mode's board tab and on a phone. Merge is disabled with its
// reason rather than hidden: the operator should see that the folder is dirty, not wonder where the
// button went.

import { useState } from "react";
import { api } from "../api";
import { Skeleton } from "../ui/components";
import { Sheet } from "../ui/dialogs";
import { relTime } from "../format";
import { Icon } from "../icons";
import { plural, t } from "../i18n";
import { DiffView } from "../previewparts";
import { invalidate, useQuery } from "../store";
import { errorText } from "../ui";
import { BLOCKER_CODES, Review, ReviewBlocker, mergeBlock } from "./board";

export const reviewKey = (taskId: string) => `/api/board/${encodeURIComponent(taskId)}/review`;

const COMMITS_SHOWN = 5;

/** A blocker in the reader's language; the host's own sentence when the app has no words for its code. */
export function blockerText(blocker: ReviewBlocker, review: Pick<Review, "current" | "base" | "conflicts">): string {
  if (!(BLOCKER_CODES as readonly string[]).includes(blocker.code)) return blocker.text;
  return t(`pboard.review.block.${blocker.code}`, { current: review.current, base: review.base, files: (review.conflicts ?? []).slice(0, 3).join(", ") });
}

export function ReviewPanel({ taskId, onMerged, onRejected, toast }: { taskId: string; onMerged: () => void; onRejected: () => void; toast: (text: string) => void }) {
  const { data, error, loading, refresh } = useQuery<Review>(reviewKey(taskId), { staleMs: 2000 });
  const [diff, setDiff] = useState(false);
  const [rejecting, setRejecting] = useState(false);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [showAll, setShowAll] = useState(false);

  async function merge() {
    setBusy(true);
    try {
      await api.post(`/api/board/${encodeURIComponent(taskId)}/merge`);
      toast(t("pboard.review.merged", { branch: data?.branch ?? "" }));
      onMerged();
    } catch (e) {
      toast(errorText(e));
      invalidate(reviewKey(taskId));
      refresh();
    } finally {
      setBusy(false);
    }
  }
  async function reject() {
    setBusy(true);
    try {
      await api.post(`/api/board/${encodeURIComponent(taskId)}/reject`, { note: note.trim() });
      toast(t("pboard.review.rejected"));
      setRejecting(false);
      setNote("");
      onRejected();
    } catch (e) {
      toast(errorText(e));
    } finally {
      setBusy(false);
    }
  }

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
      {others.length > 0 && (
        <ul className="review-blockers">
          {others.map((b) => <li key={b.code} className={b.code === "conflicts" ? "bad" : "attn"} title={b.text}>{blockerText(b, data)}</li>)}
        </ul>
      )}
      <div className="btnrow review-actions">
        <button className="btn small" disabled={!data.patch} onClick={() => setDiff(true)}><Icon name="changes" size={14} /> {t("pboard.review.diff")}</button>
        <button className="btn small" onClick={() => setRejecting(!rejecting)} aria-expanded={rejecting}>{t("pboard.review.reject")}</button>
        <span className="grow" />
        <button className="btn small primary" disabled={busy || block !== null} title={block ? blockerText(block, data) : undefined} onClick={merge}>
          <Icon name="check" size={14} /> {t("pboard.review.merge")}
        </button>
      </div>
      {block && <div className={`sub review-why ${block.code === "conflicts" ? "bad" : "attn"}`}>{t("pboard.review.why", { reason: blockerText(block, data) })}</div>}
      {rejecting && (
        <div className="review-reject">
          <label className="field" htmlFor={`reject-${taskId}`}>{t("pboard.review.reject.note")}</label>
          <textarea id={`reject-${taskId}`} className="field" rows={3} maxLength={2000} value={note} placeholder={t("pboard.review.reject.placeholder")} onChange={(e) => setNote(e.target.value)} />
          <div className="btnrow">
            <span className="grow" />
            <button className="btn small warn" disabled={busy || !note.trim()} onClick={reject}>{t("pboard.review.reject.send")}</button>
          </div>
        </div>
      )}
      {diff && (
        <Sheet title={data.branch} onClose={() => setDiff(false)} size="full" className="review-diff">
          {!data.patch_complete && <div className="sub attn">{t("pboard.review.diff.cut")}</div>}
          <DiffView text={data.patch} />
        </Sheet>
      )}
    </section>
  );
}
