// The card under a turn whose agent created a schedule: what it is, when it runs in the operator's own
// time, and — while it waits for the operator — Accept and Decline. Also the Inbox's row for a
// schedule proposal, so the request is answered wherever the operator happens to see it.

import { type ReactNode, useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError, type Schedule } from "./api";
import { absTime, describeSchedule, untilShort } from "./format";
import { t } from "./i18n";
import { Icon } from "./icons";
import { openSchedules } from "./schedules/host";
import { invalidate, useQuery } from "./store";
import type { Activity, ToolItem } from "./turns";
import { errorText } from "./ui";

export type ScheduleProposalView = {
  id: string;
  name: string;
  prompt: string;
  cron: string | null;
  run_at: string | null;
  kind: Schedule["kind"];
  next_run_at: string;
  file_count: number;
  legacy_file_review_required: boolean;
  source_project_id: string | null;
  proposal_revision: number;
  request_digest: string;
  status: "pending" | "withdrawn" | "accepted";
  accepted_schedule_id: string | null;
};
export type ScheduleCardSchedule = { id: string; name: string; cron: string | null; run_at: string | null; next_run_at: string | null; kind: Schedule["kind"]; enabled: number; deleted: boolean };
export type ScheduleCardView = { schedule: ScheduleCardSchedule | null; proposal: ScheduleProposalView | null; collection_revision: number | null; approval_days?: number };

/**
 * The tool's answer names the schedule on its first line (daedalus/tools/scheduling.py): "Schedule: s…"
 * when it was set, "Schedule proposal: p…" when it waits. The card is drawn from that id alone.
 */
const SCHEDULE_ID_RE = /^Schedule(?: proposal)?: ([sp][0-9a-f]{15})\b/;

/** The schedules a turn created, in order: the ScheduleCreate steps that answered with an id. */
export function scheduleCardIds(activity: Activity[]): string[] {
  const ids: string[] = [];
  for (const item of activity) {
    if (item.kind !== "tool" || item.name !== "ScheduleCreate" || item.running || item.error) continue;
    const id = SCHEDULE_ID_RE.exec((item as ToolItem).result ?? "")?.[1];
    if (id && !ids.includes(id)) ids.push(id);
  }
  return ids;
}

/** How long an Accept lasts while the operator reviews the agent's schedules; the host says so with the list. */
export const HAND_APPROVAL_DAYS = 30;

/**
 * How long the operator's Accept lasts. A recurring schedule gets the horizon the host names: a month
 * while the operator reviews the agent's schedules, the standing ten years when they do not, so a
 * schedule accepted by hand does not stop a month later when nobody asked to be asked again. A
 * one-off gets a month, or a day past its occurrence when that is further out, since an approval that
 * ends before its only occurrence approves nothing.
 */
export function approvalExpiry(item: { cron: string | null; next_run_at: string | null }, now = Date.now(), days = HAND_APPROVAL_DAYS): string {
  if (item.cron) return new Date(now + days * 86400000).toISOString();
  const due = Date.parse(item.next_run_at ?? "");
  return new Date(Math.max(now + HAND_APPROVAL_DAYS * 86400000, Number.isFinite(due) ? due + 86400000 : 0)).toISOString();
}

type State = "set" | "pending" | "declined" | "removed" | "paused";

function stateOf(view: ScheduleCardView): State {
  if (view.schedule) return view.schedule.deleted ? "removed" : view.schedule.enabled ? "set" : "paused";
  return view.proposal?.status === "pending" ? "pending" : "declined";
}

/** The card for one schedule or proposal id; ``initial`` skips the first read when the caller has the row. */
export function ScheduleCard({ itemId, initial, onDecided, sessionId }: { itemId: string; initial?: ScheduleCardView; onDecided?: () => void; sessionId?: string | null }) {
  const [view, setView] = useState<ScheduleCardView | null | undefined>(initial);
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState<string | null>(null);
  // One command identity per decision and request, so a press repeated after a lost answer is the
  // same command to the host and cannot accept twice.
  const operation = useRef<Record<string, { fingerprint: string; id: string }>>({});
  // The expiry is counted from when the card was drawn, not from each press: a retried Accept must
  // send the same request under the same identity, or the host refuses it as a different command.
  const [clock] = useState(() => Date.now());
  const load = useCallback(async () => {
    try {
      setView(await api.get<ScheduleCardView>(`/api/recurring/card/${encodeURIComponent(itemId)}`));
      setFailed(null);
    } catch (e) {
      // A schedule the host no longer has is said as gone, not as an error.
      if (e instanceof ApiError && e.status === 404) setView(null);
      else setFailed(errorText(e));
    }
  }, [itemId]);
  useEffect(() => {
    if (initial === undefined) void load();
  }, [load, initial]);
  async function decide(action: "accept" | "withdraw") {
    const proposal = view?.proposal;
    if (!proposal || view?.collection_revision == null) return;
    const body = {
      request_digest: proposal.request_digest,
      expected_proposal_revision: proposal.proposal_revision,
      expected_collection_revision: view.collection_revision,
      ...(action === "accept" ? { expires_at: approvalExpiry(proposal, clock, view.approval_days) } : {}),
    };
    const key = `${proposal.id}:${action}`;
    const fingerprint = JSON.stringify(body);
    if (operation.current[key]?.fingerprint !== fingerprint) operation.current[key] = { fingerprint, id: crypto.randomUUID() };
    setBusy(true);
    setFailed(null);
    try {
      await api.post(`/api/recurring/proposals/${encodeURIComponent(proposal.id)}/${action}`, { ...body, client_operation_id: operation.current[key].id });
      delete operation.current[key];
      invalidate("/api/recurring/proposals");
      invalidate("/api/schedules");
      onDecided?.();
    } catch (e) {
      // Answered elsewhere first: the card re-reads how it now stands, and says why.
      if (e instanceof ApiError && e.status === 409) delete operation.current[key];
      setFailed(errorText(e));
    } finally {
      setBusy(false);
      void load();
    }
  }
  if (view === undefined && !failed) return null;
  if (!view) {
    return (
      <div className="setchange schedcard gone" data-schedule={itemId}>
        <div className="setchange-head">
          <Icon name="clock" size={16} />
          <span className="setchange-title">{t("schedcard.title")}</span>
        </div>
        <div className="setchange-note">{failed ? t("schedcard.failed.load", { error: failed }) : t("schedcard.gone")}</div>
      </div>
    );
  }
  const state = stateOf(view);
  const item = view.schedule ?? view.proposal!;
  const next = state === "set" || state === "pending" ? item.next_run_at : null;
  const proposal = view.proposal;
  const scheduleId = view.schedule && !view.schedule.deleted ? view.schedule.id : null;
  return (
    <div className={`setchange schedcard ${state}`} data-schedule={itemId} data-status={state}>
      <div className="setchange-head">
        <Icon name="clock" size={16} />
        <span className="setchange-title">{item.name}</span>
        <span className={`pill ${state === "set" ? "done" : state === "pending" ? "pending" : "closed"}`}>{t(`schedcard.status.${state}`)}</span>
      </div>
      <div className="schedcard-facts">
        <span className="schedcard-when">{describeSchedule(item)}</span>
        <span className="schedcard-kind">{t(`sched.kind.${item.kind}`)}</span>
      </div>
      {next && <div className="setchange-when schedcard-next">{t("schedcard.next", { at: absTime(next), rel: untilShort(next) })}</div>}
      {state === "pending" && proposal?.legacy_file_review_required && <div className="setchange-note">{t("sched.proposal.legacyfiles")}</div>}
      <div className="setchange-foot">
        {/* The schedules of this conversation, with this one open, over the chat rather than in place
            of it: the operator asked about the schedule they were reading about, not for a new page. */}
        <button type="button" className="btn small ghost" data-action="open" onClick={() => openSchedules({ sessionId: sessionId ?? null, selected: scheduleId ?? (state === "pending" ? proposal?.id ?? null : null) })}>
          <Icon name="calendar" size={14} /> {t("schedcard.open")}
        </button>
        {state === "pending" && (
          <span className="setchange-actions">
            <button type="button" className="btn small" data-action="decline" disabled={busy || view.collection_revision == null} onClick={() => void decide("withdraw")}>{t("schedcard.decline")}</button>
            <button type="button" className="btn small primary" data-action="accept" disabled={busy || view.collection_revision == null || !!proposal?.legacy_file_review_required} onClick={() => void decide("accept")}>{t("schedcard.accept")}</button>
          </span>
        )}
      </div>
      {failed && <div className="setchange-note error">{failed}</div>}
    </div>
  );
}

type ProposalList = { entries: ScheduleProposalView[]; collection_revisions: Record<string, number>; approval_days?: number };

/**
 * The schedule proposals waiting for the operator, each as the card draws it. The Inbox lists them
 * and the nav counts them, the same way a change waiting for a decision is listed and counted.
 */
export function usePendingSchedules(enabled = true): ScheduleCardView[] {
  const { data } = useQuery<ProposalList>(enabled ? "/api/recurring/proposals" : null, { pollMs: 60000, staleMs: 30000 });
  return (data?.entries ?? []).filter((item) => item.status === "pending").map((proposal) => ({
    schedule: null,
    proposal,
    collection_revision: data?.collection_revisions[proposal.source_project_id ? `project:${proposal.source_project_id}` : "global:global"] ?? null,
    approval_days: data?.approval_days,
  }));
}

/** The Inbox's section of schedule proposals; nothing at all when none wait. */
export function PendingSchedules({ views, header }: { views: ScheduleCardView[]; header: (count: number) => ReactNode }) {
  if (views.length === 0) return null;
  return (
    <section className="inbox-schedules" aria-label={t("inbox.schedules")}>
      {header(views.length)}
      {views.map((view) => <ScheduleCard key={view.proposal!.id} itemId={view.proposal!.id} initial={view} />)}
    </section>
  );
}
